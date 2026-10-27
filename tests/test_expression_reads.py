"""A result column reads back as the ORM reads it: a column as a column, an expression as an expression (#68).

The ORM converts a selected column through ``Col`` and anything else (an
aggregate, arithmetic, ``CASE``, a window, a subquery) through the expression,
which has only its ``output_field``. The backend picks its converter by that:
SQLite quantizes a ``Col`` of a ``DecimalField`` to the field's
``decimal_places`` in a context of ``max_digits`` digits, and an expression only
to its 15 significant digits. Reading an expression as a column there rounded an
average (``30.33`` for ``30.3333333333333``) and raised ``InvalidOperation`` for
a sum with more digits than the field. A table column, through labels,
subqueries and CTEs, reads as a column; anything built on it as an expression.
"""

import datetime
import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from django.db.models import Avg, Case, F, OuterRef, Subquery, Sum, Value, When, Window
from django.db.models.functions import Coalesce

from django_sqlalchemy_bridge import bulk_insert, execute, execute_result, stream, table_for
from tests.testapp.models import Author, Book

pytestmark = pytest.mark.django_db

authors = table_for(Author)
books = table_for(Book)
price = books.c.price

AT = datetime.datetime(2026, 10, 25, 9, 30, 15, tzinfo=datetime.UTC)

# Book.price is DecimalField(max_digits=10, decimal_places=2).
BIG = Decimal("99999999.99")


@pytest.fixture
def shelf():
    """Author "a" has books for 30.00, 30.00 and 31.00 (no exact average), "b" two for BIG (a sum of 11 digits)."""
    a = Author.objects.create(name="a", joined_at=AT)
    b = Author.objects.create(name="b", joined_at=AT)
    for author, value in [(a, "30.00"), (a, "30.00"), (a, "31.00"), (b, BIG), (b, BIG)]:
        Book.objects.create(author=author, title=f"{author.name}{value}", price=Decimal(value))


def _typed(rows):
    """Rows with each value's type, so equal Decimals of another type or exponent differ."""
    return sorted(tuple((type(v), str(v)) for v in row) for row in rows)


def _check(got, expected, *, decimal=True):
    assert _typed(got) == _typed(expected)
    if decimal:
        assert {type(v) for row in got for v in row[1:]} <= {Decimal, type(None)}


# name -> (the ORM's annotation of a book, the bridge's column of books)
PER_BOOK = {
    "column": (F("price"), price),
    "label": (F("price"), price.label("p")),
    "product": (F("price") * 3, price * 3),
    "sum of columns": (F("price") + F("price"), price + price),
    "case": (
        Case(When(price__gt=30, then=F("price") * 2), default=Value(Decimal("0"))),
        sa.case((price > 30, price * 2), else_=0),
    ),
    "window": (
        Window(Sum("price"), partition_by=[F("author")]),
        sa.func.sum(price).over(partition_by=books.c.author_id),
    ),
}


@pytest.mark.parametrize("pair", PER_BOOK.values(), ids=PER_BOOK.keys())
def test_a_book_column_reads_as_the_orm_reads_it(shelf, pair):
    orm, bridge = pair
    expected = Book.objects.annotate(r=orm).values_list("id", "r")
    _check(execute(sa.select(books.c.id, bridge)), expected)


# name -> (the ORM's aggregate of an author's books, the bridge's of books.c.price)
PER_AUTHOR = {
    "sum": (Sum("books__price"), sa.func.sum(price)),
    # SQLAlchemy gives func.avg() no type; the ORM's Avg of a DecimalField is a DecimalField.
    "avg": (Avg("books__price"), sa.func.avg(price, type_=price.type)),
    "coalesce": (Coalesce(Sum("books__price"), Value(Decimal("0"))), sa.func.coalesce(sa.func.sum(price), 0)),
    "sum of a product": (Sum(F("books__price") * 3), sa.func.sum(price * 3)),
}


def _per_author(column):
    return sa.select(books.c.author_id, column).group_by(books.c.author_id)


@pytest.mark.parametrize("pair", PER_AUTHOR.values(), ids=PER_AUTHOR.keys())
def test_an_aggregate_reads_as_the_orm_reads_it(shelf, pair):
    orm, bridge = pair
    expected = Author.objects.filter(books__isnull=False).annotate(r=orm).values_list("id", "r")
    _check(execute(_per_author(bridge)), expected)


