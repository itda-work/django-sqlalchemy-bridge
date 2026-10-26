"""execute_pipeline(): several statements in one round trip on PostgreSQL (#50).

On PostgreSQL with psycopg 3 and libpq pipeline support the statements run
inside ``connection.connection.pipeline()``, each on its own Django cursor; on
every other backend they run one after another. Either way the results are the
same, in input order, and the call writes all of its rows or none.
"""

import asyncio
import datetime
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.core.exceptions import SynchronousOnlyOperation
from django.db import IntegrityError, NotSupportedError, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, execute_pipeline, table_for
from django_sqlalchemy_bridge.execution import _ReturningCompiler
from tests.testapp.models import Author, Book, Gadget, Stamp

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 26, 9, 30, 15, 123456, tzinfo=ZoneInfo("Asia/Seoul"))
IS_PG = connection.vendor == "postgresql"
PIPELINED = IS_PG and connection.Database.Pipeline.is_supported()

authors = table_for(Author)
books = table_for(Book)
gadgets = table_for(Gadget)


@pytest.fixture
def pipelines(monkeypatch):
    """How many times psycopg's Connection.pipeline() was entered (PostgreSQL only)."""
    if not IS_PG:
        return None
    import psycopg

    entered = []
    original = psycopg.Connection.pipeline

    def counting(self, *args, **kwargs):
        entered.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "pipeline", counting)
    return entered


def make_author(name="a"):
    return Author.objects.create(name=name, joined_at=AT)


def add(code):
    return sa.insert(gadgets).values(code=code)


def codes():
    return sorted(Gadget.objects.values_list("code", flat=True))


# --- results ---------------------------------------------------------------


def test_results_come_in_input_order_with_the_shapes_execute_gives(pipelines):
    first, second = make_author("x"), make_author("y")
    by_pk = sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk"))
    items = [
        (by_pk, {"pk": second.pk}),
        sa.update(authors).values(name="z").where(authors.c.id == first.pk),
        (by_pk, {"pk": first.pk}),
        sa.select(sa.func.count()).select_from(authors),
        sa.text(f"SELECT COUNT(*) FROM {Author._meta.db_table}"),
        (sa.delete(authors).where(authors.c.name == "nope"), None),
    ]
    assert execute_pipeline(items) == [[("y",)], 1, [("z",)], [(2,)], [(2,)], 0]
    if IS_PG:
        assert len(pipelines) == 1


def test_the_same_items_give_the_same_results_as_execute_one_by_one(pipelines):
    for name in "abc":
        make_author(name)
    items = [
        sa.select(authors.c.name).order_by(authors.c.name),
        sa.select(sa.func.max(authors.c.joined_at)),
        (sa.select(authors.c.id).where(authors.c.name.in_(sa.bindparam("names", expanding=True))), {"names": ["b"]}),
    ]
    expected = [execute(stmt) if not isinstance(stmt, tuple) else execute(*stmt) for stmt in items]
    assert execute_pipeline(items) == expected


def test_values_convert_both_ways_as_in_execute(needs_returning):
    author = make_author()
    uid = uuid.uuid4()
    insert = sa.insert(books).values(
        uid=uid, author_id=author.pk, title="t", price=Decimal("1.50"), status=Book.Status.PUBLISHED, meta={"k": 1}
    )
    rows = execute_pipeline(
        [
            insert.returning(books.c.uid, books.c.price),
            sa.select(books.c.uid, books.c.status, books.c.meta).where(books.c.uid == uid),
            sa.select(authors.c.joined_at).where(authors.c.id == author.pk),
        ]
    )
    assert rows == [[(uid, Decimal("1.50"))], [(uid, "published", {"k": 1})], [(AT,)]]
    assert rows[2][0][0].utcoffset() == datetime.timedelta(0)


def test_executemany_items_give_their_rowcount(pipelines):
    rows = execute_pipeline(
        [
            (sa.insert(gadgets), [{"code": "a"}, {"code": "b"}]),
            sa.select(sa.func.count()).select_from(gadgets),
            (sa.insert(gadgets), []),
            (sa.update(gadgets).where(gadgets.c.code == sa.bindparam("c")).values(code="z"), [{"c": "a"}, {"c": "q"}]),
        ]
    )
    assert rows == [2, [(2,)], 0, 1]
    assert codes() == ["b", "z"]
    if IS_PG:
        assert len(pipelines) == 1


