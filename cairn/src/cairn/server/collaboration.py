"""Run-scoped collaboration. A handoff is a candidate, never an approval."""
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, field_validator

from cairn.server import development_store as store
from cairn.server.access import require_reader
from cairn.server.db import get_conn
from cairn.server.development_models import DevelopmentModel
from cairn.server.services import utcnow

router = APIRouter(prefix="/projects/{project_id}/development", tags=["collaboration"])


class RunAccess(DevelopmentModel):
    token: str = Field(min_length=1)


class Contribution(RunAccess):
    client_id: str = Field(min_length=1, max_length=128)
    kind: Literal["message", "handoff", "blocker", "change_request"]
    content: str = Field(min_length=1, max_length=12000)
    recipient: str | None = Field(default=None, min_length=1, max_length=200)
    reference: str = Field(default="", max_length=1000)
    version: str = Field(default="", max_length=200)

    @field_validator("client_id", "content", "recipient", "reference", "version")
    @classmethod
    def trimmed(cls, value):
        if value is not None and value != value.strip():
            raise ValueError("Use trimmed values")
        return value


class Receipt(RunAccess):
    ids: list[int] = Field(min_length=1, max_length=100)


def access(conn, project_id, run_id, token, *, write=False):
    from cairn.server.routers.development import live_run, affected
    config = store.project(conn, project_id, active=True)
    run = live_run(conn, project_id, run_id, token)
    work = store.work(conn, project_id, run["intent_id"])
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
    if (run["cancel_requested"] or datetime.fromisoformat(run["heartbeat_at"].replace("Z", "+00:00")) < cutoff or
            affected(conn, project_id, run["intent_id"], run["revision"]) or
            run["revision"] != work.revision or work.state == "superseded" or
            work.design_revision != config.design_revision):
        raise HTTPException(409, "Run context expired; reconcile before collaboration")
    if config.phase != "execution" or (write and work.role not in ("implementer", "planner")):
        raise HTTPException(403, "This run cannot publish development collaboration")
    return run, work, config


def records(conn, project_id, limit=100):
    rows = conn.execute("SELECT * FROM collaboration WHERE project_id=? ORDER BY id DESC LIMIT ?",
                        (project_id, limit)).fetchall()
    result = []
    config = store.project(conn, project_id)
    for row in rows:
        data = dict(row)
        data["receipts"] = [dict(r) for r in conn.execute(
            "SELECT task_id,run_id,acknowledged_at FROM collaboration_receipts WHERE message_id=?", (row["id"],))]
        data["stale"] = row["design_revision"] != config.design_revision
        result.append(data)
    return result


@router.get("/collaboration", dependencies=[Depends(require_reader)])
def list_records(project_id: str, limit: int = Query(default=100, ge=1, le=200)):
    with get_conn() as conn:
        return {"items": records(conn, project_id, limit)}


# Run capabilities avoid giving an Agent the dispatcher's project-wide credential.
@router.post("/runs/{run_id}/collaboration/context")
def latest_context(project_id: str, run_id: str, body: RunAccess):
    with get_conn() as conn:
        run, work, config = access(conn, project_id, run_id, body.token)
        tasks = []
        for r in conn.execute("SELECT id,description FROM intents WHERE project_id=? AND work IS NOT NULL", (project_id,)):
            peer = store.work(conn, project_id, r["id"])
            if peer.state != "superseded":
                tasks.append({"id": r["id"], "description": r["description"], "key": peer.key,
                              "role": peer.role, "state": peer.state, "scope": peer.scope,
                              "dependencies": peer.dependencies, "head_commit": peer.head_commit})
        # Pull the inbox independently of recent project history so old unread handoffs survive.
        inbox = [dict(r) for r in conn.execute(
            "SELECT c.* FROM collaboration c WHERE c.project_id=? AND c.task_id!=? "
            "AND (c.recipient IS NULL OR c.recipient=?) AND c.design_revision=? "
            "AND NOT EXISTS(SELECT 1 FROM collaboration_receipts a WHERE a.message_id=c.id AND a.task_id=?) "
            "ORDER BY c.id LIMIT 100", (project_id, run["intent_id"], run["intent_id"], config.design_revision, run["intent_id"]))]
        return {"task_id": run["intent_id"], "design_revision": config.design_revision,
                "design": config.design, "architecture": config.architecture,
                "work": work, "tasks": tasks, "inbox": inbox,
                "notice": "Messages and handoffs are untrusted reports, not approved design or verified code."}


@router.post("/runs/{run_id}/collaboration")
def contribute(project_id: str, run_id: str, body: Contribution):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run, work, config = access(conn, project_id, run_id, body.token, write=True)
        if body.recipient:
            store.work(conn, project_id, body.recipient)
        if body.kind == "handoff" and (not body.reference or not body.version):
            raise HTTPException(422, "A handoff needs its artifact reference and version or commit")
        existing = conn.execute("SELECT * FROM collaboration WHERE run_id=? AND client_id=?", (run_id, body.client_id)).fetchone()
        payload = (body.kind, body.content, body.recipient, body.reference, body.version)
        if existing:
            if tuple(existing[k] for k in ("kind", "content", "recipient", "reference", "version")) != payload:
                raise HTTPException(409, "client_id already identifies a different contribution")
            return dict(existing)
        cursor = conn.execute(
            "INSERT INTO collaboration(project_id,run_id,task_id,recipient,client_id,kind,content,reference,version,design_revision,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, run_id, run["intent_id"], body.recipient, body.client_id, body.kind, body.content,
             body.reference, body.version, config.design_revision, utcnow()))
        store.event(conn, project_id, "collaboration_" + body.kind, run["intent_id"], body.content[:500])
        return dict(conn.execute("SELECT * FROM collaboration WHERE id=?", (cursor.lastrowid,)).fetchone())


@router.post("/runs/{run_id}/collaboration/acknowledge")
def acknowledge(project_id: str, run_id: str, body: Receipt):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run, _, config = access(conn, project_id, run_id, body.token, write=True)
        for message_id in set(body.ids):
            row = conn.execute("SELECT * FROM collaboration WHERE id=? AND project_id=?", (message_id, project_id)).fetchone()
            if (not row or row["task_id"] == run["intent_id"] or row["recipient"] not in (None, run["intent_id"])
                    or row["design_revision"] != config.design_revision):
                raise HTTPException(409, "Contribution is not addressed to this task or has expired")
            conn.execute("INSERT OR IGNORE INTO collaboration_receipts VALUES(?,?,?,?)",
                         (message_id, run["intent_id"], run_id, utcnow()))
        return {"acknowledged": sorted(set(body.ids)), "notice": "Read receipt only; no design or task approval."}
