"""Minimal Django project the test suite runs against."""

from tests import dbenv

SECRET_KEY = "django-sqlalchemy-bridge-tests"
USE_TZ = True
TIME_ZONE = "Asia/Seoul"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "tests.testapp",
]

DATABASES = {"default": dbenv.database()}
