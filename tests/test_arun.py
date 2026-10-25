import asyncio
import warnings

import psycopg
import pytest
import sqlalchemy as sa
from asgiref.sync import async_to_sync
from django.core.exceptions import SynchronousOnlyOperation
from django.db import OperationalError, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_sqlalchemy_bridge import arun, bulk_insert, execute, stream, table_for
from tests.testapp.models import Gadget

pytestmark = pytest.mark.django_db

IS_PG = connection.vendor == "postgresql"
pg_only = pytest.mark.skipif(not IS_PG, reason="PostgreSQL only")

gadgets = table_for(Gadget)


def run(coroutine_function, *args):
    # async_to_sync runs arun's thread-sensitive hop back on this test thread, so
    # it uses the test's connection (and, without transaction=True, its transaction).
    return async_to_sync(coroutine_function)(*args)


def add(code):
    return execute(sa.insert(gadgets).values(code=code))


def codes():
    return sorted(Gadget.objects.values_list("code", flat=True))


def write(code, fail=False):
    with transaction.atomic():
        add(code)
        if fail:
            raise RuntimeError(code)
    return code


# --- calling convention ------------------------------------------------------


def test_returns_what_fn_returns_and_passes_its_arguments():
    def fn(a, b, *, c):
        return (a, b, c)

    async def main():
        return await arun(fn, 1, 2, c=3)

    assert run(main) == (1, 2, 3)


def test_fn_is_positional_only_so_fn_may_take_a_keyword_named_fn():
    async def main():
        return await arun(lambda **kw: kw, fn="x", using="other")

    assert run(main) == {"fn": "x", "using": "other"}


def test_the_exception_fn_raises_reaches_the_caller_unchanged():
    error = LookupError("boom")

    def fn():
        raise error

    async def main():
        await arun(fn)

    with pytest.raises(LookupError) as excinfo:
        run(main)
    assert excinfo.value is error


def test_a_coroutine_function_is_refused():
    async def fn():
        return 1

    async def main():
        await arun(fn)

    with pytest.raises(TypeError, match="coroutine function"):
        run(main)


def test_a_sync_callable_that_returns_an_awaitable_is_refused():
    async def fn():
        return 1

    async def main():
        await arun(lambda: fn())

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # the stray coroutine is closed, not left to warn
        with pytest.raises(TypeError, match="awaitable"):
            run(main)


# --- the unit of work runs on Django's connection, in one hop -----------------


def test_the_whole_fn_runs_in_one_thread_hop():
    async def main():
        return await arun(lambda: [execute(sa.select(gadgets.c.id)), Gadget.objects.count(), add("a")])

    assert run(main) == [[], 0, 1]
    assert codes() == ["a"]


def test_queries_inside_arun_are_captured_and_counted():
    def fn():
        with CaptureQueriesContext(connection) as inner:
            add("a")
            execute(sa.select(gadgets.c.code))
        return len(inner.captured_queries)

    async def main():
        return await arun(fn)

    with CaptureQueriesContext(connection) as outer:
        assert run(main) == 2
    assert len(outer.captured_queries) == 2


@pytest.mark.django_db(transaction=True)
def test_atomic_inside_arun_commits_and_rolls_back():
    async def main():
        assert await arun(write, "kept") == "kept"
        with pytest.raises(RuntimeError):
            await arun(write, "dropped", True)

    run(main)
    assert codes() == ["kept"]


@pytest.mark.django_db(transaction=True)
def test_on_commit_runs_after_the_commit_and_not_after_a_rollback():
    calls = []

    def fn(code, fail):
        with transaction.atomic():
            add(code)
            transaction.on_commit(lambda: calls.append((code, Gadget.objects.count())))
            assert code not in [c for c, _ in calls]  # not before the commit
            if fail:
                raise RuntimeError(code)

    async def main():
        await arun(fn, "a", False)
        with pytest.raises(RuntimeError):
            await arun(fn, "b", True)

    run(main)
    assert calls == [("a", 1)]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failing", [0, 1])
