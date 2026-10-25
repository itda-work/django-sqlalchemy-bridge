"""django-sqlalchemy-bridge — design stage, no public API yet.

See README.md for the planned architecture.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("django-sqlalchemy-bridge")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
