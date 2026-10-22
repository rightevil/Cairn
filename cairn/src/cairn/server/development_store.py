from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import HTTPException

from cairn.server.development_models import DevelopmentProject, WorkPackage


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    intent_id TEXT NOT NULL, worker TEXT NOT NULL, role TEXT NOT NULL, revision INTEGER NOT NULL,
    token_hash TEXT NOT NULL, host TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, finished_at TEXT,
    session TEXT, pid INTEGER, checkpoint TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
    evidence TEXT NOT NULL DEFAULT '[]', detail TEXT NOT NULL DEFAULT '', retry_at TEXT,
    applied INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (intent_id, project_id) REFERENCES intents(id, project_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_run ON runs(project_id, intent_id) WHERE state = 'running';
CREATE TABLE IF NOT EXISTS operations (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, key TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL, request TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'unknown', resource_id TEXT, evidence TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL, PRIMARY KEY(project_id, key)
);
CREATE TABLE IF NOT EXISTS development_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL, subject TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def migrate(conn: sqlite3.Connection) -> None:
    for table, column in (("projects", "development"), ("intents", "work"), ("hints", "control")):
        if column not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.executescript(SCHEMA)
    if "process_identity" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
        conn.execute("ALTER TABLE runs ADD COLUMN process_identity TEXT")
    if "streams" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
        conn.execute("ALTER TABLE runs ADD COLUMN streams TEXT NOT NULL DEFAULT '{}'")
    if "cancel_requested" not in {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}:
        conn.execute("ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")


def dump(value: Any) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def project(conn: sqlite3.Connection, project_id: str, *, active: bool = False) -> DevelopmentProject:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    if not row["development"]:
        raise HTTPException(409, "Project is not configured for development")
    if active and row["status"] != "active":
        raise HTTPException(409, f"Project is {row['status']}")
    return DevelopmentProject.model_validate_json(row["development"])


def save_project(conn: sqlite3.Connection, project_id: str, value: DevelopmentProject) -> None:
    conn.execute("UPDATE projects SET development = ? WHERE id = ?", (dump(value), project_id))


def stop_project(conn: sqlite3.Connection, project_id: str) -> str:
    project(conn, project_id)
    status = conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"]
    if status == "completed":
        return status
    conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project_id,))
    conn.execute("UPDATE runs SET cancel_requested=1 WHERE project_id=? AND state='running'", (project_id,))
    if status != "stopped":
        event(conn, project_id, "stop_requested", project_id, "Cancel running work and stop new dispatch")
    running = conn.execute("SELECT 1 FROM runs WHERE project_id=? AND state='running'", (project_id,)).fetchone()
    return "stopping" if running else "stopped"


def work(conn: sqlite3.Connection, project_id: str, intent_id: str) -> WorkPackage:
    row = conn.execute("SELECT work FROM intents WHERE project_id = ? AND id = ?", (project_id, intent_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Intent not found")
    if not row["work"]:
        raise HTTPException(409, "Intent is not a development work package")
    return WorkPackage.model_validate_json(row["work"])


def save_work(conn: sqlite3.Connection, project_id: str, intent_id: str, value: WorkPackage) -> None:
    conn.execute("UPDATE intents SET work = ? WHERE project_id = ? AND id = ?", (dump(value), project_id, intent_id))


def stale_review(conn: sqlite3.Connection, project_id: str, value: WorkPackage) -> bool:
    if value.purpose not in ("plan", "implementation", "milestone") or not value.subject_run:
        return False
    subject = work(conn, project_id, value.subject)
    return subject.state in ("verified", "superseded") or bool(subject.result.get("pending_design")) or subject.result.get("source_run_id") != value.subject_run


def event(conn: sqlite3.Connection, project_id: str, kind: str, subject: str, detail: str) -> None:
    from cairn.server.services import utcnow
    conn.execute("INSERT INTO development_events(project_id,kind,subject,detail,created_at) VALUES(?,?,?,?,?)",
                 (project_id, kind, subject, detail, utcnow()))


def add_intent(conn: sqlite3.Connection, project_id: str, description: str, value: WorkPackage,
               sources: list[str] | None = None) -> str:
    from cairn.server.services import next_intent_id, utcnow, validate_facts_exist
    sources = sources or ["origin"]
    value.design_revision = project(conn, project_id).design_revision
    validate_facts_exist(conn, project_id, sources)
    intent_id = next_intent_id(conn, project_id)
    conn.execute("INSERT INTO intents(id,project_id,description,creator,created_at,work) VALUES(?,?,?,?,?,?)",
                 (intent_id, project_id, description, "dispatcher.development", utcnow(), dump(value)))
    for source in sources:
        conn.execute("INSERT INTO intent_sources(intent_id,project_id,fact_id) VALUES(?,?,?)",
                     (intent_id, project_id, source))
    event(conn, project_id, "work_created", intent_id, description)
    return intent_id


def block_legacy_write(conn: sqlite3.Connection, project_id: str) -> None:
    row = conn.execute("SELECT development FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row and row["development"]:
        raise HTTPException(409, "Development projects require versioned run/control APIs")
