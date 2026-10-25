"""Run the benchmark scenarios and write one result file per scenario.

    uv run --group bench python -m bench [scenario ...] [--db sqlite|postgres] [--out DIR] [--quick]

Scenarios: update, insert, read, lookup, async, report (all by default).
PostgreSQL is reached through DSB_PG_HOST/PORT/USER/PASSWORD/NAME; the run
creates and drops its own database (``dsb_bench``) there. ``--quick`` runs the
smallest size only, once, for trying the scripts out; its files are marked so.
"""

from __future__ import annotations

import argparse
import datetime
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from bench import env

SCENARIOS = {
    "update": "bench.scenarios.update",
    "insert": "bench.scenarios.insert",
    "read": "bench.scenarios.read",
    "lookup": "bench.scenarios.lookup",
    "async": "bench.scenarios.aio",
    "report": "bench.scenarios.report",
}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m bench")
    parser.add_argument("scenarios", nargs="*", metavar="scenario", help=", ".join(SCENARIOS))
    parser.add_argument("--db", choices=["sqlite", "postgres"], default="sqlite")
    parser.add_argument("--out", type=Path, default=env.ROOT / "bench" / "results")
    parser.add_argument("--quick", action="store_true", help="smallest size, one repetition")
    args = parser.parse_args(argv)
    names = args.scenarios or list(SCENARIOS)
    if unknown := [n for n in names if n not in SCENARIOS]:
        parser.error(f"unknown scenario(s) {', '.join(unknown)}; choose from {', '.join(SCENARIOS)}")

    with tempfile.TemporaryDirectory(prefix="dsb-bench-") as tmp:
        if args.db == "sqlite":
            os.environ["DSB_BENCH_SQLITE_FILE"] = str(Path(tmp) / "bench.sqlite3")
        env.configure(args.db)
        from bench import harness

        if args.quick:
            harness.REPEAT, harness.WARMUP = 1, 0
        old_name = env.setup_database()
        engine = env.sa_engine()
        try:
            described = env.describe()
            print(f"{described['database']['server']} | {described['machine']['cpu']}", flush=True)
            args.out.mkdir(parents=True, exist_ok=True)
            today = datetime.date.today().isoformat()
            for name in names:
                module = importlib.import_module(SCENARIOS[name])
                print(f"[{name}]", flush=True)
                started = time.time()
                load_before = os.getloadavg()
                sizes = module.SIZES[:1] if args.quick and hasattr(module, "SIZES") else None
                body = module.run(engine, sizes)
                result = {
                    "scenario": name,
                    "db": args.db,
                    "date": today,
                    "quick": args.quick,
                    "command": " ".join(["python -m bench", name, f"--db {args.db}"] + (["--quick"] * args.quick)),
                    "method": {
                        "repeat": harness.REPEAT,
                        "warmup": harness.WARMUP,
                        "time": "perf_counter around the measured call; best_s = min(times_s)",
                        "memory": "tracemalloc peak of one extra run, minus what was traced at its start",
                    },
                    "environment": described,
                    "elapsed_s": round(time.time() - started, 1),
                    # Other processes on the machine: 1/5/15-minute load averages around the scenario.
                    "load_average": {"before": load_before, "after": os.getloadavg()},
                    **body,
                }
                path = args.out / f"{today}-{args.db}-{name}{'-quick' if args.quick else ''}.json"
                path.write_text(json.dumps(result, indent=1, ensure_ascii=False, default=str) + "\n")
                print(f"  -> {path}", flush=True)
        finally:
            engine.dispose()
            env.teardown_database(old_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
