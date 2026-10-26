import asyncio
import datetime
import types
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
from django_sqlalchemy_bridge.execution import dialect_for, prepare
from tests.testapp.models import Author, Book, Gadget, OracleSpelledNameModelWithALongName, Specimen

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=SEOUL)

authors = table_for(Author)
books = table_for(Book)
specimens = table_for(Specimen)


def raw(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return list(cursor.fetchall())


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


@pytest.mark.usefixtures("needs_returning")
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
    length = sa.func.length(books.c.title)
    stmt = sa.select(sa.func.count(books.c.id), length.label("n")).group_by(length).order_by(length)
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
    # text() is the database's own SQL: MySQL reads a backslash in a string literal as an escape.
    backslash = "\\\\" if connection.vendor == "mysql" else "\\"
    like = f"SELECT title FROM testapp_book WHERE title LIKE 'a{backslash}%%' ESCAPE '{backslash}'"
    assert execute(sa.text(like)) == [("a%b",)]
    assert execute(sa.text("SELECT '100%'")) == [("100%",)]
    # Literals the compiler renders itself come back as given: one backslash as written under
    # PostgreSQL's standard_conforming_strings, escaped as two under MySQL's default sql_mode.
    assert execute(sa.select(sa.literal("a\\b%", literal_execute=True))) == [("a\\b%",)]
    assert execute(sa.text("SELECT '100%' WHERE 1 = :one"), {"one": 1}) == [("100%",)]


@pytest.mark.skipif(connection.vendor != "mysql", reason="MySQL sql_mode")
def test_literal_backslashes_follow_mysql_no_backslash_escapes(monkeypatch):
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@SESSION.sql_mode")
        (mode,) = cursor.fetchone()
        cursor.execute("SET SESSION sql_mode = CONCAT(@@SESSION.sql_mode, ',NO_BACKSLASH_ESCAPES')")
    # Django reads the mode once per connection; a connection opened in this mode reports it.
    monkeypatch.setattr(connection, "sql_mode", connection.sql_mode | {"NO_BACKSLASH_ESCAPES"})
    try:
        assert execute(sa.select(sa.literal("a\\b%", literal_execute=True))) == [("a\\b%",)]
        assert execute(sa.select(sa.literal_column("'a%'"))) == [("a%",)]
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION sql_mode = %s", [mode])


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
    assert "testapp_author" in ctx.captured_queries[0]["sql"].lower()  # Oracle: "TESTAPP_AUTHOR"
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
    assert "testapp_author" in seen[0][0].lower()
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
    qn = connection.ops.quote_name  # an unquoted uid is Oracle's UID pseudocolumn
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT * FROM {qn('dsb_specimen')} WHERE {qn('uid')} = %s",
            [column.get_db_prep_value(uid, connection, prepared=False)],
        )
        names = [c[0].lower() for c in cursor.description]
        # Oracle's driver hands NCLOB and BLOB columns over as LOB objects.
        values = [v.read() if hasattr(v, "read") else v for v in cursor.fetchone()]
        return {k: v for k, v in zip(names, values) if k not in ("uid", "twin_id")}


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


@pytest.mark.parametrize("form", ["literal", "bindparam"])
def test_typed_values_in_an_insert_select_list_are_written_as_the_orm_writes_them(form):
    # The bridge cannot tell which target column a SELECT-list value lands in (SQLAlchemy keeps the from_select()
    # names only in a private attribute), so values there convert only when given the target column's type.
    author = make_author()
    via_orm = specimen_values(author=author)
    Specimen.objects.create(**via_orm)
    values = specimen_columns(specimen_values(author=author, uid=uuid.uuid4()))
    names = [name for name in values if name != "writer"]
    if form == "literal":
        items = [sa.literal(values[name], specimens.c[name].type) for name in names]
        params = None
    else:
        items = [sa.bindparam(f"v_{name}", type_=specimens.c[name].type) for name in names]
        params = {f"v_{name}": values[name] for name in names}
    select = sa.select(*items, authors.c.id).where(authors.c.id == author.pk)
    assert execute(sa.insert(specimens).from_select([*names, "writer"], select), params) == 1

    assert stored(values["uid"]) == stored(via_orm["uid"])
    assert orm_row(values["uid"]) == orm_row(via_orm["uid"])
    assert Specimen.objects.filter(happened_at=AT, amount=via_orm["amount"], level=Specimen.Level.HIGH).count() == 2


