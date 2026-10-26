"""copy_to(): PostgreSQL COPY (SELECT ...) TO STDOUT, the server's own CSV or text bytes."""

import csv
import datetime
import io
import json
import types
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import NotSupportedError, ProgrammingError, connection, reset_queries, transaction
from django.db.transaction import TransactionManagementError
from django.http import StreamingHttpResponse
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import copy_to, execute, table_for
from tests.test_bulk import AT, make_author, specimen_values
from tests.testapp.models import Author, Specimen

pytestmark = pytest.mark.django_db

HAS_COPY = connection.vendor == "postgresql" and hasattr(connection.Database, "Copy")
needs_copy = pytest.mark.skipif(not HAS_COPY, reason="copy_to() needs PostgreSQL with psycopg 3")

authors = table_for(Author)
specimens = table_for(Specimen)

# Text the CSV and text formats each have to quote or escape.
TRICKY = ["comma, here", 'quote " here', "line\nbreak", "tab\there", "back\\slash", "\\N", "", "100% %s %%"]


def export(*args, **kwargs):
    return b"".join(copy_to(*args, **kwargs))


def csv_rows(data):
    return list(csv.reader(io.StringIO(data.decode(), newline="")))


def session_time(value):
    """An aware datetime as PostgreSQL writes a timestamptz in the session's time zone (Django sets it)."""
    local = value.astimezone(ZoneInfo(connection.timezone_name))
    text = local.strftime("%Y-%m-%d %H:%M:%S")
    if local.microsecond:
        text += f".{local.microsecond:06d}".rstrip("0")
    minutes = int(local.utcoffset().total_seconds()) // 60
    sign, minutes = ("+" if minutes >= 0 else "-"), abs(minutes)
    return f"{text}{sign}{minutes // 60:02d}" + (f":{minutes % 60:02d}" if minutes % 60 else "")


def server_text(value):
    """What COPY writes for a value execute() returned (bar NULL, which each format spells its way)."""
    if isinstance(value, bool):
        return "t" if value else "f"
    if isinstance(value, datetime.datetime):
        return session_time(value)
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    return str(value)


def make_specimens():
    author = make_author()
    rows = [specimen_values(n, author=author, label=f"{n:02d}", body=text) for n, text in enumerate(TRICKY)]
    for values in rows:
        Specimen.objects.create(**values)
    return rows


SCALARS = ["label_text", "body", "uid", "amount", "flag", "maybe", "happened_at", "blob", "email"]


@needs_copy
def test_csv_values_are_the_servers_text_of_what_execute_reads():
    make_specimens()
    stmt = sa.select(*[specimens.c[name] for name in SCALARS]).order_by(specimens.c.label_text)
    got = csv_rows(export(stmt))
    assert got[0] == SCALARS
    rows = execute(stmt)
    assert len(got) == len(rows) + 1
    for line, row in zip(got[1:], rows):
        # The csv module reads NULL (an empty field) and "" (a quoted one) alike; see the next test.
        assert line == ["" if value is None else server_text(value) for value in row]
    # The text the CSV quoting carries: commas, quotes, newlines, tabs, backslashes and "\N".
    assert [line[1] for line in got[1:]] == TRICKY


@needs_copy
def test_csv_spells_null_as_an_empty_field_and_the_empty_string_quoted():
    author = make_author()
    Specimen.objects.create(**specimen_values(0, author=author, body="", email=None))  # happened_at NULL
    data = export(sa.select(specimens.c.body, specimens.c.email, specimens.c.happened_at), header=False)
    assert data == b'"",,\n'


