"""bulk_insert(method="copy"): PostgreSQL COPY FROM STDIN.

The rest of its behaviour is tested in tests/test_bulk.py, which runs its INSERT tests with each ``method``.
"""

import datetime
import types
import uuid
from decimal import Decimal

import pytest
from django.db import IntegrityError, NotSupportedError, connection, reset_queries, transaction
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import bulk_insert
from tests.test_bulk import AT, HAS_COPY, authors, book_row, books, columns_of, make_author, specimen_values, specimens
from tests.testapp.models import Author, Book, Counter, Specimen, Stamp

pytestmark = pytest.mark.django_db

needs_copy = pytest.mark.skipif(not HAS_COPY, reason="method='copy' needs PostgreSQL with psycopg 3")

ROW = {"name": "a", "joined_at": AT}

# What COPY's text format escapes: the column and row delimiters, the escape character and NULL's spelling.
TRICKY = [
    "tab\there",
    "line\nbreak",
    "carriage\rreturn",
    "back\\slash",
    "trailing\\",
    "\\N",
    "\\.",
    "\\.\nafter",
    "",
    "100% %s %% %(x)s",
    "'quote\" \\x41 \\101",
    "본문\t\\N\n끝",
]


@needs_copy
def test_copy_escapes_text_as_the_orm_stores_it():
    author = make_author()
    rows = [
        specimen_values(n, author=author, body=text, email=text, label=f"{n:02d}", data={"s": text, "l": [text]})
        for n, text in enumerate(TRICKY)
    ]
    rows.append(specimen_values(99, author=author, label="99", email=None, data=None))
    assert bulk_insert(specimens, [columns_of(values) for values in rows], method="copy") == len(rows)
    got = {s.label: s for s in Specimen.objects.all()}
    for values in rows:
        specimen = got[values["label"]]
        assert (specimen.body, specimen.email, specimen.data) == (values["body"], values["email"], values["data"])
    # NULL stays NULL, and the string "\N" stays a string.
    assert Specimen.objects.filter(email__isnull=True).count() == 1
    assert Specimen.objects.get(email="\\N").body == "\\N"


@needs_copy
def test_copy_writes_bytes_with_the_escape_characters():
    author = make_author()
    blobs = [b"", b"\\", b"\t\n\r", b"\\N", bytes(range(256))]
    rows = [specimen_values(n, author=author, label=f"{n}", blob=blob) for n, blob in enumerate(blobs)]
    bulk_insert(specimens, [columns_of(values) for values in rows], method="copy")
    assert {s.label: bytes(s.blob) for s in Specimen.objects.all()} == {str(n): b for n, b in enumerate(blobs)}


@needs_copy
def test_copy_returns_the_rowcount():
    rows = [ROW | {"name": f"n{i}"} for i in range(1234)]
    assert bulk_insert(authors, rows, method="copy") == 1234
    assert bulk_insert(authors, rows, method="copy", batch_size=500) == 1234
    assert Author.objects.count() == 2468


@needs_copy
def test_copy_runs_one_copy_per_batch():
    rows = [ROW | {"name": f"n{i}"} for i in range(10)]
    with CaptureQueriesContext(connection) as ctx:
        assert bulk_insert(authors, rows, method="copy", batch_size=4) == 10
    sqls = [q["sql"] for q in ctx.captured_queries]
    assert sqls == ['COPY "testapp_author" ("name", "joined_at") FROM STDIN'] * 3  # 4 + 4 + 2
    assert all(float(q["time"]) >= 0 for q in ctx.captured_queries)


@needs_copy
def test_copy_is_not_logged_without_debug():
    reset_queries()
    assert not connection.queries_logged
    bulk_insert(authors, [ROW], method="copy")
    assert connection.queries == []


@needs_copy
def test_copy_is_invisible_to_execute_wrapper():
    seen = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(wrapper):
        bulk_insert(authors, [ROW], method="copy")
    assert seen == []
    assert Author.objects.count() == 1


