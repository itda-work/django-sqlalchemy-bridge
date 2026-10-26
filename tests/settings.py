"""Minimal Django project the test suite runs against."""

from tests import dbenv

SECRET_KEY = "django-sqlalchemy-bridge-tests"
USE_TZ = True
TIME_ZONE = "Asia/Seoul"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "tests.testapp",
    # The benchmark models, so compare_table can check them against a live table too.
    "bench.benchapp",
]

DATABASES = {"default": dbenv.database(), "other": dbenv.other_database()}
