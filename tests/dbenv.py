"""Which database the test run targets, read from the environment.

DSB_DB=sqlite|postgres|mysql|oracle picks the backend. Left unset it is sqlite,
unless REQUIRE_PG=1 asks for PostgreSQL, REQUIRE_MYSQL=1 for MySQL or
REQUIRE_ORACLE=1 for Oracle. PostgreSQL is reached through
DSB_PG_HOST/PORT/USER/PASSWORD/NAME, MySQL through
DSB_MYSQL_HOST/PORT/USER/PASSWORD/NAME, Oracle through
DSB_ORACLE_HOST/PORT/USER/PASSWORD/NAME (NAME is the service name; the user
creates the test user, as Django's Oracle test runner does, so it is an
administrator such as SYSTEM). Without the backend's HOST the database tests are
skipped, and REQUIRE_PG=1 / REQUIRE_MYSQL=1 / REQUIRE_ORACLE=1 turns that skip
into a failure. DSB_PG_SERVER_SIDE_BINDING=1 connects PostgreSQL with psycopg's
server-side binding and prepared statements from the fifth run of a statement
(OPTIONS server_side_binding=True, prepare_threshold=5) instead of Django's default.

A second alias, "other", is always an in-memory SQLite database with its own
TIME_ZONE, for tests that run one statement on two connections.
"""

from __future__ import annotations

import os

REQUIRE_PG = os.environ.get("REQUIRE_PG") == "1"
REQUIRE_MYSQL = os.environ.get("REQUIRE_MYSQL") == "1"
REQUIRE_ORACLE = os.environ.get("REQUIRE_ORACLE") == "1"
BACKEND = os.environ.get("DSB_DB") or (
    "postgres" if REQUIRE_PG else "mysql" if REQUIRE_MYSQL else "oracle" if REQUIRE_ORACLE else "sqlite"
)
if BACKEND not in ("sqlite", "postgres", "mysql", "oracle"):
    raise RuntimeError(f"DSB_DB must be 'sqlite', 'postgres', 'mysql' or 'oracle', not {BACKEND!r}")

PG_CONFIGURED = bool(os.environ.get("DSB_PG_HOST"))
MYSQL_CONFIGURED = bool(os.environ.get("DSB_MYSQL_HOST"))
ORACLE_CONFIGURED = bool(os.environ.get("DSB_ORACLE_HOST"))
PG_SERVER_SIDE_BINDING = os.environ.get("DSB_PG_SERVER_SIDE_BINDING") == "1"
# Database tests cannot run: a server backend was asked for but no server was given.
PG_MISSING = BACKEND == "postgres" and not PG_CONFIGURED
MYSQL_MISSING = BACKEND == "mysql" and not MYSQL_CONFIGURED
ORACLE_MISSING = BACKEND == "oracle" and not ORACLE_CONFIGURED


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
    if BACKEND == "oracle":
        # Django makes a DSN from HOST, PORT and NAME as a SID; the free images
        # serve a pluggable database by service name, so give an Easy Connect string.
        host, port = os.environ.get("DSB_ORACLE_HOST", ""), os.environ.get("DSB_ORACLE_PORT", "1521")
        return {
            "ENGINE": "django.db.backends.oracle",
            "NAME": f"{host}:{port}/{os.environ.get('DSB_ORACLE_NAME', 'FREEPDB1')}",
            "USER": os.environ.get("DSB_ORACLE_USER", "system"),
            "PASSWORD": os.environ.get("DSB_ORACLE_PASSWORD", ""),
        }
    settings = {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("DSB_PG_HOST", ""),
        "PORT": os.environ.get("DSB_PG_PORT", "5432"),
        "USER": os.environ.get("DSB_PG_USER", "postgres"),
        "PASSWORD": os.environ.get("DSB_PG_PASSWORD", ""),
        "NAME": os.environ.get("DSB_PG_NAME", "postgres"),
    }
    if PG_SERVER_SIDE_BINDING:
        settings["OPTIONS"] = {"server_side_binding": True, "prepare_threshold": 5}
    return settings


def other_database() -> dict:
    # Naive datetimes stored in New York time, unlike "default" (UTC on SQLite).
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:", "TIME_ZONE": "America/New_York"}
