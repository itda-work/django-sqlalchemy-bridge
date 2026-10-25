import datetime
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction
from django.db.models.signals import post_save, pre_save
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import bulk_insert, bulk_update, execute, table_for
from tests.testapp.models import Author, Book, Gadget, Specimen

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
AT = datetime.datetime(2026, 10, 25, 9, 30, 15, 123456, tzinfo=SEOUL)

authors = table_for(Author)
books = table_for(Book)
specimens = table_for(Specimen)

IS_PG = connection.vendor == "postgresql"


def make_author(name="a"):
    return Author.objects.create(name=name, joined_at=AT)


def specimen_values(n, **overrides):
    """Model-field values for one Specimen, varied by ``n``."""
    return {
        "uid": uuid.uuid4(),
        "label": f"label {n}",
        "body": "" if n % 2 else "본문 % 100%",
        "slug": f"s-{n}",
        "email": None if n % 2 else f"{n}@x.y",
        "small": -n,
        "positive": n,
        "big": 2**40 + n,
        "flag": bool(n % 2),
        "maybe": [None, True, False][n % 3],
        "ratio": n / 4,
        "amount": Decimal(n) + Decimal("0.125"),
        "day": datetime.date(2026, 10, 25) + datetime.timedelta(days=n),
        "at": datetime.time(23, 59, n % 60, 500),
        "happened_at": None if n % 3 == 0 else AT + datetime.timedelta(hours=n),
        "took": datetime.timedelta(days=n, microseconds=7),
        "blob": bytes([0, 255, n % 256]),
        "data": [None, {"k": [1, "두", None], "n": n + 0.5}, [], [{"deep": {"x": None}}], "str", 7][n % 6],
        "level": Specimen.Level.HIGH if n % 2 else Specimen.Level.LOW,
        "gadget": None,
        "twin": None,
    } | overrides


def columns_of(model_values):
    """The same values keyed by column name, as the bridge takes them."""
    fields = {f.name: f for f in Specimen._meta.concrete_fields}
    out = {}
    for name, value in model_values.items():
        field = fields[name]
        if field.is_relation and value is not None:
            value = getattr(value, field.target_field.attname)
        out[field.column] = value
    return out


def stored(table, order_by, exclude=()):
    """Rows as the database holds them (no field converters), ordered, minus ``exclude`` columns."""
    qn = connection.ops.quote_name
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM {qn(table)} ORDER BY {qn(order_by)}")
        names = [c[0] for c in cursor.description]
        return [{k: v for k, v in zip(names, row) if k not in exclude} for row in cursor.fetchall()]


def specimen_fixtures(count=6):
    author = make_author()
    gadget = Gadget.objects.create(code="g1")
    rows = []
    for n in range(count):
        rows.append(specimen_values(n, author=author, gadget=gadget if n % 2 else None))
    return rows


# --- results match the ORM ---------------------------------------------------


def test_bulk_insert_matches_bulk_create():
    rows = specimen_fixtures()
    Specimen.objects.bulk_create([Specimen(**values) for values in rows])
    via_orm = stored("dsb_specimen", "label_text", exclude=("uid",))
    Specimen.objects.all().delete()

    assert bulk_insert(specimens, [columns_of(values) for values in rows]) == len(rows)
    assert stored("dsb_specimen", "label_text", exclude=("uid",)) == via_orm
    # The ORM reads the rows back as written.
    by_uid = {s.uid: s for s in Specimen.objects.all()}
    for values in rows:
        specimen = by_uid[values["uid"]]
        assert specimen.data == values["data"]
        assert specimen.happened_at == values["happened_at"]
        assert specimen.amount == values["amount"]


def test_bulk_insert_books_match_bulk_create():
    author = make_author()
    statuses = [Book.Status.PUBLISHED, Book.Status.DRAFT, "published"]
    values = [
        {"uid": uuid.uuid4(), "title": f"t{i}", "price": Decimal(f"{i}.5"), "status": s, "meta": {"i": [i, None]}}
        for i, s in enumerate(statuses)
    ]
    Book.objects.bulk_create([Book(author=author, **v) for v in values])
    via_orm = stored("testapp_book", "title", exclude=("id", "uid"))
    Book.objects.all().delete()

    assert bulk_insert(books, [v | {"author_id": author.pk} for v in values]) == 3
    assert stored("testapp_book", "title", exclude=("id", "uid")) == via_orm
    assert [r["status"] for r in via_orm] == ["published", "draft", "published"]


