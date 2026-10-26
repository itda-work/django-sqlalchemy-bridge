"""Which database the test run targets, read from the environment.

DSB_DB=sqlite|postgres|mysql picks the backend. Left unset it is sqlite, unless
REQUIRE_PG=1 asks for PostgreSQL or REQUIRE_MYSQL=1 for MySQL. PostgreSQL is
reached through DSB_PG_HOST/PORT/USER/PASSWORD/NAME, MySQL through
DSB_MYSQL_HOST/PORT/USER/PASSWORD/NAME; without the backend's HOST the
database tests are skipped, and REQUIRE_PG=1 / REQUIRE_MYSQL=1 turns that skip
into a failure.

A second alias, "other", is always an in-memory SQLite database with its own
TIME_ZONE, for tests that run one statement on two connections.
"""

from __future__ import annotations

import os

REQUIRE_PG = os.environ.get("REQUIRE_PG") == "1"
REQUIRE_MYSQL = os.environ.get("REQUIRE_MYSQL") == "1"
BACKEND = os.environ.get("DSB_DB") or ("postgres" if REQUIRE_PG else "mysql" if REQUIRE_MYSQL else "sqlite")
if BACKEND not in ("sqlite", "postgres", "mysql"):
    raise RuntimeError(f"DSB_DB must be 'sqlite', 'postgres' or 'mysql', not {BACKEND!r}")

PG_CONFIGURED = bool(os.environ.get("DSB_PG_HOST"))
MYSQL_CONFIGURED = bool(os.environ.get("DSB_MYSQL_HOST"))
# Database tests cannot run: a server backend was asked for but no server was given.
PG_MISSING = BACKEND == "postgres" and not PG_CONFIGURED
MYSQL_MISSING = BACKEND == "mysql" and not MYSQL_CONFIGURED


def database() -> dict:
    if BACKEND == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    if BACKEND == "mysql":
        return {
            "ENGINE": "django.db.backends.mysql",
            "HOST": os.environ.get("DSB_MYSQL_HOST", ""),
            "PORT": os.environ.get("DSB_MYSQL_PORT", "3306"),
            "USER": os.environ.get("DSB_MYSQL_USER", "root"),
            "PASSWORD": os.environ.get("DSB_MYSQL_PASSWORD", ""),
            "NAME": os.environ.get("DSB_MYSQL_NAME", "mysql"),
            "OPTIONS": {"charset": "utf8mb4"},
            "TEST": {"CHARSET": "utf8mb4", "COLLATION": "utf8mb4_bin"},
        }
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("DSB_PG_HOST", ""),
        "PORT": os.environ.get("DSB_PG_PORT", "5432"),
        "USER": os.environ.get("DSB_PG_USER", "postgres"),
        "PASSWORD": os.environ.get("DSB_PG_PASSWORD", ""),
        "NAME": os.environ.get("DSB_PG_NAME", "postgres"),
    }


def other_database() -> dict:
    # Naive datetimes stored in New York time, unlike "default" (UTC on SQLite).
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:", "TIME_ZONE": "America/New_York"}
