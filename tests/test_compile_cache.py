"""Compiled statements are reused when the same statement object runs again (#28).

The cache is keyed by the statement object the caller passed, the database
vendor and the params' names. What depends on the connection is never shared
between aliases: the RETURNING check is made on every call, and the result
converters are kept per connection object (tests/test_converter_cache.py).
"""

import datetime
import gc
import threading
import types
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import NotSupportedError, connection, connections
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, execute_result, execution, forget_table, stream, table_for
from django_sqlalchemy_bridge.execution import _ReturningCompiler, prepare
from tests.testapp.models import Author, Book, Stamp

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=ZoneInfo("Asia/Seoul"))

authors = table_for(Author)
books = table_for(Book)


@pytest.fixture
def compiles(monkeypatch):
    """The statements compiled so far, one entry per compilation."""
    compiled = []

    def counting_init(self, dialect, statement, *args, **kwargs):
        compiled.append(statement)
        super(_ReturningCompiler, self).__init__(dialect, statement, *args, **kwargs)

    monkeypatch.setattr(_ReturningCompiler, "__init__", counting_init, raising=False)
    return compiled


def make_author(name="a", using="default"):
    return Author.objects.using(using).create(name=name, joined_at=AT)


def book_values(author, **values):
    defaults = {"uid": uuid.uuid4(), "author_id": author.pk, "title": "t", "price": Decimal("1.00"), "meta": {}}
    return defaults | {"status": "draft"} | values


# --- compiled once -------------------------------------------------------------


def test_a_statement_run_again_is_not_compiled_again(compiles):
    first, second = make_author("x"), make_author("y")
    stmt = sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk"))
    assert execute(stmt, {"pk": first.pk}) == [("x",)]
    assert execute(stmt, {"pk": second.pk}) == [("y",)]
    assert execute_result(stmt, {"pk": first.pk}).scalar_one() == "x"
    assert list(stream(stmt, {"pk": second.pk})) == [("y",)]
    assert len(compiles) == 1


def test_a_statement_built_per_call_is_compiled_per_call(compiles):
    author = make_author()
    for _ in range(3):
        assert execute(sa.select(authors.c.name).where(authors.c.id == author.pk)) == [("a",)]
    assert len(compiles) == 3


def test_other_param_names_compile_other_sql(compiles):
    author = make_author("old")
    stmt = sa.update(authors).where(authors.c.id == sa.bindparam("pk"))
    later = AT + datetime.timedelta(days=1)
    sent = []

    def record(execute, sql, params, many, context):
        sent.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(record):
        assert execute(stmt, {"pk": author.pk, "name": "n1"}) == 1
        assert execute(stmt, {"pk": author.pk, "name": "n2", "joined_at": later}) == 1
        assert execute(stmt, {"pk": author.pk, "name": "n3"}) == 1
    first, second, third = sent
    assert first != second
    assert first == third
    assert len(compiles) == 2
    assert Author.objects.values_list("name", "joined_at").get() == ("n3", later)


def test_the_order_of_the_param_names_does_not_change_the_result():
    author = make_author("old")
    stmt = sa.update(authors).where(authors.c.id == sa.bindparam("pk"))
    assert execute(stmt, {"pk": author.pk, "name": "a"}) == 1
    assert execute(stmt, {"name": "b", "pk": author.pk}) == 1
    assert Author.objects.get().name == "b"


# --- a hit returns what a miss returns -----------------------------------------


def test_select_converts_on_a_hit():
    author = make_author()
    uid = uuid.uuid4()
    execute(sa.insert(books), book_values(author, uid=uid, price=Decimal("3.10"), meta={"a": 1}))
    stmt = sa.select(books.c.uid, books.c.price, books.c.meta, authors.c.joined_at).join_from(books, authors)
    stmt = stmt.where(books.c.uid == sa.bindparam("uid"))
    expected = [(uid, Decimal("3.10"), {"a": 1}, AT)]
    assert execute(stmt, {"uid": uid}) == expected
    assert execute(stmt, {"uid": uid}) == expected
    assert execute(stmt, {"uid": uuid.uuid4()}) == []


@pytest.mark.usefixtures("needs_returning")
def test_returning_converts_on_a_hit():
    author = make_author()
    stmt = sa.insert(books).returning(books.c.uid, books.c.price, books.c.meta)
    for price in ("1.10", "2.20"):
        uid = uuid.uuid4()
        rows = execute(stmt, book_values(author, uid=uid, price=Decimal(price), meta=[price]))
        assert rows == [(uid, Decimal(price), [price])]
    assert Book.objects.count() == 2


def test_executemany_on_a_hit():
    author = make_author()
    stmt = sa.insert(books)
    assert execute(stmt, [book_values(author, title="a"), book_values(author, title="b")]) == 2
    assert execute(stmt, [book_values(author, title="c")]) == 1
    assert execute(stmt, []) == 0
    assert sorted(Book.objects.values_list("title", flat=True)) == ["a", "b", "c"]
    with pytest.raises(ValueError, match="executemany"):
        execute(sa.insert(books).returning(books.c.id), [book_values(author)])


