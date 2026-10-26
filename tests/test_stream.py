import contextlib
import datetime
import types
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import ProgrammingError, connection, connections, transaction
from django.db.backends.utils import CursorWrapper
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, stream, table_for
from tests.testapp.models import Author, Book

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=ZoneInfo("Asia/Seoul"))

authors = table_for(Author)
books = table_for(Book)

pg_only = pytest.mark.skipif(connection.vendor != "postgresql", reason="server-side cursors are PostgreSQL only")


def make_authors(n):
    Author.objects.bulk_create(Author(name=f"a{i:03}", joined_at=AT) for i in range(n))


def server_cursors():
    """Cursors PostgreSQL holds open for this session: (is_holdable, statement)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT is_holdable, statement FROM pg_cursors")
        return cursor.fetchall()


@pytest.fixture
def fetches(monkeypatch):
    """Sizes passed to every Django cursor's fetchmany()."""
    sizes = []
    original = CursorWrapper.__getattr__

    def fetchmany(self, size):
        sizes.append(size)
        return original(self, "fetchmany")(size)

    monkeypatch.setattr(CursorWrapper, "fetchmany", fetchmany, raising=False)
    return sizes


@pytest.fixture
def stream_cursors(monkeypatch):
    """[cursor, closed] for each cursor chunked_cursor() hands out."""
    opened = []
    wrapper = connections["default"]
    original_chunked_cursor = wrapper.chunked_cursor
    original_getattr = CursorWrapper.__getattr__

    def chunked_cursor():
        cursor = original_chunked_cursor()
        opened.append([cursor, False])
        return cursor

    def close(self):
        for entry in opened:
            if entry[0] is self:
                entry[1] = True
        return original_getattr(self, "close")()

    monkeypatch.setattr(wrapper, "chunked_cursor", chunked_cursor)
    monkeypatch.setattr(CursorWrapper, "close", close, raising=False)
    return opened


@pytest.fixture
def no_server_side_cursors(monkeypatch):
    monkeypatch.setitem(connections["default"].settings_dict, "DISABLE_SERVER_SIDE_CURSORS", True)


# --- results -----------------------------------------------------------------


def test_stream_yields_the_rows_execute_returns():
    author = Author.objects.create(name="x", joined_at=AT)
    for i in range(3):
        Book.objects.create(author=author, title=f"t{i}", price=Decimal(f"{i}.50"), meta={"i": i, "s": "한"})
    stmt = sa.select(books.c.id, books.c.uid, books.c.price, books.c.meta, authors.c.joined_at)
    stmt = stmt.join_from(books, authors).order_by(books.c.id)

    gen = stream(stmt, chunk_size=2)
    assert isinstance(gen, types.GeneratorType)
    rows = list(gen)
    assert rows == execute(stmt)
    assert len(rows) == 3
    assert all(isinstance(uid, uuid.UUID) for _, uid, *_ in rows)
    assert rows[2][2:] == (Decimal("2.50"), {"i": 2, "s": "한"}, AT)


@pytest.mark.usefixtures("needs_update_returning")
def test_stream_fills_params_and_streams_returning():
    make_authors(3)
    stmt = sa.select(authors.c.name).where(authors.c.name != sa.bindparam("skip")).order_by(authors.c.name)
    assert list(stream(stmt, {"skip": "a001"})) == [("a000",), ("a002",)]
    returning = sa.update(authors).values(name="z").returning(authors.c.joined_at)
    assert list(stream(returning, chunk_size=2)) == [(AT,)] * 3


@pg_only
def test_postgresql_runs_returning_on_a_plain_cursor(monkeypatch):
    # DECLARE takes only SELECT or VALUES, so DML ... RETURNING cannot use a server-side cursor.
    monkeypatch.setattr(connections["default"], "chunked_cursor", lambda: pytest.fail("chunked_cursor() used"))
    rows = stream(sa.insert(authors).values(name="r", joined_at=AT).returning(authors.c.name), chunk_size=1)
    assert list(rows) == [("r",)]


