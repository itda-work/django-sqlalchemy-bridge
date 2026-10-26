import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import OperationalError, ProgrammingError, connection
from django.db.models.fields.json import KT
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import execute, table_for
from django_sqlalchemy_bridge.execution import dialect_for
from tests.testapp.models import Author, Book

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, tzinfo=ZoneInfo("Asia/Seoul"))

books = table_for(Book)
meta = books.c.meta

IS_PG = connection.vendor == "postgresql"
IS_SQLITE = connection.vendor == "sqlite"
IS_MYSQL = connection.vendor == "mysql"
IS_ORACLE = connection.vendor == "oracle"
# SQLAlchemy compiles JSON for Oracle from 2.1 on.
SA_ORACLE_JSON = tuple(int(n) for n in sa.__version__.split(".")[:2]) >= (2, 1)
pg_only = pytest.mark.skipif(not IS_PG, reason="jsonb operators are PostgreSQL only")
sqlite_only = pytest.mark.skipif(not IS_SQLITE, reason="SQLite behaviour")
mysql_only = pytest.mark.skipif(not IS_MYSQL, reason="MySQL behaviour")
oracle_only = pytest.mark.skipif(not IS_ORACLE, reason="Oracle behaviour")
json_paths = pytest.mark.skipif(
    IS_ORACLE and not SA_ORACLE_JSON, reason="SQLAlchemy 2.0 cannot compile JSON paths for Oracle"
)

METAS = [
    {"k": 1},
    {"k": "1"},
    {"k": 2, "x": True},
    {"x": {"k": 1}},
    {"k": [1, 2], "tag": "a%b"},
    {},
    [1, "k"],
    "k",
]


@pytest.fixture
def ids():
    """Book pk by its index in METAS."""
    author = Author.objects.create(name="a", joined_at=AT)
    return [Book.objects.create(author=author, title=str(i), price=Decimal(1), meta=m).pk for i, m in enumerate(METAS)]


def bridge(where):
    return sorted(pk for (pk,) in execute(sa.select(books.c.id).where(where)))


def orm(**lookup):
    return sorted(Book.objects.filter(**lookup).values_list("pk", flat=True))


# --- every backend: keys and paths ---------------------------------------------


@json_paths
@pytest.mark.parametrize("value", [1, "1", 2, "a"])
def test_key_equality_matches_the_orm(ids, value):
    if IS_ORACLE and value in (1, "1"):
        # The ORM on Oracle compares the key's JSON_VALUE() text, so 1 and "1" match
        # both {"k": 1} and {"k": "1"}; the bridge compares JSON values with
        # JSON_EQUAL(), which tells them apart as PostgreSQL and MySQL do.
        assert orm(meta__k=value) == [ids[0], ids[1]]
        assert bridge(meta["k"] == value) == [ids[0] if value == 1 else ids[1]]
        return
    assert bridge(meta["k"] == value) == orm(meta__k=value)


@json_paths
def test_key_equality_with_a_structured_value(ids):
    if not IS_SQLITE:
        assert bridge(meta["k"] == [1, 2]) == orm(meta__k=[1, 2]) == [ids[4]]
    else:
        # SQLite compares JSON text: its "[1,2]" is not Django's dumped "[1, 2]".
        # The ORM wraps the value in JSON() to normalise it; SQLAlchemy does not.
        assert bridge(meta["k"] == [1, 2]) == []
        assert orm(meta__k=[1, 2]) == [ids[4]]


@json_paths
def test_index_and_path_match_the_orm(ids):
    # The key, index and path bind as the dialect spells them, not as raw values.
    assert bridge(meta[0] == 1) == orm(meta__0=1) == [ids[6]]
    assert bridge(meta[("x", "k")] == 1) == orm(meta__x__k=1) == [ids[3]]
    assert bridge(meta[("k", 1)] == 2) == orm(meta__k__1=2) == [ids[4]]


@pytest.mark.parametrize("value", [{"k": 1}, {}, [1, "k"], "k"])
def test_whole_document_equality_matches_the_orm(ids, value):
    assert bridge(meta == value) == orm(meta=value)


@json_paths
def test_key_in_a_list_matches_the_orm(ids):
    assert bridge(meta["k"].in_([1, "1"])) == orm(meta__k__in=[1, "1"])
    if not (IS_SQLITE or IS_ORACLE):
        assert bridge(meta["k"].in_([2, [1, 2]])) == orm(meta__k__in=[2, [1, 2]])
    else:
        # IN compares JSON text there (on Oracle, JSON_QUERY()'s "[1,2]"), not
        # Django's dumped "[1, 2]"; only == and != compare JSON values on Oracle.
        assert bridge(meta["k"].in_([2, [1, 2]])) == [ids[2]]
        assert orm(meta__k__in=[2, [1, 2]]) == [ids[2], ids[4]]


@json_paths
def test_key_text_with_as_string(ids):
    # as_string() is the generic text extraction; it compiles on every backend.
    # A row without the key gives NULL (on Oracle too, not an error).
    assert bridge(meta["k"].as_string() == "a") == []
    assert bridge(meta["tag"].as_string() == "a%b") == [ids[4]]


