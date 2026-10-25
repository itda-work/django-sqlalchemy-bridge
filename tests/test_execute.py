import asyncio
import datetime
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.core.exceptions import SynchronousOnlyOperation
from django.db import NotSupportedError, connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import bulk_insert, execute, execute_result, stream, table_for
from tests.testapp.models import Author, Book, Gadget, Specimen

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=SEOUL)

authors = table_for(Author)
books = table_for(Book)
specimens = table_for(Specimen)


def raw(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def make_author(name="a"):
    return Author.objects.create(name=name, joined_at=AT)


def insert_book(author, **values):
    # Every column is given: the bridge does not apply Django field defaults.
    defaults = {"uid": uuid.uuid4(), "author_id": author.pk, "title": "t", "price": Decimal("1.00"), "meta": {}}
    values = defaults | {"status": Book.Status.DRAFT} | values
    return execute(sa.insert(books).values(**values))


# --- return shape ------------------------------------------------------------


def test_select_returns_a_list_of_tuples():
    author = make_author("x")
    assert execute(sa.select(authors.c.id, authors.c.name)) == [(author.pk, "x")]


def test_dml_without_returning_returns_the_rowcount():
    author = make_author()
    assert insert_book(author) == 1
    insert_book(author)
    assert execute(sa.update(books).values(title="u")) == 2
    assert execute(sa.delete(books).where(books.c.title == "nope")) == 0


def test_returning_returns_rows_converted_by_the_field():
    author = make_author()
    uid = uuid.uuid4()
    rows = execute(
        sa.insert(books)
        .values(uid=uid, author_id=author.pk, title="t", price=Decimal("3.10"), meta={"a": 1}, status="draft")
        .returning(books.c.id, books.c.uid, books.c.price, books.c.meta)
    )
    pk = Book.objects.get().pk
    assert rows == [(pk, uid, Decimal("3.10"), {"a": 1})]


def test_params_fill_bindparams():
    author = make_author("old")
    stmt = sa.update(authors).where(authors.c.id == sa.bindparam("pk")).values(name=sa.bindparam("new"))
    assert execute(stmt, {"pk": author.pk, "new": "renamed"}) == 1
    assert Author.objects.get().name == "renamed"


def test_params_name_insert_columns():
    author = make_author()
    row = {"uid": uuid.uuid4(), "author_id": author.pk, "title": "p", "price": 2, "meta": [1], "status": "draft"}
    execute(sa.insert(books), row)
    book = Book.objects.get()
    assert (book.title, book.price, book.meta) == ("p", Decimal("2.00"), [1])


def test_executemany_inserts_and_updates_every_row():
    author = make_author()
    uids = [uuid.uuid4(), uuid.uuid4()]
    common = {"author_id": author.pk, "status": "draft"}
    rows = [
        common | {"uid": uids[0], "title": "a", "price": Decimal("1.5"), "meta": {"n": 1}},
        common | {"uid": uids[1], "title": "b", "price": Decimal("2.5"), "meta": []},
    ]
    assert execute(sa.insert(books), rows) == 2
    assert list(Book.objects.order_by("title").values_list("uid", "price", "meta")) == [
        (uids[0], Decimal("1.50"), {"n": 1}),
        (uids[1], Decimal("2.50"), []),
    ]

    stmt = sa.update(books).where(books.c.uid == sa.bindparam("u")).values(title=sa.bindparam("t"))
    assert execute(stmt, [{"u": uids[0], "t": "A"}, {"u": uids[1], "t": "B"}]) == 2
    assert list(Book.objects.order_by("title").values_list("title", flat=True)) == ["A", "B"]


def test_executemany_with_no_rows_runs_nothing():
    with CaptureQueriesContext(connection) as ctx:
        assert execute(sa.insert(authors), []) == 0
    assert ctx.captured_queries == []


def test_executemany_refuses_a_statement_that_returns_rows():
    with pytest.raises(ValueError, match="executemany"):
        execute(sa.insert(authors).returning(authors.c.id), [{"name": "a", "joined_at": AT}])


def test_in_expands_and_converts_each_value():
    author = make_author()
    uids = [uuid.uuid4() for _ in range(3)]
    for uid in uids:
        insert_book(author, uid=uid)
    stmt = sa.select(books.c.uid).where(books.c.uid.in_(uids[:2])).order_by(books.c.uid)
    assert execute(stmt) == [(u,) for u in sorted(uids[:2])]
    stmt = sa.select(books.c.uid).where(books.c.uid.in_(sa.bindparam("ids", expanding=True)))
    assert execute(stmt, {"ids": [uids[2]]}) == [(uids[2],)]


def test_expressions_that_are_not_columns_come_back_raw():
    author = make_author()
    insert_book(author, title="abc")
    insert_book(author, title="abcd")
    stmt = sa.select(sa.func.count(books.c.id), sa.func.length(books.c.title).label("n")).group_by(sa.text("2"))
    stmt = stmt.order_by(sa.text("2"))
    assert execute(stmt) == [(1, 3), (1, 4)]


def test_labels_and_aggregates_of_a_column_keep_its_conversion():
    author = make_author()
    insert_book(author, price=Decimal("1.25"))
    insert_book(author, price=Decimal("9.75"))
    stmt = sa.select(sa.func.max(books.c.price), authors.c.joined_at.label("when")).join_from(books, authors)
    stmt = stmt.group_by(authors.c.joined_at)
    assert execute(stmt) == [(Decimal("9.75"), AT)]


def test_unknown_database_alias_is_rejected():
    from django.utils.connection import ConnectionDoesNotExist

    with pytest.raises(ConnectionDoesNotExist):
        execute(sa.select(authors.c.id), using="nope")


# --- literal percent signs ---------------------------------------------------


def test_literal_percent_signs_survive():
    author = make_author()
    insert_book(author, title="a%b")
    insert_book(author, title="ab")
    title = books.c.title
    assert execute(sa.select(title).where(title.like(sa.literal_column("'a%'"))).order_by(title)) == [("a%b",), ("ab",)]
    assert execute(sa.select(title).where(title == "a%b")) == [("a%b",)]  # bound values are not %-processed
    assert execute(sa.select(title).where(title.like("a\\%b", escape="\\"))) == [("a%b",)]
    assert execute(sa.text("SELECT title FROM testapp_book WHERE title LIKE 'a\\%%' ESCAPE '\\'")) == [("a%b",)]
    assert execute(sa.text("SELECT '100%'")) == [("100%",)]
    # Literals the compiler renders itself keep a single backslash (standard_conforming_strings).
    assert execute(sa.select(sa.literal("a\\b%", literal_execute=True))) == [("a\\b%",)]
    assert execute(sa.text("SELECT '100%' WHERE 1 = :one"), {"one": 1}) == [("100%",)]


# --- Django owns the connection and the transaction --------------------------


def test_atomic_rollback_discards_bridge_writes():
    author = make_author()
    with pytest.raises(ZeroDivisionError):
        with transaction.atomic():
            insert_book(author)
            assert Book.objects.count() == 1
            1 / 0
    assert Book.objects.count() == 0


def test_nested_savepoint_rolls_back_only_the_inner_writes():
    author = make_author()
    with transaction.atomic():
        insert_book(author, title="outer")
        with pytest.raises(ZeroDivisionError):
            with transaction.atomic():
                insert_book(author, title="inner")
                1 / 0
        insert_book(author, title="after")
    assert sorted(Book.objects.values_list("title", flat=True)) == ["after", "outer"]


@pytest.mark.django_db(transaction=True)
def test_on_commit_runs_once_when_the_bridge_write_commits():
    calls = []
    author = make_author()
    with transaction.atomic():
        insert_book(author)
        transaction.on_commit(lambda: calls.append(Book.objects.count()))
        assert calls == []
    assert calls == [1]

    with pytest.raises(ZeroDivisionError):
        with transaction.atomic():
            insert_book(author)
            transaction.on_commit(lambda: calls.append("rolled back"))
            1 / 0
    assert calls == [1]
    assert Book.objects.count() == 1


def test_queries_are_captured_and_counted(django_assert_num_queries):
    make_author()
    with CaptureQueriesContext(connection) as ctx:
        execute(sa.select(authors.c.name).where(authors.c.name == "a"))
    assert len(ctx.captured_queries) == 1
    assert "testapp_author" in ctx.captured_queries[0]["sql"]
    with django_assert_num_queries(2):
        execute(sa.select(authors.c.id))
        execute(sa.update(authors).values(name="b"))


def test_execute_wrapper_sees_bridge_queries():
    seen = []

    def wrapper(execute_, sql, params, many, context):
        seen.append((sql, params, many))
        return execute_(sql, params, many, context)

    author = make_author()
    with connection.execute_wrapper(wrapper):
        execute(sa.select(authors.c.name).where(authors.c.id == author.pk))
        execute(sa.insert(authors), [{"name": "x", "joined_at": AT}, {"name": "y", "joined_at": AT}])
    assert len(seen) == 2
    assert "testapp_author" in seen[0][0]
    assert list(seen[0][1]) == [author.pk]
    assert seen[0][2] is False
    assert seen[1][2] is True


def test_calling_from_async_code_is_refused():
    async def main():
        execute(sa.select(authors.c.id))

    with pytest.raises(SynchronousOnlyOperation):
        asyncio.run(main())


def test_bridge_writes_send_no_model_signals():
    received = []

    def receiver(sender, **kwargs):
        received.append(sender)

    author = make_author()
    pre_save.connect(receiver, sender=Book)
    post_save.connect(receiver, sender=Book)
    try:
        insert_book(author)
        execute(sa.update(books).values(title="u"))
        execute(sa.delete(books))
    finally:
        pre_save.disconnect(receiver, sender=Book)
        post_save.disconnect(receiver, sender=Book)
    assert received == []


# --- values round-trip exactly as through the ORM ----------------------------


def specimen_values(**overrides):
    return {
        "uid": uuid.uuid4(),
        "label": "label",
        "body": "",
        "slug": "s",
        "email": None,
        "small": -3,
        "positive": 7,
        "big": 2**40,
        "flag": True,
        "maybe": None,
        "ratio": 0.25,
        "amount": Decimal("123.456"),
        "day": datetime.date(2026, 10, 25),
        "at": datetime.time(23, 59, 1, 500),
        "happened_at": AT,
        "took": datetime.timedelta(days=1, microseconds=7),
        "blob": b"\x00\xffbytes",
        "data": {"k": [1, "두", None], "n": 1.5},
        "level": Specimen.Level.HIGH,
        "gadget": None,
        "twin": None,
    } | overrides


def specimen_columns(values):
    """The same values keyed by column name, as the bridge takes them."""
    fields = {f.name: f for f in Specimen._meta.concrete_fields}
    out = {}
    for name, value in values.items():
        field = fields[name]
        if field.is_relation and value is not None:
            value = getattr(value, field.target_field.attname)
        out[field.column] = value
    return out


def orm_rows(**filters):
    attnames = [specimens.c[name].info["django_field"].attname for name in specimens.c.keys()]
    return list(Specimen.objects.filter(**filters).values_list(*attnames))


def stored(uid):
    """The row as the database holds it (no field converters), without its uid and twin."""
    column = Specimen._meta.get_field("uid")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT * FROM dsb_specimen WHERE uid = %s", [column.get_db_prep_value(uid, connection, prepared=False)]
        )
        names = [c[0] for c in cursor.description]
        return {k: v for k, v in zip(names, cursor.fetchone()) if k not in ("uid", "twin_id")}


