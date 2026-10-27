"""A JSON column IN a list compares whole documents on every backend, Oracle too (#86).

Oracle stores ``JSONField`` as an ``NCLOB``, which it cannot compare with ``=``
(ORA-22848), so ``meta.in_([...])`` compiled as SQLAlchemy writes it fails
there. The bridge spells a literal list as the ``OR`` of ``JSON_EQUAL()``
comparisons, as it compiles ``meta == {...}`` (#38), and refuses an expanding
``bindparam``, whose values come only at run time. The other backends keep
SQLAlchemy's ``IN``. A key (``meta["k"].in_([...])``) is not a document and
keeps its ``IN`` everywhere. An empty list is ``1 != 1`` on MySQL too, where
SQLAlchemy would write its empty set inside ``JSON_EXTRACT()``, and an expanding
``bindparam`` given an empty list is refused there before running (#90).
"""

import django
import pytest
import sqlalchemy as sa
from django.db import NotSupportedError, connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge.execution import prepare
from tests.test_json import SA_ORACLE_JSON
from tests.test_lookup_values import comparable, orm_lookup_params
from tests.test_tuple_in import bridge_uids, make_specimens, orm_uids, select_uids, specimens
from tests.test_tuple_in_json import DATA
from tests.testapp.models import Specimen

pytestmark = pytest.mark.django_db

IS_MYSQL = connection.vendor == "mysql"
IS_ORACLE = connection.vendor == "oracle"
data = specimens.c.data


@pytest.fixture
def rows():
    rows = make_specimens(len(DATA))
    for row, value in zip(rows, DATA, strict=True):
        Specimen.objects.filter(pk=row.pk).update(data=value)
    return rows


def uids(rows):
    return sorted(row.uid for row in rows)


def orm_in(values):
    # data=<document> one value at a time, which the ORM compares with JSON_EQUAL() on Oracle.
    return orm_uids(*(Q(data=value) for value in values))


def test_a_json_column_in_a_literal_list_matches_the_orm(rows):
    values = [{"k": 1}, DATA[2], [1, {"a": None}], {"missing": True}]
    expected = orm_in(values)
    assert expected == uids([rows[0], rows[2], rows[3]])
    assert bridge_uids(select_uids().where(data.in_(values))) == expected
    # The ORM's data__in compares documents too, except on Oracle (ORA-22848) and with Django 5.2 on
    # MySQL, which binds the documents as text there (6.1 wraps them in JSON_EXTRACT(%s, '$')).
    if not (IS_ORACLE or (IS_MYSQL and django.VERSION < (6,))):
        assert sorted(Specimen.objects.filter(data__in=values).values_list("uid", flat=True)) == expected


def test_a_json_column_not_in_a_literal_list_leaves_the_other_documents(rows):
    stmt = select_uids().where(data.not_in([{"k": 1}, [1, {"a": None}]]))
    # NOT IN gives no row for SQL NULL (rows[4]), as everywhere.
    assert bridge_uids(stmt) == uids([rows[1], rows[2], rows[5]])


def test_a_json_column_in_an_empty_list(rows):
    # On MySQL SQLAlchemy would write its empty set inside JSON_EXTRACT() (#90).
    assert bridge_uids(select_uids().where(data.in_([]))) == orm_in([]) == []
    assert bridge_uids(select_uids().where(data.not_in([]))) == uids(rows)


@pytest.mark.skipif(IS_ORACLE and not SA_ORACLE_JSON, reason="SQLAlchemy 2.0 cannot compile JSON keys for Oracle")
def test_a_key_in_an_empty_list(rows):
    assert bridge_uids(select_uids().where(data["k"].in_([]))) == []
    assert bridge_uids(select_uids().where(data["k"].not_in([]))) == uids(rows)


def test_only_mysql_spells_an_empty_list_out():
    sql = prepare(select_uids().where(data.in_([])), None, connection).sql
    if IS_MYSQL or IS_ORACLE:
        assert sql.endswith("WHERE 1 != 1")
    else:
        assert " IN (" in sql


def expanding_values():
    return sa.bindparam("values", expanding=True)