def test_aware_datetime_is_stored_in_utc_and_read_back_aware():
    execute(sa.insert(authors).values(name="tz", joined_at=AT))
    (author_id,) = execute(sa.select(authors.c.id).where(authors.c.name == "tz"))[0]
    assert Author.objects.get(pk=author_id).joined_at == AT
    (joined_at,) = execute(sa.select(authors.c.joined_at).where(authors.c.id == author_id))[0]
    assert joined_at == AT
    assert joined_at.utcoffset() == datetime.timedelta(0)
    if connection.vendor in ("sqlite", "mysql"):
        # Stored as naive UTC, as the ORM stores it (no time zone in the column).
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


@pytest.mark.usefixtures("needs_returning")
def test_execute_result_for_returning():
    result = execute_result(sa.insert(authors).values(name="n", joined_at=AT).returning(authors.c.id, authors.c.name))
    assert result.mappings().one() == {"id": Author.objects.get().pk, "name": "n"}


def test_execute_result_refuses_a_statement_without_rows():
    with pytest.raises(ValueError, match="no rows"):
        execute_result(sa.update(authors).values(name="x"))


# --- result keys -------------------------------------------------------------

spelled = table_for(OracleSpelledNameModelWithALongName)
LONG_NAME = "a_column_name_longer_than_thirty_characters"


def description_names(sql, params=()):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return [column[0] for column in cursor.description]


def test_result_keys_are_the_names_the_statement_was_built_with():
    # On Oracle the database reports "MIXEDCASE", "A_COLUMN_NAME_LONGER_THAN_9E63"
    # and "COUNT_1"; the keys are the table's names, as on every other backend.
    OracleSpelledNameModelWithALongName.objects.create(cased="c", **{LONG_NAME: 7})
    result = execute_result(sa.select(spelled.c.MixedCase, spelled.c[LONG_NAME]))
    assert list(result.keys()) == ["MixedCase", LONG_NAME]
    assert result.mappings().one() == {"MixedCase": "c", LONG_NAME: 7}
    labelled = sa.select(spelled.c.MixedCase.label("Cased"), sa.func.count(), sa.func.max(spelled.c[LONG_NAME]))
    result = execute_result(labelled.group_by(spelled.c.MixedCase))
    assert list(result.keys()) == ["Cased", "count_1", "max_1"]
    sub = sa.select(spelled).subquery()
    assert list(execute_result(sa.select(sub)).keys()) == ["id", "MixedCase", LONG_NAME]


def oracle_sql(stmt):
    # No server needed: the dialect compiles without connecting.
    return str(stmt.compile(dialect=dialect_for(types.SimpleNamespace(vendor="oracle"))))


def test_oracle_spells_identifiers_as_django_does():
    # Django's quote_name() on Oracle: upper case, cut past 30 characters with a
    # hash. SQLAlchemy's own Oracle dialect would quote "MixedCase" and the long
    # name as they are, and "uid" (a reserved word), naming columns Django never made.
    stmt = sa.select(spelled.c.MixedCase, spelled.c[LONG_NAME], books.c.uid).join_from(spelled, books, sa.true())
    sql = oracle_sql(stmt)
    for name in ('"MIXEDCASE"', '"A_COLUMN_NAME_LONGER_THAN_9E63"', '"TESTAPP_BOOK"."UID"'):
        assert name in sql
    assert oracle_sql(sa.select(authors.c.name.label("Who"))).startswith('SELECT "TESTAPP_AUTHOR"."NAME" AS "WHO"')


def test_oracle_spells_a_quoted_name_upper_case_too():
    # Django makes no case-sensitive Oracle names (quote_name() upper-cases even
    # a quoted one), so quoted_name(..., quote=True) is spelled the same way.
    legacy = sa.table(sa.sql.quoted_name("Legacy", quote=True), sa.column(sa.sql.quoted_name("Col", quote=True)))
    assert oracle_sql(sa.select(legacy.c.Col)) == 'SELECT "LEGACY"."COL" \nFROM "LEGACY"'