def orm_row(uid):
    """The row as the ORM reads it, without its uid and twin."""
    names = list(specimens.c.keys())
    return {k: v for k, v in zip(names, orm_rows(uid=uid)[0]) if k not in ("uid", "twin_id")}


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {
            "email": "a@b.c",
            "maybe": False,
            "happened_at": None,
            "data": None,
            "level": Specimen.Level.LOW,
            "gadget": None,
        },
        {"happened_at": AT.astimezone(datetime.UTC), "data": [], "flag": False},
    ],
    ids=["filled", "nulls", "utc"],
)
def test_bridge_write_matches_orm_write(overrides):
    author = make_author()
    gadget = Gadget.objects.create(code="g1")
    twins = [Specimen.objects.create(**specimen_values(author=author)) for _ in range(2)]
    via_orm = specimen_values(author=author, gadget=gadget, twin=twins[0]) | overrides
    via_bridge = via_orm | {"uid": uuid.uuid4(), "twin": twins[1]}  # twin is one-to-one

    Specimen.objects.create(**via_orm)
    execute(sa.insert(specimens).values(**specimen_columns(via_bridge)))

    # The database holds the same thing either way (no 9-hour shift, no Enum names) ...
    assert stored(via_bridge["uid"]) == stored(via_orm["uid"])
    # ... the ORM reads the bridge's row back as it reads its own ...
    assert orm_row(via_bridge["uid"]) == orm_row(via_orm["uid"])
    # ... and the bridge reads every column (uid and twin included) as the ORM does.
    bridge_row = execute(sa.select(specimens).where(specimens.c.uid == via_bridge["uid"]))
    assert bridge_row == orm_rows(uid=via_bridge["uid"])
    assert bridge_row[0][list(specimens.c.keys()).index("twin_id")] == twins[1].uid