def test_two_aruns_gathered_commit_or_roll_back_independently(failing):
    jobs = [("first", failing == 0), ("second", failing == 1)]

    async def main():
        return await asyncio.gather(*(arun(write, code, fail) for code, fail in jobs), return_exceptions=True)

    outcomes = run(main)
    assert isinstance(outcomes[failing], RuntimeError)
    kept = jobs[1 - failing][0]
    assert outcomes[1 - failing] == kept
    assert codes() == [kept]


def test_outside_a_running_event_loop_thread_too():
    # asyncio.run gives arun no calling sync thread: it hops to asgiref's shared one.
    async def main():
        return await arun(lambda: Gadget.objects.exists())

    assert asyncio.run(main()) in (True, False)


# --- connection lifetime -----------------------------------------------------


def test_a_connection_inside_an_outer_atomic_is_left_open():
    # Here the test's own transaction wraps the call; closing it would break it.
    Gadget.objects.create(code="before")

    async def main():
        return await arun(codes)

    assert run(main) == ["before"]
    assert connection.in_atomic_block
    assert codes() == ["before"]


@pg_only
@pytest.mark.django_db(transaction=True)
def test_an_obsolete_connection_is_closed_around_fn():
    # CONN_MAX_AGE is 0 in the test settings: like a request, each unit of work
    # ends by closing the connection it used.
    async def main():
        return await arun(lambda: execute(sa.select(sa.literal(1))))

    assert run(main) == [(1,)]
    assert connection.connection is None


@pytest.fixture
def persistent_connection():
    # Keep the connection open between units of work, as CONN_MAX_AGE=None does.
    settings = connection.settings_dict
    saved = settings["CONN_MAX_AGE"], settings["CONN_HEALTH_CHECKS"]
    settings["CONN_MAX_AGE"] = None
    connection.close()
    yield settings
    settings["CONN_MAX_AGE"], settings["CONN_HEALTH_CHECKS"] = saved
    connection.close()


def backend_pid():
    return execute(sa.select(sa.func.pg_backend_pid()))[0][0]


@pg_only
@pytest.mark.django_db(transaction=True)
def test_a_connection_dropped_by_the_server_is_replaced_after_the_failure(persistent_connection):
    async def main():
        pid = await arun(backend_pid)
        await arun(terminate, pid)
        with pytest.raises(OperationalError):
            await arun(backend_pid)  # nothing checks the connection before this query
        return pid, await arun(backend_pid)

    old, new = run(main)
    assert old != new


@pg_only
@pytest.mark.django_db(transaction=True)
def test_with_health_checks_a_dropped_connection_is_replaced_before_fn(persistent_connection):
    persistent_connection["CONN_HEALTH_CHECKS"] = True

    async def main():
        pid = await arun(backend_pid)
        await arun(terminate, pid)
        return pid, await arun(backend_pid)

    old, new = run(main)
    assert old != new


def terminate(pid):
    # From a separate connection, as an administrator or a failover would.
    settings = connection.settings_dict
    with psycopg.connect(
        host=settings["HOST"],
        port=settings["PORT"],
        user=settings["USER"],
        password=settings["PASSWORD"],
        dbname=settings["NAME"],
        autocommit=True,
    ) as other:
        other.execute("SELECT pg_terminate_backend(%s)", (pid,))


# --- direct calls from async code stay refused --------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: execute(sa.select(gadgets.c.id)),
        lambda: next(stream(sa.select(gadgets.c.id))),
        lambda: bulk_insert(gadgets, [{"code": "x"}]),
    ],
    ids=["execute", "stream", "bulk_insert"],
)
def test_calling_the_bridge_directly_from_async_code_is_refused(call):
    async def main():
        call()

    with pytest.raises(SynchronousOnlyOperation):
        asyncio.run(main())
