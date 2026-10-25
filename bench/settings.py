"""Django settings for the benchmarks, picked by the same variables as the tests.

``DSB_DB=sqlite|postgres``; PostgreSQL is reached through
``DSB_PG_HOST/PORT/USER/PASSWORD/NAME``. The benchmark database itself is a
throwaway one Django's test-database machinery creates and drops
(:func:`bench.env.setup_database`); ``DSB_BENCH_DB_NAME`` points a child process
at the one its parent created.
"""

import os

SECRET_KEY = "django-sqlalchemy-bridge-bench"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = ["bench.benchapp"]

BACKEND = os.environ.get("DSB_DB", "sqlite")
_child_db = os.environ.get("DSB_BENCH_DB_NAME")

if BACKEND == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _child_db or "",
            "TEST": {"NAME": os.environ.get("DSB_BENCH_SQLITE_FILE", "")},
        }
    }
elif BACKEND == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "HOST": os.environ.get("DSB_PG_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DSB_PG_PORT", "5432"),
            "USER": os.environ.get("DSB_PG_USER", "postgres"),
            "PASSWORD": os.environ.get("DSB_PG_PASSWORD", ""),
            "NAME": _child_db or os.environ.get("DSB_PG_NAME", "postgres"),
            "TEST": {"NAME": "dsb_bench"},
        }
    }
else:
    raise RuntimeError(f"DSB_DB must be 'sqlite' or 'postgres', not {BACKEND!r}")
