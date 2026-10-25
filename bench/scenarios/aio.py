"""Scenario 5: async code reading by primary key, ``arun`` against Django's async ORM (``aget``).

Both hand the work to Django's thread-sensitive thread: ``aget`` one hop per
query, ``arun`` one hop per unit of work. "gather" runs that many single-row
lookups concurrently with ``asyncio.gather``; thread-sensitive calls still run
one at a time on that one thread. The "unit5" variants do 200 units of work of
5 lookups each. Connections persist (``CONN_MAX_AGE=None``) unless the variant
says otherwise: with the default ``CONN_MAX_AGE=0``, ``arun`` closes the
connection after every call (as Django does after every request) and the next
call reconnects; ``aget`` outside a request never closes it.

SQLAlchemy's ``AsyncEngine`` and psycopg's ``AsyncConnection`` are not measured:
they were dropped in #1 because they run outside Django's connection and
transactions (``atomic()`` rollback, row locks, query counting), which is the
bridge's premise.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from django.db import connection, connections

from bench import data
from bench.harness import Variant, no_scope, run_variants

N_ROWS = 20_000
SEQUENTIAL = 1_000
UNITS = 200
UNIT_SIZE = 5

SCOPE = (
    "Timed: asyncio.run() of the coroutine doing the lookups (event loop start included, well under a "
    "millisecond). Autocommit, no transaction. Each lookup returns one row of 7 columns."
)


def _pks(k: int) -> list[int]:
    return [(i * 7919) % N_ROWS + 1 for i in range(k)]


def _in_db_thread(fn) -> None:
    from asgiref.sync import sync_to_async

    async def hop():
        await sync_to_async(fn, thread_sensitive=True)()

    asyncio.run(hop())


def _close_thread_connection() -> None:
    _in_db_thread(lambda: connections["default"].close())


def _reset(conn_max_age) -> None:
    """Reconnect the thread-sensitive thread's connection with ``CONN_MAX_AGE`` set (untimed).

    The setting is read when a connection opens, so the open one is closed first.
    """
    connections.settings["default"]["CONN_MAX_AGE"] = conn_max_age
    _close_thread_connection()
    if conn_max_age is None:
        _in_db_thread(lambda: connections["default"].ensure_connection())


def variants() -> list[Variant]:
    from asgiref.sync import sync_to_async

    from bench.benchapp.models import Item
    from django_sqlalchemy_bridge import arun, execute, table_for

    items = table_for(Item)
    columns = [items.c[name] for name in data.COLUMNS]

    def lookup(pk):
        return execute(sa.select(*columns).where(items.c.id == pk))[0]

    def unit(pks):
        return [lookup(pk) for pk in pks]

    async def aget(pk):
        return await Item.objects.values_list(*data.COLUMNS).aget(pk=pk)

    def timed(coro_fn, k):
        def run(_, __):
            return asyncio.run(coro_fn(_pks(k)))

        return run

    async def seq_aget(pks):
        return [await aget(pk) for pk in pks]

    async def seq_arun(pks):
        return [await arun(lookup, pk) for pk in pks]

    async def gather_aget(pks):
        return await asyncio.gather(*(aget(pk) for pk in pks))

    async def gather_arun(pks):
        return await asyncio.gather(*(arun(lookup, pk) for pk in pks))

    def units(pks):
        return [pks[i : i + UNIT_SIZE] for i in range(0, len(pks), UNIT_SIZE)]

    async def unit_aget(pks):
        out = []
        for group in units(pks):
            out.extend([await aget(pk) for pk in group])
        return out

    async def unit_arun(pks):
        out = []
        for group in units(pks):
            out.extend(await arun(unit, group))
        return out

    async def unit_gather_arun(pks):
        return [row for rows in await asyncio.gather(*(arun(unit, g) for g in units(pks))) for row in rows]

    async def unit_gather_aget(pks):
        async def one(group):
            return [await aget(pk) for pk in group]

        return [row for rows in await asyncio.gather(*(one(g) for g in units(pks))) for row in rows]

    def sync_execute(_, __):
        return [lookup(pk) for pk in _pks(SEQUENTIAL)]

    async def threaded_sync(pks):
        # Same thread hop as arun, without arun's connection housekeeping: what that costs.
        return [await sync_to_async(lookup, thread_sensitive=True)(pk) for pk in pks]

    def check_for(k):
        def check(result, _data, _handle):
            assert len(result) == k and all(result)

        return check

    def v(key, run, k, note):
        max_age = 0 if key.endswith("_conn_max_age_0") else None
        return Variant(
            key,
            run,
            prepare=lambda: _reset(max_age),
            scope=no_scope,
            check=check_for(k),
            note=note,
            extra={"queries": k, "conn_max_age": max_age},
        )

    total = UNITS * UNIT_SIZE
    return [
        v("sync_execute_sequential", sync_execute, SEQUENTIAL, "plain sync execute(), no event loop: the floor"),
        v("aget_sequential", timed(seq_aget, SEQUENTIAL), SEQUENTIAL, "await values_list(...).aget(pk=pk)"),
        v("arun_sequential", timed(seq_arun, SEQUENTIAL), SEQUENTIAL, "await arun(lookup, pk), one query each"),
        v(
            "sync_to_async_sequential",
            timed(threaded_sync, SEQUENTIAL),
            SEQUENTIAL,
            "await sync_to_async(lookup, thread_sensitive=True)(pk): arun without its connection checks",
        ),
        v(
            "arun_sequential_conn_max_age_0",
            timed(seq_arun, SEQUENTIAL),
            SEQUENTIAL,
            "as arun_sequential with CONN_MAX_AGE=0: reconnects every call",
        ),
        v("aget_gather_100", timed(gather_aget, 100), 100, "asyncio.gather of 100 aget()"),
        v("arun_gather_100", timed(gather_arun, 100), 100, "asyncio.gather of 100 arun(lookup, pk)"),
        v("aget_gather_1000", timed(gather_aget, 1000), 1000, "asyncio.gather of 1000 aget()"),
        v("arun_gather_1000", timed(gather_arun, 1000), 1000, "asyncio.gather of 1000 arun(lookup, pk)"),
        v("unit5_aget_sequential", timed(unit_aget, total), total, "200 units x 5 sequential aget()"),
        v("unit5_arun_sequential", timed(unit_arun, total), total, "200 x arun(unit) doing 5 execute() each"),
        v("unit5_aget_gather", timed(unit_gather_aget, total), total, "gather of 200 units, 5 aget() each"),
        v("unit5_arun_gather", timed(unit_gather_arun, total), total, "gather of 200 arun(unit)"),
    ]


def run(engine, sizes=None) -> dict:
    data.seed(N_ROWS)
    print("  async", flush=True)
    try:
        results = run_variants(variants(), connection.vendor, memory=False)
    finally:
        connections.settings["default"]["CONN_MAX_AGE"] = 0
        _close_thread_connection()  # or the benchmark database cannot be dropped
    return {"scope": SCOPE, "cases": [{"n": SEQUENTIAL, "variants": results}]}
