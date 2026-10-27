"""The values of a ``tuple_()`` compared with a tuple of columns convert as the columns' lookups (#86).

``tuple_(a, b) == (x, y)`` and ``.in_([(x, y)])`` bind each value with its
column's type (#73). Wrapped in ``tuple_()`` (``tuple_(a, b) == tuple_(x, y)``,
``.in_([tuple_(x, y)])``) each value is a bind of its own, typed by what
SQLAlchemy guesses from the value, and went to the driver unconverted: an
aware datetime matched nothing on SQLite, a dict failed there and matched
nothing on MySQL. The bridge binds them with the columns' types, as the plain
tuple does.
"""

import datetime
import uuid

import pytest
import sqlalchemy as sa
from django.db import connection
from django.db.models import Q

from django_sqlalchemy_bridge.execution import prepare
from tests.test_lookup_values import column_of, comparable, orm_lookup_params
from tests.test_tuple_in import AT, bridge_uids, key_of, make_specimens, orm_in, orm_uids, select_uids, specimens
from tests.testapp.models import Specimen

pytestmark = pytest.mark.django_db

amount, data, happened_at, uid = specimens.c.amount, specimens.c.data, specimens.c.happened_at, specimens.c.uid
KEY = ("uid", "happened_at", "amount")


def columns(attnames=KEY):
    return sa.tuple_(*(getattr(specimens.c, attname) for attname in attnames))


def values(key):
    return sa.tuple_(*key)


def utc(key):
    # The same instants in another zone: they match only when converted as the ORM converts them.
    return tuple(v.astimezone(datetime.UTC) if isinstance(v, datetime.datetime) else v for v in key)


def test_a_tuple_equal_to_a_tuple_of_values_matches_the_orm():
    rows = make_specimens()
    key = key_of(rows[2], KEY)
    assert bridge_uids(select_uids().where(columns() == values(key))) == orm_in([key], KEY) == [rows[2].uid]
    assert bridge_uids(select_uids().where(columns() == values(utc(key)))) == [rows[2].uid]


def test_a_tuple_not_equal_to_a_tuple_of_values_leaves_the_other_rows():
    rows = make_specimens()
    stmt = select_uids().where(columns() != values(key_of(rows[2], KEY)))
    assert bridge_uids(stmt) == sorted(row.uid for row in rows if row is not rows[2])


def test_a_tuple_in_a_list_of_tuples_of_values_matches_the_orm():
    rows = make_specimens()
    missing = (uuid.uuid4(), rows[0].happened_at, rows[0].amount)
    keys = [key_of(rows[0], KEY), utc(key_of(rows[3], KEY)), missing]
    stmt = select_uids().where(columns().in_([values(key) for key in keys]))
    assert bridge_uids(stmt) == orm_in(keys, KEY) == sorted([rows[0].uid, rows[3].uid])


def test_a_tuple_not_in_a_list_of_tuples_of_values_leaves_the_other_rows():
    rows = make_specimens()
    stmt = select_uids().where(columns().not_in([values(key_of(rows[0], KEY)), values(key_of(rows[3], KEY))]))
    assert bridge_uids(stmt) == sorted([rows[1].uid, rows[2].uid])


def test_a_tuple_greater_than_a_tuple_of_values_matches_the_orm():
    rows = make_specimens()
    author_id, at = rows[1].author_id, rows[1].happened_at
    stmt = select_uids().where(sa.tuple_(column_of("author_id"), happened_at) > sa.tuple_(author_id, at))
    expected = orm_uids(Q(author_id__gt=author_id), Q(author_id=author_id, happened_at__gt=at))
    assert len(expected) == 2
    assert bridge_uids(stmt) == expected


def test_a_json_member_among_tuples_of_values_matches_the_orm():
    rows = make_specimens(3)
    Specimen.objects.filter(pk=rows[0].pk).update(data={"k": 1})
    Specimen.objects.filter(pk=rows[1].pk).update(data={"k": "1"})
    keys = [(rows[0].amount, {"k": 1}), (rows[1].amount, {"k": 1})]
    attnames = ("amount", "data")
    expected = orm_in(keys, attnames)
    assert expected == [rows[0].uid]
    stmt = select_uids().where(sa.tuple_(amount, data).in_([sa.tuple_(sa.literal(a), d) for a, d in keys]))
    assert bridge_uids(stmt) == expected
    stmt = select_uids().where(sa.tuple_(amount, data) == sa.tuple_(sa.literal(rows[0].amount), {"k": 1}))
    assert bridge_uids(stmt) == expected
    # A named bind keeps the rows as tuples; MySQL and Oracle still compare the JSON member as a document.
    stmt = select_uids().where(sa.tuple_(amount, data).in_([sa.tuple_(sa.bindparam("a"), {"k": 1})]))
    assert bridge_uids(stmt, {"a": rows[0].amount}) == expected
    assert bridge_uids(stmt, {"a": rows[1].amount}) == []


def test_named_bindparams_in_a_tuple_of_values_convert_too():
    rows = make_specimens()
    stmt = select_uids().where(sa.tuple_(uid, happened_at) == sa.tuple_(sa.bindparam("u"), sa.bindparam("at")))
    for row in rows[1:3]:
        params = {"u": row.uid, "at": row.happened_at.astimezone(datetime.UTC)}
        assert bridge_uids(stmt, params) == [row.uid]
    stmt = select_uids().where(sa.tuple_(uid, happened_at).in_([sa.tuple_(sa.bindparam("u"), rows[0].happened_at)]))
    assert bridge_uids(stmt, {"u": rows[0].uid}) == [rows[0].uid]


def test_the_values_convert_as_the_orm_lookups_convert_them():
    rows = make_specimens()
    key = key_of(rows[1], KEY)
    expected = []
    for attname, value in zip(KEY, key, strict=True):
        expected += orm_lookup_params(attname, value)
    named = sa.tuple_(*(sa.bindparam(attname) for attname in KEY))
    for stmt, params in (
        (select_uids().where(columns() == values(key)), None),
        (select_uids().where(columns().in_([values(key)])), None),
        (select_uids().where(columns() == named), dict(zip(KEY, key, strict=True))),
    ):
        assert comparable(prepare(stmt, params, connection).param_rows[0]) == expected


def test_values_against_members_with_no_field_pass_through():
    stmt = select_uids().where(sa.tuple_(happened_at, sa.literal(1)) == sa.tuple_(AT, 2))
    (params,) = prepare(stmt, None, connection).param_rows
    # The literal on the left is a bind too.
    assert comparable(params) == comparable([1]) + orm_lookup_params("happened_at", AT) + comparable([2])


def test_tuples_of_expressions_are_left_alone():
    rows = make_specimens()
    # A column on the value side is no value to convert: each row equals itself.
    assert bridge_uids(select_uids().where(columns(("uid", "amount")) == sa.tuple_(uid, amount))) == sorted(
        row.uid for row in rows
    )
    subquery = sa.select(uid, amount).where(uid == rows[1].uid)
    assert bridge_uids(select_uids().where(columns(("uid", "amount")).in_(subquery))) == [rows[1].uid]
