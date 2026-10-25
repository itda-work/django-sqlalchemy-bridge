"""A fresh process that measures one read variant's RSS growth, for :mod:`bench.scenarios.read`.

    DSB_DB=... DSB_BENCH_DB_NAME=<benchmark database> python -m bench.child read <variant>

Connects (Django, and the SQLAlchemy engine), then records the RSS before and
after running the variant while still holding its result, and prints one JSON line.
"""

from __future__ import annotations

import gc
import json
import os
import resource
import sys


def _maxrss() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # bytes on macOS, KiB on Linux


def main(argv: list[str]) -> None:
    from bench import env

    scenario, key = argv
    if scenario != "read":
        raise SystemExit(f"no RSS measurement for scenario {scenario!r}")
    env.configure(os.environ["DSB_DB"])

    import psutil
    from django.db import connection

    from bench.scenarios import read

    engine = env.sa_engine()
    (variant,) = [v for v in read.variants(engine) if v.key == key]
    connection.ensure_connection()
    engine.connect().close()  # pooled, as in the parent's timed runs
    process = psutil.Process()
    gc.collect()
    rss0, max0 = process.memory_info().rss, _maxrss()
    result = variant.run(None, None)
    gc.collect()
    rss1, max1 = process.memory_info().rss, _maxrss()
    count = result if isinstance(result, int) else len(result)
    print(json.dumps({"rows": count, "rss_delta_bytes": rss1 - rss0, "maxrss_delta_bytes": max1 - max0}))


if __name__ == "__main__":
    main(sys.argv[1:])
