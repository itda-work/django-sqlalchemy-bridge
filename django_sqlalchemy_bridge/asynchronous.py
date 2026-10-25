"""Run a unit of synchronous database work from async code, in one thread hop.

Django's async ORM hands every query to a thread on its own, so an ``atomic()``
block cannot span them. :func:`arun` hands the whole unit of work, queries and
transactions included, to Django's thread-sensitive thread at once.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from asgiref.sync import iscoroutinefunction, sync_to_async
from django.db import connections

__all__ = ["arun"]

P = ParamSpec("P")
R = TypeVar("R")


async def arun(fn: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``fn(*args, **kwargs)`` in one ``sync_to_async(thread_sensitive=True)`` hop.

    ``fn`` is a sync function that may use :func:`execute`, :func:`stream`,
    ``bulk_*``, ``atomic()``, ``on_commit`` and the ORM on any database alias;
    its return value or exception reaches the caller unchanged. A transaction
    opened inside ``fn`` ends inside it, so concurrent ``arun`` calls commit or
    roll back independently (thread-sensitive calls queue on one thread).

    Like Channels' ``database_sync_to_async``, the connections of that thread
    are closed before and after ``fn`` when they are unusable after an error or
    older than ``CONN_MAX_AGE``, as Django does at each request's start and end.
    Connections inside an ``atomic()`` block are left alone.
    """
    if iscoroutinefunction(fn) or iscoroutinefunction(getattr(fn, "__call__", fn)):
        raise TypeError(f"arun() runs a sync function; {fn!r} is a coroutine function, await it instead")
    return await sync_to_async(_unit_of_work, thread_sensitive=True)(fn, args, kwargs)


def _unit_of_work(fn: Callable[..., R], args: tuple, kwargs: dict) -> R:
    _close_old_connections()
    try:
        result = fn(*args, **kwargs)
    finally:
        _close_old_connections()
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError(f"arun() runs a sync function; {fn!r} returned an awaitable, await it instead")
    return result


def _close_old_connections() -> None:
    # django.db.close_old_connections() without closing a connection that an
    # enclosing atomic() still uses (the caller's sync code, or a test case).
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close_if_unusable_or_obsolete()