def test_bridge_read_matches_orm_read():
    author = make_author()
    gadget = Gadget.objects.create(code="g1")
    twin = Specimen.objects.create(**specimen_values(author=author, gadget=gadget))
    Specimen.objects.create(**specimen_values(author=author, twin=twin, email="e@x.y", data=None, happened_at=None))
    rows = execute(sa.select(specimens))
    assert len(rows) == 2
    assert sorted(rows, key=lambda r: str(r[0])) == sorted(orm_rows(), key=lambda r: str(r[0]))


def test_aware_datetime_is_stored_in_utc_and_read_back_aware():
    author_id = execute(sa.insert(authors).values(name="tz", joined_at=AT).returning(authors.c.id))[0][0]
    assert Author.objects.get(pk=author_id).joined_at == AT
    (joined_at,) = execute(sa.select(authors.c.joined_at).where(authors.c.id == author_id))[0]
    assert joined_at == AT
    assert joined_at.utcoffset() == datetime.timedelta(0)
    if connection.vendor == "sqlite":
        # Stored as naive UTC, as the ORM stores it (Django's sqlite3 module parses the text back).
        assert raw("SELECT joined_at FROM testapp_author")[0][0] == datetime.datetime(2026, 10, 25, 0, 30, 15, 123456)
    # A filter on the column converts the bound value the same way.
    assert execute(sa.select(authors.c.id).where(authors.c.joined_at == AT)) == [(author_id,)]
    later = AT + datetime.timedelta(seconds=1)
    assert execute(sa.select(authors.c.id).where(authors.c.joined_at < later)) == [(author_id,)]


