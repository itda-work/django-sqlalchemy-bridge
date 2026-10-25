import pytest

from tests import dbenv


def pytest_report_header(config):
    return f"django-sqlalchemy-bridge: DSB_DB={dbenv.BACKEND} REQUIRE_PG={int(dbenv.REQUIRE_PG)}"


def pytest_collection_modifyitems(config, items):
    if not dbenv.PG_MISSING:
        return
    db_items = [item for item in items if item.get_closest_marker("django_db")]
    if not db_items:
        return
    if dbenv.REQUIRE_PG:
        raise pytest.UsageError("REQUIRE_PG=1 but DSB_PG_HOST is not set; refusing to skip the PostgreSQL tests")
    skip = pytest.mark.skip(reason="PostgreSQL not configured (set DSB_PG_HOST)")
    for item in db_items:
        item.add_marker(skip)
