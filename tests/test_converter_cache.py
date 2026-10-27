"""Result converters are found once per connection object and column type (#60), apart for an expression (#68).

The ORM finds a column's converters with ``connection.ops.get_db_converters()``
and the field's ``get_db_converters()``. They are bound methods (or closures) of
that connection's ``ops``, which lives as long as the connection object
(``connections[alias]``, one per thread), so the bridge keeps them on that
object: another alias or thread has its own, and a reconnect keeps them.
"""

import datetime
import gc
import weakref
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import connection, connections
from django.test import override_settings

from django_sqlalchemy_bridge import execute, execution, forget_table, table_for
from django_sqlalchemy_bridge.execution import prepare
from tests.testapp.models import Author, Stamp

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=ZoneInfo("Asia/Seoul"))

authors = table_for(Author)


@pytest.fixture
def lookups(monkeypatch):
    """The converter lookups made so far, one ``(vendor, column)`` entry per lookup, from an empty cache."""
    found = []
    for alias in ("default", "other"):
        monkeypatch.delitem(connections[alias].__dict__, execution._CONVERTERS, raising=False)

    def counting(original):
        def get_db_converters(self, expression):
            # A Col has its column (target); an expression only its output field.
            found.append((self.connection.vendor, getattr(expression, "target", expression.output_field).name))
            return original(self, expression)

        return get_db_converters

    for ops in {type(connections[alias].ops) for alias in ("default", "other")}:
        monkeypatch.setattr(ops, "get_db_converters", counting(ops.get_db_converters))
    return found


def forget_time_zone():
    # As django.test.signals.update_connections_time_zone does.
    connections["default"].__dict__.pop("timezone", None)
    connections["default"].__dict__.pop("timezone_name", None)


def by_pk():
    return sa.select(authors.c.name, authors.c.joined_at).where(authors.c.id == sa.bindparam("pk"))


def test_a_second_call_finds_no_converters_again(lookups):
    stmt = by_pk()
    first = prepare(stmt, {"pk": 1}, connection)
    assert sorted(column for _, column in lookups) == ["joined_at", "name"]
    lookups.clear()
    second = prepare(stmt, {"pk": 2}, connection)
    # Another statement over the same columns finds them cached too.
    prepare(sa.select(authors.c.joined_at), None, connection)
    assert lookups == []
    assert second.converters == first.converters


def test_a_column_and_an_expression_of_its_type_find_their_own_converters(lookups):
    # The ORM converts a Col and an expression of the same field differently (#68).
    column = authors.c.joined_at
    first = prepare(sa.select(column, sa.func.max(column)), None, connection)
    assert [column for _, column in lookups] == ["joined_at", "joined_at"]
    if connection.vendor == "sqlite":  # PostgreSQL's driver gives aware datetimes and needs no converter
        assert [type(expression).__name__ for _, expression in first.converters] == ["Col", "Expression"]
    lookups.clear()
    second = prepare(sa.select(sa.func.min(column), column.label("j")), None, connection)
    assert lookups == []
    assert second.converters == first.converters[::-1]


def test_a_forgotten_table_finds_its_converters_again(lookups):
    table = table_for(Stamp)
    try:
        prepare(sa.select(table.c.name), None, connection)
        forget_table(Stamp)
        lookups.clear()
        prepare(sa.select(table_for(Stamp).c.name), None, connection)
        assert [column for _, column in lookups] == ["name"]
    finally:
        forget_table(Stamp)


@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite converts datetimes in Python with its time zone")
def test_cached_sqlite_converters_read_the_time_zone_when_called(lookups):
    # SQLite's convert_datetimefield_value reads settings.USE_TZ and
    # connection.timezone each time it is called, so a converter cached under
    # one setting reads the next one; override_settings(USE_TZ/TIME_ZONE)
    # clears connection.timezone (django.test.signals.update_connections_time_zone).
    author = Author.objects.create(name="a", joined_at=AT)
    stmt = by_pk()
    assert execute(stmt, {"pk": author.pk}) == [("a", AT)]
    lookups.clear()
    stored = AT.astimezone(datetime.UTC).replace(tzinfo=None)  # naive UTC, as SQLite holds it
    with override_settings(USE_TZ=False):
        assert execute(stmt, {"pk": author.pk}) == [("a", stored)]
    # The database's own TIME_ZONE, as Django's test suite overrides it.
    saved = connection.settings_dict["TIME_ZONE"]
    connection.settings_dict["TIME_ZONE"] = "America/New_York"
    forget_time_zone()
    try:
        assert execute(stmt, {"pk": author.pk}) == [("a", stored.replace(tzinfo=ZoneInfo("America/New_York")))]
    finally:
        connection.settings_dict["TIME_ZONE"] = saved
        forget_time_zone()
    assert execute(stmt, {"pk": author.pk}) == [("a", AT)]
    assert lookups == []


@pytest.mark.django_db(databases=["default", "other"])
def test_each_alias_finds_and_keeps_its_own_converters(lookups):
    other = connections["other"]
    stmt = by_pk()
    pks = {using: Author.objects.using(using).create(name=using, joined_at=AT).pk for using in ("default", "other")}
    lookups.clear()  # the ORM's own
    for using, pk in pks.items():
        assert execute(stmt, {"pk": pk}, using=using) == [(using, AT)]
    assert sorted(lookups) == sorted(
        [(connection.vendor, "name"), (connection.vendor, "joined_at"), ("sqlite", "name"), ("sqlite", "joined_at")]
    )
    mine = prepare(stmt, {"pk": 1}, connection).converters
    theirs = prepare(stmt, {"pk": 1}, other).converters
    # SQLite's datetime converter is a method of each connection's own ops
    # (PostgreSQL's driver gives aware datetimes and needs none).
    functions = [function for function in theirs[1][0] if getattr(function, "__self__", None) is other.ops]
    assert functions and not any(function in (mine[1] or ((),))[0] for function in functions)


@pytest.mark.django_db(transaction=True)
def test_a_reconnected_connection_keeps_its_converters(lookups):
    stmt = by_pk()
    found = prepare(stmt, {"pk": 1}, connection).converters
    connection.close()  # an in-memory SQLite database ignores it, keeping its tables
    author = Author.objects.create(name="a", joined_at=AT)
    lookups.clear()
    assert execute(stmt, {"pk": author.pk}) == [("a", AT)]
    assert prepare(stmt, {"pk": 1}, connection).converters == found
    assert lookups == []


def test_the_converters_do_not_keep_their_connection_alive():
    # A connection object other than the thread's own, as another thread would have.
    other = connections.create_connection("other")
    assert prepare(by_pk(), {"pk": 1}, other).converters[1] is not None
    ref = weakref.ref(other)
    del other
    gc.collect()
    assert ref() is None