def test_expanding_in_takes_lists_of_any_length_on_a_hit(compiles):
    author = make_author()
    uids = sorted(uuid.uuid4() for _ in range(3))
    execute(sa.insert(books), [book_values(author, uid=uid) for uid in uids])
    compiles.clear()
    stmt = sa.select(books.c.uid).where(books.c.uid.in_(sa.bindparam("ids", expanding=True))).order_by(books.c.uid)
    for ids in ([uids[0]], uids, uids[1:], []):
        assert execute(stmt, {"ids": ids}) == [(uid,) for uid in ids]
    assert len(compiles) == 1


def test_stream_on_a_hit():
    author = make_author()
    execute(sa.insert(books), [book_values(author, price=Decimal(f"{i}.50")) for i in range(3)])
    stmt = sa.select(books.c.price).where(books.c.price > sa.bindparam("min")).order_by(books.c.price)
    assert list(stream(stmt, {"min": 0}, chunk_size=2)) == [(Decimal("0.50"),), (Decimal("1.50"),), (Decimal("2.50"),)]
    assert list(stream(stmt, {"min": 1}, chunk_size=2)) == [(Decimal("1.50"),), (Decimal("2.50"),)]
    if connection.vendor not in ("mysql", "oracle"):  # no UPDATE ... RETURNING the bridge runs
        returning = sa.update(authors).values(name=sa.bindparam("name")).returning(authors.c.name, authors.c.joined_at)
        assert list(stream(returning, {"name": "r"})) == [("r", AT)]
        assert list(stream(returning, {"name": "s"})) == [("s", AT)]


@pytest.mark.skipif(
    connection.vendor == "oracle" and tuple(int(n) for n in sa.__version__.split(".")[:2]) < (2, 1),
    reason="SQLAlchemy 2.0 cannot compile JSON paths for Oracle",
)
def test_json_keys_and_paths_on_a_hit():
    author = make_author()
    metas = [{"k": 1, "x": {"y": "a"}}, {"k": 2, "x": {"y": "b"}}]
    execute(sa.insert(books), [book_values(author, title=str(i), meta=m) for i, m in enumerate(metas)])
    by_key = sa.select(books.c.title).where(books.c.meta["k"].as_integer() == sa.bindparam("v"))
    by_path = sa.select(books.c.title).where(books.c.meta[("x", "y")].as_string() == sa.bindparam("v"))
    for _ in range(2):
        assert execute(by_key, {"v": 2}) == [("1",)]
        assert execute(by_path, {"v": "a"}) == [("0",)]


stamps = sa.Table(
    "testapp_stamp",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(50), default="scalar"),
)


def test_a_python_side_default_is_refused_on_every_call():
    stmt = sa.insert(stamps)
    with CaptureQueriesContext(connection) as queries:
        for _ in range(2):
            with pytest.raises(ValueError, match="Python-side default"):
                execute(stmt, {"id": 1})
    assert queries.captured_queries == []
    assert execute(stmt, {"id": 1, "name": "given"}) == 1
    with pytest.raises(ValueError, match="Python-side default"):
        execute(stmt, {"id": 2})
    assert list(Stamp.objects.values_list("name", flat=True)) == ["given"]


# --- the cached compilation follows the statement ------------------------------


def test_statements_derived_from_a_cached_one_compile_their_own_sql():
    first, second = make_author("x"), make_author("y")
    base = sa.select(authors.c.name).order_by(authors.c.name)
    assert execute(base) == [("x",), ("y",)]
    assert execute(base.where(authors.c.id == second.pk)) == [("y",)]
    assert execute(base.add_columns(authors.c.joined_at).where(authors.c.id == first.pk)) == [("x", AT)]
    insert = sa.insert(authors)
    assert execute(insert, {"name": "z", "joined_at": AT}) == 1
    if connection.features.can_return_columns_from_insert and connection.vendor != "oracle":
        # #21: .returning() derived from an executed statement still returns and converts.
        assert execute(insert.values(name="w", joined_at=AT).returning(authors.c.joined_at)) == [(AT,)]
    assert execute(insert, {"name": "v", "joined_at": AT}) == 1


def test_a_statement_still_runs_after_its_table_is_forgotten():
    # Stamp: no other table points at it, so forgetting it leaves the rest alone.
    table = table_for(Stamp)
    try:
        stmt = sa.select(table.c.name).where(table.c.id == sa.bindparam("pk"))
        stamp = Stamp.objects.create(name="x")
        assert execute(stmt, {"pk": stamp.pk}) == [("x",)]
        forget_table(Stamp)
        assert table_for(Stamp) is not table
        assert execute(stmt, {"pk": stamp.pk}) == [("x",)]
        assert execute(sa.select(table_for(Stamp).c.name)) == [("x",)]
    finally:
        forget_table(Stamp)