# --- PostgreSQL: jsonb operators, same rows as the ORM lookups -----------------


@pg_only
@pytest.mark.parametrize("value", [{"k": 1}, {"k": "1"}, {"x": True}, {"k": [1]}, {}, [1], "k"])
def test_contains_matches_the_orm(ids, value):
    assert bridge(meta.contains(value)) == orm(meta__contains=value)


@pg_only
@pytest.mark.parametrize("value", [{"k": 1, "x": True}, {"k": 2, "x": True}, [1, "k", 3], {}])
def test_contained_by_matches_the_orm(ids, value):
    assert bridge(meta.contained_by(value)) == orm(meta__contained_by=value)


@pg_only
def test_has_key_matches_the_orm(ids):
    assert bridge(meta.has_key("k")) == orm(meta__has_key="k")
    assert bridge(meta.has_all(["k", "x"])) == orm(meta__has_keys=["k", "x"])
    assert bridge(meta.has_any(["x", "tag"])) == orm(meta__has_any_keys=["x", "tag"])


@pg_only
def test_astext_matches_key_text_transform(ids):
    expected = sorted(Book.objects.annotate(v=KT("meta__k")).filter(v="1").values_list("pk", flat=True))
    assert bridge(meta["k"].astext == "1") == expected
    assert expected == [ids[0], ids[1]]


@pg_only
def test_contained_value_is_bound_as_jsonb_through_the_field(ids):
    # A literal "%" inside the bound document is a value, not a placeholder.
    assert bridge(meta.contains({"tag": "a%b"})) == [ids[4]]
    assert bridge(meta.contains(None)) == []  # None is SQL NULL, as everywhere in the bridge


# --- SQLite: the jsonb operators have no SQLite meaning --------------------------


@sqlite_only
def test_contains_fails_on_sqlite(ids):
    with pytest.raises(OperationalError):
        bridge(meta.contains({"k": 1}))


@sqlite_only
def test_has_key_fails_on_sqlite(ids):
    with pytest.raises(OperationalError, match="syntax error"):
        bridge(meta.has_key("k"))


@sqlite_only
def test_astext_on_sqlite_is_sqlite_json_text_extraction(ids):
    # SQLite's ->> returns the SQL value: 1 for {"k": 1}, which is not the text '1'.
    assert bridge(meta["k"].astext == "1") == [ids[1]]


# --- MySQL: JSON documents, no jsonb operators ----------------------------------


@mysql_only
def test_contains_and_has_key_fail_on_mysql(ids):
    with pytest.raises(ProgrammingError):
        bridge(meta.contains({"k": 1}))
    with pytest.raises(ProgrammingError):
        bridge(meta.has_key("k"))


@mysql_only
def test_astext_on_mysql_is_the_unquoted_text(ids):
    # MySQL's ->> is JSON_UNQUOTE(JSON_EXTRACT()): the text '1' for {"k": 1} and {"k": "1"}.
    assert bridge(meta["k"].astext == "1") == [ids[0], ids[1]]


@mysql_only
def test_bound_values_are_json_documents_on_mysql(ids):
    # MySQL compares a JSON value with a string as a JSON string; the bridge, like
    # the ORM, binds JSON_EXTRACT(%s, '$') so 1 does not match {"k": "1"}.
    assert bridge(meta["k"] == 1) == [ids[0]]
    assert bridge(meta["k"] == "1") == [ids[1]]
    # A stored document reads back as the document it was given.
    pk = ids[0]
    execute(sa.update(books).where(books.c.id == pk).values(meta={"k": "1", "n": None}))
    assert execute(sa.select(meta).where(books.c.id == pk)) == [({"k": "1", "n": None},)]
    assert Book.objects.get(pk=pk).meta == {"k": "1", "n": None}


# --- Oracle: JSON text compared as JSON values ------------------------------------


@oracle_only
@json_paths
def test_json_values_compare_with_json_equal_on_oracle(ids):
    # Oracle cannot compare the NCLOB Django stores with "=" (ORA-22848), and a
    # key's JSON_QUERY() text "[1,2]" is not Django's "[1, 2]".
    assert "JSON_EQUAL(" in str(sa.select(books.c.id).where(meta == {"k": 1}).compile(dialect=dialect_for(connection)))
    assert bridge(meta["k"] == [1, 2]) == [ids[4]]
    assert bridge(meta["k"] != 2) == [ids[0], ids[1], ids[4]]
    assert bridge(meta == {"k": 1}) == orm(meta={"k": 1}) == [ids[0]]
    # JSON_VALUE() gives NULL, not an error, where the key is missing or not a number.
    assert bridge(meta["k"].as_integer() == 2) == [ids[2]]


@oracle_only
@pytest.mark.skipif(SA_ORACLE_JSON, reason="SQLAlchemy 2.1 compiles JSON paths for Oracle")
def test_json_paths_are_refused_before_running_on_oracle_with_sqlalchemy_2_0(ids):
    with CaptureQueriesContext(connection) as queries, pytest.raises(sa.exc.UnsupportedCompilationError):
        bridge(meta["k"] == 1)
    assert queries.captured_queries == []
    # Whole documents compare without a path.
    assert bridge(meta == {"k": 1}) == [ids[0]]