def test_bulk_update_matches_orm_bulk_update():
    rows = specimen_fixtures()
    other_author = make_author("other")
    gadget = Gadget.objects.get()
    # Two identical sets of rows: the ORM updates one, the bridge the other.
    first = Specimen.objects.bulk_create([Specimen(**values) for values in rows])
    second = Specimen.objects.bulk_create(
        [Specimen(**(values | {"uid": uuid.uuid4(), "label": values["label"] + "'"})) for values in rows]
    )
    changes = [
        {
            "data": [{"new": n}, None, [1, [2]], {"x": None}, "s", None][n],
            "happened_at": None if n % 2 else AT - datetime.timedelta(days=n),
            "amount": Decimal("9.999") * n,
            "level": Specimen.Level.LOW if n % 2 else Specimen.Level.HIGH,
            "author": other_author,
            "gadget": None if n % 2 else gadget,
            "email": f"new{n}@x.y" if n % 3 else None,
        }
        for n in range(len(rows))
    ]
    for specimen, change in zip(first, changes):
        for name, value in change.items():
            setattr(specimen, name, value)
    Specimen.objects.bulk_update(first, list(changes[0]))

    bridge_rows = [{"uid": s.uid} | columns_of(change) for s, change in zip(second, changes)]
    assert bulk_update(specimens, bridge_rows) == len(rows)

    def strip(row):
        return {k: v for k, v in row.items() if k != "uid"} | {"label_text": row["label_text"].rstrip("'")}

    stored_rows = stored("dsb_specimen", "label_text")
    assert [strip(r) for r in stored_rows[1::2]] == [strip(r) for r in stored_rows[0::2]]


def test_bulk_update_text_choices_and_decimal():
    author = make_author()
    books_ = Book.objects.bulk_create(
        [Book(uid=uuid.uuid4(), author=author, title=f"t{i}", price=Decimal("1.00")) for i in range(3)]
    )
    rows = [{"id": b.pk, "status": Book.Status.PUBLISHED, "price": Decimal("2.50")} for b in books_[:2]]
    assert bulk_update(books, rows) == 2
    assert list(Book.objects.order_by("title").values_list("status", "price")) == [
        ("published", Decimal("2.50")),
        ("published", Decimal("2.50")),
        ("draft", Decimal("1.00")),
    ]
    assert stored("testapp_book", "title")[0]["status"] == "published"


def test_aware_datetimes_are_stored_in_utc():
    rows = [{"name": "tz", "joined_at": AT}]
    bulk_insert(authors, rows)
    author = Author.objects.get()
    assert author.joined_at == AT
    if connection.vendor == "sqlite":
        # Naive UTC, as the ORM stores it: no 9-hour shift.
        assert stored("testapp_author", "id")[0]["joined_at"] == datetime.datetime(2026, 10, 25, 0, 30, 15, 123456)
    later = AT + datetime.timedelta(hours=1)
    assert bulk_update(authors, [{"id": author.pk, "joined_at": later}]) == 1
    author.refresh_from_db()
    assert author.joined_at == later


def test_null_only_columns():
    author = make_author()
    rows = [columns_of(specimen_values(n, author=author, data=None, email=None, maybe=None)) for n in range(3)]
    bulk_insert(specimens, rows)
    assert list(Specimen.objects.values_list("data", "email", "maybe")) == [(None, None, None)] * 3
    updates = [{"uid": r["uid"], "happened_at": None, "gadget_id": None} for r in rows]
    assert bulk_update(specimens, updates) == 3
    assert set(Specimen.objects.values_list("happened_at", flat=True)) == {None}


# --- one statement on PostgreSQL ---------------------------------------------


def test_bulk_insert_runs_one_query():
    rows = [{"name": f"n{i}", "joined_at": AT} for i in range(50)]
    with CaptureQueriesContext(connection) as ctx:
        assert bulk_insert(authors, rows) == 50
    assert len(ctx.captured_queries) == 1
    if IS_PG:
        assert "unnest" in ctx.captured_queries[0]["sql"].lower()


def test_bulk_update_runs_one_query():
    rows = [{"name": f"n{i}", "joined_at": AT} for i in range(50)]
    ids = [r[0] for r in bulk_insert(authors, rows, returning=["id"])]
    with CaptureQueriesContext(connection) as ctx:
        assert bulk_update(authors, [{"id": pk, "name": "x"} for pk in ids]) == 50
    assert len(ctx.captured_queries) == 1
    if IS_PG:
        assert "unnest" in ctx.captured_queries[0]["sql"].lower()
    assert set(Author.objects.values_list("name", flat=True)) == {"x"}


def test_empty_rows_run_nothing():
    with CaptureQueriesContext(connection) as ctx:
        assert bulk_insert(authors, []) == 0
        assert bulk_insert(authors, [], returning=["id"]) == []
        assert bulk_update(authors, []) == 0
    assert ctx.captured_queries == []


