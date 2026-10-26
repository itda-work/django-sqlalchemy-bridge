"""Queries that are hard to write with the Django ORM, written with the bridge.

Each test is a small scenario on the shop models (``Category``, ``Product``,
``Sale``, ``DailyStock``, ``PriceChange``) and checks the bridge's answer
against the ORM or against values worked out by hand. The code between
``# readme:NAME`` and ``# /readme:NAME`` is printed in README.md as it is here;
``tests/test_readme_examples.py`` fails when the two differ.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa
from django.db import connection, transaction
from sqlalchemy.dialects import mysql, postgresql, sqlite

from django_sqlalchemy_bridge import execute, table_for
from tests.testapp.models import Category, DailyStock, PriceChange, Product, Sale

pytestmark = pytest.mark.django_db

SEOUL = ZoneInfo("Asia/Seoul")
IS_PG = connection.vendor == "postgresql"
IS_MYSQL = connection.vendor == "mysql"
_NAMES = {"postgresql": "PostgreSQL", "sqlite": "SQLite", "mysql": "MySQL"}


def only_on(*vendors, syntax):
    names = " and ".join(_NAMES[vendor] for vendor in vendors)
    return pytest.mark.skipif(connection.vendor not in vendors, reason=f"{syntax}: {names} only")


def pg_only(syntax):
    return only_on("postgresql", syntax=syntax)


categories = table_for(Category)
products = table_for(Product)
sales = table_for(Sale)
daily_stock = table_for(DailyStock)
price_changes = table_for(PriceChange)


def at(day, hour):
    return datetime.datetime(2026, 10, day, hour, tzinfo=SEOUL)


@pytest.fixture
def shop():
    """전체 > (의류 > (상의, 하의), 식품), three products in the leaves, a few sales each."""
    root = Category.objects.create(name="전체")
    clothes = Category.objects.create(name="의류", parent=root)
    tops = Category.objects.create(name="상의", parent=clothes)
    bottoms = Category.objects.create(name="하의", parent=clothes)
    food = Category.objects.create(name="식품", parent=root)
    shirt = Product.objects.create(sku="SHIRT", category=tops, price=Decimal("30.00"))
    jeans = Product.objects.create(sku="JEANS", category=bottoms, price=Decimal("50.00"))
    tea = Product.objects.create(sku="TEA", category=food, price=Decimal("8.50"))
    for product, sold in [
        (shirt, [(24, 9, 1), (25, 10, 2), (26, 23, 1)]),
        (jeans, [(25, 11, 1), (26, 8, 3)]),
        (tea, [(24, 12, 10), (25, 13, 4), (26, 14, 6)]),
    ]:
        for day, hour, quantity in sold:
            Sale.objects.create(
                product=product, sold_at=at(day, hour), quantity=quantity, amount=product.price * quantity
            )
    return {c.name: c for c in (root, clothes, tops, bottoms, food)}


def test_recursive_cte_walks_a_category_subtree(shop):
    clothes = shop["의류"]
    # readme:recursive-cte
    categories = table_for(Category)

    tree = (
        sa.select(categories.c.id, categories.c.name, sa.literal_column("0").label("depth"))
        .where(categories.c.id == sa.bindparam("root"))
        .cte("tree", recursive=True)
    )
    tree = tree.union_all(
        sa.select(categories.c.id, categories.c.name, tree.c.depth + 1).join(tree, categories.c.parent_id == tree.c.id)
    )
    subtree = sa.select(tree.c.name, tree.c.depth).order_by(tree.c.depth, tree.c.name)

    rows = execute(subtree, {"root": clothes.pk})
    # [("의류", 0), ("상의", 1), ("하의", 1)]
    # /readme:recursive-cte
    assert rows == [("의류", 0), ("상의", 1), ("하의", 1)]
    assert execute(subtree, {"root": shop["전체"].pk}) == [
        ("전체", 0),
        ("식품", 1),
        ("의류", 1),
        ("상의", 2),
        ("하의", 2),
    ]


def test_recursive_cte_sums_sales_under_each_category(shop):
    # Every category with the revenue of its whole subtree: the closure (ancestor, descendant) is the recursive part.
    closure = sa.select(categories.c.id.label("ancestor"), categories.c.id.label("descendant")).cte(
        "closure", recursive=True
    )
    closure = closure.union_all(
        sa.select(closure.c.ancestor, categories.c.id).join(closure, categories.c.parent_id == closure.c.descendant)
    )
    revenue = sa.func.coalesce(sa.func.sum(sales.c.amount), 0)
    stmt = (
        sa.select(categories.c.name, revenue.label("revenue"))
        .select_from(categories)
        .join(closure, closure.c.ancestor == categories.c.id)
        .outerjoin(products, products.c.category_id == closure.c.descendant)
        .outerjoin(sales, sales.c.product_id == products.c.id)
        .group_by(categories.c.id, categories.c.name)
    )

    expected = {}
    for category in Category.objects.all():
        ids, frontier = {category.pk}, [category.pk]
        while frontier:
            frontier = list(Category.objects.filter(parent__in=frontier).values_list("pk", flat=True))
            ids.update(frontier)
        expected[category.name] = sum(s.amount for s in Sale.objects.filter(product__category__in=ids))
    assert dict(execute(stmt)) == expected
    assert expected == {
        "전체": Decimal("490.00"),
        "의류": Decimal("320.00"),
        "상의": Decimal("120.00"),
        "하의": Decimal("200.00"),
        "식품": Decimal("170.00"),
    }


def test_several_ctes_find_products_above_their_category_average(shop):
    Product.objects.create(sku="SOCKS", category=shop["하의"], price=Decimal("5.00"))
    Sale.objects.create(product=Product.objects.get(sku="SOCKS"), sold_at=at(26, 9), quantity=2, amount=Decimal("10"))

    revenue = (
        sa.select(products.c.id, products.c.category_id, sa.func.sum(sales.c.amount).label("revenue"))
        .join(sales, sales.c.product_id == products.c.id)
        .group_by(products.c.id, products.c.category_id)
        .cte("revenue")
    )
    category_avg = (
        sa.select(revenue.c.category_id, sa.func.avg(revenue.c.revenue).label("average"))
        .group_by(revenue.c.category_id)
        .cte("category_avg")
    )
    stmt = (
        sa.select(products.c.sku, revenue.c.revenue)
        .join(revenue, revenue.c.id == products.c.id)
        .join(category_avg, category_avg.c.category_id == revenue.c.category_id)
        .where(revenue.c.revenue > category_avg.c.average)
        .order_by(products.c.sku)
    )

    rows = execute(stmt)
    # sum() keeps the column's type, so the revenue converts like Sale.amount does.
    assert rows == [("JEANS", Decimal("200.00"))]
    assert isinstance(rows[0][1], Decimal)


def test_window_over_a_cte_ranks_products_by_revenue_in_their_category(shop):
    Product.objects.create(sku="SOCKS", category=shop["하의"], price=Decimal("5.00"))
    revenue = (
        sa.select(products.c.sku, products.c.category_id, sa.func.coalesce(sa.func.sum(sales.c.amount), 0).label("r"))
        .outerjoin(sales, sales.c.product_id == products.c.id)
        .group_by(products.c.sku, products.c.category_id)
        .cte("revenue")
    )
    rank = sa.func.rank().over(partition_by=revenue.c.category_id, order_by=revenue.c.r.desc())
    stmt = sa.select(revenue.c.sku, rank.label("rank")).order_by(revenue.c.sku)
    assert execute(stmt) == [("JEANS", 1), ("SHIRT", 1), ("SOCKS", 2), ("TEA", 1)]


@only_on("postgresql", "mysql", syntax="LATERAL")
def test_lateral_join_takes_the_two_largest_sales_of_each_product(shop):
    top = (
        sa.select(sales.c.sold_at, sales.c.amount)
        .where(sales.c.product_id == products.c.id)
        .order_by(sales.c.amount.desc(), sales.c.sold_at)
        .limit(2)
        .lateral("top")
    )
    stmt = (
        sa.select(products.c.sku, top.c.sold_at, top.c.amount)
        .select_from(products)
        .join(top, sa.true())
        .order_by(products.c.sku, top.c.amount.desc(), top.c.sold_at)
    )

    expected = [
        (product.sku, sale.sold_at, sale.amount)
        for product in Product.objects.order_by("sku")
        for sale in product.sales.order_by("-amount", "sold_at")[:2]
    ]
    rows = execute(stmt)
    assert rows == expected
    assert rows[0] == ("JEANS", at(26, 8), Decimal("150.00"))
    assert rows[0][1].utcoffset() == datetime.timedelta(0)


@pg_only("DISTINCT ON")
def test_distinct_on_takes_the_latest_sale_of_each_product(shop):
    stmt = sa.select(sales.c.product_id, sales.c.sold_at, sales.c.quantity).order_by(
        sales.c.product_id, sales.c.sold_at.desc()
    )
    # SQLAlchemy 2.1 deprecates select().distinct(*columns) for the distinct_on() extension 2.0 lacks.
    if hasattr(postgresql, "distinct_on"):
        stmt = stmt.ext(postgresql.distinct_on(sales.c.product_id))
    else:  # pragma: no cover - SQLAlchemy 2.0
        stmt = stmt.distinct(sales.c.product_id)

    orm = Sale.objects.order_by("product_id", "-sold_at").distinct("product_id")
    assert execute(stmt) == list(orm.values_list("product_id", "sold_at", "quantity"))
    assert [quantity for _, _, quantity in execute(stmt)] == [1, 3, 6]


def upsert_insert():
    """The INSERT construct with ON CONFLICT for the default connection's backend."""
    return postgresql.insert if connection.vendor == "postgresql" else sqlite.insert


