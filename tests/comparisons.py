"""Six requests on the shop models, each written with the Django ORM alone and with the bridge.

The code between ``# readme:NAME`` and ``# /readme:NAME`` is printed in
docs/comparisons.md (and some of it in README.md) as it is here;
tests/test_readme_examples.py fails when they differ.
tests/test_comparisons.py checks that the implementations of one request give
the same answer and fixes their query counts; bench/scenarios/compare.py times
them. The ORM side is the best plain Django 5.2 / 6.1 code we know for the
request, not a strawman.
"""

from collections import defaultdict
from decimal import Decimal

import sqlalchemy as sa
from django.db import connection, transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Subquery, Sum, Window
from django.db.models.functions import Coalesce, Rank, RowNumber
from sqlalchemy.dialects import postgresql, sqlite

from django_sqlalchemy_bridge import execute, table_for
from tests.testapp.models import Category, DailyStock, PriceChange, Product, Sale, SaleArchive

categories = table_for(Category)
products = table_for(Product)
sales = table_for(Sale)
daily_stock = table_for(DailyStock)
price_changes = table_for(PriceChange)
archive = table_for(SaleArchive)


# 1. The revenue of a category's whole subtree.


# readme:cmp-tree-orm-levels
def subtree_revenue_orm_levels(root_id):
    # 한 단계에 쿼리 하나씩 내려가며 하위 카테고리 id 를 모은다
    ids, level = [root_id], [root_id]
    while level:
        level = list(Category.objects.filter(parent_id__in=level).values_list("id", flat=True))
        ids += level
    total = Sale.objects.filter(product__category_id__in=ids).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


# /readme:cmp-tree-orm-levels


# readme:cmp-tree-orm-python
def subtree_revenue_orm_python(root_id):
    # 카테고리 전체의 (id, parent_id) 를 한 번에 읽어 파이썬에서 트리를 걷는다
    children = defaultdict(list)
    for category_id, parent_id in Category.objects.values_list("id", "parent_id"):
        children[parent_id].append(category_id)
    ids, stack = [], [root_id]
    while stack:
        ids.append(stack.pop())
        stack += children[ids[-1]]
    total = Sale.objects.filter(product__category_id__in=ids).aggregate(total=Sum("amount"))["total"]
    return total or Decimal("0")


# /readme:cmp-tree-orm-python


# readme:cmp-tree-bridge
tree = sa.select(categories.c.id).where(categories.c.id == sa.bindparam("root")).cte("tree", recursive=True)
tree = tree.union_all(sa.select(categories.c.id).join(tree, categories.c.parent_id == tree.c.id))
subtree_revenue = (
    sa.select(sa.func.coalesce(sa.func.sum(sales.c.amount), 0))
    .join(products, products.c.id == sales.c.product_id)
    .where(products.c.category_id.in_(sa.select(tree.c.id)))
)


def subtree_revenue_bridge(root_id):
    [(total,)] = execute(subtree_revenue, {"root": root_id})
    return total


# /readme:cmp-tree-bridge


# 2. The two newest sales of every product.


# readme:cmp-top2-orm-window
def latest_sales_orm_window():
    # Django 4.2+ 는 윈도 함수로 거를 수 있다(바깥 쿼리로 감싼다)
    newest_first = Window(RowNumber(), partition_by=F("product_id"), order_by=[F("sold_at").desc(), F("id").desc()])
    rows = (
        Sale.objects.annotate(n=newest_first)
        .filter(n__lte=2)
        .order_by("product__sku", "-sold_at", "-id")
        .values_list("product__sku", "sold_at", "amount")
    )
    return list(rows)


# /readme:cmp-top2-orm-window


# readme:cmp-top2-orm-prefetch
def latest_sales_orm_prefetch():
    # Django 4.2+ 는 잘라 낸 prefetch 를 상품별 윈도 함수로 한 쿼리에 읽는다
    two = Prefetch("sales", queryset=Sale.objects.order_by("-sold_at", "-id")[:2], to_attr="newest")
    return [
        (p.sku, s.sold_at, s.amount) for p in Product.objects.order_by("sku").prefetch_related(two) for s in p.newest
    ]


# /readme:cmp-top2-orm-prefetch


# readme:cmp-top2-bridge
newest = (
    sa.select(sales.c.id, sales.c.sold_at, sales.c.amount)
    .where(sales.c.product_id == products.c.id)
    .order_by(sales.c.sold_at.desc(), sales.c.id.desc())
    .limit(2)
    .lateral("newest")
)
latest_sales = (
    sa.select(products.c.sku, newest.c.sold_at, newest.c.amount)
    .select_from(products)
    .join(newest, sa.true())
    .order_by(products.c.sku, newest.c.sold_at.desc(), newest.c.id.desc())
)