@needs_copy
def test_copy_takes_the_model_and_leaves_a_db_default_to_the_database():
    assert bulk_insert(Stamp, [{"name": "n", "note": None, "at": None}], method="copy") == 1
    assert Stamp.objects.values_list("name", "note", "kind").get() == ("n", None, "db")


@needs_copy
def test_copy_of_a_duplicate_key_raises_integrity_error_and_rolls_back():
    author = make_author()
    taken = uuid.uuid4()
    Book.objects.create(**{k: v for k, v in book_row(author, taken).items() if k != "author_id"}, author=author)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            bulk_insert(books, [book_row(author), book_row(author, taken)], method="copy")
    assert Book.objects.count() == 1
    # The connection is usable afterwards.
    assert bulk_insert(books, [book_row(author)], method="copy") == 1


@needs_copy
def test_copy_of_a_bad_value_raises_django_data_error():
    from django.db import DataError

    with pytest.raises(DataError):
        with transaction.atomic():
            bulk_insert(authors, [ROW | {"name": "x" * 101}], method="copy")
    rows = [book_row(make_author()) | {"price": Decimal("123456789.99")}]  # max_digits=10
    with pytest.raises(DataError):
        with transaction.atomic():
            bulk_insert(books, rows, method="copy")


@needs_copy
def test_copy_converts_values_as_the_orm_writes_them():
    # A naive datetime is made aware in the current time zone, as the ORM does (with a warning).
    naive = datetime.datetime(2026, 10, 25, 9, 30)
    with pytest.warns(RuntimeWarning, match="naive datetime"):
        bulk_insert(authors, [ROW | {"joined_at": naive}], method="copy")
    with pytest.warns(RuntimeWarning, match="naive datetime"):
        Author.objects.create(name="orm", joined_at=naive)
    assert len(set(Author.objects.values_list("joined_at", flat=True))) == 1


# --- refused before anything runs --------------------------------------------


def test_copy_refuses_returning():
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(ValueError, match="returning"):
            bulk_insert(authors, [ROW], method="copy", returning=["id"])
        with pytest.raises(ValueError, match="returning"):
            bulk_insert(authors, [], method="copy", returning=["id"])
    assert ctx.captured_queries == []


@pytest.mark.parametrize("bad", ["COPY", "unnest", None, ""])
def test_unknown_method_is_refused(bad):
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(ValueError, match="method"):
            bulk_insert(authors, [ROW], method=bad)
    assert ctx.captured_queries == []


@pytest.mark.skipif(connection.vendor == "postgresql", reason="COPY runs on PostgreSQL")
def test_copy_is_not_supported_elsewhere():
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(NotSupportedError, match="PostgreSQL"):
            bulk_insert(authors, [ROW], method="copy")
        with pytest.raises(NotSupportedError, match="PostgreSQL"):
            bulk_insert(authors, [], method="copy")
    assert ctx.captured_queries == []
    assert Author.objects.count() == 0


@pytest.mark.skipif(connection.vendor != "postgresql", reason="psycopg2 is a PostgreSQL driver")
def test_copy_is_not_supported_with_psycopg2(monkeypatch):
    # psycopg2's module has no Copy (its cursors have copy_expert() instead).
    monkeypatch.setattr(connection, "Database", types.SimpleNamespace(__name__="psycopg2"))
    with pytest.raises(NotSupportedError, match="psycopg 3"):
        bulk_insert(authors, [ROW], method="copy")
    monkeypatch.undo()
    assert Author.objects.count() == 0


@needs_copy
def test_copy_of_no_rows_runs_nothing():
    with CaptureQueriesContext(connection) as ctx:
        assert bulk_insert(authors, [], method="copy") == 0
        assert bulk_insert(authors, [], method="copy", batch_size=3) == 0
    assert ctx.captured_queries == []


@needs_copy
def test_copy_refuses_rows_with_no_column():
    # COPY names the columns it reads; a row with none has nothing to copy.
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(ValueError, match="no column"):
            bulk_insert(Counter, [{}, {}], method="copy")
    assert ctx.captured_queries == []
    assert Counter.objects.count() == 0
