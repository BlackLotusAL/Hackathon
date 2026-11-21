"""SQLite schema and shared transaction boundaries."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3

from .domain import ContestError


SCHEMA_VERSION = 2
SQLITE_BUSY_TIMEOUT = 5
SCHEMA = (
    '''CREATE TABLE prelim_batches (
        batch_id TEXT PRIMARY KEY, roster TEXT NOT NULL, planned_at TEXT, confirmed_at TEXT, stopped_at TEXT
    )''',
    '''CREATE TABLE duels (
        id INTEGER PRIMARY KEY, duel_id TEXT UNIQUE NOT NULL, submission TEXT NOT NULL, kind TEXT NOT NULL,
        batch_id TEXT REFERENCES prelim_batches(batch_id), commits TEXT, started_at TEXT,
        awaiting_ruling INTEGER NOT NULL DEFAULT 0, ruling TEXT, result TEXT, closed_at TEXT,
        confirmed_order INTEGER UNIQUE
    )''',
    'CREATE INDEX duels_by_batch ON duels (batch_id, id)',
    '''CREATE TABLE games (
        id INTEGER PRIMARY KEY AUTOINCREMENT, duel INTEGER NOT NULL REFERENCES duels(id),
        ordinal INTEGER NOT NULL, bots TEXT NOT NULL, map TEXT NOT NULL, UNIQUE (duel, ordinal)
    )''',
    '''CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, game INTEGER NOT NULL REFERENCES games(id), attempt INTEGER NOT NULL,
        submission TEXT NOT NULL DEFAULT 'pending', arena_result TEXT,
        poll_after REAL NOT NULL DEFAULT 0, poll_failures INTEGER NOT NULL DEFAULT 0,
        review TEXT, issue TEXT, manifest TEXT, ruling TEXT, UNIQUE (game, attempt)
    )''',
    '''CREATE VIEW current_runs AS
        SELECT r.* FROM runs r WHERE r.attempt = (SELECT MAX(attempt) FROM runs WHERE game = r.game)''',
    '''CREATE TABLE prelim_withdrawals (
        batch_id TEXT NOT NULL REFERENCES prelim_batches(batch_id), team_id TEXT NOT NULL, withdrawn_at TEXT NOT NULL,
        PRIMARY KEY (batch_id, team_id)
    )''',
)


class DatabaseStopping(ContestError):
    def __init__(self):
        super().__init__(503, 'DATABASE_STOPPING', 'Contest 正在停止')


class Database:
    def __init__(self, path, *, shutdown=None):
        self.path = Path(path)
        self.shutdown = shutdown
        with self.transaction() as connection:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version != SCHEMA_VERSION:
                existing = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' LIMIT 1").fetchone()
                if version or existing is not None:
                    raise ValueError(f'数据库结构版本不受支持，当前结构版本为 {SCHEMA_VERSION}')
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute(f'PRAGMA user_version = {SCHEMA_VERSION}')
        with closing(self._connect()) as connection:
            mode = self._wait_for_lock(lambda: connection.execute('PRAGMA journal_mode = WAL').fetchone())
            if mode[0] != 'wal':
                raise ValueError('Contest 数据库需要支持 SQLite WAL 的本地文件系统')

    def _connect(self):
        return sqlite3.connect(self.path, timeout=SQLITE_BUSY_TIMEOUT, isolation_level=None)

    def _wait_for_lock(self, operation):
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as error:
                if not str(error).startswith(('database is locked', 'database table is locked',
                                              'database schema is locked')):
                    raise
                if self.shutdown is None:
                    raise ContestError(503, 'DATABASE_BUSY', 'Contest 数据库正忙，请稍后重试') from error
                if self.shutdown.wait(0.05):
                    raise DatabaseStopping() from error

    @contextmanager
    def transaction(self, *, write=True):
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            self._wait_for_lock(lambda: connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN'))
            try:
                if not write:
                    # A deferred read transaction opens its snapshot at the first read.
                    self._wait_for_lock(lambda: connection.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchone())
                yield connection
                # Retry the commit itself so an Arena response is never discarded or reissued on a busy database.
                self._wait_for_lock(connection.commit)
            except BaseException:
                connection.rollback()
                raise

