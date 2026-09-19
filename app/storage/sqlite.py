"""SQLite storage engine and schema management for IssueAnalyzer."""
import os
import time
from pathlib import Path
import aiosqlite

CURRENT_SCHEMA_VERSION = 1


def get_default_db_path() -> str:
    env_path = os.environ.get("ISSUE_ANALYZER_DB_PATH")
    if env_path:
        return env_path
    base_dir = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return str(Path(base_dir) / "issue_analyzer" / "issue_analyzer.db")


class ConnectionContextManager:
    def __init__(self, db: aiosqlite.Connection):
        self.db = db

    async def __aenter__(self) -> aiosqlite.Connection:
        return self.db

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.db.close()


async def get_db_connection(db_path: str) -> ConnectionContextManager:
    if db_path != ":memory:":
        parent = Path(db_path).parent
        parent.mkdir(parents=True, exist_ok=True)

    db = await aiosqlite.connect(db_path, timeout=15.0)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON;")
    await db.execute("PRAGMA busy_timeout = 15000;")
    if db_path != ":memory:":
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA synchronous = NORMAL;")
    return ConnectionContextManager(db)


async def init_schema(db_path: str) -> None:
    async with await get_db_connection(db_path) as db:
        async with db.execute("PRAGMA user_version;") as cursor:
            row = await cursor.fetchone()
            current_version = row[0] if row else 0

        if current_version < 1:
            await db.execute("BEGIN IMMEDIATE;")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_jobs (
                    delivery_id TEXT PRIMARY KEY,
                    body_hash TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    available_at REAL NOT NULL,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS dedup_reservations (
                    signature TEXT PRIMARY KEY,
                    finding_id TEXT NOT NULL,
                    reservation_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    issue_number INTEGER,
                    created_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS issue_write_intents (
                    signature TEXT PRIMARY KEY,
                    intent_id TEXT UNIQUE NOT NULL,
                    finding_id TEXT NOT NULL,
                    verification_id TEXT NOT NULL,
                    installation_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    repo_name TEXT NOT NULL,
                    marker TEXT NOT NULL,
                    state TEXT NOT NULL,
                    issue_number INTEGER,
                    attempt_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            await db.execute("PRAGMA user_version = 1;")
            await db.commit()