# --- connections: vendor and alias ---------------------------------------------


def test_the_compiled_sql_is_kept_apart_per_vendor():
    # No database: compile for a PostgreSQL and a SQLite connection in turn.
    names = sa.table("names", sa.column("name", sa.String))
    stmt = sa.select(names.c.name).where(names.c.name.ilike(sa.bindparam("p")))
    postgresql = types.SimpleNamespace(vendor="postgresql")
    sqlite = types.SimpleNamespace(vendor="sqlite")
    for _ in range(2):
        assert "ILIKE" in prepare(stmt, {"p": "a"}, postgresql).sql
        assert "lower(" in prepare(stmt, {"p": "a"}, sqlite).sql
        assert "ILIKE" not in prepare(stmt, {"p": "a"}, sqlite).sql


@pytest.mark.django_db(databases=["default", "other"])
def test_one_statement_on_two_aliases_converts_for_each():
    other = connections["other"]
    assert other.vendor == "sqlite" and other.timezone_name == "America/New_York"
    insert = sa.insert(authors).values(name=sa.bindparam("name"), joined_at=sa.bindparam("at"))
    select = sa.select(authors.c.name, authors.c.joined_at).where(authors.c.name.ilike(sa.bindparam("p")))
    for using in ("default", "other", "default", "other"):
        execute(insert, {"name": f"n-{using}", "at": AT}, using=using)
    for using in ("default", "other", "default"):
        assert execute(select, {"p": f"N-{using}"}, using=using) == [(f"n-{using}", AT)] * 2
        assert execute(select, {"p": "n-%"}, using=using) == [(f"n-{using}", AT)] * 2
    with other.cursor() as cursor:
        cursor.execute("SELECT joined_at FROM testapp_author")
        stored = [row[0] for row in cursor.fetchall()]
    # Naive New York time, as the ORM stores it under that database's TIME_ZONE.
    assert stored == [datetime.datetime(2026, 10, 24, 20, 30, 15, 123456)] * 2


@pytest.mark.django_db(databases=["default", "other"])
@pytest.mark.usefixtures("needs_returning")
def test_the_returning_check_is_made_per_alias(monkeypatch):
    stmt = sa.insert(authors).values(name=sa.bindparam("name"), joined_at=AT).returning(authors.c.name)
    assert execute(stmt, {"name": "a"}) == [("a",)]
    monkeypatch.setattr(connections["other"].features, "can_return_columns_from_insert", False)
    with pytest.raises(NotSupportedError, match="'other'"):
        execute(stmt, {"name": "b"}, using="other")
    assert execute(stmt, {"name": "c"}) == [("c",)]
    assert Author.objects.using("other").count() == 0


# --- memory --------------------------------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: (sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk")), {"pk": 1}),
        lambda: (sa.insert(authors).returning(authors.c.id), {"name": "a", "joined_at": AT}),
        lambda: (sa.update(authors).values(name="u").returning(authors.c.joined_at), None),
        lambda: (sa.delete(authors).where(authors.c.id.in_(sa.bindparam("ids", expanding=True))), {"ids": [1, 2]}),
        lambda: (sa.text("SELECT :x"), {"x": 1}),
        lambda: (sa.union(sa.select(authors.c.id), sa.select(books.c.id)), None),
    ],
    ids=["select", "insert returning", "update returning", "delete in", "text", "union"],
)
def test_the_cache_entry_goes_with_its_statement(make, request):
    stmt, params = make()
    if isinstance(stmt, sa.Insert | sa.Update) and stmt.returning_column_descriptions:
        request.getfixturevalue("needs_update_returning" if isinstance(stmt, sa.Update) else "needs_returning")
    execute(stmt, params)
    assert stmt in execution._cache
    gc.collect()  # statements of earlier tests may be garbage still waiting for the collector
    size = len(execution._cache)
    ref = weakref.ref(stmt)
    del stmt
    gc.collect()
    assert ref() is None
    assert len(execution._cache) == size - 1


# --- threads -------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_threads_running_one_statement_get_their_own_rows(compiles):
    pks = {Author.objects.create(name=f"a{i}", joined_at=AT + datetime.timedelta(hours=i)).pk: i for i in range(8)}
    stmt = sa.select(authors.c.name, authors.c.joined_at).where(authors.c.id == sa.bindparam("pk"))
    start = threading.Barrier(len(pks))

    def lookups(pk):
        try:
            start.wait()
            return [execute(stmt, {"pk": pk}) for _ in range(30)]
        finally:
            connection.close()

    with ThreadPoolExecutor(len(pks)) as pool:
        results = dict(zip(pks, pool.map(lookups, pks)))
    for pk, i in pks.items():
        assert results[pk] == [[(f"a{i}", AT + datetime.timedelta(hours=i))]] * 30
    # Racing threads may each compile it once, never once per call.
    assert 1 <= len(compiles) <= len(pks)
