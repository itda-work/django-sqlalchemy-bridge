"""A DecimalField expression compares with a value as the ORM's does, also on SQLite (#59).

SQLite compares a column of NUMERIC affinity with text as a number, but the
result of ``SUM()``, ``a + b`` or ``CASE`` has no affinity, and a number is
less than any text there. Django binds a ``DecimalField`` value as text on
SQLite, so ``SUM(price) >= '51.00'`` was false for every row. The ORM casts an
expression whose output field is a ``DecimalField`` (``SQLiteNumericMixin``):
``(CAST(SUM(price) AS NUMERIC))``, outside a window or ``FILTER``; a ``CAST``
has the affinity of its type. The bridge's SQLite dialect does the same for an
expression whose type is a ``DecimalField``'s. Other databases compile as before.
"""

import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa
from django.db import connection
from django.db.models import Avg, Case, F, Max, OuterRef, Q, Subquery, Sum, Value, When, Window
from django.db.models.functions import Coalesce

from django_sqlalchemy_bridge import execute, table_for
from django_sqlalchemy_bridge.execution import prepare
from tests.testapp.models import Author, Book

pytestmark = pytest.mark.django_db

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, tzinfo=datetime.UTC)

authors = table_for(Author)
books = table_for(Book)
price = books.c.price


@pytest.fixture
def shelf():
    """Author "a" has books for 30.00 and 30.00, "b" one for 10.00, "c" none."""
    a = Author.objects.create(name="a", joined_at=AT)
    b = Author.objects.create(name="b", joined_at=AT)
    Author.objects.create(name="c", joined_at=AT)
    Book.objects.create(author=a, title="a1", price=Decimal("30.00"))
    Book.objects.create(author=a, title="a2", price=Decimal("30.00"))
    Book.objects.create(author=b, title="b1", price=Decimal("10.00"))
    return a


# name -> (ORM aggregate over an author's books, the bridge's over books.c.price, a bound "a" alone reaches)
AGGREGATES = {
    "sum": (Sum("books__price"), sa.func.sum(price), Decimal("51")),
    # SQLAlchemy gives func.avg() no type; the ORM's Avg of a DecimalField is a DecimalField.
    "avg": (Avg("books__price"), sa.func.avg(price, type_=price.type), Decimal("25")),
    "max": (Max("books__price"), sa.func.max(price), Decimal("25")),
    "coalesce": (
        Coalesce(Sum("books__price"), Value(Decimal("0"))),
        sa.func.coalesce(sa.func.sum(price), 0),
        Decimal("51"),
    ),
}

VALUES = {
    "bindparam Decimal": lambda bound: (sa.bindparam("x"), {"x": bound}),
    "bindparam int": lambda bound: (sa.bindparam("x"), {"x": int(bound)}),
    "literal Decimal": lambda bound: (bound, None),
    "literal int": lambda bound: (int(bound), None),
}


def _per_author():
    return (
        sa.select(authors.c.id)
        .select_from(authors.outerjoin(books, books.c.author_id == authors.c.id))
        .group_by(authors.c.id)
    )


def _correlated(aggregate):
    return sa.select(aggregate).where(books.c.author_id == authors.c.id).scalar_subquery()


# Where the aggregate meets the value: in HAVING, as a correlated subquery in
# WHERE, and as a column of a derived table.
PLACES = {
    "having": lambda aggregate, value: _per_author().having(aggregate >= value),
    "subquery": lambda aggregate, value: sa.select(authors.c.id).where(_correlated(aggregate) >= value),
    "derived table": lambda aggregate, value: _from_derived(_per_author().add_columns(aggregate.label("r")), value),
}


def _from_derived(stmt, value):
    derived = stmt.subquery()
    return sa.select(derived.c.id).where(derived.c.r >= value)


def _ids(stmt, params):
    return sorted(id_ for id_, *_ in execute(stmt, params))


@pytest.mark.parametrize("place", PLACES.values(), ids=PLACES.keys())
@pytest.mark.parametrize("value", VALUES.values(), ids=VALUES.keys())
@pytest.mark.parametrize("aggregate", AGGREGATES.values(), ids=AGGREGATES.keys())
def test_comparing_a_decimal_aggregate_matches_the_orm(shelf, aggregate, value, place):
    orm, bridge, bound = aggregate
    expected = sorted(Author.objects.annotate(r=orm).filter(r__gte=bound).values_list("id", flat=True))
    assert expected == [shelf.id]
    rhs, params = value(bound)
    assert _ids(place(bridge, rhs), params) == expected


