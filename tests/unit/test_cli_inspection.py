import os
import sqlite3
import tempfile
from pathlib import Path
import pytest

from scripts.run_live_webhook_receiver import inspect_delivery_cli


def test_inspect_delivery_cli_success(capsys):
    """Verifies inspect_delivery_cli queries real schema without OperationalError, KeyError, or AttributeError."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_durable.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE webhook_jobs (
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
            conn.execute(
                """
                INSERT INTO webhook_jobs (
                    delivery_id, body_hash, event_type, payload_json, state,
                    attempt_count, max_attempts, available_at, lease_token,
                    lease_expires_at, created_at, updated_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "del-live-456",
                    "dummy-hash",
                    "push",
                    '{"ref": "refs/heads/main"}',
                    "COMPLETED",
                    1,
                    3,
                    1700000000.0,
                    None,
                    None,
                    1700000000.0,
                    1700000005.0,
                    None,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # Call inspect_delivery_cli; must succeed without OperationalError, KeyError, or AttributeError
        inspect_delivery_cli(db_path, "del-live-456")
        captured = capsys.readouterr().out

        assert "=== DELIVERY JOB DETAILS ===" in captured
        assert "delivery_id: del-live-456" in captured
        assert "state: COMPLETED" in captured
        assert "attempt_count: 1" in captured
        assert "max_attempts: 3" in captured
        assert "event_type: push" in captured


def test_inspect_delivery_cli_nonexistent_delivery(capsys):
    """Verifies inspect_delivery_cli exits 1 if delivery_id is not found."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "test_durable.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE webhook_jobs (
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
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(SystemExit) as exc_info:
            inspect_delivery_cli(db_path, "nonexistent-id")
        assert exc_info.value.code == 1
        captured = capsys.readouterr().out
        assert "No delivery job found for delivery_id: nonexistent-id" in captured


def test_inspect_delivery_cli_nonexistent_database(capsys):
    """Verifies inspect_delivery_cli exits 1 if database file does not exist."""
    with pytest.raises(SystemExit) as exc_info:
        inspect_delivery_cli("nonexistent_path_to_db.db", "any-id")
    assert exc_info.value.code == 1
    captured = capsys.readouterr().out
    assert "Database not found" in captured