def test_a_whole_table_aggregate_reads_as_the_orm_reads_it(shelf):
    expected = Book.objects.aggregate(s=Sum("price"), a=Avg("price"))
    got = execute(sa.select(sa.func.sum(price), sa.func.avg(price, type_=price.type)))
    _check([(None, *got[0])], [(None, expected["s"], expected["a"])])


def test_a_derived_tables_columns_read_as_what_they_select(shelf):
    # The ORM's values("author").annotate(Sum(...)): an aggregate, however reached.
    expected = (
        Book.objects.values("author").annotate(t=Sum("price"), m=Sum(F("price") * 3)).values_list("author", "t", "m")
    )
    stmt = _per_author(sa.func.sum(price).label("total")).add_columns(sa.func.sum(price * 3).label("triple"))
    for derived in (stmt.subquery(), stmt.cte()):
        _check(execute(sa.select(derived.c.author_id, derived.c.total, derived.c.triple)), expected)


def test_a_derived_tables_column_of_a_column_reads_as_a_column(shelf):
    expected = Book.objects.values_list("id", "price")
    derived = sa.select(books.c.id, price.label("p")).cte()
    _check(execute(sa.select(derived.c.id, derived.c.p)), expected)


def test_a_scalar_subquery_reads_as_the_orms_subquery(shelf):
    total = Book.objects.filter(author=OuterRef("pk")).values("author").annotate(s=Sum("price")).values("s")
    expected = Author.objects.annotate(r=Subquery(total)).values_list("id", "r")
    correlated = sa.select(sa.func.sum(price)).where(books.c.author_id == authors.c.id).scalar_subquery()
    _check(execute(sa.select(authors.c.id, correlated.label("r"))), expected)


def test_a_column_and_an_expression_in_one_select_each_read_as_theirs(shelf):
    expected = Book.objects.annotate(r=F("price") * 3, s=F("price") + F("price")).values_list("id", "price", "r", "s")
    stmt = sa.select(books.c.id, price, price * 3, price + price)
    _check(execute(stmt), expected)
    _check([tuple(row) for row in execute_result(stmt)], expected)
    _check(list(stream(stmt, chunk_size=2)), expected)


def test_bulk_insert_returning_reads_a_column(needs_returning):
    author = Author.objects.create(name="a", joined_at=AT)
    rows = [
        {
            "author_id": author.pk,
            "title": f"t{i}",
            "price": Decimal(value),
            "uid": uuid.uuid4(),
            "status": "draft",
            "meta": {},
        }
        for i, value in enumerate(["30.00", BIG])
    ]
    got = bulk_insert(books, rows, returning=["id", "price"])
    _check(got, Book.objects.values_list("id", "price"))


def test_dml_returning_an_expression_reads_it_as_one(shelf, needs_update_returning):
    expected = Book.objects.annotate(r=F("price") * 3).values_list("id", "price", "r")
    got = execute(sa.update(books).values(title=books.c.title).returning(books.c.id, price, price * 3))
    _check(got, expected)


@pytest.mark.parametrize("column_first", [True, False], ids=["column first", "expression first"])
def test_a_union_reads_as_its_first_select(shelf, column_first):
    # The ORM reads a union by its first query's columns. Read as a column,
    # BIG * 2 has more digits than the field (InvalidOperation in the ORM too),
    # so the column-first union leaves author "b" out.
    small = Book.objects.filter(author__name="a") if column_first else Book.objects.all()
    column, expression = small.values_list("id", "price"), small.annotate(r=F("price") * 2).values_list("id", "r")
    expected = column.union(expression, all=True) if column_first else expression.union(column, all=True)
    selects = [_books_of("a" if column_first else None, c) for c in (price, price * 2)]
    # On Oracle the ORM gives price * 2 read as a column as int; so does the bridge.
    _check(execute(sa.union_all(*(selects if column_first else selects[::-1]))), expected, decimal=False)


def test_a_unions_subquery_column_of_a_column_and_an_expression_reads_as_an_expression(shelf):
    # SQLAlchemy proxies a union's subquery column from every SELECT's; the ORM
    # has no such column. Read as an expression, it neither rounds nor overflows.
    expected = (
        Book.objects.annotate(r=F("price") * 1)
        .values_list("id", "r")
        .union(Book.objects.annotate(r=F("price") * 2).values_list("id", "r"), all=True)
    )
    union = sa.union_all(_books_of(None, price), _books_of(None, price * 2)).subquery()
    _check(execute(sa.select(union)), expected)


def _books_of(name, column):
    stmt = sa.select(books.c.id, column).select_from(books.join(authors, authors.c.id == books.c.author_id))
    return stmt.where(authors.c.name == name) if name is not None else stmt
