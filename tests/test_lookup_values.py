"""A value compared with a column converts as an ORM lookup does; a written value as the ORM saves it (#47).

The ORM prepares a lookup's right-hand side with ``field.get_prep_value()`` and
then ``get_db_prep_value(prepared=True)`` (``Lookup.get_prep_lookup()``,
``FieldGetDbPrepValueMixin.get_db_prep_lookup()``), and a value it writes with
``field.get_db_prep_save()``. The two differ where a backend checks a written
value: Django 5.2 checks ``AutoField`` values for MySQL in
``get_db_prep_value(prepared=False)``, 6.1 in ``get_db_prep_save()``.
"""

import datetime
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from django.db import connection, transaction

from django_sqlalchemy_bridge import bulk_insert, bulk_update, execute, table_for
from django_sqlalchemy_bridge.execution import prepare
from tests.testapp.models import Author, Gadget, Specimen

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=datetime.UTC)

authors = table_for(Author)
specimens = table_for(Specimen)


@pytest.fixture
def no_auto_pk_0(monkeypatch):
    """Make the backend refuse 0 for an AutoField when it is written, as MySQL does by default."""

    def validate_autopk_value(value):
        if value == 0:
            raise ValueError("The database backend does not accept 0 as a value for AutoField.")
        return value

    monkeypatch.setattr(connection.ops, "validate_autopk_value", validate_autopk_value)


@pytest.mark.parametrize("form", ["bindparam", "literal", "in"])
@pytest.mark.usefixtures("no_auto_pk_0")
def test_comparing_an_auto_pk_with_zero_runs_as_the_orm_lookup(form):
    Author.objects.create(name="a", joined_at=AT)
    assert list(Author.objects.filter(id=0)) == []
    assert list(Author.objects.filter(id__in=[0])) == []

    stmt = sa.select(authors.c.name)
    if form == "bindparam":
        assert execute(stmt.where(authors.c.id == sa.bindparam("pk")), {"pk": 0}) == []
    elif form == "literal":
        assert execute(stmt.where(authors.c.id == 0)) == []
    else:
        assert execute(stmt.where(authors.c.id.in_([0]))) == []
    assert execute(sa.delete(authors).where(authors.c.id == 0)) == 0


WRITES = {
    "orm": lambda: Author.objects.create(id=0, name="a", joined_at=AT),
    "insert values": lambda: execute(sa.insert(authors).values(id=0, name="a", joined_at=AT)),
    "insert params": lambda: execute(sa.insert(authors), {"id": 0, "name": "a", "joined_at": AT}),
    "update set": lambda: execute(sa.update(authors).values(id=sa.bindparam("new")), {"new": 0}),
    "bulk_insert": lambda: bulk_insert(authors, [{"id": 0, "name": "a", "joined_at": AT}]),
}


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
@pytest.mark.usefixtures("no_auto_pk_0")
def test_writing_zero_to_an_auto_pk_is_checked_as_the_orm_saves_it(write):
    # The ORM marks the transaction for rollback when its write fails.
    with pytest.raises(ValueError, match="does not accept 0"), transaction.atomic():
        write()


