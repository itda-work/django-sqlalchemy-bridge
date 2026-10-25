# django-sqlalchemy-bridge

> **Status: planning (0.0.x).** This release reserves the name and publishes the design. There is no public API yet — do not depend on it.

Use SQLAlchemy 2.x Core expressions (bulk DML, CTEs, window functions, complex joins) **inside Django's own connection and transaction**.

## Why

[aldjemy](https://github.com/aldjemy/aldjemy) pioneered this, but it is pinned to SQLAlchemy < 2 because it shares Django's connection through SQLAlchemy's private pool internals. This project takes a different route.

## Planned architecture

- **Django owns the connection.** SQLAlchemy is used only to build and compile statements; execution always goes through Django's cursor. So `atomic()`, savepoints, `on_commit`, `execute_wrapper`, `assertNumQueries`, Django's connection pool and PgBouncer settings all keep working.
- **Public SQLAlchemy API only.** No `_`-prefixed internals, tested against SQLAlchemy 2.0, 2.1 and latest.
- **Django is the single schema source.** `sa.Table` objects are generated from Django models; migrations stay in Django.
- **Value conversion follows Django fields** (`get_db_prep_value` / `from_db_value`), so JSON, UUID, aware datetimes and Decimal round-trip the same way the ORM does.
- **Explicit boundary.** Writes through the bridge do not fire Django signals; the API says so.
- **Sync and async** (`aexecute` follows Django's thread rules).

Planned first targets: SQLite and PostgreSQL (psycopg 3), Django 5.2+, SQLAlchemy 2.0+, Python 3.12+.

## License

MIT