@only_on("postgresql", "sqlite", syntax="ON CONFLICT and RETURNING")
def test_insert_on_conflict_do_update_returning(shop):
    shirt = Product.objects.get(sku="SHIRT")
    DailyStock.objects.create(product=shirt, day=datetime.date(2026, 10, 26), quantity=7, counted_at=at(26, 9))

    # readme:upsert
    daily_stock = table_for(DailyStock)
    insert = postgresql.insert if connection.vendor == "postgresql" else sqlite.insert

    stmt = insert(daily_stock)  # 컬럼은 아래 params 가 정한다
    stmt = stmt.on_conflict_do_update(
        index_elements=[daily_stock.c.product_id, daily_stock.c.day],
        set_={"quantity": daily_stock.c.quantity + stmt.excluded.quantity, "counted_at": stmt.excluded.counted_at},
    ).returning(daily_stock.c.day, daily_stock.c.quantity, daily_stock.c.counted_at)

    counted_at = datetime.datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Seoul"))
    params = {"product_id": shirt.pk, "day": counted_at.date(), "quantity": 3, "counted_at": counted_at}
    with transaction.atomic():
        rows = execute(stmt, params)
    # [(datetime.date(2026, 10, 26), 10, datetime.datetime(2026, 10, 26, 9, 0, tzinfo=datetime.timezone.utc))]
    # /readme:upsert
    assert rows == [(datetime.date(2026, 10, 26), 10, counted_at)]
    assert rows[0][2].utcoffset() == datetime.timedelta(0)

    # The same statement inserts when there is no row yet, and the ORM reads back what the bridge wrote.
    tea = Product.objects.get(sku="TEA")
    later = at(27, 1)
    assert execute(stmt, {"product_id": tea.pk, "day": later.date(), "quantity": 5, "counted_at": later}) == [
        (datetime.date(2026, 10, 27), 5, later)
    ]
    assert sorted(DailyStock.objects.values_list("product__sku", "day", "quantity", "counted_at")) == [
        ("SHIRT", datetime.date(2026, 10, 26), 10, counted_at),
        ("TEA", datetime.date(2026, 10, 27), 5, later),
    ]