@needs_copy
def test_csv_writes_json_as_its_text():
    author = make_author()
    docs = [{"k": [1, "두", None], "n": 0.5}, [], "str", 7, None]
    for n, doc in enumerate(docs):
        Specimen.objects.create(**specimen_values(n, author=author, label=f"{n}", data=doc))
    stmt = sa.select(specimens.c.label_text, specimens.c.data).order_by(specimens.c.label_text)
    lines = csv_rows(export(stmt, header=False))
    # save() stores None as NULL (an empty field), not JSON null.
    assert [json.loads(data) if data else None for _, data in lines] == docs
    assert lines[0][1] == '{"k": [1, "두", null], "n": 0.5}'
    assert lines[-1] == ["4", ""]


@needs_copy
def test_header_is_the_selected_labels():
    make_author("x")
    stmt = sa.select(authors.c.name.label("who"), (authors.c.id * 0).label("zero"))
    assert export(stmt) == b"who,zero\nx,0\n"
    assert export(stmt, header=False) == b"x,0\n"


@needs_copy
def test_no_rows_gives_the_header_alone():
    stmt = sa.select(authors.c.id, authors.c.name)
    assert export(stmt) == b"id,name\n"
    assert export(stmt, header=False) == b""


@needs_copy
def test_text_format_escapes_and_spells_null():
    author = make_author()
    for n, text in enumerate(TRICKY):
        Specimen.objects.create(**specimen_values(n, author=author, label=f"{n:02d}", body=text, email=None))
    stmt = sa.select(specimens.c.body, specimens.c.email).order_by(specimens.c.label_text)
    lines = export(stmt, format="text", header=False).decode().split("\n")
    assert lines.pop() == ""
    escaped = [text.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t") for text in TRICKY]
    assert lines == [f"{text}\t\\N" for text in escaped]


@needs_copy
def test_bindparams_convert_as_lookup_values():
    # The values are written into the SQL by psycopg (COPY takes no protocol
    # parameters), so a literal percent sign and a quote must come through too.
    make_author("it's 100%")
    make_author("other")
    stmt = sa.select(authors.c.name, sa.literal_column("'50%'").label("pct")).where(
        authors.c.name == sa.bindparam("name"),
        authors.c.joined_at == sa.bindparam("at"),
    )
    # A naive datetime matches only made aware in TIME_ZONE, as filter(joined_at=...) makes it (with a
    # warning); written into the SQL as it is, PostgreSQL would read it in the session's UTC.
    params = {"name": "it's 100%", "at": AT.replace(tzinfo=None)}
    with pytest.warns(RuntimeWarning, match="naive datetime"):
        assert csv_rows(export(stmt, params)) == [["name", "pct"], ["it's 100%", "50%"]]
    # Without any parameter, "%%" (SQLAlchemy's literal "%") is still read as "%".
    assert export(sa.select(sa.literal_column("'50%'").label("pct")), header=False) == b"50%\n"


@needs_copy
def test_bindparams_of_bytes_json_and_uuid_are_written_as_psycopg_adapts_them():
    author = make_author()
    blob, doc = b"\x00'\\\xff", {"k": ["it's", 1]}
    specimen = Specimen.objects.create(**specimen_values(1, author=author, label="hit", blob=blob, data=doc))
    Specimen.objects.create(**specimen_values(2, author=author, label="miss"))
    stmt = sa.select(specimens.c.label_text).where(
        specimens.c.blob == sa.bindparam("blob"),
        specimens.c.data == sa.bindparam("doc"),
        specimens.c.uid.in_(sa.bindparam("uids", expanding=True)),
    )
    params = {"blob": blob, "doc": doc, "uids": [specimen.uid, str(specimen.uid)]}
    assert export(stmt, params, header=False) == b"hit\n"


@pytest.fixture
def server_side_binding():
    """Reconnect the default database with psycopg's ServerBindingCursor (OPTIONS["server_side_binding"])."""
    options = connection.settings_dict.setdefault("OPTIONS", {})
    connection.close()
    options["server_side_binding"] = True
    try:
        yield
    finally:
        options.pop("server_side_binding")
        connection.close()


@needs_copy
@pytest.mark.django_db(transaction=True)
def test_bindparams_with_server_side_binding(server_side_binding):
    make_author("it's 100%")
    with connection.cursor() as cursor:
        assert type(cursor.cursor).__name__ == "ServerBindingCursor"
    stmt = sa.select(authors.c.name, sa.literal_column("'50%'").label("pct")).where(
        authors.c.name == sa.bindparam("name"), authors.c.joined_at == sa.bindparam("at")
    )
    with pytest.warns(RuntimeWarning, match="naive datetime"):
        data = export(stmt, {"name": "it's 100%", "at": AT.replace(tzinfo=None)}, header=False)
    assert data == b"it's 100%,50%\n"
    assert export(sa.select(sa.literal_column("'50%'").label("pct")), header=False) == b"50%\n"


@needs_copy
def test_large_results_come_in_chunks_of_at_least_64_kib():
    chunks = list(copy_to(sa.text("SELECT g, repeat('x', 100) FROM generate_series(1, 5000) AS g"), header=False))
    assert all(isinstance(chunk, bytes) for chunk in chunks)
    assert len(chunks) > 1
    assert all(len(chunk) >= 64 * 1024 for chunk in chunks[:-1])
    data = b"".join(chunks)
    assert data.count(b"\n") == 5000
    assert data.startswith(b"1," + b"x" * 100 + b"\n2,")


@pytest.fixture
def cursors(monkeypatch):
    """[cursor, closed] for each cursor connection.cursor() hands out."""
    opened = []
    original = connection.cursor

    def cursor():
        wrapper = original()
        entry = [wrapper, False]
        opened.append(entry)
        close = wrapper.close

        def closing():
            entry[1] = True
            return close()

        wrapper.close = closing
        return wrapper

    monkeypatch.setattr(connection, "cursor", cursor)
    return opened


BIG = sa.text("SELECT g FROM generate_series(1, 1000000) AS g")


@needs_copy
def test_nothing_runs_until_the_first_chunk_is_asked_for(cursors):
    with CaptureQueriesContext(connection) as ctx:
        chunks = copy_to(BIG)
        assert cursors == []
        next(chunks)
        assert len(cursors) == 1 and not cursors[0][1]
        chunks.close()
    assert cursors[0][1]
    assert len(ctx.captured_queries) == 1


@needs_copy
def test_the_cursor_closes_when_the_bytes_run_out(cursors):
    data = export(sa.select(sa.literal(1).label("one")))
    assert data == b"one\n1\n"
    assert [closed for _, closed in cursors] == [True]


@needs_copy
@pytest.mark.django_db(transaction=True)
def test_closing_early_outside_a_transaction_leaves_the_connection_usable(cursors):
    assert connection.get_autocommit()
    chunks = copy_to(BIG)
    next(chunks)
    chunks.close()
    assert cursors[0][1]
    assert execute(sa.select(sa.literal(1))) == [(1,)]


@needs_copy
def test_closing_early_inside_atomic_keeps_the_transaction(cursors):
    # psycopg would cancel the COPY, which aborts the transaction; the rest is read and dropped instead.
    with transaction.atomic():
        make_author("kept")
        chunks = copy_to(BIG)
        next(chunks)
        chunks.close()
        assert cursors[0][1]
        assert execute(sa.select(authors.c.name)) == [("kept",)]
    assert Author.objects.get().name == "kept"


@needs_copy
def test_inside_atomic_it_sees_the_transactions_rows():
    with transaction.atomic():
        make_author("uncommitted")
        assert export(sa.select(authors.c.name), header=False) == b"uncommitted\n"


@needs_copy
def test_a_database_error_is_djangos():
    with pytest.raises(ProgrammingError, match="nope"), transaction.atomic():
        export(sa.text("SELECT nope"))
    assert export(sa.select(sa.literal(1).label("one"))) == b"one\n1\n"


@needs_copy
def test_a_broken_transaction_is_refused_as_execute_refuses_it():
    with transaction.atomic():
        with pytest.raises(ProgrammingError):
            with transaction.atomic(savepoint=False):  # marks the outer block for rollback
                execute(sa.text("SELECT nope"))
        with pytest.raises(TransactionManagementError):
            execute(sa.select(sa.literal(1)))
        with pytest.raises(TransactionManagementError):
            export(sa.select(sa.literal(1)))


@needs_copy
def test_one_query_is_logged_and_execute_wrapper_sees_none(settings):
    make_author("x")
    seen = []

    def wrapper(execute, sql, params, many, context):
        seen.append(sql)
        return execute(sql, params, many, context)

    stmt = sa.select(authors.c.name).where(authors.c.name == sa.bindparam("n"))
    with connection.execute_wrapper(wrapper), CaptureQueriesContext(connection) as ctx:
        assert export(stmt, {"n": "x"}, header=False) == b"x\n"
    assert seen == []
    [query] = ctx.captured_queries
    assert query["sql"].startswith("COPY (SELECT testapp_author.name \nFROM testapp_author")
    assert query["sql"].endswith(") TO STDOUT WITH (FORMAT csv, HEADER false)")
    assert "'x'" in query["sql"]
    assert float(query["time"]) >= 0


@needs_copy
def test_nothing_is_logged_without_debug():
    reset_queries()
    assert not connection.queries_logged
    export(sa.select(sa.literal(1)))
    assert connection.queries == []


@needs_copy
def test_streaming_http_response_sends_the_same_bytes():
    make_specimens()
    stmt = sa.select(specimens.c.label_text, specimens.c.body).order_by(specimens.c.label_text)
    response = StreamingHttpResponse(copy_to(stmt), content_type="text/csv")
    assert b"".join(response.streaming_content) == export(stmt)


@needs_copy
def test_a_text_statement_is_taken():
    assert export(sa.text("SELECT 1 AS a, 'b' AS b")) == b"a,b\n1,b\n"


# --- refused before anything runs --------------------------------------------


@pytest.mark.parametrize(
    "stmt",
    [
        sa.insert(authors).values(name="x", joined_at=AT),
        sa.insert(authors).values(name="x", joined_at=AT).returning(authors.c.id),
        sa.update(authors).values(name="x"),
        sa.delete(authors).returning(authors.c.id),
    ],
    ids=["insert", "insert-returning", "update", "delete-returning"],
)
def test_dml_is_refused(stmt):
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(ValueError, match="SELECT"):
            copy_to(stmt)
    assert ctx.captured_queries == []


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"format": "binary"}, "format"),
        ({"format": "CSV"}, "format"),
        ({"format": "text", "header": True}, "header"),
    ],
)
def test_bad_options_are_refused(kwargs, match):
    with pytest.raises(ValueError, match=match):
        copy_to(sa.select(authors.c.id), **kwargs)


@needs_copy
def test_a_sequence_of_params_is_refused():
    with pytest.raises(TypeError, match="single mapping"):
        copy_to(sa.select(authors.c.id), [{}])


@pytest.mark.skipif(connection.vendor == "postgresql", reason="COPY runs on PostgreSQL")
def test_copy_to_is_not_supported_elsewhere():
    with CaptureQueriesContext(connection) as ctx:
        with pytest.raises(NotSupportedError, match="PostgreSQL"):
            copy_to(sa.select(authors.c.id))
    assert ctx.captured_queries == []


@pytest.mark.skipif(connection.vendor != "postgresql", reason="psycopg2 is a PostgreSQL driver")
def test_copy_to_is_not_supported_with_psycopg2(monkeypatch):
    # psycopg2's module has no Copy (its cursors have copy_expert() instead).
    monkeypatch.setattr(connection, "Database", types.SimpleNamespace(__name__="psycopg2"))
    with pytest.raises(NotSupportedError, match="psycopg 3"):
        copy_to(sa.select(authors.c.id))