def test_an_empty_sequence_runs_nothing(pipelines):
    with CaptureQueriesContext(connection) as queries:
        assert execute_pipeline([]) == []
        assert execute_pipeline(iter(())) == []
    assert len(queries) == 0
    if IS_PG:
        assert pipelines == []


def test_any_iterable_of_items_is_accepted():
    make_author("x")
    assert execute_pipeline(sa.select(authors.c.name) for _ in range(2)) == [[("x",)], [("x",)]]


def test_each_statement_is_one_query_on_django_s_cursor():
    author = make_author()
    by_pk = sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk"))
    with CaptureQueriesContext(connection) as queries:
        execute_pipeline([(by_pk, {"pk": author.pk})] * 3 + [(sa.insert(gadgets), [{"code": "a"}, {"code": "b"}])])
    assert len(queries) == 4
    assert all("SELECT" in query["sql"] for query in queries.captured_queries[:3])


def test_execute_wrapper_sees_every_statement():
    seen = []

    def wrapper(execute, sql, params, many, context):
        seen.append(many)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        execute_pipeline([add("a"), (sa.insert(gadgets), [{"code": "b"}]), sa.select(gadgets.c.code)])
    assert seen == [False, True, False]


@pytest.mark.skipif(not IS_PG, reason="PostgreSQL only")
def test_postgresql_runs_every_item_inside_one_pipeline(pipelines):
    by_pk = sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk"))
    authors_ = [make_author(str(i)) for i in range(20)]
    rows = execute_pipeline([(by_pk, {"pk": author.pk}) for author in authors_])
    assert rows == [[(str(i),)] for i in range(20)]
    assert pipelines == [connection.connection]


def test_other_backends_run_without_a_pipeline():
    if IS_PG:
        pytest.skip("PostgreSQL pipelines")
    make_author("x")
    assert execute_pipeline([sa.select(authors.c.name)] * 2) == [[("x",)], [("x",)]]


@pytest.mark.skipif(not IS_PG, reason="PostgreSQL only")
def test_without_libpq_pipeline_support_it_runs_one_by_one(pipelines, monkeypatch):
    monkeypatch.setattr(connection.Database.Pipeline, "is_supported", classmethod(lambda cls: False))
    make_author("x")
    with CaptureQueriesContext(connection) as queries:
        assert execute_pipeline([sa.select(authors.c.name), add("a")]) == [[("x",)], 1]
    assert pipelines == []
    assert len(queries) == 2


# --- failures ----------------------------------------------------------------


def failing_items():
    """Item 2 breaks the unique code; item 0 and 1 write, item 3 would."""
    return [add("a"), add("b"), add("a"), add("c")]


def item_note(error):
    return [note for note in getattr(error, "__notes__", []) if "execute_pipeline" in note]


def test_a_failing_item_raises_its_own_error_with_its_index():
    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic():
            execute_pipeline(failing_items())
    assert item_note(raised.value) == ["execute_pipeline(): item 2 failed"]
    assert codes() == []


def test_the_items_after_a_failure_do_not_run():
    seen = []

    def wrapper(execute, sql, params, many, context):
        seen.append(params)
        return execute(sql, params, many, context)

    with pytest.raises(IntegrityError):
        with transaction.atomic(), connection.execute_wrapper(wrapper):
            execute_pipeline(failing_items())
    # A pipeline sends every statement before reading any result, so the server
    # gets item 3 too and skips it (the block is aborted); one by one it is never sent.
    assert seen == [("a",), ("b",), ("a",), ("c",)] if PIPELINED else [("a",), ("b",), ("a",)]
    assert codes() == []


def test_the_error_is_django_s_and_no_pipeline_aborted_shows():
    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic():
            execute_pipeline(failing_items())
    chain, error = [], raised.value
    while error is not None:
        chain.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    assert "PipelineAborted" not in chain


@pytest.mark.parametrize(
    ("items", "index"),
    [
        # A failing executemany row, then a statement after it.
        ([add("a"), (sa.insert(gadgets), [{"code": "b"}, {"code": "a"}]), add("c")], 1),
        # A whole executemany, then a failing statement.
        ([(sa.insert(gadgets), [{"code": "a"}, {"code": "b"}]), add("b"), add("c")], 1),
        # The first item.
        ([add("a"), add("a")], 1),
        ([sa.text("SELECT * FROM no_such_table"), add("a")], 0),
    ],
)
def test_the_index_is_right_around_executemany_items(items, index):
    with pytest.raises(Exception) as raised:
        with transaction.atomic():
            execute_pipeline(items)
    assert item_note(raised.value) == [f"execute_pipeline(): item {index} failed"]
    assert codes() == []


