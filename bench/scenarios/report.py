"""Scenario 6: report queries with a CTE and window functions over 20,000 rows.

``rank``: per-customer totals in a CTE, ranked with ``RANK() OVER`` (500 rows).
``running``: a running total per customer, ``SUM() OVER (PARTITION BY ...
ORDER BY ...)``, for every row (20,000 rows). Each tool writes the query the
way it is written there: Django with django-cte, Django without a CTE (an
aggregate with a window over it, which Django supports directly), the bridge
with SQLAlchemy Core, and the raw SQL on a Django cursor. Every variant must
return the same rows (checked).
"""

from __future__ import annotations

import sqlalchemy as sa
from django.db import connection
from django.db.models import Count, F, Sum, Window
from django.db.models.functions import Rank

from bench import data
from bench.harness import Variant, no_scope, run_variants

N = 20_000

SCOPE = (
    "Timed: building the query, running it and fetching every row as tuples. No transaction. "
    "tracemalloc counts what the call allocates."
)


def _rank_variants(expected) -> list[Variant]:
    from django_cte import CTE, with_cte

    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, table_for

    def django_cte(_, __):
        totals = CTE(Item.objects.values("customer_id").annotate(total=Sum("price"), n=Count("id")).order_by())
        qs = with_cte(
            totals,
            select=totals.queryset()
            .annotate(rnk=Window(Rank(), order_by=F("total").desc()))
            .order_by("rnk", "customer_id")
            .values_list("customer_id", "total", "n", "rnk"),
        )
        return list(qs)

    def django_plain(_, __):
        qs = (
            Item.objects.values("customer_id")
            .annotate(total=Sum("price"), n=Count("id"), rnk=Window(Rank(), order_by=Sum("price").desc()))
            .order_by("rnk", "customer_id")
            .values_list("customer_id", "total", "n", "rnk")
        )
        return list(qs)

    def bridge(_, __):
        t = table_for(Item)
        totals = (
            sa.select(t.c.customer_id, sa.func.sum(t.c.price).label("total"), sa.func.count(t.c.id).label("n"))
            .group_by(t.c.customer_id)
            .cte("totals")
        )
        rnk = sa.func.rank().over(order_by=totals.c.total.desc()).label("rnk")
        stmt = sa.select(totals.c.customer_id, totals.c.total, totals.c.n, rnk).order_by(
            sa.text("rnk"), totals.c.customer_id
        )
        return execute(stmt)

    qn = connection.ops.quote_name
    table = qn(Item._meta.db_table)
    raw_sql = (
        f"WITH totals AS (SELECT customer_id, SUM(price) AS total, COUNT(id) AS n FROM {table} GROUP BY customer_id) "
        "SELECT customer_id, total, n, RANK() OVER (ORDER BY total DESC) AS rnk FROM totals "
        "ORDER BY rnk, customer_id"
    )

    def raw(_, __):
        with connection.cursor() as cursor:
            cursor.execute(raw_sql, ())
            # Same Decimal the ORM returns (SQLite hands back a float or an int for SUM()).
            return [(c, _decimal(total), n, r) for c, total, n, r in cursor.fetchall()]

    return [
        Variant("django_cte", django_cte, scope=no_scope, check=expected, note="django-cte CTE() + with_cte()"),
        Variant(
            "django_window_no_cte",
            django_plain,
            scope=no_scope,
            check=expected,
            note="values().annotate(Sum, Count, Window(Rank(), order_by=Sum desc)): no CTE needed",
        ),
        Variant("bridge", bridge, scope=no_scope, check=expected, note="select(...).cte() + func.rank().over()"),
        Variant("raw_cursor", raw, scope=no_scope, check=expected, note="the same SQL on a Django cursor"),
    ]


def _decimal(value):
    from decimal import Decimal

    from bench.benchapp.models import Item

    field = Item._meta.get_field("price")
    return field.to_python(value).quantize(Decimal("0.01")) if value is not None else None


def _running_variants(expected) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import execute, table_for

    def django(_, __):
        qs = (
            Item.objects.annotate(
                running=Window(Sum("price"), partition_by=[F("customer_id")], order_by=[F("created_at"), F("id")])
            )
            .order_by("customer_id", "created_at", "id")
            .values_list("id", "customer_id", "price", "running")
        )
        return list(qs)

    def bridge(_, __):
        t = table_for(Item)
        running = (
            sa.func.sum(t.c.price)
            .over(partition_by=t.c.customer_id, order_by=(t.c.created_at, t.c.id))
            .label("running")
        )
        stmt = sa.select(t.c.id, t.c.customer_id, t.c.price, running).order_by(t.c.customer_id, t.c.created_at, t.c.id)
        return execute(stmt)

    return [
        Variant("django_window", django, scope=no_scope, check=expected, note="annotate(Window(Sum, partition_by))"),
        Variant("bridge", bridge, scope=no_scope, check=expected, note="func.sum().over(partition_by=...)"),
    ]


def _same_as(reference_run):
    """A check that the variant's rows equal ``reference_run``'s (normalized to Decimal)."""
    reference = []

    def norm(rows):
        return [tuple(_decimal(v) if isinstance(v, float) else v for v in r) for r in rows]

    def check(result, _data, _handle):
        if not reference:
            reference.extend(norm(reference_run(None, None)))
        got = norm(result)
        assert got == reference, (got[:3], reference[:3])

    return check


def run(engine, sizes=None) -> dict:
    data.seed(N)
    print(f"  report n={N}", flush=True)
    vendor = connection.vendor
    rank = _rank_variants(None)
    rank_check = _same_as(rank[-1].run)  # the raw SQL is the reference
    for v in rank:
        v.check = rank_check
    running = _running_variants(None)
    running_check = _same_as(running[0].run)
    for v in running:
        v.check = running_check
    return {
        "scope": SCOPE,
        "cases": [
            {"n": N, "query": "rank", "variants": run_variants(rank, vendor)},
            {"n": N, "query": "running", "variants": run_variants(running, vendor)},
        ],
    }
