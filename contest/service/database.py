"""SQLite schema, legacy migrations and shared transaction boundaries."""
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3


SCHEMA = '''
CREATE TABLE IF NOT EXISTS duels (
    id INTEGER PRIMARY KEY, duel_id TEXT UNIQUE NOT NULL, submission TEXT NOT NULL,
    kind TEXT NOT NULL, commits TEXT, started_at TEXT, issue TEXT, result TEXT,
    confirmed_order INTEGER UNIQUE
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY AUTOINCREMENT, duel INTEGER NOT NULL REFERENCES duels(id),
    ordinal INTEGER NOT NULL, bots TEXT NOT NULL, map TEXT NOT NULL, UNIQUE (duel, ordinal)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, game INTEGER NOT NULL REFERENCES games(id), attempt INTEGER NOT NULL,
    submission TEXT NOT NULL DEFAULT 'pending', arena_result TEXT,
    poll_after REAL NOT NULL DEFAULT 0, poll_failures INTEGER NOT NULL DEFAULT 0,
    review TEXT, issue TEXT, manifest TEXT, UNIQUE (game, attempt)
);
CREATE VIEW IF NOT EXISTS current_runs AS
    SELECT r.* FROM runs r WHERE r.attempt = (SELECT MAX(attempt) FROM runs WHERE game = r.game);
CREATE TABLE IF NOT EXISTS prelim_batch (
    id INTEGER PRIMARY KEY CHECK (id = 1), roster TEXT NOT NULL, planned_at TEXT, confirmed_at TEXT
);
CREATE TABLE IF NOT EXISTS prelim_duels (
    duel INTEGER PRIMARY KEY REFERENCES duels(id)
);
CREATE TABLE IF NOT EXISTS prelim_withdrawals (
    team_id TEXT PRIMARY KEY, withdrawn_at TEXT NOT NULL
);
'''


class Database:
    def __init__(self, path):
        self.path = Path(path)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(SCHEMA)
        with self.transaction() as connection:
            columns = {row['name'] for row in connection.execute('PRAGMA table_info(runs)')}
            if 'arena_result' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN arena_result TEXT')
                if 'callback' in columns:
                    connection.execute('UPDATE runs SET arena_result = callback WHERE callback IS NOT NULL')
            if 'poll_after' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN poll_after REAL NOT NULL DEFAULT 0')
            if 'poll_failures' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN poll_failures INTEGER NOT NULL DEFAULT 0')

    @contextmanager
    def transaction(self, *, write=True):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA foreign_keys = ON')
            connection.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield connection