# --- RETURNING ---------------------------------------------------------------


def test_returning_gives_rows_in_input_order_converted_by_the_field():
    author = make_author()
    names = [f"b{i:04d}" for i in range(300)][::-1]
    rows = [
        {"uid": uuid.uuid4(), "author_id": author.pk, "title": t, "price": Decimal("1.5"), "status": "draft", "meta": i}
        for i, t in enumerate(names)
    ]
    out = bulk_insert(books, rows, returning=[books.c.id, "title", books.c.uid, books.c.price, books.c.meta])
    assert [r[1:] for r in out] == [(r["title"], r["uid"], Decimal("1.50"), r["meta"]) for r in rows]
    ids = [r[0] for r in out]
    assert ids == sorted(ids)
    assert dict(Book.objects.values_list("id", "title")) == {r[0]: r[1] for r in out}


def test_returning_keeps_the_order_across_batches(monkeypatch):
    # Force small batches on backends that split the multi-row INSERT.
    monkeypatch.setattr(type(connection.features), "max_query_params", 7, raising=False)
    rows = [{"name": f"n{i}", "joined_at": AT} for i in range(10)][::-1]
    with CaptureQueriesContext(connection) as ctx:
        out = bulk_insert(authors, rows, returning=["name", "id", "joined_at"])
    assert [r[0] for r in out] == [r["name"] for r in rows]
    assert [r[2] for r in out] == [AT] * 10
    assert len(ctx.captured_queries) == (1 if IS_PG else 4)  # 7 params // 2 columns = 3 rows a batch


# --- transactions and signals ------------------------------------------------


def test_atomic_rollback_discards_bulk_writes():
    author = make_author("before")
    with pytest.raises(ZeroDivisionError):
        with transaction.atomic():
            bulk_insert(authors, [{"name": "new", "joined_at": AT}])
            bulk_update(authors, [{"id": author.pk, "name": "after"}])
            assert sorted(Author.objects.values_list("name", flat=True)) == ["after", "new"]
            1 / 0
    assert list(Author.objects.values_list("name", flat=True)) == ["before"]


def test_bulk_writes_send_no_model_signals():
    received = []

    def receiver(sender, **kwargs):
        received.append(sender)

    pre_save.connect(receiver, sender=Author)
    post_save.connect(receiver, sender=Author)
    try:
        (pk,) = bulk_insert(authors, [{"name": "n", "joined_at": AT}], returning=["id"])[0]
        bulk_update(authors, [{"id": pk, "name": "m"}])
    finally:
        pre_save.disconnect(receiver, sender=Author)
        post_save.disconnect(receiver, sender=Author)
    assert received == []


# --- keys --------------------------------------------------------------------


def test_update_by_another_unique_key_and_model_argument():
    author = make_author()
    uids = [uuid.uuid4(), uuid.uuid4()]
    Book.objects.bulk_create([Book(uid=u, author=author, title="t", price=1) for u in uids])
    assert bulk_update(Book, [{"uid": uids[1], "title": "second"}], key="uid") == 1
    assert dict(Book.objects.values_list("uid", "title")) == {uids[0]: "t", uids[1]: "second"}


def test_update_by_a_composite_key():
    a, b = make_author("a"), make_author("b")
    Book.objects.bulk_create([Book(uid=uuid.uuid4(), author=x, title=t, price=1) for x in (a, b) for t in ("p", "q")])
    rows = [{"author_id": b.pk, "title": "p", "price": Decimal("7")}, {"author_id": a.pk, "title": "q", "price": 8}]
    assert bulk_update(books, rows, key=("author_id", "title")) == 2
    assert sorted(Book.objects.values_list("author__name", "title", "price")) == [
        ("a", "p", Decimal("1.00")),
        ("a", "q", Decimal("8.00")),
        ("b", "p", Decimal("7.00")),
        ("b", "q", Decimal("1.00")),
    ]


def test_update_of_missing_keys_changes_nothing_and_counts_only_matches():
    author = make_author("keep")
    rows = [{"id": author.pk + 1000, "name": "ghost"}, {"id": author.pk, "name": "real"}]
    assert bulk_update(authors, rows) == 1
    assert list(Author.objects.values_list("name", flat=True)) == ["real"]


def test_update_refuses_duplicate_keys():
    author = make_author()
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(ValueError, match="duplicate key"):
            bulk_update(authors, [{"id": author.pk, "name": "x"}, {"id": author.pk, "name": "y"}])
    assert ctx.captured_queries == []


def test_update_refuses_a_row_without_its_key():
    with pytest.raises(ValueError, match="row 1 .*'id'"):
        bulk_update(authors, [{"id": 1, "name": "x"}, {"name": "y"}])


