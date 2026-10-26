"""The ORM and bridge implementations of each request in tests/comparisons.py agree, in a fixed number of queries.

Each test runs one implementation and the request's reference (its first ORM
implementation, which runs everywhere) on the same data, each inside a
transaction that is rolled back: both must return the same result and leave
the same rows. Query counts leave out savepoints. An implementation is skipped
only where its database lacks the syntax, and the reason says which.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.db import connection, transaction

from tests import comparisons as cmp
from tests.testapp.models import Category, DailyStock, PriceChange, Product, Sale, SaleArchive

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
_NAMES = {"postgresql": "PostgreSQL", "sqlite": "SQLite", "mysql": "MySQL", "oracle": "Oracle"}


def at(day, hour, minute=0):
    return datetime.datetime(2026, 10, day, hour, minute, tzinfo=SEOUL)


def only_on(*vendors, syntax):
    """The skip reason on a database without ``syntax``, else None."""
    if connection.vendor in vendors:
        return None
    return f"{syntax}: {', '.join(_NAMES[v] for v in vendors)} only"


LATERAL = only_on("postgresql", "mysql", "oracle", syntax="LATERAL")
ON_CONFLICT = only_on("postgresql", "sqlite", syntax="ON CONFLICT ... RETURNING")
# MySQL's ON DUPLICATE KEY UPDATE takes no unique_fields; Oracle has no bulk_create(update_conflicts=True).
UPDATE_CONFLICTS = only_on("postgresql", "sqlite", syntax="bulk_create(update_conflicts=True, unique_fields=...)")
UPDATE_RETURNING = only_on("postgresql", "sqlite", syntax="UPDATE ... FROM ... RETURNING")
DML_IN_CTE = only_on("postgresql", syntax="DML in a CTE")


def run(fn, *args):
    """``fn(*args)``, its query count and the rows it leaves, in a transaction that is rolled back."""
    with transaction.atomic():
        result, queries = cmp.count_queries(fn, *args)
        state = {
            "stock": sorted(DailyStock.objects.values_list("product_id", "day", "quantity", "counted_at")),
            "prices": sorted(Product.objects.values_list("sku", "price")),
            "sales": sorted(Sale.objects.values_list("id", "product_id", "sold_at", "quantity", "amount")),
            "archive": sorted(SaleArchive.objects.values_list("id", "product_id", "sold_at", "quantity", "amount")),
        }
        transaction.set_rollback(True)
    return result, queries, state


def check(request, name, reference, queries, *args, skip=None):
    """Implementation ``<request>_<name>`` agrees with ``<request>_<reference>`` in ``queries`` queries."""
    if skip:
        pytest.skip(skip)
    result, count, state = run(getattr(cmp, f"{request}_{name}"), *args)
    expected, _, expected_state = run(getattr(cmp, f"{request}_{reference}"), *args)
    assert result == expected
    assert state == expected_state
    assert count == queries
    return result, state


@pytest.fixture
def shop():
    """전체 > 의류 > (상의 > (반팔, 긴팔), 하의), 식품 > 차; products in leaves and in 상의, 식품 itself; sales."""
    root = Category.objects.create(name="전체")
    clothes = Category.objects.create(name="의류", parent=root)
    tops = Category.objects.create(name="상의", parent=clothes)
    short = Category.objects.create(name="반팔", parent=tops)
    long_ = Category.objects.create(name="긴팔", parent=tops)
    bottoms = Category.objects.create(name="하의", parent=clothes)
    food = Category.objects.create(name="식품", parent=root)
    tea = Category.objects.create(name="차", parent=food)
    sold = {
        # sku: (category, price, [(day, hour, quantity)])
        "TEE": (short, "10.00", [(24, 9, 2), (25, 10, 1), (26, 11, 2)]),
        "POLO": (short, "25.00", [(25, 9, 1), (26, 9, 1)]),
        "SHIRT": (long_, "30.00", [(24, 9, 1), (25, 10, 2), (26, 23, 1), (26, 23, 1)]),  # two at the same time
        "KNIT": (tops, "45.00", [(23, 15, 1)]),
        "JEANS": (bottoms, "50.00", [(25, 11, 1), (26, 8, 3)]),
        "GREEN": (tea, "8.50", [(24, 12, 10), (25, 13, 4), (26, 14, 6)]),
        "BLACK": (tea, "9.00", [(22, 10, 3)]),
        "SNACK": (food, "3.00", []),  # never sold
    }
    for sku, (category, price, sales) in sold.items():
        product = Product.objects.create(sku=sku, category=category, price=Decimal(price))
        for day, hour, quantity in sales:
            Sale.objects.create(
                product=product, sold_at=at(day, hour), quantity=quantity, amount=product.price * quantity
            )
    return {c.name: c for c in (root, clothes, tops, short, long_, bottoms, food, tea)}


@pytest.mark.parametrize("name", ["orm_levels", "orm_python", "bridge"])
@pytest.mark.parametrize(
    ("root", "revenue", "levels"),
    [("전체", "692.00", 3), ("의류", "495.00", 2), ("상의", "295.00", 1), ("차", "197.00", 0), ("반팔", "100.00", 0)],
)
def test_subtree_revenue(shop, name, root, revenue, levels):
    # orm_levels: one query per level below the root, one more that finds the next level empty, then the sum.
    queries = {"orm_levels": levels + 2, "orm_python": 2, "bridge": 1}[name]
    total, _ = check("subtree_revenue", name, "orm_levels", queries, shop[root].pk)
    assert total == Decimal(revenue)


@pytest.mark.parametrize(("name", "queries"), [("orm_levels", 2), ("orm_python", 2), ("bridge", 1)])
def test_subtree_revenue_of_a_category_with_no_sales(shop, name, queries):
    empty = Category.objects.create(name="빈", parent=shop["식품"])
    total, _ = check("subtree_revenue", name, "orm_levels", queries, empty.pk)
    assert total == 0


@pytest.mark.parametrize(
    ("name", "queries", "skip"), [("orm_window", 1, None), ("orm_prefetch", 2, None), ("bridge", 1, LATERAL)]
)
def test_latest_sales(shop, name, queries, skip):
    rows, _ = check("latest_sales", name, "orm_window", queries, skip=skip)
    skus = ["BLACK", "GREEN", "GREEN", "JEANS", "JEANS", "KNIT", "POLO", "POLO", "SHIRT", "SHIRT", "TEE", "TEE"]
    assert [sku for sku, _, _ in rows] == skus
    # SHIRT sold twice at 23:00 on the 26th: both are newer than its sale on the 25th.
    assert [(sold_at, amount) for sku, sold_at, amount in rows if sku == "SHIRT"] == [(at(26, 23), Decimal("30"))] * 2
    assert all(sold_at.utcoffset() == datetime.timedelta(0) for _, sold_at, _ in rows)


@pytest.mark.parametrize(
    ("name", "queries", "skip"),
    # orm_loop: an UPDATE per row, an INSERT per new row, one read back.
    [("orm_loop", 4 + 2 + 1, None), ("orm_bulk", 2, UPDATE_CONFLICTS), ("bridge", 1, ON_CONFLICT)],
)
def test_add_stock(shop, name, queries, skip):
    tee, polo, jeans = (Product.objects.get(sku=sku).pk for sku in ("TEE", "POLO", "JEANS"))
    day, next_day = datetime.date(2026, 10, 26), datetime.date(2026, 10, 27)
    DailyStock.objects.create(product_id=tee, day=day, quantity=7, counted_at=at(26, 9))
    DailyStock.objects.create(product_id=polo, day=day, quantity=1, counted_at=at(26, 9))
    DailyStock.objects.create(product_id=jeans, day=datetime.date(2026, 10, 25), quantity=4, counted_at=at(25, 9))
    counts = [
        {"product_id": tee, "day": day, "quantity": 3, "counted_at": at(26, 18)},  # 7 + 3
        {"product_id": polo, "day": day, "quantity": 2, "counted_at": at(26, 18)},  # 1 + 2
        {"product_id": jeans, "day": day, "quantity": 5, "counted_at": at(26, 18)},  # new; the 25th's row stays
        {"product_id": jeans, "day": next_day, "quantity": 1, "counted_at": at(26, 18, 30)},  # new
    ]
    rows, state = check("add_stock", name, "orm_loop", queries, counts, skip=skip)
    assert rows == [
        (tee, day, 10, at(26, 18)),
        (polo, day, 3, at(26, 18)),
        (jeans, day, 5, at(26, 18)),
        (jeans, next_day, 1, at(26, 18, 30)),
    ]
    assert sorted(rows + [(jeans, datetime.date(2026, 10, 25), 4, at(25, 9))]) == state["stock"]


@pytest.mark.parametrize(
    ("name", "queries", "skip"),
    [("orm_subquery", 2, None), ("orm_bulk_update", 2, None), ("bridge", 1, UPDATE_RETURNING)],
)
def test_apply_prices(shop, name, queries, skip):
    PriceChange.objects.create(sku="SHIRT", price=Decimal("27.50"))
    PriceChange.objects.create(sku="GREEN", price=Decimal("9.00"))
    PriceChange.objects.create(sku="TEE", price=Decimal("10.00"))  # unchanged: not updated, not returned
    PriceChange.objects.create(sku="GONE", price=Decimal("1.00"))  # no such product
    changed, state = check("apply_prices", name, "orm_subquery", queries, skip=skip)
    assert changed == [("GREEN", Decimal("9.00")), ("SHIRT", Decimal("27.50"))]
    assert dict(state["prices"]) == {
        "TEE": Decimal("10.00"),
        "POLO": Decimal("25.00"),
        "SHIRT": Decimal("27.50"),
        "KNIT": Decimal("45.00"),
        "JEANS": Decimal("50.00"),
        "GREEN": Decimal("9.00"),
        "BLACK": Decimal("9.00"),
        "SNACK": Decimal("3.00"),
    }


@pytest.mark.parametrize(
    ("name", "queries", "skip"),
    # bulk_update() of no objects sends nothing.
    [("orm_subquery", 2, None), ("orm_bulk_update", 1, None), ("bridge", 1, UPDATE_RETURNING)],
)
def test_apply_prices_with_nothing_to_change(shop, name, queries, skip):
    PriceChange.objects.create(sku="TEE", price=Decimal("10.00"))
    changed, _ = check("apply_prices", name, "orm_subquery", queries, skip=skip)
    assert changed == []


@pytest.mark.parametrize(
    ("name", "queries", "skip"), [("orm", 3, None), ("bridge", 2, None), ("bridge_cte", 1, DML_IN_CTE)]
)
def test_archive_sales(shop, name, queries, skip):
    cutoff = at(25, 0)
    moved, state = check("archive_sales", name, "orm", queries, cutoff, skip=skip)
    assert moved == 5
    assert [sold_at for _, _, sold_at, _, _ in sorted(state["archive"], key=lambda row: row[2])] == [
        at(22, 10),
        at(23, 15),
        at(24, 9),
        at(24, 9),
        at(24, 12),
    ]
    assert len(state["sales"]) == 11
    assert all(sold_at >= cutoff for _, _, sold_at, _, _ in state["sales"])


@pytest.mark.parametrize("name", ["orm", "bridge"])
def test_category_report(shop, name):
    rows, _ = check("category_report", name, "orm", 1)
    by_sku = {sku: (category, revenue, rank, running) for category, sku, revenue, rank, running in rows}
    assert len(rows) == len(by_sku) == 8
    # 반팔: TEE 50 and POLO 50 tie for first; the running total breaks the tie by sku.
    assert by_sku["POLO"] == ("반팔", Decimal("50.00"), 1, Decimal("50.00"))
    assert by_sku["TEE"] == ("반팔", Decimal("50.00"), 1, Decimal("100.00"))
    # 식품: SNACK, alone there, never sold: its revenue is 0, not NULL.
    assert by_sku["SNACK"] == ("식품", Decimal("0"), 1, Decimal("0"))
    assert by_sku["GREEN"] == ("차", Decimal("170.00"), 1, Decimal("170.00"))
    assert by_sku["BLACK"] == ("차", Decimal("27.00"), 2, Decimal("197.00"))