def test_stream_of_no_rows_is_empty():
    assert list(stream(sa.select(authors.c.id))) == []


def test_stream_refuses_statements_without_rows():
    with pytest.raises(ValueError, match="no rows"):
        stream(sa.update(authors).values(name="x"))
    with pytest.raises(ValueError, match="no rows"):
        stream(sa.insert(authors).values(name="x", joined_at=AT))
    # A text() statement can only be told apart once it ran. PostgreSQL refuses it
    # earlier: a server-side cursor (DECLARE) takes only SELECT or VALUES.
    gen = stream(sa.text("UPDATE testapp_author SET name = 'x'"))
    error = ProgrammingError if connection.vendor == "postgresql" else ValueError
    with pytest.raises(error, match="no rows" if error is ValueError else "syntax error"):
        with transaction.atomic():
            next(gen)


def test_stream_refuses_executemany_params():
    with pytest.raises(TypeError, match="single mapping"):
        stream(sa.select(authors.c.id), [{"a": 1}, {"a": 2}])


@pytest.mark.parametrize("size", [0, -1])
def test_stream_refuses_a_chunk_size_below_one(size):
    with pytest.raises(ValueError, match="chunk_size"):
        stream(sa.select(authors.c.id), chunk_size=size)


def test_default_chunk_size_matches_django_iterator(fetches):
    make_authors(1)
    list(Author.objects.values_list("id").iterator())
    django_sizes = list(fetches)
    fetches.clear()
    list(stream(sa.select(authors.c.id)))
    assert fetches == django_sizes == [2000, 2000]


# --- chunks and cursors ------------------------------------------------------


def test_fetchmany_is_called_with_chunk_size(fetches):
    make_authors(7)
    gen = stream(sa.select(authors.c.name).order_by(authors.c.name), chunk_size=3)
    assert next(gen) == ("a000",)
    assert fetches == [3]
    assert len(list(gen)) == 6
    assert fetches == [3, 3, 3, 3]  # 3 + 3 + 1, then an empty fetch ends it


def test_nothing_runs_until_the_first_row_is_asked_for():
    with CaptureQueriesContext(connection) as ctx:
        gen = stream(sa.select(authors.c.id))
        assert ctx.captured_queries == []
        list(gen)
    assert len(ctx.captured_queries) == 1


def test_stream_counts_as_many_queries_as_django_iterator():
    make_authors(5)
    with CaptureQueriesContext(connection) as django_ctx:
        list(Author.objects.values_list("id").iterator(chunk_size=2))
    with CaptureQueriesContext(connection) as bridge_ctx:
        list(stream(sa.select(authors.c.id), chunk_size=2))
    assert len(bridge_ctx.captured_queries) == len(django_ctx.captured_queries) == 1
    assert "testapp_author" in bridge_ctx.captured_queries[0]["sql"]


def test_stream_uses_django_chunked_cursor(monkeypatch):
    calls = []
    wrapper = connections["default"]
    original = wrapper.chunked_cursor

    def chunked_cursor():
        calls.append(1)
        return original()

    monkeypatch.setattr(wrapper, "chunked_cursor", chunked_cursor)
    make_authors(1)
    list(stream(sa.select(authors.c.id)))
    assert calls == [1]


def test_disabled_server_side_cursors_take_a_plain_cursor(monkeypatch, no_server_side_cursors):
    wrapper = connections["default"]
    monkeypatch.setattr(wrapper, "chunked_cursor", lambda: pytest.fail("chunked_cursor() used"))
    make_authors(2)
    assert len(list(stream(sa.select(authors.c.id), chunk_size=1))) == 2


