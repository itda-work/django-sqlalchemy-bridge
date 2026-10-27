"""Closing the database connections a worker thread opened."""

from django.db import connections


def close_connections():
    """Close this thread's connections, in-memory SQLite ones too.

    Django's SQLite ``close()`` leaves an in-memory database's connection open,
    since the database lasts only as long as a connection to it. A worker thread's
    is a second connection to the shared-cache test database, which the test
    thread's own keeps alive, so it can go. Left open it is finalized whenever the
    garbage collector reaches it, warning (ResourceWarning) in whichever test runs
    then (#94).
    """
    for connection in connections.all(initialized_only=True):
        connection.close()
        if connection.connection is not None:
            connection.connection.close()
            connection.connection = None