def test_a_correlated_orm_subquery_matches_too(shelf):
    per_author = Book.objects.filter(author=OuterRef("pk")).values("author").annotate(s=Sum("price")).values("s")
    expected = list(Author.objects.annotate(r=Subquery(per_author)).filter(r__gte=51).values_list("id", flat=True))
    assert expected == [shelf.id]
    assert _ids(PLACES["subquery"](sa.func.sum(price), sa.bindparam("x")), {"x": Decimal("51")}) == expected


@pytest.mark.parametrize("aggregate", AGGREGATES.values(), ids=AGGREGATES.keys())
def test_a_decimal_aggregate_reads_back_as_the_orm_reads_it(shelf, aggregate):
    orm, bridge, _ = aggregate
    expected = dict(Author.objects.annotate(r=orm).values_list("id", "r"))
    got = dict(execute(_per_author().add_columns(bridge)))
    assert got == expected
    assert {type(v) for v in got.values() if v is not None} == {Decimal}


# Expressions other than aggregates: the ORM casts them alike.
EXPRESSIONS = {
    "arithmetic": (F("price") + F("price"), price + price, Decimal("51")),
    "negation": (-F("price"), -price, Decimal("-15")),
    "case": (
        Case(When(price__gt=20, then=F("price")), default=Value(Decimal("0"))),
        sa.case((price > 20, price), else_=0),
        Decimal("25"),
    ),
}


@pytest.mark.parametrize("value", VALUES.values(), ids=VALUES.keys())
@pytest.mark.parametrize("expression", EXPRESSIONS.values(), ids=EXPRESSIONS.keys())
def test_comparing_a_decimal_expression_matches_the_orm(shelf, expression, value):
    orm, bridge, bound = expression
    expected = sorted(Book.objects.annotate(r=orm).filter(r__gte=bound).values_list("id", flat=True))
    assert 0 < len(expected) < Book.objects.count()
    rhs, params = value(bound)
    assert _ids(sa.select(books.c.id).where(bridge >= rhs), params) == expected


def test_a_window_over_a_decimal_aggregate_matches_the_orm(shelf):
    # A window is not filterable in the ORM's WHERE; compare the values it gives.
    running = Window(Sum("price"), partition_by=[F("author")])
    expected = dict(Book.objects.annotate(r=running).values_list("id", "r"))
    window = sa.func.sum(price).over(partition_by=books.c.author_id)
    stmt = sa.select(books.c.id, window.label("r"))
    assert dict(execute(stmt)) == expected
    big = sorted(i for i, r in expected.items() if r >= 51)
    assert len(big) == 2
    assert _ids(_from_derived(stmt, sa.bindparam("x")), {"x": Decimal("51")}) == big


def test_a_filtered_decimal_aggregate_matches_the_orm(shelf):
    if not connection.features.supports_aggregate_filter_clause:
        pytest.skip(f"{connection.display_name} has no FILTER (WHERE ...)")
    orm = Sum("books__price", filter=Q(books__title__startswith="a"))
    expected = sorted(Author.objects.annotate(r=orm).filter(r__gte=51).values_list("id", flat=True))
    assert expected == [shelf.id]
    total = sa.func.sum(price).filter(books.c.title.startswith("a"))
    assert _ids(_per_author().having(total >= sa.bindparam("x")), {"x": Decimal("51")}) == expected