def test_many_items_with_an_early_failure():
    # Enough statements that psycopg reads results while they are still being sent.
    items = [add("dup"), add("dup")] + [add(f"x{i}") for i in range(2000)]
    with pytest.raises(IntegrityError) as raised:
        with transaction.atomic():
            execute_pipeline(items)
    assert item_note(raised.value) == ["execute_pipeline(): item 1 failed"]
    assert codes() == []


def test_a_failure_inside_atomic_rolls_the_block_back():
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            execute(add("before"))
            execute_pipeline(failing_items())
    assert codes() == []
    with transaction.atomic():
        execute(add("after"))
    assert codes() == ["after"]


@pytest.mark.django_db(transaction=True)
def test_outside_atomic_a_call_writes_all_of_its_rows_or_none():
    with pytest.raises(IntegrityError):
        execute_pipeline(failing_items())
    assert codes() == []
    assert connection.get_autocommit()
    assert execute_pipeline([add("a"), add("b")]) == [1, 1]
    assert codes() == ["a", "b"]
    assert connection.get_autocommit()


@pytest.mark.django_db(transaction=True)
def test_outside_atomic_an_executemany_before_a_failure_is_not_kept():
    # On PostgreSQL a sync follows an executemany; outside a transaction it would commit.
    with pytest.raises(IntegrityError):
        execute_pipeline([(sa.insert(gadgets), [{"code": "a"}, {"code": "b"}]), add("c"), add("a")])
    assert codes() == []


@pytest.mark.django_db(transaction=True)
def test_outside_atomic_a_later_call_runs_after_a_failed_one():
    with pytest.raises(IntegrityError):
        execute_pipeline([add("a"), add("a")])
    assert execute_pipeline([add("b")]) == [1]
    assert codes() == ["b"]


# --- checks before anything runs ---------------------------------------------


def test_returning_is_refused_for_every_item_before_any_runs():
    if connection.features.can_return_columns_from_insert and connection.vendor != "oracle":
        pytest.skip(f"{connection.display_name} has RETURNING")
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(NotSupportedError) as raised:
            execute_pipeline([add("a"), add("b").returning(gadgets.c.id)])
    assert len(queries) == 0
    assert item_note(raised.value) == ["execute_pipeline(): item 1 failed"]
    assert codes() == []


stamps = sa.Table(
    "testapp_stamp",
    sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("name", sa.String(50), default="scalar"),
)


def test_python_side_defaults_are_refused_for_every_item_before_any_runs():
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match="Python-side default") as raised:
            execute_pipeline([add("a"), sa.insert(stamps).values(id=1)])
    assert len(queries) == 0
    assert item_note(raised.value) == ["execute_pipeline(): item 1 failed"]
    assert not Stamp.objects.exists()


def test_executemany_with_returning_is_refused_before_any_runs(needs_returning):
    with CaptureQueriesContext(connection) as queries:
        with pytest.raises(ValueError, match="executemany cannot return rows"):
            execute_pipeline([add("a"), (sa.insert(gadgets).returning(gadgets.c.id), [{"code": "b"}])])
    assert len(queries) == 0


def test_an_item_that_is_not_a_statement_or_a_pair_is_refused():
    with pytest.raises((TypeError, ValueError)):
        execute_pipeline([(add("a"), None, None)])


# --- compile cache, async ----------------------------------------------------


@pytest.fixture
def compiles(monkeypatch):
    compiled = []

    def counting_init(self, dialect, statement, *args, **kwargs):
        compiled.append(statement)
        super(_ReturningCompiler, self).__init__(dialect, statement, *args, **kwargs)

    monkeypatch.setattr(_ReturningCompiler, "__init__", counting_init, raising=False)
    return compiled


def test_a_statement_object_is_compiled_once_across_items_and_calls(compiles):
    first, second = make_author("x"), make_author("y")
    by_pk = sa.select(authors.c.name).where(authors.c.id == sa.bindparam("pk"))
    assert execute_pipeline([(by_pk, {"pk": first.pk}), (by_pk, {"pk": second.pk})]) == [[("x",)], [("y",)]]
    assert execute_pipeline([(by_pk, {"pk": second.pk})]) == [[("y",)]]
    assert execute(by_pk, {"pk": first.pk}) == [("x",)]
    assert len(compiles) == 1


def test_calling_it_from_async_code_raises():
    async def main():
        execute_pipeline([sa.select(authors.c.id)])

    with pytest.raises(SynchronousOnlyOperation):
        asyncio.run(main())
