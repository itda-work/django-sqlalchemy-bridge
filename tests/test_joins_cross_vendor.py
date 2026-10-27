"""``join_across`` between every ordered pair of SQLite, PostgreSQL, MySQL and Oracle (#87).

The keys are converted by the outer column's field and looked up again through the
inner column's, so a string, a ``Decimal``, an aware datetime and a UUID read on one
vendor must find the same value stored on another. "other" is the SQLite side; "pg",
"mysql" and "oracle" exist only with DSB_CROSS=1 or REQUIRE_CROSS=1 and all three
servers given (``tests/dbenv.py``).
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.conf import settings
from django.db import connections

from django_sqlalchemy_bridge import join_across, table_for
from tests.testapp.models import Author, Book, Category, PriceChange, Product

ALIASES = ["other", "pg", "mysql", "oracle"]
if not set(ALIASES) <= set(settings.DATABASES):
    pytest.skip(
        "cross-vendor aliases not configured (set DSB_CROSS=1, DSB_PG_HOST, DSB_MYSQL_HOST and DSB_ORACLE_HOST)",
        allow_module_level=True,
    )

PAIRS = [(a, b) for a in ALIASES for b in ALIASES if a != b]
# "default" too: every other alias's test database depends on it (TEST["DEPENDENCIES"]).
pytestmark = pytest.mark.django_db(databases=["default", *ALIASES])


def vendors(outer_db, inner_db):
    return f"{connections[outer_db].vendor}->{connections[inner_db].vendor}"


@pytest.mark.parametrize("outer_db,inner_db", PAIRS)
def test_string_and_decimal_keys(outer_db, inner_db):
    category = Category.objects.using(outer_db).create(name="c")
    Product.objects.using(outer_db).bulk_create(
        [
            Product(sku=sku, category=category, price=Decimal(price))
            for sku, price in [("JEANS", "99.00"), ("SHIRT", "27.50"), ("TEA", "8.50")]
        ]
    )
    PriceChange.objects.using(inner_db).bulk_create(
        [
            PriceChange(sku=sku, price=Decimal(price))
            for sku, price in [("SHIRT", "27.50"), ("TEA", "8.50"), ("CAP", "3.00")]
        ]
    )
    products, changes = table_for(Product), table_for(PriceChange)

    rows = join_across(
        sa.select(products.c.sku, products.c.price).order_by(products.c.sku),
        sa.select(changes.c.sku, changes.c.price),
        (products.c.sku, changes.c.sku),
        using=outer_db,
        inner_using=inner_db,
        how="left",
    )
    assert rows == [
        ("JEANS", Decimal("99.00"), None, None),
        ("SHIRT", Decimal("27.50"), "SHIRT", Decimal("27.50")),
        ("TEA", Decimal("8.50"), "TEA", Decimal("8.50")),
    ], vendors(outer_db, inner_db)

    rows = join_across(
        sa.select(products.c.sku, products.c.price),
        sa.select(changes.c.price, changes.c.sku),
        (products.c.price, changes.c.price),
        using=outer_db,
        inner_using=inner_db,
    )
    assert sorted(rows) == [
        ("SHIRT", Decimal("27.50"), Decimal("27.50"), "SHIRT"),
        ("TEA", Decimal("8.50"), Decimal("8.50"), "TEA"),
    ], vendors(outer_db, inner_db)
    assert all(type(r[1]) is Decimal and type(r[2]) is Decimal for r in rows), vendors(outer_db, inner_db)


@pytest.mark.parametrize("outer_db,inner_db", PAIRS)
def test_aware_datetime_and_uuid_keys(outer_db, inner_db):
    # Written in Seoul time on one side and New York time on the other: the same instant.
    at = datetime(2026, 10, 27, 12, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    a = Author.objects.using(outer_db).create(name="a", joined_at=at)
    b = Author.objects.using(inner_db).create(name="b", joined_at=at.astimezone(ZoneInfo("America/New_York")))
    Author.objects.using(inner_db).create(name="c", joined_at=at + timedelta(hours=1))
    authors = table_for(Author)

    rows = join_across(
        sa.select(authors.c.name, authors.c.joined_at),
        sa.select(authors.c.name, authors.c.joined_at),
        (authors.c.joined_at, authors.c.joined_at),
        using=outer_db,
        inner_using=inner_db,
    )
    assert [(r[0], r[2]) for r in rows] == [("a", "b")], vendors(outer_db, inner_db)
    assert rows[0][1] == at and rows[0][3] == at, vendors(outer_db, inner_db)
    assert rows[0][1].tzinfo is not None and rows[0][3].tzinfo is not None, vendors(outer_db, inner_db)

    shared, unmatched = uuid.uuid4(), uuid.uuid4()
    Book.objects.using(outer_db).create(uid=shared, author=a, title="o", price=Decimal("1"))
    Book.objects.using(inner_db).create(uid=shared, author=b, title="i", price=Decimal("2"))
    Book.objects.using(inner_db).create(uid=unmatched, author=b, title="x", price=Decimal("3"))
    books = table_for(Book)

    rows = join_across(
        sa.select(books.c.title, books.c.uid),
        sa.select(books.c.uid, books.c.title),
        (books.c.uid, books.c.uid),
        using=outer_db,
        inner_using=inner_db,
    )
    assert rows == [("o", shared, shared, "i")], vendors(outer_db, inner_db)
    assert type(rows[0][1]) is uuid.UUID and type(rows[0][2]) is uuid.UUID, vendors(outer_db, inner_db)
