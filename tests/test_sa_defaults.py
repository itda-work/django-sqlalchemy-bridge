"""SQLAlchemy Python-side column defaults on a hand-built table (#19).

``Column(default=...)`` and ``Column(onupdate=...)`` holding a Python value or
callable are filled in by SQLAlchemy's execution context, which the bridge does
not use. The compiler still puts those columns in the SQL, so they would bind
``NULL``; the bridge refuses such a statement instead. SQL expression defaults
compile into the statement, and ``server_default`` / Django ``db_default`` are
left to the database.
"""

import pytest
import sqlalchemy as sa
from django.db import connection
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import bulk_insert, bulk_update, execute, execute_result, stream
from tests.testapp.models import Stamp

pytestmark = pytest.mark.django_db

stamps = sa.Table(
    "testapp_stamp",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(50), default="scalar"),
    sa.Column("note", sa.String(50), default=lambda: "callable", onupdate=lambda: "touched"),
    sa.Column("at", sa.DateTime(timezone=True), default=sa.func.now(), onupdate=sa.func.now()),
    # Django's db_default puts "db" in the schema; this one only feeds SQLAlchemy DDL.
    sa.Column("kind", sa.String(10), server_default="sa"),
)


def stored():
    return list(Stamp.objects.order_by("id").values_list("name", "note", "kind"))


def refused_inserts():
    # Each call builds its statement afresh: a DML statement whose exported_columns
    # were read hands that (empty) collection on to .returning() copies of it.
    def insert():
        return sa.insert(stamps)

    return {
        "execute": lambda: execute(insert().values(name="n")),
        "executemany": lambda: execute(insert(), [{"name": "a"}, {"name": "b"}]),
        "execute_result": lambda: execute_result(insert().values(name="n").returning(stamps.c.id)),
        "stream": lambda: stream(insert().values(name="n").returning(stamps.c.id)),
        "multi values": lambda: execute(insert().values([{"name": "a"}, {"name": "b"}])),
        "from select": lambda: execute(insert().from_select(["note"], sa.select(sa.literal("x")))),
    }


@pytest.mark.parametrize("name", refused_inserts())
def test_insert_leaving_out_a_python_side_default_is_refused_before_running(name, request):
    if name in ("execute_result", "stream"):
        request.getfixturevalue("needs_returning")
    call = refused_inserts()[name]
    Stamp.objects.create(name="kept")
    with CaptureQueriesContext(connection) as queries, pytest.raises(ValueError, match="Python-side default") as e:
        call()
    assert queries.captured_queries == []
    assert stored() == [("kept", None, "db")]
    assert "testapp_stamp." in str(e.value)
    assert "pass the value" in str(e.value)


def test_the_error_names_each_column_left_out():
    with pytest.raises(ValueError) as e:
        execute(sa.insert(stamps).values(id=1))
    assert "testapp_stamp.name, testapp_stamp.note cannot" in str(e.value)
    with pytest.raises(ValueError) as e:
        execute(sa.insert(stamps).values(note="n"))
    assert "testapp_stamp.name cannot" in str(e.value)
    assert "note" not in str(e.value)


def update_calls():
    def update():
        return sa.update(stamps).where(stamps.c.id == sa.bindparam("pk"))

    return {
        "execute": lambda pk: execute(update().values(name="u"), {"pk": pk}),
        "executemany": lambda pk: execute(update().values(name=sa.bindparam("n")), [{"pk": pk, "n": "u"}]),
        "execute_result": lambda pk: execute_result(update().values(name="u").returning(stamps.c.id), {"pk": pk}),
        "stream": lambda pk: stream(update().values(name="u").returning(stamps.c.id), {"pk": pk}),
    }


@pytest.mark.parametrize("name", update_calls())
def test_update_leaving_out_a_python_side_onupdate_is_refused_instead_of_nulling_it(name, request):
    if name in ("execute_result", "stream"):
        request.getfixturevalue("needs_update_returning")
    call = update_calls()[name]
    pk = Stamp.objects.create(name="n", note="kept").pk
    with pytest.raises(ValueError, match=r"Python-side default or onupdate of testapp_stamp\.note cannot"):
        call(pk)
    assert stored() == [("n", "kept", "db")]


def test_values_given_for_those_columns_run():
    assert execute(sa.insert(stamps).values(name="a", note="b")) == 1
    assert execute(sa.insert(stamps), [{"name": "c", "note": "d"}]) == 1
    if connection.features.can_return_columns_from_insert:  # MySQL has no RETURNING
        rows = execute_result(sa.insert(stamps).values(name="e", note="f").returning(stamps.c.name)).all()
        assert [tuple(row) for row in rows] == [("e",)]
    else:
        assert execute(sa.insert(stamps).values(name="e", note="f")) == 1
    assert stored() == [("a", "b", "db"), ("c", "d", "db"), ("e", "f", "db")]
    assert execute(sa.update(stamps).where(stamps.c.name == "a").values(name="A", note="B")) == 1
    update = sa.update(stamps).where(stamps.c.name == "c").values(note="D")
    if connection.vendor == "mysql":  # MySQL and MariaDB have no UPDATE ... RETURNING
        assert execute(update) == 1
    else:
        assert list(stream(update.returning(stamps.c.note))) == [("D",)]
    assert stored() == [("A", "B", "db"), ("c", "D", "db"), ("e", "f", "db")]


def test_sql_expression_defaults_compile_into_the_statement():
    execute(sa.insert(stamps).values(name="a", note="b"))
    assert Stamp.objects.get().at is not None
    Stamp.objects.update(at=None)
    execute(sa.update(stamps).values(note="c"))
    assert Stamp.objects.get().at is not None


def test_server_and_db_defaults_are_left_to_the_database():
    execute(sa.insert(stamps).values(name="a", note="b"))
    assert stored() == [("a", "b", "db")]


def test_statements_that_write_no_values_are_not_affected():
    Stamp.objects.create(name="a")
    assert execute(sa.select(stamps.c.name, stamps.c.note)) == [("a", None)]
    assert execute(sa.delete(stamps)) == 1


@pytest.mark.parametrize("bulk", [bulk_insert, bulk_update])
def test_bulk_writes_do_not_take_a_hand_built_table(bulk):
    # bulk_* build their own statements from table_for() tables, which have no such defaults.
    with pytest.raises(TypeError, match="table_for"):
        bulk(stamps, [{"id": 1, "name": "a", "note": "b"}])