def latest_sales_bridge():  # LATERAL: PostgreSQL, MySQL, Oracle (SQLite 에는 없다)
    return execute(latest_sales)


# /readme:cmp-top2-bridge


# 3. Add counted stock to (product, day) rows, inserting the missing ones, and return the rows.


# readme:cmp-upsert-orm-loop
def add_stock_orm_loop(counts):
    # 행마다 UPDATE(quantity + n), 맞는 행이 없으면 INSERT. 끝에 결과를 다시 읽는다
    with transaction.atomic():
        for c in counts:
            found = DailyStock.objects.filter(product_id=c["product_id"], day=c["day"]).update(
                quantity=F("quantity") + c["quantity"], counted_at=c["counted_at"]
            )
            if not found:
                DailyStock.objects.create(**c)
        keys = {(c["product_id"], c["day"]) for c in counts}
        rows = DailyStock.objects.filter(product_id__in={p for p, _ in keys}, day__in={d for _, d in keys}).values_list(
            "product_id", "day", "quantity", "counted_at"
        )
        return sorted(row for row in rows if row[:2] in keys)


# /readme:cmp-upsert-orm-loop


# readme:cmp-upsert-orm-bulk
def add_stock_orm_bulk(counts):
    # 있는 행을 잠가 읽고 파이썬에서 더한 뒤 INSERT ... ON CONFLICT DO UPDATE 한 번으로 쓴다
    with transaction.atomic():
        have = DailyStock.objects.select_for_update().filter(
            product_id__in={c["product_id"] for c in counts}, day__in={c["day"] for c in counts}
        )
        quantity = {(s.product_id, s.day): s.quantity for s in have}
        rows = [
            DailyStock(**{**c, "quantity": quantity.get((c["product_id"], c["day"]), 0) + c["quantity"]})
            for c in counts
        ]
        DailyStock.objects.bulk_create(
            rows, update_conflicts=True, unique_fields=["product", "day"], update_fields=["quantity", "counted_at"]
        )
    return sorted((r.product_id, r.day, r.quantity, r.counted_at) for r in rows)


# /readme:cmp-upsert-orm-bulk


# readme:cmp-upsert-bridge
def add_stock_bridge(counts):  # ON CONFLICT + RETURNING: PostgreSQL, SQLite
    insert = postgresql.insert if connection.vendor == "postgresql" else sqlite.insert
    stmt = insert(daily_stock).values(counts)
    stmt = stmt.on_conflict_do_update(
        index_elements=[daily_stock.c.product_id, daily_stock.c.day],
        set_={"quantity": daily_stock.c.quantity + stmt.excluded.quantity, "counted_at": stmt.excluded.counted_at},
    ).returning(daily_stock.c.product_id, daily_stock.c.day, daily_stock.c.quantity, daily_stock.c.counted_at)
    return sorted(execute(stmt))


# /readme:cmp-upsert-bridge


# 4. Apply a staged price list to the products whose price differs, and return them.


# readme:cmp-prices-orm-subquery
def apply_prices_orm_subquery():
    # 바뀔 행을 먼저 읽고(UPDATE 는 행을 돌려주지 않는다) 상관 서브쿼리 UPDATE 한 번
    new_price = PriceChange.objects.filter(sku=OuterRef("sku")).values("price")
    differs = PriceChange.objects.filter(sku=OuterRef("sku")).exclude(price=OuterRef("price"))
    with transaction.atomic():
        targets = Product.objects.filter(Exists(differs))
        changed = list(targets.select_for_update().annotate(new=Subquery(new_price)).values_list("sku", "new"))
        targets.update(price=Subquery(new_price))
    return sorted(changed)


# /readme:cmp-prices-orm-subquery


# readme:cmp-prices-orm-bulk-update
def apply_prices_orm_bulk_update():
    # 바뀔 상품을 새 가격과 함께 모델로 읽어 bulk_update()
    new_price = PriceChange.objects.filter(sku=OuterRef("sku")).values("price")
    differs = PriceChange.objects.filter(sku=OuterRef("sku")).exclude(price=OuterRef("price"))
    with transaction.atomic():
        changed = list(Product.objects.select_for_update().filter(Exists(differs)).annotate(new=Subquery(new_price)))
        for product in changed:
            product.price = product.new
        Product.objects.bulk_update(changed, ["price"], batch_size=1000)
    return sorted((p.sku, p.price) for p in changed)


# /readme:cmp-prices-orm-bulk-update


# readme:cmp-prices-bridge
apply_prices = (
    sa.update(products)
    .where(products.c.sku == price_changes.c.sku, products.c.price != price_changes.c.price)
    .values(price=price_changes.c.price)
    .returning(products.c.sku, products.c.price)
)


