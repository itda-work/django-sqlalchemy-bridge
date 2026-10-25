"""Build a SQLAlchemy ``Result`` from rows fetched elsewhere.

This is the one place the bridge uses SQLAlchemy API outside the documented
public surface. SQLAlchemy has no public way to make a ``Result`` from rows it
did not fetch itself: ``Result`` objects come from ``Connection.execute()``,
which the bridge must not use (Django owns the connection). ``IteratorResult``
and ``SimpleResultMetaData`` live in ``sqlalchemy.engine.result`` and are not
documented as constructible; only the two positional arguments used below are
relied on.

Checked against SQLAlchemy 2.0.55 and 2.1.6 (2026-10-25). ``pyproject.toml``
caps SQLAlchemy below 2.2, and CI runs the opt-in ``Result`` tests on 2.0 and
2.1; widen the cap only after this module passes on the new minor.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.engine import Result
from sqlalchemy.engine.result import IteratorResult, SimpleResultMetaData


def result_from_rows(keys: Sequence[str], rows: Sequence[Sequence[Any]]) -> Result:
    """A ``Result`` over already converted ``rows``, whose columns are named ``keys``."""
    return IteratorResult(SimpleResultMetaData(list(keys)), iter(rows))
