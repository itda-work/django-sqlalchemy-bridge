"""Scenario 1: UPDATE many rows by primary key, each to its own values (3 columns)."""

from __future__ import annotations

import sqlalchemy as sa
from django.db import connection

from bench import data
from bench.harness import Variant, run_variants, sa_session_rollback

SIZES = [5_000, 50_000]
FIELDS = ["label", "amount", "price"]

SCOPE = (
    "Timed: the update call alone. Untimed: building its input (model instances for Django and "
    "django-pgbulk, dicts for the bridge and SQLAlchemy) and the transaction, rolled back after each run."
)


def _check_django(n):
    def check(result, _data, _handle):
        from bench.benchapp.models import Item

        assert Item.objects.filter(label__startswith="changed-").count() == n

    return check


def _check_sa(n):
    def check(result, _data, session):
        got = session.scalar(sa.select(sa.func.count()).where(data.SAItem.label.like("changed-%")))
        assert got == n, got

    return check


def variants(n: int, engine) -> list[Variant]:
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import bulk_update, execute, table_for

    items = table_for(Item)

    def instances():
        objs = []
        for i in range(1, n + 1):
            obj = Item(id=i)
            for k, v in data.changed(i).items():
                setattr(obj, k, v)
            objs.append(obj)
        return objs

    def dicts():
        return [data.changed(i) for i in range(1, n + 1)]

    def pgbulk_update(objs, _):
        import pgbulk

        pgbulk.update(Item, objs, FIELDS)

    stmt = (
        sa.update(items)
        .where(items.c.id == sa.bindparam("pk"))
        .values(label=sa.bindparam("l"), amount=sa.bindparam("a"), price=sa.bindparam("p"))
    )

    def bridge_executemany(rows, _):
        return execute(stmt, [{"pk": r["id"], "l": r["label"], "a": r["amount"], "p": r["price"]} for r in rows])

    check = _check_django(n)
    return [
        Variant(
            "django_bulk_update",
            lambda objs, _: Item.objects.bulk_update(objs, FIELDS),
            instances,
            check=check,
            note="batch_size=None (default: the backend's parameter limit, no limit on PostgreSQL)",
            skip=(
                "not run on PostgreSQL above 5,000 rows: with no batch_size Django sends one UPDATE with a "
                "CASE of every row per column; at 50,000 rows (2026-10-25) the server backend spent over 30 "
                "minutes at 100% CPU and 3.6 GB without finishing or answering a cancel, and had to be killed"
            )
            if connection.vendor == "postgresql" and n > 5_000
            else None,
        ),
        Variant(
            "django_bulk_update_batch1000",
            lambda objs, _: Item.objects.bulk_update(objs, FIELDS, batch_size=1000),
            instances,
            check=check,
            note="batch_size=1000",
        ),
        Variant(
            "pgbulk_update",
            pgbulk_update,
            instances,
            check=check,
            vendors=("postgresql",),
            note="pgbulk.update(Item, objs, fields)",
        ),
        Variant(
            "bridge_bulk_update",
            lambda rows, _: bulk_update(Item, rows),
            dicts,
            check=check,
            note="PostgreSQL: one UPDATE ... FROM unnest(...); SQLite: executemany",
        ),
        Variant(
            "bridge_execute_executemany",
            bridge_executemany,
            dicts,
            check=check,
            note="execute(update(...).where(id == bindparam), [params, ...]) -> cursor.executemany",
        ),
        Variant(
            "sqlalchemy_orm_bulk_update",
            lambda rows, session: session.execute(sa.update(data.SAItem), rows),
            dicts,
            scope=sa_session_rollback(engine),
            check=_check_sa(n),
            note="Session.execute(update(SAItem), [dicts]): ORM bulk UPDATE by primary key; own engine, not the bridge",
        ),
    ]


def run(engine, sizes=None) -> dict:
    from bench.benchapp.models import Item

    cases = []
    for n in sizes or SIZES:
        print(f"  update n={n}", flush=True)
        data.seed(n)
        cases.append(
            {"n": n, "variants": run_variants(variants(n, engine), connection.vendor, after=lambda: data.vacuum(Item))}
        )
    return {"scope": SCOPE, "columns_set": FIELDS, "cases": cases}
