"""The benchmark database, the SQLAlchemy engine pointed at it, and what the run ran on."""

from __future__ import annotations

import os
import platform
import sqlite3
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parent.parent


def configure(db: str) -> None:
    """Point Django at the benchmark settings for backend ``db`` and set it up."""
    os.environ["DSB_DB"] = db
    os.environ["DJANGO_SETTINGS_MODULE"] = "bench.settings"
    django.setup()


def setup_database() -> str:
    """Create the throwaway benchmark database with the tables, and return its name."""
    from django.db import connection

    return connection.creation.create_test_db(verbosity=0, autoclobber=True, serialize=False)


def teardown_database(old_name: str) -> None:
    from django.db import connection

    connection.creation.destroy_test_db(old_name, verbosity=0)


def sa_engine():
    """A SQLAlchemy engine on the benchmark database, for the SQLAlchemy-only comparisons.

    Not the bridge: it opens its own connections, outside Django's transactions.
    """
    import sqlalchemy as sa
    from django.db import connection

    s = connection.settings_dict
    if connection.vendor == "sqlite":
        return sa.create_engine(f"sqlite:///{s['NAME']}")
    url = sa.URL.create(
        "postgresql+psycopg",
        username=s["USER"],
        password=s["PASSWORD"],
        host=s["HOST"],
        port=int(s["PORT"]),
        database=s["NAME"],
    )
    return sa.create_engine(url)


def _package(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _run(command: list[str]) -> str | None:
    try:
        done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def _cpu() -> str | None:
    if sys.platform == "darwin":
        return _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or None


def _memory_bytes() -> int | None:
    try:
        import psutil
    except ImportError:
        return None
    return psutil.virtual_memory().total


def describe() -> dict:
    """Versions, database server and machine, recorded in every result file."""
    from django.db import connection

    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute("SHOW server_version")
            server = f"PostgreSQL {cursor.fetchone()[0]}"
        else:
            cursor.execute("SELECT sqlite_version()")
            server = f"SQLite {cursor.fetchone()[0]}"
    return {
        "revision": _run(["git", "describe", "--always", "--dirty"]),
        "python": platform.python_version(),
        "packages": {
            name: _package(name)
            for name in (
                "django-sqlalchemy-bridge",
                "django",
                "sqlalchemy",
                "psycopg",
                "psycopg-binary",
                "django-pgbulk",
                "django-cte",
                "asgiref",
            )
        },
        "python_sqlite3": sqlite3.sqlite_version,
        "database": {"vendor": connection.vendor, "server": server},
        "machine": {
            "os": platform.platform(),
            "cpu": _cpu(),
            "cpu_count": os.cpu_count(),
            "memory_bytes": _memory_bytes(),
        },
    }