@only_on("mysql", syntax="ON DUPLICATE KEY UPDATE")
def test_insert_on_duplicate_key_update_on_mysql(shop):
    shirt = Product.objects.get(sku="SHIRT")
    DailyStock.objects.create(product=shirt, day=datetime.date(2026, 10, 26), quantity=7, counted_at=at(26, 9))

    # readme:upsert-mysql
    daily_stock = table_for(DailyStock)

    stmt = mysql.insert(daily_stock)  # 컬럼은 아래 params 가 정한다
    stmt = stmt.on_duplicate_key_update(
        quantity=daily_stock.c.quantity + stmt.inserted.quantity, counted_at=stmt.inserted.counted_at
    )

    counted_at = datetime.datetime(2026, 10, 26, 18, tzinfo=ZoneInfo("Asia/Seoul"))
    params = {"product_id": shirt.pk, "day": counted_at.date(), "quantity": 3, "counted_at": counted_at}
    with transaction.atomic():
        count = execute(stmt, params)  # 2: MySQL 은 갱신한 행을 2, 넣은 행을 1 로 센다
    # /readme:upsert-mysql
    assert count == 2

    tea = Product.objects.get(sku="TEA")
    later = at(27, 1)
    assert execute(stmt, {"product_id": tea.pk, "day": later.date(), "quantity": 5, "counted_at": later}) == 1
    assert sorted(DailyStock.objects.values_list("product__sku", "day", "quantity", "counted_at")) == [
        ("SHIRT", datetime.date(2026, 10, 26), 10, counted_at),
        ("TEA", datetime.date(2026, 10, 27), 5, later),
    ]