@pytest.mark.skipif(
    connection.vendor != "mysql", reason="MySQL refuses 0 for an AutoField without NO_AUTO_VALUE_ON_ZERO"
)
def test_mysql_compares_an_auto_pk_with_zero_but_refuses_to_write_it():
    if connection.features.allows_auto_pk_0:
        pytest.skip("sql_mode has NO_AUTO_VALUE_ON_ZERO")
    assert list(Author.objects.filter(id=0)) == []
    assert execute(sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk")), {"pk": 0}) == []
    with pytest.raises(ValueError, match="does not accept 0"), transaction.atomic():
        Author.objects.create(id=0, name="a", joined_at=AT)
    with pytest.raises(ValueError, match="does not accept 0"):
        execute(sa.insert(authors).values(id=0, name="a", joined_at=AT))


def lookup_values():
    """One value per Specimen field (by attname) to compare it with, none of them None."""
    return {
        "uid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "label": "label",
        "body": "body",
        "slug": "s",
        "email": "a@b.c",
        "small": -3,
        "positive": 7,
        "big": 2**40,
        "flag": True,
        "maybe": False,
        "ratio": 0.25,
        "amount": Decimal("123.4"),
        "day": datetime.date(2026, 10, 25),
        "at": datetime.time(23, 59, 1, 500),
        "happened_at": AT,
        "took": datetime.timedelta(days=1, microseconds=7),
        "blob": b"\x00\xffbytes",
        "data": {"k": [1, "두", None], "n": 1.5},
        "level": Specimen.Level.HIGH,
        "author_id": 5,
        "gadget_id": "g1",
        "twin_id": uuid.UUID("87654321-4321-8765-4321-876543218765"),
    }


def comparable(params):
    # Compare types too (PostgreSQL's Int2 and Int4 are ints); psycopg's Binary and Jsonb do not define ==.
    return [(type(p), getattr(p, "obj", p)) for p in params]


def orm_lookup_params(lookup, value):
    return comparable(Specimen.objects.filter(**{lookup: value}).values_list("pk").query.sql_with_params()[1])


def bridge_params(column, comparison, value):
    stmt = sa.select(specimens.c.uid).where(comparison(column, sa.bindparam("v")))
    return comparable(prepare(stmt, {"v": value}, connection).param_rows[0])


def column_of(attname):
    return next(c for c in specimens.c if c.info["django_field"].attname == attname)


VALUE = lookup_values()
SKIP = {
    # Oracle cannot compare a BLOB (ORA-00932), and the ORM compares an NCLOB through DBMS_LOB.SUBSTR().
    "oracle": {"blob"},
}


@pytest.mark.parametrize("attname", list(VALUE))
def test_compared_values_convert_as_the_orm_lookup_converts_them(attname):
    if attname in SKIP.get(connection.vendor, ()):
        pytest.skip(f"{connection.display_name} does not compare this column with =")
    column, value = column_of(attname), VALUE[attname]
    if not isinstance(value, bool):  # Django 5.2 renders filter(flag=True) as "WHERE flag", with no param
        assert bridge_params(column, lambda c, v: c == v, value) == orm_lookup_params(attname, value)
    assert bridge_params(column, lambda c, v: c < v, value) == orm_lookup_params(f"{attname}__lt", value)


@pytest.mark.parametrize("attname", ["uid", "amount", "happened_at", "day", "took", "twin_id"])
def test_values_in_an_in_list_convert_as_the_orm_lookup_converts_them(attname):
    stmt = sa.select(specimens.c.uid).where(column_of(attname).in_(sa.bindparam("v", expanding=True)))
    params = comparable(prepare(stmt, {"v": [VALUE[attname]]}, connection).param_rows[0])
    assert params == orm_lookup_params(f"{attname}__in", [VALUE[attname]])


def test_values_compared_inside_a_write_convert_as_lookups():
    # A comparison in an UPDATE's WHERE is a lookup; the SET value beside it is written.
    gadget = Gadget.objects.create(code="g1")
    stmt = (
        sa.update(table_for(Gadget))
        .values(code=sa.bindparam("new"))
        .where(table_for(Gadget).c.code == sa.bindparam("old"))
    )
    assert execute(stmt, {"new": "g2", "old": gadget.code}) == 1
    assert Gadget.objects.get().code == "g2"


@pytest.mark.usefixtures("no_auto_pk_0")
def test_bulk_update_matches_keys_as_lookups_and_writes_values_as_saves():
    author = Author.objects.create(name="a", joined_at=AT)
    assert bulk_update(authors, [{"id": 0, "name": "b"}, {"id": author.pk, "name": "c"}]) == 1
    assert Author.objects.get().name == "c"
    with pytest.raises(ValueError, match="does not accept 0"):
        bulk_update(authors, [{"name": "d", "id": 0}], key="name")
