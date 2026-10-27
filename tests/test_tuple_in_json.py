"""A JSON member of a tuple compared with columns compares as the ORM compares the document (#76).

SQLAlchemy renders ``tuple_(a, meta).in_([...])`` as one expanding bind whose
members get no ``bind_expression``: on MySQL the JSON value is not wrapped in
``JSON_EXTRACT(%s, '$')`` (#37), and on Oracle the tuple compares the ``NCLOB``
with ``=`` (#38). There the bridge spells a literal list as ``OR`` of each
row's ``AND`` of member comparisons, which go the way a single comparison
goes, and refuses an expanding ``bindparam``, whose rows come only at run time.
"""

import uuid

import pytest
import sqlalchemy as sa
from django.db import NotSupportedError, connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge.execution import prepare
from tests.test_lookup_values import comparable, orm_lookup_params
from tests.test_tuple_in import bridge_uids, make_specimens, orm_in, select_uids, specimens
from tests.testapp.models import Specimen

pytestmark = pytest.mark.django_db

try:  # Django 6.1+; there None as the value of an exact lookup on a JSONField is deprecated
    from django.db.models import JSONNull

    JSON_NULL = JSONNull()
except ImportError:  # Django 5.2: None is the JSON scalar null there
    JSON_NULL = None

REWRITES = connection.vendor in ("mysql", "oracle")
# MySQL normalizes a stored document ({"a": 2, "b": 1}); Django binds json.dumps() text.
DATA = [{"k": 1}, {"k": "1"}, {"x": {"k": [1, 2]}, "tag": "a%b"}, [1, {"a": None}], None, {"b": 1, "a": 2}]
amount, data, flag, uid = specimens.c.amount, specimens.c.data, specimens.c.flag, specimens.c.uid


@pytest.fixture
def rows():
    rows = make_specimens(len(DATA))
    for row, value in zip(rows, DATA, strict=True):
        Specimen.objects.filter(pk=row.pk).update(data=value)
        row.data = value
    return rows


def uids(rows):
    return sorted(row.uid for row in rows)


def test_a_literal_tuple_in_list_with_a_json_member_matches_the_orm(rows):
    keys = [
        (rows[0].amount, {"k": 1}),
        (rows[1].amount, {"k": 1}),  # rows[1] holds {"k": "1"}
        (rows[2].amount, DATA[2]),
        (rows[3].amount, [1, {"a": None}]),
        (rows[5].amount, {"b": 1, "a": 2}),
    ]
    attnames = ("amount", "data")
    expected = orm_in(keys, attnames)
    assert expected == uids([rows[0], rows[2], rows[3], rows[5]])
    assert bridge_uids(select_uids().where(sa.tuple_(amount, data).in_(keys))) == expected


def test_a_literal_tuple_not_in_list_with_a_json_member_leaves_the_other_rows(rows):
    keys = [(rows[0].amount, {"k": 1}), (rows[1].amount, {"k": 1}), (rows[3].amount, [1, {"a": None}])]
    stmt = select_uids().where(data.is_not(None), sa.tuple_(amount, data).not_in(keys))
    assert bridge_uids(stmt) == uids([rows[1], rows[2], rows[5]])
    stmt = select_uids().where(data.is_not(None), sa.tuple_(amount, data).not_in([(rows[5].amount, DATA[5])]))
    assert bridge_uids(stmt) == uids(rows[:4])


def test_the_rewritten_or_keeps_its_place_among_other_conditions(rows):
    keys = [(rows[0].amount, {"k": 1}), (rows[2].amount, DATA[2])]
    # Unparenthesized, "uid = %s AND <row 0> OR <row 2>" would match rows[2].
    stmt = select_uids().where(uid == rows[1].uid, sa.tuple_(amount, data).in_(keys))
    assert bridge_uids(stmt) == []
    stmt = select_uids().where(uid == rows[0].uid, sa.tuple_(amount, data).in_(keys))
    assert bridge_uids(stmt) == [rows[0].uid]