@only_on("postgresql", "sqlite", syntax="ON CONFLICT and RETURNING")
def test_insert_on_conflict_do_nothing_reports_what_it_skipped(shop):
    shirt = Product.objects.get(sku="SHIRT")
    day = datetime.date(2026, 10, 26)
    DailyStock.objects.create(product=shirt, day=day, quantity=7, counted_at=at(26, 9))

    stmt = upsert_insert()(daily_stock).on_conflict_do_nothing(
        index_elements=[daily_stock.c.product_id, daily_stock.c.day]
    )
    rows = [
        {"product_id": shirt.pk, "day": day, "quantity": 1, "counted_at": at(26, 10)},
        {"product_id": shirt.pk, "day": datetime.date(2026, 10, 27), "quantity": 2, "counted_at": at(27, 10)},
    ]
    assert execute(stmt.returning(daily_stock.c.day), rows[0]) == []
    assert execute(stmt.returning(daily_stock.c.day), rows[1]) == [(datetime.date(2026, 10, 27),)]
    assert list(DailyStock.objects.order_by("day").values_list("quantity", flat=True)) == [7, 2]


def test_insert_select_with_on_conflict_rolls_sales_up_into_daily_stock(shop):
    # One row per product and Seoul day. SQLite reads the ON of ON CONFLICT as a join constraint when the
    # SELECT ends in "FROM t" (no WHERE, no GROUP BY); a WHERE avoids that.
    day = datetime.date(2026, 10, 25)
    start = datetime.datetime.combine(day, datetime.time(), SEOUL)
    in_day = sa.and_(sales.c.sold_at >= start, sales.c.sold_at < start + datetime.timedelta(days=1))
    rollup = (
        sa.select(
            sales.c.product_id,
            sa.literal(day, daily_stock.c.day.type),
            sa.func.sum(sales.c.quantity),
            sa.literal(at(26, 0), daily_stock.c.counted_at.type),
        )
        .where(in_day)
        .group_by(sales.c.product_id)
    )
    names = ["product_id", "day", "quantity", "counted_at"]
    if IS_MYSQL:
        stmt = mysql.insert(daily_stock).from_select(names, rollup)
        stmt = stmt.on_duplicate_key_update(quantity=stmt.inserted.quantity, counted_at=stmt.inserted.counted_at)
    else:
        stmt = upsert_insert()(daily_stock).from_select(names, rollup)
        stmt = stmt.on_conflict_do_update(
            index_elements=[daily_stock.c.product_id, daily_stock.c.day],
            set_={"quantity": stmt.excluded.quantity, "counted_at": stmt.excluded.counted_at},
        )

    tea = Product.objects.get(sku="TEA")
    DailyStock.objects.create(product=tea, day=day, quantity=99, counted_at=at(25, 0))
    # Two inserted rows and one updated: MySQL counts the updated one twice.
    assert execute(stmt) == (4 if IS_MYSQL else 3)
    assert sorted(DailyStock.objects.values_list("product__sku", "day", "quantity", "counted_at")) == [
        ("JEANS", day, 1, at(26, 0)),
        ("SHIRT", day, 2, at(26, 0)),
        ("TEA", day, 4, at(26, 0)),
    ]