@pytest.mark.skipif(connection.vendor != "sqlite", reason="the SQL the SQLite dialect compiles")
def test_sqlite_casts_decimal_expressions_as_the_orm_does():
    def sql(stmt):
        return prepare(stmt, None, connection).sql

    s = "sum(testapp_book.price)"
    # Wrapped where the ORM wraps it, in every clause; a column itself is not.
    assert sql(sa.select(sa.func.sum(price))) == f"SELECT (CAST({s} AS NUMERIC)) AS sum_1 \nFROM testapp_book"
    assert f"(CAST({s} AS NUMERIC)) >= %s" in sql(sa.select(price).having(sa.func.sum(price) >= 1))
    assert "CAST" not in sql(sa.select(price).where(price >= 1))
    # Outside a window or FILTER, as the ORM casts them (SUM(...) OVER is the syntax).
    assert f"(CAST({s} OVER (PARTITION BY" in sql(sa.select(sa.func.sum(price).over(partition_by=books.c.author_id)))
    assert f"(CAST({s} FILTER (WHERE" in sql(sa.select(sa.func.sum(price).filter(price > 1)))
    # Nested expressions are each cast, as the ORM's are.
    assert f"(CAST(coalesce((CAST({s} AS NUMERIC)), %s) AS NUMERIC))" in sql(
        sa.select(sa.func.coalesce(sa.func.sum(price), 0))
    )
    # Not an expression of a DecimalField's type: untouched.
    assert "CAST" not in sql(sa.select(sa.func.sum(books.c.id), sa.func.avg(price), sa.func.count(price)))


@pytest.mark.skipif(connection.vendor == "sqlite", reason="the SQL the other dialects compile")
def test_other_databases_compile_no_cast():
    stmt = sa.select(sa.func.sum(price), price + price, sa.case((price > 1, price), else_=0)).having(
        sa.func.sum(price) >= 1
    )
    assert "CAST" not in prepare(stmt, None, connection).sql.upper()


# One aggregate object used both under a FILTER or window and elsewhere (#72).
# The ORM casts it outside the FILTER or window and again at every other place;
# the bridge kept the object out of every cast once it had met it under one.
title_a = books.c.title.startswith("a")


def _needs_filter():
    if not connection.features.supports_aggregate_filter_clause:
        pytest.skip(f"{connection.display_name} has no FILTER (WHERE ...)")


def _sql(stmt):
    return prepare(stmt, {"x": Decimal("51")}, connection).sql


def _rows(stmt):
    names = [c.name for c in stmt.selected_columns]
    return [dict(zip(names, row, strict=True)) for row in execute(stmt)]


S = "sum(testapp_book.price)"
CAST_S = f"(CAST({S} AS NUMERIC))"


def test_an_aggregate_under_a_filter_is_cast_again_in_having(shelf):
    _needs_filter()
    total = sa.func.sum(price)
    stmt = _per_author().add_columns(total.filter(title_a).label("r")).having(total >= sa.bindparam("x"))
    if connection.vendor == "sqlite":
        sql = _sql(stmt)
        assert sql.count(f"(CAST({S} FILTER (WHERE") == 1
        assert f"HAVING {CAST_S} >= %s" in sql
    orm = Author.objects.annotate(r=Sum("books__price", filter=Q(books__title__startswith="a")), t=Sum("books__price"))
    expected = sorted(orm.filter(t__gte=51).values_list("id", "r"))
    assert expected == [(shelf.id, Decimal("60.00"))]
    assert sorted(execute(stmt, {"x": Decimal("51")})) == expected


def _per_price(*columns):
    """Books grouped by author and price: (a, 30.00) sums to 60.00, (b, 10.00) to 10.00."""
    return sa.select(books.c.author_id, *columns).group_by(books.c.author_id, price)


def test_an_aggregate_under_a_window_is_cast_again_in_having(shelf):
    total = sa.func.sum(price)
    stmt = _per_price(total.over().label("w"), total.label("s")).having(total >= sa.bindparam("x"))
    if connection.vendor == "sqlite":
        sql = _sql(stmt)
        assert sql.count(f"(CAST({S} OVER ()") == 1
        assert f"HAVING {CAST_S} >= %s" in sql
    # After HAVING one group is left, and the window sums the grouped prices.
    assert execute(stmt, {"x": Decimal("51")}) == [(shelf.id, Decimal("30.00"), Decimal("60.00"))]


@pytest.mark.parametrize("plain_first", [True, False], ids=["plain first", "plain last"])
def test_an_aggregate_in_one_select_list_twice_is_cast_at_each(shelf, plain_first):
    _needs_filter()
    total = sa.func.sum(price)
    columns = [total.label("s"), total.filter(title_a).label("f")]
    stmt = sa.select(*(columns if plain_first else columns[::-1]))
    if connection.vendor == "sqlite":
        sql = _sql(stmt)
        assert sql.count(f"(CAST({S} FILTER (WHERE") == 1
        assert sql.count(f"{CAST_S} AS s") == 1
    assert _rows(stmt) == [{"s": Decimal("70.00"), "f": Decimal("60.00")}]