def test_a_json_none_matches_no_row(rows):
    # The bridge binds None as SQL NULL, which "=" matches with no row. rows[4] holds
    # SQL NULL, so the ORM's JSON null lookup does not match it either.
    keys = [(rows[4].uid, None), (rows[0].uid, {"k": 1})]
    expected = sorted(
        Specimen.objects.filter(Q(uid=rows[4].uid, data=JSON_NULL) | Q(uid=rows[0].uid, data={"k": 1})).values_list(
            "uid", flat=True
        )
    )
    assert bridge_uids(select_uids().where(sa.tuple_(uid, data).in_(keys))) == expected == [rows[0].uid]
    assert list(Specimen.objects.filter(data__isnull=True).values_list("uid", flat=True)) == [rows[4].uid]


def test_an_empty_tuple_in_list_with_a_json_member_matches_nothing(rows):
    assert bridge_uids(select_uids().where(sa.tuple_(amount, data).in_([]))) == []
    assert bridge_uids(select_uids().where(sa.tuple_(amount, data).not_in([]))) == uids(rows)


def test_a_tuple_equal_to_a_tuple_with_a_json_member_matches_the_orm(rows):
    key = (rows[5].amount, DATA[5])
    attnames = ("amount", "data")
    assert bridge_uids(select_uids().where(sa.tuple_(amount, data) == key)) == orm_in([key], attnames)
    assert orm_in([key], attnames) == [rows[5].uid]
    stmt = select_uids().where(data.is_not(None), sa.tuple_(amount, data) != (rows[0].amount, {"k": 1}))
    assert bridge_uids(stmt) == uids(rows[1:4] + rows[5:])


def test_the_members_convert_as_the_orm_lookups_convert_them(rows):
    keys = [(rows[0].amount, {"k": 1}), (rows[2].amount, DATA[2])]
    (params,) = prepare(select_uids().where(sa.tuple_(amount, data).in_(keys)), None, connection).param_rows
    expected = []
    for value, document in keys:
        expected += orm_lookup_params("amount", value) + orm_lookup_params("data", document)
    assert comparable(params) == expected


def test_only_mysql_and_oracle_spell_the_tuple_out():
    keys = [(1, {"k": 1}), (2, {"k": 2})]
    sql = prepare(select_uids().where(sa.tuple_(amount, data).in_(keys)), None, connection).sql
    if REWRITES:
        assert " IN " not in sql
        assert sql.count("JSON_EXTRACT(" if connection.vendor == "mysql" else "JSON_EQUAL(") == 2
    else:
        assert " IN (" in sql
    # A tuple with no JSON member keeps SQLAlchemy's IN everywhere.
    sql = prepare(select_uids().where(sa.tuple_(amount, flag).in_([(1, True)])), None, connection).sql
    assert " IN (" in sql


@pytest.mark.skipif(not REWRITES, reason="SQLite and PostgreSQL compare JSON members of a tuple")
def test_an_expanding_tuple_bindparam_with_a_json_member_is_refused_before_running():
    stmt = select_uids().where(sa.tuple_(data, flag).in_(sa.bindparam("keys", expanding=True)))
    with CaptureQueriesContext(connection) as queries, pytest.raises(NotSupportedError, match="literal list"):
        bridge_uids(stmt, {"keys": [({"k": 1}, True)]})
    assert len(queries) == 0
    stmt = select_uids().where(sa.tuple_(data, flag).not_in(sa.bindparam("keys", expanding=True)))
    with pytest.raises(NotSupportedError):
        bridge_uids(stmt, {"keys": []})


@pytest.mark.skipif(REWRITES, reason="MySQL and Oracle refuse an expanding tuple bind with a JSON member")
def test_an_expanding_tuple_bindparam_with_a_json_member_matches_the_orm(rows):
    stmt = select_uids().where(sa.tuple_(data, flag).in_(sa.bindparam("keys", expanding=True)))
    keys = [(DATA[0], rows[0].flag), (DATA[1], not rows[1].flag), (DATA[3], rows[3].flag)]
    assert bridge_uids(stmt, {"keys": keys}) == orm_in(keys, ("data", "flag")) == uids([rows[0], rows[3]])


def test_a_tuple_with_a_json_member_still_compares_a_missing_key(rows):
    assert bridge_uids(select_uids().where(sa.tuple_(uid, data).in_([(uuid.uuid4(), {"k": 1})]))) == []