def test_insert_from_select_copies_rows_inside_the_django_transaction(shop):
    # Untyped values in the SELECT list pass through raw, so give them the target column's type.
    totals = sa.select(
        sales.c.product_id,
        sa.literal(datetime.date(2026, 10, 31), daily_stock.c.day.type),
        sa.func.sum(sales.c.quantity),
        sa.literal(at(31, 23), daily_stock.c.counted_at.type),
    ).group_by(sales.c.product_id)
    stmt = sa.insert(daily_stock).from_select(["product_id", "day", "quantity", "counted_at"], totals)

    with pytest.raises(RuntimeError), transaction.atomic():
        assert execute(stmt) == 3
        assert DailyStock.objects.count() == 3
        raise RuntimeError("roll back")
    assert DailyStock.objects.count() == 0

    execute(stmt)
    assert sorted(DailyStock.objects.values_list("product__sku", "day", "quantity", "counted_at")) == [
        ("JEANS", datetime.date(2026, 10, 31), 4, at(31, 23)),
        ("SHIRT", datetime.date(2026, 10, 31), 4, at(31, 23)),
        ("TEA", datetime.date(2026, 10, 31), 20, at(31, 23)),
    ]


@only_on("postgresql", "sqlite", syntax="UPDATE ... RETURNING")
def test_update_from_applies_a_staged_price_list(shop):
    PriceChange.objects.create(sku="SHIRT", price=Decimal("27.50"))
    PriceChange.objects.create(sku="TEA", price=Decimal("8.50"))  # unchanged: not updated, not returned
    PriceChange.objects.create(sku="GONE", price=Decimal("1.00"))  # no such product

    # readme:update-from
    products = table_for(Product)
    changes = table_for(PriceChange)

    stmt = (
        sa.update(products)
        .where(products.c.sku == changes.c.sku, products.c.price != changes.c.price)
        .values(price=changes.c.price)
        .returning(products.c.sku, products.c.price)
    )
    changed = execute(stmt)  # [("SHIRT", Decimal("27.50"))]
    # /readme:update-from
    assert changed == [("SHIRT", Decimal("27.50"))]
    assert dict(Product.objects.values_list("sku", "price")) == {
        "SHIRT": Decimal("27.50"),
        "JEANS": Decimal("50.00"),
        "TEA": Decimal("8.50"),
    }


@only_on("mysql", syntax="multiple-table UPDATE")
def test_multiple_table_update_applies_a_staged_price_list_on_mysql(shop):
    PriceChange.objects.create(sku="SHIRT", price=Decimal("27.50"))
    PriceChange.objects.create(sku="TEA", price=Decimal("8.50"))  # unchanged: not counted
    PriceChange.objects.create(sku="GONE", price=Decimal("1.00"))  # no such product
    # The same statement as the UPDATE ... FROM example, without RETURNING: MySQL writes "UPDATE products, changes".
    stmt = (
        sa.update(products)
        .where(products.c.sku == price_changes.c.sku, products.c.price != price_changes.c.price)
        .values(price=price_changes.c.price)
    )
    assert execute(stmt) == 1
    assert dict(Product.objects.values_list("sku", "price")) == {
        "SHIRT": Decimal("27.50"),
        "JEANS": Decimal("50.00"),
        "TEA": Decimal("8.50"),
    }