def test_text_choices_are_stored_by_value():
    author = make_author()
    insert_book(author, status=Book.Status.PUBLISHED)
    assert raw("SELECT status FROM testapp_book") == [("published",)]
    assert execute(sa.select(books.c.status)) == [("published",)]
    assert Book.objects.get().status == Book.Status.PUBLISHED
    assert execute(sa.select(books.c.id).where(books.c.status == Book.Status.PUBLISHED)) != []


def test_json_uuid_decimal_written_by_orm_read_by_bridge():
    author = make_author()
    uid = uuid.uuid4()
    Book.objects.create(uid=uid, author=author, title="t", price=Decimal("12.50"), meta={"k": [1, 2], "s": "한"})
    assert execute(sa.select(books.c.uid, books.c.price, books.c.meta, books.c.author_id)) == [
        (uid, Decimal("12.50"), {"k": [1, 2], "s": "한"}, author.pk)
    ]
    assert execute(sa.select(books.c.title).where(books.c.uid == uid)) == [("t",)]


# --- SQLAlchemy Result, opt-in -------------------------------------------------


def test_execute_result_gives_a_sqlalchemy_result():
    author = make_author("r")
    result = execute_result(sa.select(authors.c.id, authors.c.name, authors.c.joined_at.label("joined")))
    assert isinstance(result, sa.Result)
    row = result.one()
    assert row.name == "r"
    assert row.joined == AT
    assert row._mapping["id"] == author.pk
    assert tuple(row) == (author.pk, "r", AT)