@pytest.mark.skipif(connection.vendor == "oracle", reason="Oracle reports upper case names")
def test_result_keys_elsewhere_are_the_names_the_database_reports():
    author = make_author("k")
    stmt = sa.select(authors.c.id, authors.c.name.label("Who"), sa.func.count()).group_by(authors.c.id, authors.c.name)
    prepared = prepare(stmt, None, connection)
    keys = list(execute_result(stmt).keys())
    assert keys == description_names(prepared.sql, prepared.param_rows[0]) == ["id", "Who", "count_1"]
    # text(): PostgreSQL folds an unquoted alias to lower case, SQLite and MySQL keep it.
    quoted = "`Quoted`" if connection.vendor == "mysql" else '"Quoted"'
    sql = f"SELECT name AS Plain, name AS {quoted} FROM testapp_author WHERE id = :pk"
    keys = list(execute_result(sa.text(sql), {"pk": author.pk}).keys())
    assert keys == ["plain" if connection.vendor == "postgresql" else "Plain", "Quoted"]


@pytest.mark.skipif(connection.vendor != "oracle", reason="Oracle reports upper case names")
def test_oracle_text_result_keys_are_lower_case_unless_quoted_in_mixed_case():
    # As SQLAlchemy's Oracle dialect reports them: an all upper case name
    # (what Oracle makes of an unquoted one) in lower case, others as written.
    author = make_author("k")
    sql = 'SELECT name AS Plain, name AS "Quoted", name AS "UPPER", name AS "lower" FROM testapp_author WHERE id = :pk'
    assert description_names("SELECT name AS Plain FROM testapp_author") == ["PLAIN"]
    keys = list(execute_result(sa.text(sql), {"pk": author.pk}).keys())
    assert keys == ["plain", "Quoted", "upper", "lower"]


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


@pytest.mark.skipif(connection.vendor != "mysql", reason="MySQL has no RETURNING")
@pytest.mark.parametrize("call", ["execute insert", "execute update", "execute delete", "stream", "bulk_insert"])
def test_mysql_refuses_returning_before_running(call):
    if connection.mysql_is_mariadb:
        pytest.skip("MariaDB has INSERT and DELETE ... RETURNING")
    make_author("kept")
    with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match="RETURNING"):
        returning_calls()[call]()
    assert queries.captured_queries == []
    assert list(Author.objects.values_list("name", flat=True)) == ["kept"]


@pytest.mark.skipif(connection.vendor != "oracle", reason="Oracle RETURNING ... INTO")
@pytest.mark.parametrize("call", returning_calls().values(), ids=returning_calls().keys())
def test_oracle_refuses_returning_before_running(call):
    # Oracle returns the rows into output binds (RETURNING ... INTO), which Django's
    # cursor does not hand back: run anyway, the statement gave its rowcount, no error.
    make_author("kept")
    with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match="RETURNING"):
        call()
    assert queries.captured_queries == []
    assert list(Author.objects.values_list("name", flat=True)) == ["kept"]


@pytest.mark.skipif(connection.vendor != "oracle", reason="Oracle stores an empty string as NULL")
def test_oracle_compares_an_empty_string_as_null():
    # SQL semantics, not the ORM's: filter(name="") becomes IS NULL there, "= ''" matches nothing.
    make_author("")
    assert Author.objects.filter(name="").count() == 1
    assert execute(sa.select(sa.func.count()).where(authors.c.name == "")) == [(0,)]
    assert execute(sa.select(sa.func.count()).where(authors.c.name.is_(None))) == [(1,)]
    assert execute(sa.select(authors.c.name)) == [("",)]  # read back as the ORM reads it


@pytest.mark.skipif(connection.vendor != "oracle", reason="Oracle NCLOB")
def test_oracle_refuses_to_compare_a_text_field_with_equals():
    # TextField is NCLOB on Oracle, which "=" cannot compare; the database says so.
    from django.db import DatabaseError

    with pytest.raises(DatabaseError, match="ORA-22848"):
        execute(sa.select(specimens.c.uid).where(specimens.c.body == "x"))


