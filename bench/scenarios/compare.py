"""Scenario 8: the same request written with the Django ORM and with the bridge (tests/comparisons.py).

Six requests on the test suite's shop models (``tests.testapp``, installed in
the benchmark settings), each at two or three sizes. The variants are the
functions docs/comparisons.md shows, called as they are there. Before timing,
each variant runs once more in a rolled-back transaction to count its queries
(savepoints left out) and to check that it returns what the request's first
ORM implementation returns; a variant that disagrees stops the run.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, django_rollback, run_variants

EPOCH = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
SALES_PER_PRODUCT = 10
CATEGORIES = 50
# Every size below the tree's root has this many children; each leaf has two products sold five times.
BRANCHING = 4

SCOPE = (
    "Timed: the request's function from tests/comparisons.py, called with its input, inside a transaction "
    "rolled back after each run (the ORM's own atomic() is a savepoint in it). 'queries' is the number of "
    "statements one run sends, savepoints left out. Untimed: seeding the shop tables (committed) per size."
)


def _shop_models():
    from tests.testapp.models import Category, DailyStock, PriceChange, Product, Sale, SaleArchive

    # Children before parents, for SQLite's DELETE.
    return [SaleArchive, DailyStock, Sale, PriceChange, Product, Category]


def _empty() -> None:
    models = _shop_models()
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            tables = ", ".join(connection.ops.quote_name(m._meta.db_table) for m in models)
            cursor.execute(f"TRUNCATE {tables} RESTART IDENTITY")
        else:
            for model in models:
                cursor.execute(f"DELETE FROM {connection.ops.quote_name(model._meta.db_table)}")


def _vacuum() -> None:
    data.vacuum(*_shop_models())


def _ids(rows) -> list[int]:
    return [row[0] for row in rows]


def _categories(levels: list[int]) -> list[list[int]]:
    """A tree with ``levels[i]`` children under each category of level ``i``; the ids of every level, root first."""
    from django_sqlalchemy_bridge import bulk_insert
    from tests.testapp.models import Category

    tree = [_ids(bulk_insert(Category, [{"name": "c0", "parent_id": None}], returning=["id"]))]
    for depth, children in enumerate(levels, 1):
        rows = [{"name": f"c{depth}-{p}-{i}", "parent_id": p} for p in tree[-1] for i in range(children)]
        tree.append(_ids(bulk_insert(Category, rows, returning=["id"])))
    return tree


def _products(category_ids: list[int], per_category: int, sales_per_product: int) -> list[int]:
    from django_sqlalchemy_bridge import bulk_insert
    from tests.testapp.models import Product, Sale

    rows = [
        {"sku": f"P{c:06d}-{i}", "category_id": c, "price": Decimal(f"{(c * 7 + i) % 90 + 10}.50")}
        for c in category_ids
        for i in range(per_category)
    ]
    ids = _ids(bulk_insert(Product, rows, returning=["id"]))
    sales = []
    for product_id, row in zip(ids, rows, strict=True):
        for k in range(sales_per_product):
            quantity = (product_id + k) % 5 + 1
            sold_at = EPOCH + datetime.timedelta(hours=(product_id * 13 + k * 37) % (30 * 24))
            sales.append(
                {"product_id": product_id, "sold_at": sold_at, "quantity": quantity, "amount": row["price"] * quantity}
            )
    for start in range(0, len(sales), 50_000):
        bulk_insert(Sale, sales[start : start + 50_000])
    return ids


def _flat_shop(products: int) -> list[int]:
    """``CATEGORIES`` categories under a root, ``products`` products spread over them, each sold 10 times."""
    tree = _categories([CATEGORIES])
    return _products(tree[1], products // CATEGORIES, SALES_PER_PRODUCT)


def _variant(request: str, name: str, prepare=None, vendors=("sqlite", "postgresql")) -> Variant:
    """``<request>_<name>()`` of tests/comparisons.py, called with what ``prepare()`` returns when given."""
    from tests import comparisons

    fn = getattr(comparisons, f"{request}_{name}")
    note = f"tests/comparisons.py {fn.__name__}()"
    if prepare is None:
        return Variant(name, lambda _data, _handle: fn(), vendors=vendors, note=note)
    return Variant(name, lambda data, _handle: fn(data), prepare, vendors=vendors, note=note)


def _measure(variants: list[Variant]) -> dict:
    """Count each variant's queries and check its result against the first one's, then time them all."""
    from tests.comparisons import count_queries

    vendor = connection.vendor
    reference = None
    for v in variants:
        if vendor not in v.vendors:
            continue
        try:
            with django_rollback():
                result, queries = count_queries(v.run, v.prepare(), None)
        except Exception as e:  # run_variants records the same failure
            print(f"    {v.key:<34} fails: {type(e).__name__}", flush=True)
            continue
        if reference is None:
            reference = result
        assert result == reference, f"{v.key} disagrees with {variants[0].key}"
        v.extra["queries"] = queries
    return run_variants(variants, vendor, after=_vacuum)


def tree(depth: int) -> dict:
    """The revenue of the first subtree under the root, in a tree ``depth`` levels below the root."""
    _empty()
    levels = _categories([BRANCHING] * depth)
    _products(levels[-1], 2, 5)
    _vacuum()
    root = levels[1][0]
    variants = [_variant("subtree_revenue", name, lambda: root) for name in ("orm_levels", "orm_python", "bridge")]
    sizes = {"categories": sum(len(level) for level in levels), "sales": len(levels[-1]) * 2 * 5}
    print(f"  compare tree depth={depth} {sizes}", flush=True)
    return {"request": "tree", "depth": depth, **sizes, "variants": _measure(variants)}


def latest(products: int) -> dict:
    _empty()
    _flat_shop(products)
    _vacuum()
    print(f"  compare latest products={products}", flush=True)
    variants = [_variant("latest_sales", "orm_window"), _variant("latest_sales", "orm_prefetch")]
    variants.append(_variant("latest_sales", "bridge", vendors=("postgresql",)))
    return {
        "request": "latest",
        "products": products,
        "sales": products * SALES_PER_PRODUCT,
        "variants": _measure(variants),
    }


def upsert(rows: int) -> dict:
    """``rows`` counts for one day; half of them find a row already there."""
    from django_sqlalchemy_bridge import bulk_insert
    from tests.testapp.models import DailyStock

    _empty()
    ids = _flat_shop(max(rows, 1_000))[:rows]
    day = datetime.date(2026, 10, 26)
    bulk_insert(
        DailyStock,
        [{"product_id": p, "day": day, "quantity": 5, "counted_at": EPOCH} for p in ids[::2]],
    )
    _vacuum()

    def counts():
        at = EPOCH + datetime.timedelta(days=25, hours=9)
        return [{"product_id": p, "day": day, "quantity": i % 7 + 1, "counted_at": at} for i, p in enumerate(ids)]

    print(f"  compare upsert rows={rows}", flush=True)
    variants = [_variant("add_stock", name, counts) for name in ("orm_loop", "orm_bulk", "bridge")]
    return {"request": "upsert", "rows": rows, "variants": _measure(variants)}


def prices(products: int) -> dict:
    """A price list for every product; every other one has a new price."""
    from django_sqlalchemy_bridge import bulk_insert, execute, table_for
    from tests.testapp.models import PriceChange, Product

    _empty()
    _flat_shop(products)
    t = table_for(Product)
    listed = execute(sa.select(t.c.sku, t.c.price).order_by(t.c.id))
    bulk_insert(PriceChange, [{"sku": sku, "price": price + i % 2} for i, (sku, price) in enumerate(listed)])
    _vacuum()
    print(f"  compare prices products={products}", flush=True)
    variants = [_variant("apply_prices", name) for name in ("orm_subquery", "orm_bulk_update", "bridge")]
    return {"request": "prices", "products": products, "changed": products // 2, "variants": _measure(variants)}


def archive(products: int) -> dict:
    """Move the sales of the first half of the month (about half of them)."""
    _empty()
    _flat_shop(products)
    _vacuum()
    cutoff = EPOCH + datetime.timedelta(days=15)
    print(f"  compare archive sales={products * SALES_PER_PRODUCT}", flush=True)
    variants = [_variant("archive_sales", "orm", lambda: cutoff), _variant("archive_sales", "bridge", lambda: cutoff)]
    variants.append(_variant("archive_sales", "bridge_cte", lambda: cutoff, vendors=("postgresql",)))
    return {"request": "archive", "sales": products * SALES_PER_PRODUCT, "variants": _measure(variants)}


def report(products: int) -> dict:
    _empty()
    _flat_shop(products)
    _vacuum()
    print(f"  compare report products={products}", flush=True)
    variants = [_variant("category_report", name) for name in ("orm", "bridge")]
    return {
        "request": "report",
        "products": products,
        "sales": products * SALES_PER_PRODUCT,
        "variants": _measure(variants),
    }


CASES = {"tree": tree, "latest": latest, "upsert": upsert, "prices": prices, "archive": archive, "report": report}
# (request, size): tree depth, products, or upsert rows. Each request's smallest size comes first.
SIZES = [
    ("tree", 3),
    ("tree", 5),
    ("tree", 7),
    ("latest", 1_000),
    ("latest", 10_000),
    ("upsert", 100),
    ("upsert", 1_000),
    ("upsert", 5_000),
    ("prices", 1_000),
    ("prices", 10_000),
    ("archive", 1_000),
    ("archive", 10_000),
    ("report", 1_000),
    ("report", 10_000),
]


def run(engine, sizes=None) -> dict:
    # --quick passes SIZES[:1]: run each request at its smallest size only.
    quick = sizes is not None
    seen = set()
    cases = []
    for request, n in SIZES:
        if quick and request in seen:
            continue
        seen.add(request)
        cases.append(CASES[request](n))
    _empty()
    return {"scope": SCOPE, "cases": cases}
