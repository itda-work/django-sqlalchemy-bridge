"""django-sqlalchemy-bridge — experimental, the API may change without notice. Agents: read llms.txt here.

llms.txt next to this file is the guide for AI coding agents: what to use the
bridge for, the rules it depends on (no SQLAlchemy Engine or Session; writes
skip signals and save()), the public signatures and worked examples. Print it with

    python -c "import importlib.resources as r; print(r.files('django_sqlalchemy_bridge').joinpath('llms.txt').read_text('utf-8'))"

(``uv run python -c ...`` in a uv project). See README.md for the architecture.
"""  # noqa: E501 - the command above is copied whole

from importlib.metadata import PackageNotFoundError, version

from django_sqlalchemy_bridge.asynchronous import arun
from django_sqlalchemy_bridge.bulk import bulk_insert, bulk_update
from django_sqlalchemy_bridge.execution import execute, execute_pipeline, execute_result, stream
from django_sqlalchemy_bridge.tables import (
    Difference,
    FieldType,
    UnsupportedFieldError,
    assert_table_matches,
    compare_table,
    forget_table,
    metadata,
    table_for,
)

try:
    __version__ = version("django-sqlalchemy-bridge")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = [
    "Difference",
    "FieldType",
    "UnsupportedFieldError",
    "__version__",
    "arun",
    "assert_table_matches",
    "bulk_insert",
    "bulk_update",
    "compare_table",
    "execute",
    "execute_pipeline",
    "execute_result",
    "forget_table",
    "metadata",
    "stream",
    "table_for",
]