@pg_only("column list on a VALUES alias")
def test_update_from_values_on_postgresql(shop):
    # SQLite rejects the column list SQLAlchemy puts after "(VALUES ...) AS v"; on SQLite stage the rows in a table.
    staged = sa.values(
        sa.column("sku", products.c.sku.type), sa.column("price", products.c.price.type), name="staged"
    ).data([("SHIRT", Decimal("29.00")), ("TEA", Decimal("9.00"))])
    stmt = (
        sa.update(products)
        .where(products.c.sku == staged.c.sku)
        .values(price=staged.c.price)
        .returning(products.c.sku, products.c.price)
    )
    assert sorted(execute(stmt)) == [("SHIRT", Decimal("29.00")), ("TEA", Decimal("9.00"))]
    assert Product.objects.get(sku="JEANS").price == Decimal("50.00")


@pg_only("DML inside a CTE")
def test_data_modifying_cte_moves_rows_in_one_statement(shop):
    # DELETE ... RETURNING inside WITH, feeding an INSERT: SQLite allows no DML in a CTE.
    moved = (
        sa.delete(sales)
        .where(sales.c.sold_at < at(25, 0))
        .returning(sales.c.product_id, sales.c.sold_at, sales.c.quantity)
        .cte("moved")
    )
    archive = sa.select(moved.c.product_id, sa.cast(moved.c.sold_at, sa.Date), moved.c.quantity, moved.c.sold_at)
    stmt = sa.insert(daily_stock).from_select(["product_id", "day", "quantity", "counted_at"], archive)

    assert execute(stmt) == 2
    assert Sale.objects.filter(sold_at__lt=at(25, 0)).count() == 0
    assert sorted(DailyStock.objects.values_list("product__sku", "quantity", "counted_at")) == [
        ("SHIRT", 1, at(24, 9)),
        ("TEA", 10, at(24, 12)),
    ]


def test_untyped_values_in_a_select_list_skip_the_field_conversion(shop):
    # Why the INSERT ... SELECT examples type their literals: an untyped aware datetime reaches the driver as is,
    # and SQLite stores it with its offset instead of in UTC as the ORM does (it still reads back as the same
    # instant); MySQL stores its wall time, a different instant.
    stmt = sa.insert(daily_stock).from_select(
        ["product_id", "day", "quantity", "counted_at"],
        sa.select(products.c.id, sa.literal(datetime.date(2026, 10, 31)), sa.literal(1), sa.literal(at(31, 23))),
    )
    execute(stmt)
    stored = execute(sa.select(sa.cast(daily_stock.c.counted_at, sa.String)).limit(1))[0][0]
    if IS_PG:
        assert stored.endswith("+00")  # PostgreSQL stores timestamptz as an instant either way
    elif IS_MYSQL:
        # MySQLdb drops the offset: the Seoul wall time is stored, which Django reads as UTC (9 hours late).
        assert stored == "2026-10-31 23:00:00.000000"
        assert DailyStock.objects.filter(counted_at=at(31, 23)).count() == 0
    else:
        assert stored == "2026-10-31 23:00:00+09:00"
        assert DailyStock.objects.filter(counted_at=at(31, 23)).count() == 0  # the ORM compares UTC text


def test_update_from_an_aggregate_subquery(shop):
    # Set each product's price to its average sale price per unit since the 25th; TEA sold 10 more at a discount.
    Sale.objects.create(product=Product.objects.get(sku="TEA"), sold_at=at(26, 20), quantity=10, amount=Decimal("50"))
    unit = (
        sa.select(sales.c.product_id, (sa.func.sum(sales.c.amount) / sa.func.sum(sales.c.quantity)).label("unit"))
        .where(sales.c.sold_at >= at(25, 0))
        .group_by(sales.c.product_id)
        .subquery("unit")
    )
    stmt = (
        sa.update(products)
        .where(products.c.id == unit.c.product_id)
        .values(price=sa.cast(unit.c.unit, products.c.price.type))
    )
    assert execute(stmt) == 3
    assert dict(Product.objects.values_list("sku", "price")) == {
        "SHIRT": Decimal("30.00"),
        "JEANS": Decimal("50.00"),
        "TEA": Decimal("6.75"),
    }
