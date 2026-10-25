"""Time and memory measurement shared by the scenarios.

Each repetition prepares its input outside the timer (``prepare``), then runs
the measured call (``run``) inside a ``scope`` that rolls back, so every
repetition starts from the same data. Time is ``perf_counter`` around ``run``
alone; memory is a separate repetition under ``tracemalloc`` (its overhead
would distort the timings), counting only what ``run`` allocates.
"""

from __future__ import annotations

import contextlib
import gc
import statistics
import time
import tracemalloc
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

REPEAT = 5
WARMUP = 1
# A variant whose first run takes longer than this is not repeated: that one
# run is its only timing, marked "cut_short" (Django's bulk_update with no
# batch_size on PostgreSQL grows quadratically).
SLOW_S = 30.0
# PostgreSQL cancels a statement of a Django variant that runs longer than this;
# the variant is then recorded as failed (the same bulk_update at 50,000 rows ran
# for over 30 minutes without finishing).
STATEMENT_TIMEOUT_S = 120


@contextlib.contextmanager
def django_rollback() -> Iterator[None]:
    """A Django transaction that is always rolled back."""
    from django.db import connection, transaction

    with transaction.atomic():
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_S * 1000)}")
        yield None
        transaction.set_rollback(True)


def sa_rollback(engine) -> Callable[[], contextlib.AbstractContextManager]:
    """A SQLAlchemy connection on ``engine`` in a transaction that is always rolled back."""

    @contextlib.contextmanager
    def scope():
        with engine.connect() as conn:
            with conn.begin() as tx:
                yield conn
                tx.rollback()

    return scope


def sa_session_rollback(engine) -> Callable[[], contextlib.AbstractContextManager]:
    """A SQLAlchemy ORM session on ``engine`` that is always rolled back."""
    from sqlalchemy.orm import Session

    @contextlib.contextmanager
    def scope():
        with Session(engine) as session:
            with session.begin():
                yield session
                session.rollback()

    return scope


def no_scope() -> contextlib.AbstractContextManager:
    return contextlib.nullcontext()


@dataclass
class Variant:
    """One way of doing the scenario's work.

    ``run(data, handle)`` is timed; ``data`` comes from ``prepare()`` and
    ``handle`` from ``scope()`` (``None`` for Django, a SQLAlchemy connection or
    session for the SQLAlchemy comparisons). ``check(result, data, handle)``
    runs untimed inside the scope after the first repetition, to show the
    variant did the work.
    """

    key: str
    run: Callable[[Any, Any], Any]
    prepare: Callable[[], Any] = lambda: None
    scope: Callable[[], contextlib.AbstractContextManager] = django_rollback
    check: Callable[[Any, Any, Any], None] | None = None
    vendors: tuple[str, ...] = ("sqlite", "postgresql")
    note: str = ""
    extra: dict = field(default_factory=dict)
    # Recorded instead of run, with the reason.
    skip: str | None = None


def _once(variant: Variant, *, traced: bool, check: bool) -> tuple[float, float, int | None]:
    t_prep = time.perf_counter()
    data = variant.prepare()
    prep = time.perf_counter() - t_prep
    gc.collect()
    peak = None
    with variant.scope() as handle:
        if traced:
            tracemalloc.start()
            base = tracemalloc.get_traced_memory()[0]
        t0 = time.perf_counter()
        out = variant.run(data, handle)
        elapsed = time.perf_counter() - t0
        if traced:
            peak = tracemalloc.get_traced_memory()[1] - base
            tracemalloc.stop()
        if check and variant.check is not None:
            variant.check(out, data, handle)
        del out
    del data
    return elapsed, prep, peak


def measure(
    variant: Variant,
    *,
    repeat: int = REPEAT,
    warmup: int = WARMUP,
    memory: bool = True,
    after: Callable[[], None] | None = None,
) -> dict:
    """Run ``variant`` ``warmup`` + ``repeat`` times (plus one traced run) and keep every raw value.

    ``after`` runs untimed after every repetition (VACUUM on PostgreSQL).
    """

    def cleanup():
        if after is not None:
            after()

    warm = []
    for i in range(warmup):
        warm.append(_once(variant, traced=False, check=i == 0)[0])
        cleanup()
    if warm and warm[0] > SLOW_S:
        print(f"    {variant.key:<34} one run {warm[0]:10.2f} s  (cut short)", flush=True)
        return {
            "note": variant.note,
            "cut_short": True,
            "warmup_s": [],
            "times_s": warm,
            "best_s": warm[0],
            "median_s": warm[0],
            "prepare_s": [],
            **variant.extra,
        }
    times, preps = [], []
    for i in range(repeat):
        elapsed, prep, _ = _once(variant, traced=False, check=not warm and i == 0)
        times.append(elapsed)
        preps.append(prep)
        cleanup()
    result = {
        "note": variant.note,
        "warmup_s": warm,
        "times_s": times,
        "best_s": min(times),
        "median_s": statistics.median(times),
        "prepare_s": preps,
        **variant.extra,
    }
    if memory:
        _, _, peak = _once(variant, traced=True, check=False)
        cleanup()
        result["tracemalloc_peak_bytes"] = peak
    print(f"    {variant.key:<34} best {result['best_s'] * 1000:10.2f} ms", flush=True)
    return result


def run_variants(
    variants: list[Variant], vendor: str, *, memory: bool = True, after: Callable[[], None] | None = None, **kw
) -> dict:
    results = {}
    for v in variants:
        if vendor not in v.vendors:
            continue
        if v.skip:
            results[v.key] = {"note": v.note, "skipped": v.skip}
            print(f"    {v.key:<34} skipped", flush=True)
            continue
        try:
            results[v.key] = measure(v, memory=memory, after=after, **kw)
        except Exception as e:  # a variant that fails is a result too; record it and go on
            results[v.key] = {"note": v.note, "error": f"{type(e).__name__}: {e}", "timeout_s": STATEMENT_TIMEOUT_S}
            print(f"    {v.key:<34} FAILED {type(e).__name__}: {e}", flush=True)
            if after is not None:
                after()
    return results