def test_update_columns_pick_what_to_set():
    author = make_author("old")
    rows = [{"id": author.pk, "name": "new", "joined_at": AT + datetime.timedelta(days=1)}]
    assert bulk_update(authors, rows, columns=["name"]) == 1
    author.refresh_from_db()
    assert (author.name, author.joined_at) == ("new", AT)


def test_update_refuses_bad_columns():
    with pytest.raises(ValueError, match="nothing to update"):
        bulk_update(authors, [{"id": 1}])
    with pytest.raises(ValueError, match="key column"):
        bulk_update(authors, [{"id": 1, "name": "x"}], columns=["id", "name"])
    with pytest.raises(ValueError, match="no column 'nope'"):
        bulk_update(authors, [{"id": 1, "nope": "x"}])
    with pytest.raises(ValueError, match="no column 'nope'"):
        bulk_update(authors, [{"id": 1, "name": "x"}], key="nope")
    with pytest.raises(ValueError, match="row 1 .*'joined_at'"):
        bulk_update(authors, [{"id": 1, "name": "x"}, {"id": 2, "name": "y", "joined_at": AT}])
    with pytest.raises(ValueError, match="row 0 .*'joined_at'"):
        bulk_update(authors, [{"id": 1, "name": "x"}], columns=["name", "joined_at"])


# --- missing columns on INSERT -----------------------------------------------


def test_insert_refuses_rows_that_disagree_on_columns():
    with pytest.raises(ValueError, match="row 1 .*'joined_at'"):
        bulk_insert(authors, [{"name": "a", "joined_at": AT}, {"name": "b"}])
    with pytest.raises(ValueError, match="row 1 .*'id'"):
        bulk_insert(authors, [{"name": "a", "joined_at": AT}, {"name": "b", "joined_at": AT, "id": 5}])


def test_insert_refuses_to_leave_out_a_column_django_would_fill():
    # Book.status and Book.meta have Django defaults the bridge does not apply.
    author = make_author()
    row = {"uid": uuid.uuid4(), "author_id": author.pk, "title": "t", "price": 1}
    with pytest.raises(ValueError, match="'meta', 'status'"):
        bulk_insert(books, [row])
    # A nullable column must be given too (as None), so a forgotten one is not silently NULL.
    with pytest.raises(ValueError, match="'email'"):
        bulk_insert(
            specimens, [{k: v for k, v in columns_of(specimen_values(0, author=author)).items() if k != "email"}]
        )
    with pytest.raises(ValueError, match="no column 'nope'"):
        bulk_insert(authors, [{"name": "a", "joined_at": AT, "nope": 1}])
    assert Book.objects.count() == 0


def test_insert_may_give_the_auto_primary_key():
    assert bulk_insert(authors, [{"id": 77, "name": "a", "joined_at": AT}]) == 1
    assert Author.objects.get().pk == 77


def test_insert_refuses_unknown_returning_columns():
    with pytest.raises(ValueError, match="no column 'nope'"):
        bulk_insert(authors, [{"name": "a", "joined_at": AT}], returning=["nope"])
    with pytest.raises(ValueError, match="not a column of"):
        bulk_insert(authors, [{"name": "a", "joined_at": AT}], returning=[books.c.id])


def test_rejects_a_table_not_built_by_table_for():
    import sqlalchemy as sa

    plain = sa.Table("testapp_author", sa.MetaData(), sa.Column("id", sa.Integer, primary_key=True))
    with pytest.raises(TypeError, match="table_for"):
        bulk_insert(plain, [{"id": 1}])


def test_bridge_reads_what_bulk_insert_wrote():
    author = make_author()
    values = columns_of(specimen_values(1, author=author))
    bulk_insert(specimens, [values])
    import sqlalchemy as sa

    (row,) = execute(sa.select(specimens.c.data, specimens.c.amount, specimens.c.happened_at))
    assert row == (values["data"], values["amount"], values["happened_at"])


@pytest.mark.skipif(not IS_PG, reason="SQLite does not enforce varchar lengths")
def test_too_long_values_fail_instead_of_being_cut():
    # The arrays are cast to varchar[] (no length), so the column assignment checks the length,
    # as an ORM write does, instead of a varchar(100)[] cast truncating silently.
    from django.db import DataError

    author = make_author()
    with pytest.raises(DataError):
        with transaction.atomic():
            bulk_insert(authors, [{"name": "x" * 101, "joined_at": AT}])
    with pytest.raises(DataError):
        with transaction.atomic():
            bulk_update(authors, [{"id": author.pk, "name": "y" * 101}])
    assert list(Author.objects.values_list("name", flat=True)) == ["a"]
