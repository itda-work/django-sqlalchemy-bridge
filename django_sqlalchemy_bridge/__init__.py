"""django-sqlalchemy-bridge — experimental, the API may change without notice.

See README.md for the architecture.
"""

from importlib.metadata import PackageNotFoundError, version

from django_sqlalchemy_bridge.execution import execute, execute_result
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
    "assert_table_matches",
    "compare_table",
    "execute",
    "execute_result",
    "forget_table",
    "metadata",
    "table_for",
]
