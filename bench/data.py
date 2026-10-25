"""Deterministic rows for the scenarios, and a plain SQLAlchemy mapping of the same tables.

The SQLAlchemy mapping is what an SQLAlchemy user would write (native column
types, its own engine); it is only there to compare against, not the bridge.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import sqlalchemy as sa
from django.db import connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

CUSTOMERS = 500
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


def row(i: int) -> dict:
    """Row ``i`` (1-based) of the seeded data."""
    return {
        "id": i,
        "label": f"item-{i}",
        "amount": i % 1000,
        "price": Decimal(i % 100000) / 100,
        "created_at": EPOCH + datetime.timedelta(seconds=i * 37),
        "flag": i % 2 == 0,
        "customer_id": i % CUSTOMERS + 1,
    }


def new_row(i: int) -> dict:
    """Row ``i`` for an INSERT, without the id the database assigns."""
    r = row(i)
    del r["id"]
    return r


def changed(i: int) -> dict:
    """The new values row ``i`` gets in the UPDATE scenario."""
    return {"id": i, "label": f"changed-{i}", "amount": i % 1000 + 1, "price": Decimal(i % 100000) / 100 + 1}


def seed(n: int) -> None:
    """Replace the contents of the Item table with rows 1..``n``, committed."""
    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import bulk_insert

    truncate(Item)
    for start in range(1, n + 1, 50_000):
        bulk_insert(Item, [row(i) for i in range(start, min(start + 50_000, n + 1))])
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT setval(pg_get_serial_sequence(%s, 'id'), %s)", [Item._meta.db_table, n])
    vacuum(Item)


def truncate(model) -> None:
    table = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(f"TRUNCATE {table} RESTART IDENTITY")
        else:
            cursor.execute(f"DELETE FROM {table}")


def vacuum(*models) -> None:
    """PostgreSQL: clear the dead tuples a rolled-back repetition left (outside any transaction)."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        for model in models:
            cursor.execute(f"VACUUM ANALYZE {connection.ops.quote_name(model._meta.db_table)}")


class Base(DeclarativeBase):
    pass


class _Columns:
    label: Mapped[str] = mapped_column(sa.String(50))
    amount: Mapped[int]
    price: Mapped[Decimal] = mapped_column(sa.Numeric(12, 2))
    created_at: Mapped[datetime.datetime] = mapped_column(sa.DateTime(timezone=True))
    flag: Mapped[bool]
    customer_id: Mapped[int]


class SAItem(_Columns, Base):
    __tablename__ = "benchapp_item"
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)


class SANewItem(_Columns, Base):
    __tablename__ = "benchapp_newitem"
    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)


COLUMNS = ["id", "label", "amount", "price", "created_at", "flag", "customer_id"]