def test_execute_result_mappings():
    make_author("m1")
    make_author("m2")
    result = execute_result(sa.select(authors.c.name, authors.c.joined_at).order_by(authors.c.name))
    assert result.mappings().all() == [{"name": "m1", "joined_at": AT}, {"name": "m2", "joined_at": AT}]


def test_execute_result_for_returning():
    result = execute_result(sa.insert(authors).values(name="n", joined_at=AT).returning(authors.c.id, authors.c.name))
    assert result.mappings().one() == {"id": Author.objects.get().pk, "name": "n"}


def test_execute_result_refuses_a_statement_without_rows():
    with pytest.raises(ValueError, match="no rows"):
        execute_result(sa.update(authors).values(name="x"))


# --- RETURNING needs a database that has it ------------------------------------


@pytest.fixture
def no_returning(monkeypatch):
    """Make the connection report no RETURNING; call it after the ORM writes (they rely on it)."""

    def disable():
        # SQLite has RETURNING from 3.35; Django 5.2 still runs on 3.31 and reports it here.
        monkeypatch.setattr(connection.features, "can_return_columns_from_insert", False)

    return disable


def returning_calls():
    insert = sa.insert(authors).values(name="n", joined_at=AT).returning(authors.c.id)
    update = sa.update(authors).values(name="u").returning(authors.c.id)
    delete = sa.delete(authors).returning(authors.c.id)
    return {
        "execute insert": lambda: execute(insert),
        "execute update": lambda: execute(update),
        "execute delete": lambda: execute(delete),
        "execute_result": lambda: execute_result(insert),
        "stream": lambda: stream(update),
        "bulk_insert": lambda: bulk_insert(authors, [{"name": "n", "joined_at": AT}], returning=["id"]),
        # #21: derived from a statement whose (empty) columns were read first.
        "execute derived insert": lambda: execute(
            read_columns(sa.insert(authors)).values(name="n", joined_at=AT).returning(authors.c.id)
        ),
        "execute derived update": lambda: execute(
            read_columns(sa.update(authors)).values(name="u").returning(authors.c.id)
        ),
        "execute derived delete": lambda: execute(read_columns(sa.delete(authors)).returning(authors.c.id)),
    }


@pytest.mark.parametrize("call", returning_calls().values(), ids=returning_calls().keys())
def test_returning_is_refused_before_running_where_the_database_lacks_it(no_returning, call):
    make_author("kept")
    no_returning()
    with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match="RETURNING"):
        call()
    assert queries.captured_queries == []
    assert list(Author.objects.values_list("name", flat=True)) == ["kept"]


def test_statements_without_returning_still_run_where_the_database_lacks_it(no_returning):
    no_returning()
    assert execute(sa.insert(authors).values(name="a", joined_at=AT)) == 1
    assert bulk_insert(authors, [{"name": "b", "joined_at": AT}]) == 1
    assert execute(sa.select(authors.c.name).order_by(authors.c.name)) == [("a",), ("b",)]
    assert list(stream(sa.select(authors.c.name).order_by(authors.c.name))) == [("a",), ("b",)]


