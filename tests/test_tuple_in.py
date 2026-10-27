"""Each member of a tuple compared with columns converts as an ORM lookup on its column (#73).

``tuple_(a, b).in_([...])`` binds one expanding parameter of type ``TupleType``;
its members take the types of the tuple's columns (``TupleType.types``), in the
order SQLAlchemy expands them: row by row, member by member.
"""

import datetime
import functools
import operator
import uuid
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import connection
from django.db.models import Q

from django_sqlalchemy_bridge import execute, table_for
from django_sqlalchemy_bridge.execution import prepare
from tests.test_lookup_values import SKIP, VALUE, column_of, comparable, orm_lookup_params
from tests.testapp.models import Author, Gadget, Specimen

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
AT = datetime.datetime(2026, 10, 27, 12, 0, 0, 250000, tzinfo=SEOUL)

specimens = table_for(Specimen)
KEY = ("uid", "happened_at", "amount", "flag", "author_id", "gadget_id")


def make_specimens(count=4):
    author = Author.objects.create(name="a", joined_at=AT)
    other = Author.objects.create(name="b", joined_at=AT)
    gadgets = [Gadget.objects.create(code="g1"), Gadget.objects.create(code="g2")]
    return [
        Specimen.objects.create(
            uid=uuid.uuid4(),
            label=f"{n}",
            slug=f"s{n}",
            small=n,
            positive=n,
            big=n,
            flag=bool(n % 2),
            ratio=0.5,
            amount=Decimal(n) + Decimal("0.125"),
            day=datetime.date(2026, 10, 27),
            at=datetime.time(12),
            happened_at=AT + datetime.timedelta(minutes=n),
            took=datetime.timedelta(seconds=n),
            blob=b"",
            author=other if n == 3 else author,
            gadget=gadgets[n % 2],  # not None: NULL matches no tuple
        )
        for n in range(count)
    ]


def key_of(specimen, attnames=KEY):
    return tuple(getattr(specimen, attname) for attname in attnames)


def tuple_of(attnames=KEY):
    return sa.tuple_(*(column_of(attname) for attname in attnames))


def orm_uids(*conditions):
    query = functools.reduce(operator.or_, conditions, Q(pk__in=[]))
    return sorted(Specimen.objects.filter(query).values_list("uid", flat=True))


def orm_in(keys, attnames=KEY):
    return orm_uids(*(Q(**dict(zip(attnames, key, strict=True))) for key in keys))


def bridge_uids(stmt, params=None):
    return sorted(uid for (uid,) in execute(stmt, params))


def select_uids():
    return sa.select(specimens.c.uid)


def test_a_literal_tuple_in_list_matches_the_orm():
    rows = make_specimens()
    missing = (uuid.uuid4(), AT, Decimal("0.125"), False, rows[0].author_id, "g1")
    keys = [key_of(rows[0]), key_of(rows[3]), missing]
    assert len(orm_in(keys)) == 2
    assert bridge_uids(select_uids().where(tuple_of().in_(keys))) == orm_in(keys)


def test_an_expanding_tuple_bindparam_matches_the_orm():
    rows = make_specimens()
    stmt = select_uids().where(tuple_of().in_(sa.bindparam("keys", expanding=True)))
    keys = [key_of(rows[1]), key_of(rows[2])]
    assert len(orm_in(keys)) == 2
    assert bridge_uids(stmt, {"keys": keys}) == orm_in(keys)


def test_an_empty_tuple_in_list_matches_nothing():
    make_specimens()
    assert bridge_uids(select_uids().where(tuple_of().in_([]))) == orm_in([]) == []
    stmt = select_uids().where(tuple_of().in_(sa.bindparam("keys", expanding=True)))
    assert bridge_uids(stmt, {"keys": []}) == []


def test_a_reused_tuple_in_statement_converts_each_run_s_values():
    rows = make_specimens()
    stmt = select_uids().where(tuple_of().in_(sa.bindparam("keys", expanding=True)))
    for keys in ([key_of(rows[0])], [key_of(rows[1]), key_of(rows[3])], [key_of(rows[2])]):
        assert bridge_uids(stmt, {"keys": keys}) == orm_in(keys) != []


def test_a_datetime_in_another_zone_matches_the_same_instant():
    rows = make_specimens()
    attnames = ("uid", "happened_at")
    keys = [(rows[1].uid, rows[1].happened_at.astimezone(datetime.UTC))]
    assert bridge_uids(select_uids().where(tuple_of(attnames).in_(keys))) == orm_in(keys, attnames) == [rows[1].uid]


def test_a_tuple_equal_to_a_tuple_matches_the_orm():
    rows = make_specimens()
    attnames = ("uid", "happened_at", "amount")
    stmt = select_uids().where(tuple_of(attnames) == key_of(rows[2], attnames))
    assert bridge_uids(stmt) == orm_in([key_of(rows[2], attnames)], attnames) == [rows[2].uid]


def test_a_tuple_greater_than_a_tuple_matches_the_orm():
    rows = make_specimens()
    author_id, at = rows[1].author_id, rows[1].happened_at
    stmt = select_uids().where(tuple_of(("author_id", "happened_at")) > (author_id, at))
    expected = orm_uids(Q(author_id__gt=author_id), Q(author_id=author_id, happened_at__gt=at))
    assert len(expected) == 2
    assert bridge_uids(stmt) == expected


@pytest.mark.parametrize("attname", list(VALUE))
def test_tuple_members_convert_as_the_orm_lookup_converts_them(attname):
    if attname in SKIP.get(connection.vendor, ()):
        pytest.skip(f"{connection.display_name} does not compare this column with =")
    uid, value = VALUE["uid"], VALUE[attname]
    stmt = select_uids().where(sa.tuple_(specimens.c.uid, column_of(attname)).in_([(uid, value), (uid, value)]))
    params = comparable(prepare(stmt, None, connection).param_rows[0])
    # Django 5.2 renders filter(flag=True) as "WHERE flag", with no param.
    lookup = (
        orm_lookup_params(f"{attname}__in", [value]) if isinstance(value, bool) else orm_lookup_params(attname, value)
    )
    expected = orm_lookup_params("uid", uid) + lookup
    assert params == expected * 2


def test_none_and_members_with_no_field_pass_through():
    stmt = select_uids().where(sa.tuple_(specimens.c.happened_at, sa.literal(1)).in_([(None, 1), (AT, 2)]))
    (params,) = prepare(stmt, None, connection).param_rows
    happened_at = Specimen._meta.get_field("happened_at")
    # The literal on the left is a bind too.
    assert params == (1, None, 1, happened_at.get_db_prep_value(AT, connection), 2)
