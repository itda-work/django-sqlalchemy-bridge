"""Scenario 7: bulk_insert and bulk_update of hundreds of thousands of rows, with and without batch_size.

On PostgreSQL ``batch_size=None`` sends every row in one ``unnest()`` statement,
one array per column; a batch size sends one such statement per that many rows.
``method="copy"`` (PostgreSQL only) streams the rows in one ``COPY`` per batch.
Besides the time and the client's tracemalloc peak this measures the server
backend's memory: one extra repetition on a fresh connection samples the
backend process's ``/proc/<pid>/status`` (found by the backend pid in its
namespace, so it works for a PostgreSQL in a container on the same machine,
as on the GitHub Actions runner) and records the peak of its private memory
(``RssAnon``) over the call, and ``VmHWM``. Where the backend process cannot be
seen (PostgreSQL in a VM, on another host) that is recorded instead.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from django.db import connection

from bench import data
from bench.harness import Variant, django_rollback, run_variants

SIZES = [200_000, 1_000_000]
BATCH_SIZES = [None, 10_000, 50_000, 200_000]
RETURNING_BATCH_SIZES = [None, 50_000]
COPY_BATCH_SIZES = [None, 10_000]
FIELDS = ["label", "amount", "price"]

SCOPE = (
    "Timed: the bridge call alone. Untimed: building the input dicts and the transaction, rolled back after "
    "each run. 'server' is one more run on a fresh connection, sampling the PostgreSQL backend process's "
    "/proc status every 2 ms: rss_anon_peak_delta_bytes is the peak of its private memory over the call minus "
    "before it, vm_hwm_bytes its peak resident set (shared buffers included) since the connection opened."
)


def _key(op: str, batch_size: int | None) -> str:
    return f"{op}_{'none' if batch_size is None else f'batch{batch_size}'}"


def _note(batch_size: int | None) -> str:
    if batch_size is None:
        return "batch_size=None: PostgreSQL one statement, SQLite executemany (RETURNING: bulk_batch_size() batches)"
    return f"batch_size={batch_size}"


def variants(op: str, n: int) -> list[Variant]:
    from bench.benchapp.models import Item, NewItem
    from django_sqlalchemy_bridge import bulk_insert, bulk_update

    if op == "update":

        def check(result, _data, _handle):
            assert result == n, result
            assert Item.objects.filter(label__startswith="changed-").count() == n

        return [
            Variant(
                _key("update", size),
                lambda rows, _, size=size: bulk_update(Item, rows, batch_size=size),
                lambda: [data.changed(i) for i in range(1, n + 1)],
                check=check,
                note=_note(size),
            )
            for size in BATCH_SIZES
        ]

    def dicts():
        return [data.new_row(i) for i in range(1, n + 1)]

    def check(result, _data, _handle):
        assert (len(result) if isinstance(result, list) else result) == n, result
        assert NewItem.objects.count() == n

    out = [
        Variant(
            _key("insert", size),
            lambda rows, _, size=size: bulk_insert(NewItem, rows, batch_size=size),
            dicts,
            check=check,
            note=_note(size),
        )
        for size in BATCH_SIZES
    ]
    out += [
        Variant(
            _key("insert_returning", size),
            lambda rows, _, size=size: bulk_insert(NewItem, rows, returning=["id"], batch_size=size),
            dicts,
            check=check,
            note=f"returning=['id'], {_note(size)}",
        )
        for size in RETURNING_BATCH_SIZES
    ]
    out += [
        Variant(
            _key("copy", size),
            lambda rows, _, size=size: bulk_insert(NewItem, rows, method="copy", batch_size=size),
            dicts,
            check=check,
            vendors=("postgresql",),
            note=f"method='copy' (COPY FROM STDIN, one per batch), batch_size={size}",
        )
        for size in COPY_BATCH_SIZES
    ]
    return out


# --- server memory -----------------------------------------------------------


def _status(pid: int) -> dict[str, int] | None:
    """The memory lines of ``/proc/<pid>/status``, in bytes."""
    try:
        text = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return None
    out = {}
    for line in text.splitlines():
        name, _, value = line.partition(":")
        if name in ("VmHWM", "VmRSS", "RssAnon", "RssShmem"):
            out[name] = int(value.split()[0]) * 1024
    return out


def _host_pid(backend_pid: int) -> int | None:
    """The pid, as this machine sees it, of the postgres process that is ``backend_pid`` in its own namespace."""
    found = []
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            lines = dict(line.partition(":")[::2] for line in status.read_text().splitlines())
        except OSError:
            continue
        nspid = lines.get("NSpid", "").split()
        if lines.get("Name", "").strip() == "postgres" and nspid and int(nspid[-1]) == backend_pid:
            found.append(int(status.parent.name))
    return found[0] if len(found) == 1 else None


class _Sampler(threading.Thread):
    def __init__(self, pid: int):
        super().__init__(daemon=True)
        self.pid = pid
        self.peak_anon = 0
        self.samples = 0
        self.done = threading.Event()

    def run(self):
        while not self.done.is_set():
            status = _status(self.pid)
            if status is not None:
                self.peak_anon = max(self.peak_anon, status.get("RssAnon", 0))
                self.samples += 1
            time.sleep(0.002)


def _temp_bytes() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_stat_force_next_flush()")
    with connection.cursor() as cursor:
        cursor.execute("SELECT temp_bytes FROM pg_stat_database WHERE datname = current_database()")
        return cursor.fetchone()[0]


def server_memory(variant: Variant) -> dict:
    """Run ``variant`` once more on a fresh connection and sample its backend process."""
    connection.close()
    rows = variant.prepare()
    temp_before = _temp_bytes()
    with django_rollback():
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            backend_pid = cursor.fetchone()[0]
        pid = _host_pid(backend_pid)
        if pid is None:
            return {"unavailable": f"no visible postgres process with backend pid {backend_pid} under /proc"}
        before = _status(pid)
        sampler = _Sampler(pid)
        sampler.start()
        variant.run(rows, None)
        sampler.done.set()
        sampler.join()
        after = _status(pid)
    del rows
    return {
        "rss_anon_before_bytes": before["RssAnon"],
        "rss_anon_peak_bytes": sampler.peak_anon,
        "rss_anon_peak_delta_bytes": sampler.peak_anon - before["RssAnon"],
        "vm_hwm_bytes": after["VmHWM"],
        "samples": sampler.samples,
        "temp_bytes": _temp_bytes() - temp_before,
    }


def run(engine, sizes=None) -> dict:
    from bench.benchapp.models import Item, NewItem

    cases = []
    for op in ("insert", "update"):
        for n in sizes or SIZES:
            print(f"  batch {op} n={n}", flush=True)
            if op == "update":
                data.seed(n)
            else:
                data.truncate(NewItem)
            model = Item if op == "update" else NewItem
            vs = variants(op, n)
            results = run_variants(vs, connection.vendor, after=lambda model=model: data.vacuum(model))
            if connection.vendor == "postgresql":
                for v in vs:
                    if "error" in results[v.key] or "skipped" in results[v.key]:
                        continue
                    results[v.key]["server"] = server_memory(v)
                    data.vacuum(model)
                    server = results[v.key]["server"]
                    if "rss_anon_peak_delta_bytes" in server:
                        print(
                            f"    {v.key:<34} server +{server['rss_anon_peak_delta_bytes'] / 2**20:8.1f} MiB anon",
                            flush=True,
                        )
            cases.append({"op": op, "n": n, "variants": results})
    if connection.vendor == "postgresql":
        data.truncate(Item)
    return {"scope": SCOPE, "columns_set": FIELDS, "cases": cases}
