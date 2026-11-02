"""SQLite engine with WAL, foreign keys and bounded busy waits.

Every unit of work opens its own short transaction. ``write()`` takes the
database write lock up front (``BEGIN IMMEDIATE``) so read-then-write checks such
as quota and lease validation cannot interleave with another writer.
Hashing, network, Docker and file IO must stay outside these blocks.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Connection, Engine, create_engine, event

BUSY_TIMEOUT_MS = 5000


def make_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection, _record):
        # Let SQLAlchemy's "begin" event, not pysqlite, decide when to BEGIN.
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def on_begin(connection):
        mode = connection.get_execution_options().get("sqlite_begin", "DEFERRED")
        connection.exec_driver_sql(f"BEGIN {mode}")

    return engine


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine

    @classmethod
    def open(cls, path: Path) -> "Database":
        return cls(make_engine(path))

    @contextmanager
    def read(self) -> Iterator[Connection]:
        with self.engine.connect() as connection, connection.begin():
            yield connection

    @contextmanager
    def write(self) -> Iterator[Connection]:
        with (
            self.engine.connect().execution_options(sqlite_begin="IMMEDIATE") as connection,
            connection.begin(),
        ):
            yield connection

    def dispose(self) -> None:
        self.engine.dispose()