@pytest.mark.skipif(IS_ORACLE, reason="Oracle refuses an expanding bind for a JSON column")
def test_an_expanding_bindparam_given_an_empty_list(rows):
    statements = [
        (select_uids().where(data.in_(expanding_values())), []),
        (select_uids().where(data.not_in(expanding_values())), uids(rows)),
        (select_uids().where(data["k"].in_(expanding_values())), []),
    ]
    for stmt, expected in statements:
        if IS_MYSQL:
            # SQLAlchemy writes the empty set inside each value's JSON_EXTRACT(), and the values come at run time.
            with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match=r"in_\(\[\]\)"):
                bridge_uids(stmt, {"values": []})
            assert len(queries) == 0
        else:
            assert bridge_uids(stmt, {"values": []}) == expected


@pytest.mark.skipif(IS_ORACLE, reason="Oracle refuses an expanding bind for a JSON column")
def test_an_expanding_bindparam_whose_own_value_is_empty(rows):
    stmt = select_uids().where(data.in_(sa.bindparam("values", [], expanding=True)))
    if IS_MYSQL:
        with pytest.raises(NotSupportedError):
            bridge_uids(stmt)
    else:
        assert bridge_uids(stmt) == []


@pytest.mark.skipif(IS_ORACLE, reason="Oracle refuses an expanding bind for a JSON column")
def test_one_statement_alternates_empty_and_non_empty_lists(rows):
    stmt = select_uids().where(data.in_(expanding_values()))
    values = [{"k": 1}, [1, {"a": None}]]
    for _ in range(2):
        # The compiled statement is reused: an empty list neither breaks nor changes the next run.
        assert bridge_uids(stmt, {"values": values}) == orm_in(values) == uids([rows[0], rows[3]])
        if IS_MYSQL:
            with pytest.raises(NotSupportedError):
                bridge_uids(stmt, {"values": []})
        else:
            assert bridge_uids(stmt, {"values": []}) == orm_in([]) == []


def test_the_rewritten_or_keeps_its_place_among_other_conditions(rows):
    values = [{"k": 1}, DATA[2]]
    stmt = select_uids().where(specimens.c.uid == rows[1].uid, data.in_(values))
    assert bridge_uids(stmt) == []
    stmt = select_uids().where(specimens.c.uid == rows[2].uid, data.in_(values))
    assert bridge_uids(stmt) == [rows[2].uid]


def test_the_values_convert_as_the_orm_lookups_convert_them(rows):
    values = [{"k": 1}, DATA[2]]
    (params,) = prepare(select_uids().where(data.in_(values)), None, connection).param_rows
    # One value at a time: the ORM's data__in wraps each value in JSON_EXTRACT(%s, '$') on SQLite.
    assert comparable(params) == orm_lookup_params("data", values[0]) + orm_lookup_params("data", values[1])


def test_only_oracle_spells_the_list_out():
    sql = prepare(select_uids().where(data.in_([{"k": 1}, {"k": 2}])), None, connection).sql
    if IS_ORACLE:
        assert " IN " not in sql
        assert sql.count("JSON_EQUAL(") == 2
    else:
        assert " IN (" in sql


@pytest.mark.skipif(not IS_ORACLE, reason="the other backends compare a JSON column IN an expanding bind")
def test_an_expanding_bindparam_is_refused_before_running_on_oracle():
    stmt = select_uids().where(data.in_(sa.bindparam("values", expanding=True)))
    with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match="literal list"):
        bridge_uids(stmt, {"values": [{"k": 1}]})
    assert len(queries) == 0
    with pytest.raises(NotSupportedError):
        bridge_uids(select_uids().where(data.not_in(sa.bindparam("values", expanding=True))), {"values": []})


@pytest.mark.skipif(IS_ORACLE, reason="Oracle refuses an expanding bind for a JSON column")
def test_an_expanding_bindparam_matches_the_orm(rows):
    stmt = select_uids().where(data.in_(sa.bindparam("values", expanding=True)))
    values = [{"k": 1}, [1, {"a": None}]]
    assert bridge_uids(stmt, {"values": values}) == orm_in(values) == uids([rows[0], rows[3]])


@pytest.mark.skipif(IS_ORACLE and not SA_ORACLE_JSON, reason="SQLAlchemy 2.0 cannot compile JSON keys for Oracle")
def test_a_key_in_a_list_keeps_its_in(rows):
    # A key is not rewritten: it compares as SQLAlchemy compiles IN (JSON text on SQLite and Oracle).
    stmt = select_uids().where(data["k"].in_([1, 2]))
    assert " IN (" in prepare(stmt, None, connection).sql
    assert bridge_uids(stmt) == [rows[0].uid]
