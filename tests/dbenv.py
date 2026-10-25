"""Which database the test run targets, read from the environment.

DSB_DB=sqlite|postgres picks the backend. Left unset it is sqlite, unless
REQUIRE_PG=1 asks for PostgreSQL. PostgreSQL is reached through
DSB_PG_HOST/PORT/USER/PASSWORD/NAME; without DSB_PG_HOST the database tests are
skipped, and REQUIRE_PG=1 turns that skip into a failure.
"""

from __future__ import annotations

import os

REQUIRE_PG = os.environ.get("REQUIRE_PG") == "1"
BACKEND = os.environ.get("DSB_DB") or ("postgres" if REQUIRE_PG else "sqlite")
if BACKEND not in ("sqlite", "postgres"):
    raise RuntimeError(f"DSB_DB must be 'sqlite' or 'postgres', not {BACKEND!r}")

PG_CONFIGURED = bool(os.environ.get("DSB_PG_HOST"))
# Database tests cannot run: PostgreSQL was asked for but no server was given.
PG_MISSING = BACKEND == "postgres" and not PG_CONFIGURED


def database() -> dict:
    if BACKEND == "sqlite":
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": os.environ.get("DSB_PG_HOST", ""),
        "PORT": os.environ.get("DSB_PG_PORT", "5432"),
        "USER": os.environ.get("DSB_PG_USER", "postgres"),
        "PASSWORD": os.environ.get("DSB_PG_PASSWORD", ""),
        "NAME": os.environ.get("DSB_PG_NAME", "postgres"),
    }
