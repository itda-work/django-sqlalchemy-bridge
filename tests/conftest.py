import pytest

from tests import dbenv


def pytest_report_header(config):
    return (
        f"django-sqlalchemy-bridge: DSB_DB={dbenv.BACKEND} REQUIRE_PG={int(dbenv.REQUIRE_PG)}"
        f" REQUIRE_MYSQL={int(dbenv.REQUIRE_MYSQL)} REQUIRE_ORACLE={int(dbenv.REQUIRE_ORACLE)}"
        f" DSB_PG_SERVER_SIDE_BINDING={int(dbenv.PG_SERVER_SIDE_BINDING)}"
        f" REQUIRE_CROSS={int(dbenv.REQUIRE_CROSS)} cross-vendor aliases={int(dbenv.CROSS_CONFIGURED)}"
    )


def pytest_collection_modifyitems(config, items):
    if dbenv.REQUIRE_CROSS and not dbenv.CROSS_CONFIGURED:
        raise pytest.UsageError(
            "REQUIRE_CROSS=1 but DSB_PG_HOST, DSB_MYSQL_HOST and DSB_ORACLE_HOST are not all set;"
            " refusing to skip the cross-vendor tests"
        )
    if dbenv.PG_MISSING:
        name, host, required, flag = "PostgreSQL", "DSB_PG_HOST", dbenv.REQUIRE_PG, "REQUIRE_PG"
    elif dbenv.MYSQL_MISSING:
        name, host, required, flag = "MySQL", "DSB_MYSQL_HOST", dbenv.REQUIRE_MYSQL, "REQUIRE_MYSQL"
    elif dbenv.ORACLE_MISSING:
        name, host, required, flag = "Oracle", "DSB_ORACLE_HOST", dbenv.REQUIRE_ORACLE, "REQUIRE_ORACLE"
    else:
        return
    db_items = [item for item in items if item.get_closest_marker("django_db")]
    if not db_items:
        return
    if required:
        raise pytest.UsageError(f"{flag}=1 but {host} is not set; refusing to skip the {name} tests")
    skip = pytest.mark.skip(reason=f"{name} not configured (set {host})")
    for item in db_items:
        item.add_marker(skip)


@pytest.fixture
def needs_returning(db):
    """Skip unless the bridge runs INSERT and DELETE ... RETURNING there (not on MySQL or Oracle)."""
    from django.db import connection

    if not connection.features.can_return_columns_from_insert:
        pytest.skip(f"{connection.display_name} has no RETURNING")
    if connection.vendor == "oracle":
        pytest.skip("Oracle returns RETURNING rows into output binds, which the bridge refuses")


@pytest.fixture
def needs_update_returning(needs_returning):
    """Skip unless the database has UPDATE ... RETURNING too (MariaDB has not)."""
    from django.db import connection

    if connection.vendor == "mysql":
        pytest.skip(f"{connection.display_name} has no UPDATE ... RETURNING")