def test_text_statements_are_not_checked_for_returning(no_returning):
    no_returning()
    # The bridge cannot see RETURNING inside text(); the database decides (here it has it).
    rows = execute(sa.text("INSERT INTO testapp_author (name, joined_at) VALUES ('t', :at) RETURNING name"), {"at": AT})
    assert rows == [("t",)]


# --- statements derived after their columns were read (#21) -------------------
# SQLAlchemy memoizes a DML statement's exported_columns without registering the
# memoization, so a statement derived with .returning() from one whose columns
# were read (as executing it does) keeps the stale collection.


def read_columns(stmt):
    list(stmt.exported_columns)
    return stmt


def book_values(author, uid):
    return {"uid": uid, "author_id": author.pk, "title": "t", "price": Decimal("1.00"), "meta": {}, "status": "draft"}


def test_returning_derived_after_reading_the_columns_is_converted():
    author = make_author()
    uid = uuid.uuid4()
    insert, update, delete = (read_columns(s) for s in (sa.insert(books), sa.update(authors), sa.delete(books)))
    values = book_values(author, uid) | {"meta": {"a": 1}}
    assert execute(insert.values(**values).returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]
    assert execute(update.values(name="u").returning(authors.c.joined_at)) == [(AT,)]
    assert execute(delete.returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]


def test_returning_derived_from_an_executed_statement_is_converted():
    author = make_author()
    insert = sa.insert(books)
    assert execute(insert, book_values(author, uuid.uuid4())) == 1
    uid = uuid.uuid4()
    values = book_values(author, uid) | {"meta": {"a": 1}}
    assert execute(insert.values(**values).returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]


def test_returning_added_to_after_reading_the_columns_converts_every_column():
    author = make_author()
    uid = uuid.uuid4()
    values = book_values(author, uid) | {"meta": {"a": 1}}
    base = read_columns(sa.insert(books).values(**values).returning(books.c.id))
    assert execute(base.returning(books.c.uid, books.c.meta)) == [(Book.objects.get().pk, uid, {"a": 1})]


def test_execute_result_and_stream_accept_returning_derived_after_reading_the_columns():
    make_author()
    update = read_columns(sa.update(authors))
    result = execute_result(update.values(name="r").returning(authors.c.name, authors.c.joined_at))
    assert result.mappings().one() == {"name": "r", "joined_at": AT}
    assert list(stream(update.values(name="s").returning(authors.c.joined_at))) == [(AT,)]


def test_insert_derived_after_reading_the_columns_still_returns_the_rowcount():
    base = read_columns(sa.insert(authors))
    assert execute(base.values(name="n", joined_at=AT)) == 1
    assert execute(base, [{"name": "a", "joined_at": AT}, {"name": "b", "joined_at": AT}]) == 2


def test_select_derived_after_reading_the_columns_is_converted():
    make_author("x")
    base = sa.select(authors.c.name)
    list(base.selected_columns)
    list(base.exported_columns)
    assert execute(base.add_columns(authors.c.joined_at)) == [("x", AT)]
    assert execute(base.with_only_columns(authors.c.joined_at)) == [(AT,)]
    subquery = base.add_columns(authors.c.joined_at).subquery()
    list(subquery.c)
    assert execute(sa.select(subquery.c.joined_at)) == [(AT,)]


def test_returning_a_whole_table_or_a_label_derived_after_reading_the_columns_is_converted():
    author = make_author("w")
    update = read_columns(sa.update(authors))
    assert execute(update.values(name="v").returning(authors)) == [(author.pk, "v", AT)]
    assert execute(update.values(name="l").returning(authors.c.joined_at.label("j"))) == [(AT,)]
