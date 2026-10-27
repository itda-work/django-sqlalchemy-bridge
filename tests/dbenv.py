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

When all three servers are given (DSB_PG_HOST, DSB_MYSQL_HOST and
DSB_ORACLE_HOST), three more aliases, "pg", "mysql" and "oracle", reach them for
the cross-vendor tests, each with its own test database name (test user and
tablespaces on Oracle) so it never shares a test database with "default" on the
same server. Without one of them the aliases are left out, and REQUIRE_CROSS=1
turns that into a failure.
"""

from __future__ import annotations

import os

REQUIRE_PG = os.environ.get("REQUIRE_PG") == "1"
REQUIRE_MYSQL = os.environ.get("REQUIRE_MYSQL") == "1"
REQUIRE_ORACLE = os.environ.get("REQUIRE_ORACLE") == "1"
REQUIRE_CROSS = os.environ.get("REQUIRE_CROSS") == "1"
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
CROSS_CONFIGURED = PG_CONFIGURED and MYSQL_CONFIGURED and ORACLE_CONFIGURED


def database() -> dict:
    if BACKEND == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    return {"postgres": postgres_database, "mysql": mysql_database, "oracle": oracle_database}[BACKEND]()


def postgres_database() -> dict:
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


def mysql_database() -> dict:
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


def oracle_database() -> dict:
    # Django makes a DSN from HOST, PORT and NAME as a SID; the free images
    # serve a pluggable database by service name, so give an Easy Connect string.
    host, port = os.environ.get("DSB_ORACLE_HOST", ""), os.environ.get("DSB_ORACLE_PORT", "1521")
    return {
        "ENGINE": "django.db.backends.oracle",
        "NAME": f"{host}:{port}/{os.environ.get('DSB_ORACLE_NAME', 'FREEPDB1')}",
        "USER": os.environ.get("DSB_ORACLE_USER", "system"),
        "PASSWORD": os.environ.get("DSB_ORACLE_PASSWORD", ""),
    }


def other_database() -> dict:
    # Naive datetimes stored in New York time, unlike "default" (UTC on SQLite).
    return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:", "TIME_ZONE": "America/New_York"}


def cross_vendor_databases() -> dict:
    """The "pg", "mysql" and "oracle" aliases, or none unless all three servers are given."""
    if not CROSS_CONFIGURED:
        return {}
    # Test database names of their own: Django merges aliases whose test settings
    # match into one test database, and two connections to it would not see each
    # other's uncommitted writes.
    pg, mysql, oracle = postgres_database(), mysql_database(), oracle_database()
    pg["TEST"] = {"NAME": f"test_{pg['NAME']}_cross"}
    mysql["TEST"] = {**mysql["TEST"], "NAME": f"test_{mysql['NAME']}_cross"}
    user = f"test_{oracle['USER']}_cross"
    oracle["TEST"] = {"USER": user, "TBLSPACE": user, "TBLSPACE_TMP": f"{user}_temp"}
    return {"pg": pg, "mysql": mysql, "oracle": oracle}