@pytest.mark.parametrize("plain_first", [True, False], ids=["plain first", "plain last"])
def test_an_aggregate_in_one_select_list_with_its_window_is_cast_at_each(shelf, plain_first):
    total = sa.func.sum(price)
    columns = [total.label("s"), total.over().label("w")]
    stmt = _per_price(*(columns if plain_first else columns[::-1])).order_by(books.c.author_id)
    if connection.vendor == "sqlite":
        sql = _sql(stmt)
        assert sql.count(f"(CAST({S} OVER ()") == 1
        assert sql.count(f"{CAST_S} AS s") == 1
    b = Author.objects.get(name="b").id
    assert _rows(stmt) == [
        {"author_id": shelf.id, "s": Decimal("60.00"), "w": Decimal("40.00")},
        {"author_id": b, "s": Decimal("10.00"), "w": Decimal("40.00")},
    ]


def test_an_aggregate_in_its_own_window_order_is_cast_there(shelf):
    # The ORM casts SUM() in ORDER BY of SUM() OVER (ORDER BY SUM()), the same object here.
    total = sa.func.sum(price)
    stmt = _per_price(total.over(order_by=total).label("w")).order_by(books.c.author_id)
    if connection.vendor == "sqlite":
        assert f"(CAST({S} OVER (ORDER BY {CAST_S}) AS NUMERIC))" in _sql(stmt)
    b = Author.objects.get(name="b").id
    # The window runs over the grouped prices in the order of the groups' sums: (b, 10.00), then (a, 30.00).
    assert execute(stmt) == [(shelf.id, Decimal("40.00")), (b, Decimal("10.00"))]


def test_an_aggregate_under_a_filter_in_a_subquery_is_cast_in_the_outer_where(shelf):
    _needs_filter()
    total = sa.func.sum(price)
    stmt = sa.select(authors.c.id, _correlated(total.filter(title_a)).label("f")).where(
        _correlated(total) >= sa.bindparam("x")
    )
    if connection.vendor == "sqlite":
        sql = _sql(stmt)
        assert sql.count(f"(CAST({S} FILTER (WHERE") == 1
        assert "WHERE testapp_book.author_id = testapp_author.id) >= %s" in sql
        assert sql.count(CAST_S) == 1
    assert execute(stmt, {"x": Decimal("51")}) == [(shelf.id, Decimal("60.00"))]


def test_a_function_under_the_filtered_aggregate_keeps_its_cast(shelf):
    _needs_filter()
    total = sa.func.sum(sa.func.coalesce(price, 0))
    stmt = _per_author().add_columns(total.filter(title_a).label("r")).having(total.filter(title_a) >= 1)
    if connection.vendor == "sqlite":
        # As the ORM's Sum(Coalesce(...), filter=...): the inner function inside, the aggregate outside.
        coalesced = "sum((CAST(coalesce(testapp_book.price, %s) AS NUMERIC)))"
        assert _sql(stmt).count(f"(CAST({coalesced} FILTER (WHERE") == 2
    orm = Sum(Coalesce(F("books__price"), Value(Decimal("0"))), filter=Q(books__title__startswith="a"))
    expected = sorted(Author.objects.annotate(r=orm).filter(r__gte=1).values_list("id", "r"))
    assert expected == [(shelf.id, Decimal("60.00"))]
    assert sorted(execute(stmt)) == expected
    # And the same aggregate in HAVING, outside any FILTER.
    stmt = _per_author().add_columns(total.filter(title_a).label("r")).having(total >= sa.bindparam("x"))
    assert sorted(execute(stmt, {"x": Decimal("51")})) == expected


def test_an_aggregate_compiled_in_two_statements_is_cast_in_each(shelf):
    _needs_filter()
    total = sa.func.sum(price)
    filtered = _per_author().add_columns(total.filter(title_a).label("r"))
    having = _per_author().having(total >= sa.bindparam("x"))
    if connection.vendor == "sqlite":
        assert _sql(filtered).count(f"(CAST({S} FILTER (WHERE") == 1
        assert f"HAVING {CAST_S} >= %s" in _sql(having)
    assert dict(execute(filtered))[shelf.id] == Decimal("60.00")
    assert _ids(having, {"x": Decimal("51")}) == [shelf.id]