def apply_prices_bridge():  # UPDATE ... FROM ... RETURNING: PostgreSQL, SQLite
    return sorted(execute(apply_prices))


# /readme:cmp-prices-bridge


# 5. Move the sales before a cutoff into SaleArchive (ids kept).


# readme:cmp-archive-orm
def archive_sales_orm(cutoff):
    # 읽어서 모델로 만들고 bulk_create, 그리고 DELETE
    with transaction.atomic():
        old = list(Sale.objects.select_for_update().filter(sold_at__lt=cutoff))
        SaleArchive.objects.bulk_create(
            SaleArchive(id=s.id, product_id=s.product_id, sold_at=s.sold_at, quantity=s.quantity, amount=s.amount)
            for s in old
        )
        Sale.objects.filter(sold_at__lt=cutoff).delete()
    return len(old)


# /readme:cmp-archive-orm


# readme:cmp-archive-bridge
columns = ["id", "product_id", "sold_at", "quantity", "amount"]
is_old = sales.c.sold_at < sa.bindparam("cutoff")
copy_old = sa.insert(archive).from_select(columns, sa.select(*[sales.c[name] for name in columns]).where(is_old))
delete_old = sa.delete(sales).where(is_old)


def archive_sales_bridge(cutoff):  # INSERT ... SELECT 와 DELETE: 모든 백엔드
    with transaction.atomic():
        moved = execute(copy_old, {"cutoff": cutoff})
        execute(delete_old, {"cutoff": cutoff})
    return moved


# /readme:cmp-archive-bridge


# readme:cmp-archive-bridge-cte
moved = sa.delete(sales).where(is_old).returning(*[sales.c[name] for name in columns]).cte("moved")
move_old = sa.insert(archive).from_select(columns, sa.select(moved))


def archive_sales_bridge_cte(cutoff):  # 데이터를 바꾸는 CTE, 문장 하나: PostgreSQL
    return execute(move_old, {"cutoff": cutoff})


# /readme:cmp-archive-bridge-cte


# 6. Products ranked by revenue within their category, with a running total.


# readme:cmp-report-orm
def category_report_orm():
    # Sum() 집계 위에는 Window(Sum()) 을 못 얹어서(FieldError) 매출을 상관 서브쿼리로 만든다
    per_product = Sale.objects.filter(product=OuterRef("pk")).values("product").annotate(s=Sum("amount")).values("s")
    in_category = {"partition_by": F("category_id")}
    rows = (
        Product.objects.annotate(revenue=Coalesce(Subquery(per_product), Decimal("0")))
        .annotate(
            rank=Window(Rank(), order_by=F("revenue").desc(), **in_category),
            running=Window(Sum("revenue"), order_by=[F("revenue").desc(), F("sku")], **in_category),
        )
        .order_by("category__name", "rank", "sku")
        .values_list("category__name", "sku", "revenue", "rank", "running")
    )
    return list(rows)


# /readme:cmp-report-orm


# readme:cmp-report-bridge
revenue = (
    sa.select(
        products.c.sku,
        products.c.category_id,
        sa.func.coalesce(sa.func.sum(sales.c.amount), 0).label("revenue"),
    )
    .outerjoin(sales, sales.c.product_id == products.c.id)
    .group_by(products.c.id, products.c.sku, products.c.category_id)
    .cte("revenue")
)
in_category = {"partition_by": revenue.c.category_id}
rank = sa.func.rank().over(order_by=revenue.c.revenue.desc(), **in_category).label("rank")
running = sa.func.sum(revenue.c.revenue).over(order_by=(revenue.c.revenue.desc(), revenue.c.sku), **in_category)
category_report = (
    sa.select(categories.c.name, revenue.c.sku, revenue.c.revenue, rank, running.label("running"))
    .join(categories, categories.c.id == revenue.c.category_id)
    .order_by(categories.c.name, rank, revenue.c.sku)
)


def category_report_bridge():
    return execute(category_report)


# /readme:cmp-report-bridge


def count_queries(fn, *args):
    """``fn(*args)`` and the number of statements it sent, savepoints left out (the ORM's atomic() nests in tests).

    Counted with an execute wrapper, not the debug query log, which keeps only the last 9,000 queries.
    """
    sent = []

    def count(execute, sql, params, many, context):
        sent.append(sql)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(count):
        result = fn(*args)
    savepoint = ("SAVEPOINT", "RELEASE SAVEPOINT", "ROLLBACK TO SAVEPOINT")
    return result, sum(1 for sql in sent if not sql.lstrip().upper().startswith(savepoint))