def mariadb_connection():
    """A stand-in for a MariaDB connection: Django reports vendor "mysql" and INSERT ... RETURNING."""
    features = types.SimpleNamespace(can_return_columns_from_insert=True)
    return types.SimpleNamespace(
        vendor="mysql", alias="maria", display_name="MariaDB", features=features, mysql_is_mariadb=True, sql_mode=set()
    )


def test_mariadb_runs_insert_and_delete_returning_but_refuses_update_returning():
    # No server needed: prepare() compiles and checks, it does not run. A plain
    # table, so no Django field asks the stand-in for its converters.
    maria = mariadb_connection()
    names = sa.table("names", sa.column("id", sa.Integer), sa.column("name", sa.String))
    insert = prepare(sa.insert(names).values(name="n").returning(names.c.id), None, maria)
    assert insert.sql.endswith("RETURNING names.id")
    delete = prepare(sa.delete(names).returning(names.c.name), None, maria)
    assert delete.sql.endswith("RETURNING names.name")
    with pytest.raises(NotSupportedError, match=r"'maria' \(MariaDB\) does not support UPDATE \.\.\. RETURNING"):
        prepare(sa.update(names).values(name="u").returning(names.c.id), None, maria)
    # Without RETURNING an UPDATE compiles as usual.
    assert prepare(sa.update(names).values(name="u"), None, maria).rowcount_only


def test_statements_without_returning_still_run_where_the_database_lacks_it(no_returning):
    no_returning()
    assert execute(sa.insert(authors).values(name="a", joined_at=AT)) == 1
    assert bulk_insert(authors, [{"name": "b", "joined_at": AT}]) == 1
    assert execute(sa.select(authors.c.name).order_by(authors.c.name)) == [("a",), ("b",)]
    assert list(stream(sa.select(authors.c.name).order_by(authors.c.name))) == [("a",), ("b",)]


def test_text_statements_are_not_checked_for_returning(needs_returning, no_returning):
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


@pytest.mark.usefixtures("needs_update_returning")
def test_returning_derived_after_reading_the_columns_is_converted():
    author = make_author()
    uid = uuid.uuid4()
    insert, update, delete = (read_columns(s) for s in (sa.insert(books), sa.update(authors), sa.delete(books)))
    values = book_values(author, uid) | {"meta": {"a": 1}}
    assert execute(insert.values(**values).returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]
    assert execute(update.values(name="u").returning(authors.c.joined_at)) == [(AT,)]
    assert execute(delete.returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]


@pytest.mark.usefixtures("needs_returning")
def test_returning_derived_from_an_executed_statement_is_converted():
    author = make_author()
    insert = sa.insert(books)
    assert execute(insert, book_values(author, uuid.uuid4())) == 1
    uid = uuid.uuid4()
    values = book_values(author, uid) | {"meta": {"a": 1}}
    assert execute(insert.values(**values).returning(books.c.uid, books.c.meta)) == [(uid, {"a": 1})]


@pytest.mark.usefixtures("needs_returning")
def test_returning_added_to_after_reading_the_columns_converts_every_column():
    author = make_author()
    uid = uuid.uuid4()
    values = book_values(author, uid) | {"meta": {"a": 1}}
    base = read_columns(sa.insert(books).values(**values).returning(books.c.id))
    assert execute(base.returning(books.c.uid, books.c.meta)) == [(Book.objects.get().pk, uid, {"a": 1})]


@pytest.mark.usefixtures("needs_update_returning")
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


@pytest.mark.usefixtures("needs_update_returning")
def test_returning_a_whole_table_or_a_label_derived_after_reading_the_columns_is_converted():
    author = make_author("w")
    update = read_columns(sa.update(authors))
    assert execute(update.values(name="v").returning(authors)) == [(author.pk, "v", AT)]
    assert execute(update.values(name="l").returning(authors.c.joined_at.label("j"))) == [(AT,)]
