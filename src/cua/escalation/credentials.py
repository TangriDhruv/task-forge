"""Opt-in, encrypted-at-rest store for credentials a human explicitly chooses
to let the agent reuse across runs.

CLAUDE.md's hard constraint is "never persist secrets... into artifacts or
logs" -- not "never persist them at all." This module is the one place a
secret is allowed to land on disk, and only after a human explicitly
approves saving a specific named value (the dashboard's escalation panel is
the only caller of ``save_credential``, gated behind an explicit "save this
for next time?" action -- discovery and replay never call it themselves).
Everywhere else in the system -- traces, evidence, artifact JSON -- must
keep referring to a credential by name only, never by value; see
``cua.escalation.bridge.EscalationBridge.relay_type``'s ``sensitive`` flag,
which is what keeps a saved value out of the trace in the first place.

Same SQLite file as ``cua.artifact.store`` (``data/cua.db``), its own table --
one data file for the automation system's persistent state, not a separate
store per subsystem.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet

from cua.artifact.store import DEFAULT_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    name TEXT PRIMARY KEY,
    encrypted_value BLOB NOT NULL,
    created_at TEXT NOT NULL
);
"""


class CredentialStoreError(RuntimeError):
    """Raised when CREDENTIAL_STORE_KEY is missing -- fails loudly rather
    than silently storing something unencrypted or silently no-op'ing."""


def _fernet() -> Fernet:
    import os

    key = os.environ.get("CREDENTIAL_STORE_KEY")
    if not key:
        raise CredentialStoreError(
            "CREDENTIAL_STORE_KEY is not set (see .env.example) -- required to save or read a "
            "stored credential. Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


@contextmanager
def _connect(db_path: Path | str = DEFAULT_DB_PATH):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_credential(name: str, value: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    token = _fernet().encrypt(value.encode())
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO credentials (name, encrypted_value, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (name, token),
        )


def get_credential(name: str, db_path: Path | str = DEFAULT_DB_PATH) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT encrypted_value FROM credentials WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return _fernet().decrypt(bytes(row["encrypted_value"])).decode()


def has_credential(name: str, db_path: Path | str = DEFAULT_DB_PATH) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT 1 FROM credentials WHERE name = ?", (name,)).fetchone()
    return row is not None


def list_credential_names(db_path: Path | str = DEFAULT_DB_PATH) -> list[str]:
    """Names only -- never values. Safe to render in a UI or write to a log."""
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT name FROM credentials ORDER BY name").fetchall()
    return [r["name"] for r in rows]


def delete_credential(name: str, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM credentials WHERE name = ?", (name,))
