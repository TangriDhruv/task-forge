"""SQLite-backed storage for artifacts and the shared, cross-artifact event
library (CLAUDE.md "Shared event memory" decision).

Two tables, one file, no server:

- ``artifacts`` -- every saved version of every capability. Replay loads the
  latest (or a pinned) version by ``artifact_id``.
- ``event_library`` -- a *structural* signature -> outcome mapping shared
  across every artifact and app, keyed by condition, not by app. Discovery
  may INSERT a row here (status ``pending_review``) when it meets a page
  state no existing rule explains; nothing but a human flips a row to
  ``approved``. ``match_event`` -- the only function the replay engine is
  allowed to call -- only ever looks at ``approved`` rows. This is the whole
  safety property: unreviewed model judgment can accumulate in the table
  without ever being able to change what replay does.

Deliberately SQLite, not a vector/embedding store: event matching here is
exact structural matching (substring/role+text), because anything fuzzy
would smuggle a judgment call into the one path that's required to have
none. See REPORT.md §4/§7 for how this would scale to a shared registry
across tenants -- that stays a design, not code, per CLAUDE.md's constraint
against building scaling infrastructure.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .schema import Artifact, Condition, condition_holds

# repo_root/data/cua.db -- src/cua/artifact/store.py -> parents[3] is repo root.
DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "cua.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    app_id TEXT NOT NULL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    created_from_run TEXT,
    created_at TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (artifact_id, version)
);

CREATE TABLE IF NOT EXISTS event_library (
    signature_id TEXT PRIMARY KEY,
    condition_kind TEXT NOT NULL,
    condition_value TEXT NOT NULL,
    outcome_class TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    source_app_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending_review',
    proposed_by TEXT,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


# ---- artifacts -------------------------------------------------------------


def save_artifact(artifact: Artifact, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts "
            "(artifact_id, version, name, app_id, reviewed, created_from_run, created_at, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                artifact.artifact_id,
                artifact.version,
                artifact.name,
                artifact.app_id,
                int(artifact.reviewed),
                artifact.created_from_run,
                _now(),
                artifact.to_json(),
            ),
        )


def load_artifact(
    artifact_id: str, version: int | None = None, db_path: Path | str = DEFAULT_DB_PATH
) -> Artifact | None:
    with _connect(db_path) as conn:
        if version is None:
            row = conn.execute(
                "SELECT data_json FROM artifacts WHERE artifact_id = ? ORDER BY version DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT data_json FROM artifacts WHERE artifact_id = ? AND version = ?",
                (artifact_id, version),
            ).fetchone()
    return Artifact.from_json(row["data_json"]) if row else None


def list_artifacts(db_path: Path | str = DEFAULT_DB_PATH) -> list[Artifact]:
    """Latest version of every distinct artifact_id."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT data_json FROM artifacts a WHERE version = "
            "(SELECT MAX(version) FROM artifacts b WHERE b.artifact_id = a.artifact_id) "
            "ORDER BY artifact_id"
        ).fetchall()
    return [Artifact.from_json(r["data_json"]) for r in rows]


# ---- event library ----------------------------------------------------------


def propose_event(
    condition: Condition,
    outcome_class: str,
    outcome_name: str,
    source_app_id: str | None,
    proposed_by: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> str:
    """Discovery-only entry point. Always lands as ``pending_review`` --
    nothing here can mark its own row approved."""
    signature_id = str(uuid.uuid4())
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO event_library "
            "(signature_id, condition_kind, condition_value, outcome_class, outcome_name, "
            " source_app_id, status, proposed_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?)",
            (
                signature_id,
                condition.kind,
                condition.value,
                outcome_class,
                outcome_name,
                source_app_id,
                proposed_by,
                _now(),
            ),
        )
    return signature_id


def review_event(signature_id: str, approve: bool, db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """The only way a row's status leaves 'pending_review' -- called from the
    dashboard's human review action, never from discovery or replay."""
    status = "approved" if approve else "rejected"
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE event_library SET status = ? WHERE signature_id = ?", (status, signature_id)
        )


def list_events(status: str | None = None, db_path: Path | str = DEFAULT_DB_PATH) -> list[dict]:
    with _connect(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM event_library WHERE status = ? ORDER BY created_at", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM event_library ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def match_event(
    page_text: str, url: str, element_descriptors: list[str], db_path: Path | str = DEFAULT_DB_PATH
) -> dict | None:
    """The one function replay is allowed to call. Only ever consults
    ``approved`` rows -- a pending or rejected row can never change replay
    behavior, no matter what the discovery LLM proposed.

    ``element_descriptors`` is a flat list of short strings describing each
    currently-observed interactive element (e.g. "button:Confirm Transfer"),
    so element_present/element_absent can match against it without needing
    free-form code.
    """
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM event_library WHERE status = 'approved'").fetchall()
    for row in rows:
        signature = Condition(kind=row["condition_kind"], value=row["condition_value"])
        if condition_holds(signature, page_text, url, element_descriptors):
            return dict(row)
    return None