@pytest.mark.parametrize("stop", ["close", "break", "error"])
def test_cursor_closes_when_the_consumer_stops_early(stream_cursors, stop):
    make_authors(5)
    stmt = sa.select(authors.c.id)

    if stop == "close":
        gen = stream(stmt, chunk_size=2)
        next(gen)
        gen.close()
    elif stop == "break":
        for _ in stream(stmt, chunk_size=2):
            break
    else:

        def consume():
            for _ in stream(stmt, chunk_size=2):
                raise ZeroDivisionError

        with pytest.raises(ZeroDivisionError):
            consume()
    assert [closed for _, closed in stream_cursors] == [True]


def test_cursor_closes_when_the_statement_fails(stream_cursors):
    gen = stream(sa.text("SELECT * FROM no_such_table"))
    with pytest.raises(Exception, match="no_such_table"):
        with transaction.atomic():
            next(gen)
    assert [closed for _, closed in stream_cursors] == [True]


# --- PostgreSQL server-side cursors, in and out of atomic() -------------------


@pg_only
def test_postgresql_streams_through_a_server_side_cursor_in_atomic():
    make_authors(5)
    gen = stream(sa.select(authors.c.id), chunk_size=2)
    next(gen)
    ((holdable, statement),) = server_cursors()
    assert holdable is False  # the test's transaction is open: an ordinary cursor
    assert "testapp_author" in statement
    gen.close()
    assert server_cursors() == []


@pg_only
def test_postgresql_disabled_server_side_cursors_declare_none(no_server_side_cursors):
    make_authors(5)
    gen = stream(sa.select(authors.c.id), chunk_size=2)
    next(gen)
    assert server_cursors() == []
    assert len(list(gen)) == 4


@pg_only
@pytest.mark.django_db(transaction=True)
def test_postgresql_outside_atomic_uses_a_holdable_cursor():
    assert connection.get_autocommit()
    make_authors(5)
    gen = stream(sa.select(authors.c.id).order_by(authors.c.id), chunk_size=2)
    first = next(gen)
    ((holdable, _),) = server_cursors()
    assert holdable is True  # WITH HOLD: it outlives the autocommitted DECLARE
    Author.objects.create(name="late", joined_at=AT)  # another committed transaction
    rest = list(gen)
    assert len([first, *rest]) == 5  # the cursor still sees its snapshot
    assert server_cursors() == []

    gen = stream(sa.select(authors.c.id), chunk_size=2)
    next(gen)
    gen.close()
    assert server_cursors() == []


# --- MySQL: no server-side cursor ---------------------------------------------


@pytest.mark.skipif(connection.vendor != "mysql", reason="MySQL behaviour")
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("in_atomic", [False, True], ids=["autocommit", "atomic"])
def test_mysql_streams_rows_the_driver_fetched_on_execute(in_atomic):
    # Django's MySQL backend has no server-side cursor: chunked_cursor() is cursor(),
    # whose MySQLdb cursor reads the whole result on execute(), as for QuerySet.iterator().
    import MySQLdb.cursors

    make_authors(5)
    with transaction.atomic() if in_atomic else contextlib.nullcontext():
        assert connection.get_autocommit() is not in_atomic
        with connection.chunked_cursor() as cursor:
            assert type(cursor.cursor.cursor) is MySQLdb.cursors.Cursor
        gen = stream(sa.select(authors.c.id).order_by(authors.c.id), chunk_size=2)
        first = next(gen)
        Author.objects.create(name="late", joined_at=AT)
        assert len([first, *gen]) == 5  # the rows were read before "late" was written


@pytest.mark.django_db(transaction=True)
def test_stream_inside_atomic_with_transaction_true():
    make_authors(3)
    with transaction.atomic():
        assert not connection.get_autocommit()
        gen = stream(sa.select(authors.c.name).order_by(authors.c.name), chunk_size=2)
        assert next(gen) == ("a000",)
        if connection.vendor == "postgresql":
            assert [holdable for holdable, _ in server_cursors()] == [False]
        assert list(gen) == [("a001",), ("a002",)]
    if connection.vendor == "postgresql":
        assert server_cursors() == []
