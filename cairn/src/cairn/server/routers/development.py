from __future__ import annotations

import hmac
import json
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from cairn.server.access import require_operator, require_reader, require_worker, token_hash
from cairn.server.db import get_conn
from cairn.server.development_models import (
    Control, ControlRequest, DevelopmentProject, OperationRequest, OperationResult,
    RunClaim, RunFinish, RunUpdate, WorkPackage,
)
from cairn.server import development_store as store
from cairn.server.services import get_project_or_404, next_hint_id, utcnow

router = APIRouter(prefix="/projects/{project_id}/development", tags=["development"])


def initialize(conn, project_id: str, config: DevelopmentProject) -> None:
    from pathlib import Path
    if not Path(config.repository).is_absolute():
        raise HTTPException(422, "Repository must be an absolute path on the dispatcher host")
    config = config.model_copy(update={"revision": 1, "requested_revision": 1, "state": "active",
                                       "wait_reason": "", "wake_at": None})
    store.save_project(conn, project_id, config)
    conn.execute("UPDATE projects SET bootstrap_enabled = 0 WHERE id = ?", (project_id,))
    store.add_intent(conn, project_id, "Plan the current milestone within the project design",
                     WorkPackage(role="planner", purpose="plan", revision=1))


@router.put("", dependencies=[Depends(require_operator)])
def configure(project_id: str, body: DevelopmentProject):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = get_project_or_404(conn, project_id)
        if row["development"] or conn.execute("SELECT 1 FROM intents WHERE project_id=?", (project_id,)).fetchone():
            raise HTTPException(409, "Configure an empty project; use controls to revise an existing development project")
        initialize(conn, project_id, body)
        return store.project(conn, project_id)


def public_run(row):
    data = dict(row)
    data.pop("token_hash", None)
    for key in ("result", "evidence"):
        data[key] = json.loads(data[key])
    return data


@router.get("", dependencies=[Depends(require_reader)])
def state(project_id: str):
    with get_conn() as conn:
        config = store.project(conn, project_id)
        return {
            "project": config,
            "runs": [public_run(r) for r in conn.execute(
                "SELECT * FROM runs WHERE project_id=? ORDER BY started_at DESC LIMIT 100", (project_id,))],
            "operations": [dict(r) for r in conn.execute("SELECT * FROM operations WHERE project_id=?", (project_id,))],
            "events": [dict(r) for r in conn.execute(
                "SELECT * FROM development_events WHERE project_id=? ORDER BY id DESC LIMIT 200", (project_id,))],
        }


def affected(conn, project_id: str, intent_id: str, revision: int) -> bool:
    value = store.work(conn, project_id, intent_id)
    for row in conn.execute("SELECT control FROM hints WHERE project_id=? AND control IS NOT NULL", (project_id,)):
        control = Control.model_validate_json(row["control"])
        if control.kind == "directive" and control.revision > revision:
            if not control.scope or intent_id in control.scope or value.subject in control.scope:
                return True
            if value.role in ("planner", "architect"):
                return True
    return False


def authenticated_run(conn, project_id: str, run_id: str, token: str):
    row = conn.execute("SELECT * FROM runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
    if row is None:
        raise HTTPException(404, "Run not found")
    if not hmac.compare_digest(row["token_hash"], token_hash(token)):
        raise HTTPException(403, "Wrong run token")
    return row


def live_run(conn, project_id: str, run_id: str, token: str):
    row = authenticated_run(conn, project_id, run_id, token)
    if row["state"] != "running":
        raise HTTPException(409, "Run is no longer active")
    return row


@router.post("/intents/{intent_id}/claim", dependencies=[Depends(require_worker)])
def claim(project_id: str, intent_id: str, body: RunClaim):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id, active=True)
        value = store.work(conn, project_id, intent_id)
        if value.role != body.role or value.revision != body.revision:
            raise HTTPException(409, "Role or control revision changed")
        if affected(conn, project_id, intent_id, body.revision):
            raise HTTPException(409, "New directive requires a checkpoint before dispatch")
        if value.state not in ("ready", "waiting") or (value.retry_at and value.retry_at > utcnow()):
            raise HTTPException(409, "Work package is not ready")
        for dependency in value.dependencies:
            if store.work(conn, project_id, dependency).state != "verified":
                raise HTTPException(409, f"Dependency {dependency} is not verified")
        if conn.execute("SELECT 1 FROM runs WHERE project_id=? AND intent_id=? AND state='running'",
                        (project_id, intent_id)).fetchone():
            raise HTTPException(409, "Work package already has a live run")
        run_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        now = utcnow()
        conn.execute("INSERT INTO runs(id,project_id,intent_id,worker,role,revision,token_hash,host,started_at,heartbeat_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (run_id, project_id, intent_id, body.worker, body.role, body.revision, token_hash(token), body.host, now, now))
        value.state, value.retry_at = "running", None
        store.save_work(conn, project_id, intent_id, value)
        conn.execute("UPDATE intents SET worker=?,last_heartbeat_at=? WHERE project_id=? AND id=?",
                     (body.worker, now, project_id, intent_id))
        store.event(conn, project_id, "run_started", run_id, f"{body.role}: {body.worker}")
        return {"id": run_id, "token": token, "revision": config.revision}


@router.post("/runs/{run_id}/heartbeat", dependencies=[Depends(require_worker)])
def heartbeat(project_id: str, run_id: str, body: RunUpdate):
    with get_conn() as conn:
        row = live_run(conn, project_id, run_id, body.token)
        config = store.project(conn, project_id)
        project_row = get_project_or_404(conn, project_id)
        conn.execute("UPDATE runs SET heartbeat_at=?,session=COALESCE(?,session),pid=COALESCE(?,pid),"
                     "checkpoint=COALESCE(?,checkpoint) WHERE id=?",
                     (utcnow(), body.session, body.pid, body.checkpoint, run_id))
        superseded = affected(conn, project_id, row["intent_id"], row["revision"])
        return {"cancel": project_row["status"] != "active", "checkpoint_requested": superseded,
                "revision": config.revision, "requested_revision": config.requested_revision}


@router.post("/runs/{run_id}/finish", dependencies=[Depends(require_worker)])
def finish(project_id: str, run_id: str, body: RunFinish):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = authenticated_run(conn, project_id, run_id, body.token)
        if row["state"] != "running":
            if row["result"] == store.dump(body.result) and row["state"] == body.outcome:
                return public_run(row)
            raise HTTPException(409, "Run already ended")
        stale = affected(conn, project_id, row["intent_id"], row["revision"])
        value = store.work(conn, project_id, row["intent_id"])
        cancelled = get_project_or_404(conn, project_id)["status"] != "active"
        outcome = "superseded" if stale else ("cancelled" if cancelled else body.outcome)
        conn.execute("UPDATE runs SET state=?,finished_at=?,session=COALESCE(?,session),result=?,evidence=?,detail=?,"
                     "retry_at=?,checkpoint=COALESCE(?,checkpoint) WHERE id=?",
                     (outcome, utcnow(), body.session, store.dump(body.result), store.dump([e.model_dump() for e in body.evidence]),
                      body.detail, body.retry_at, body.checkpoint, run_id))
        if stale:
            value.state = "superseded"
        elif body.outcome != "success" or cancelled:
            value.state, value.retry_at = "waiting", body.retry_at
        else:
            value.state = "implemented"
        value.checkpoint = body.checkpoint or body.detail
        store.save_work(conn, project_id, row["intent_id"], value)
        conn.execute("UPDATE intents SET worker=NULL WHERE project_id=? AND id=?", (project_id, row["intent_id"]))
        store.event(conn, project_id, "run_finished", run_id, outcome)
        result = public_run(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if stale:
            return JSONResponse(status_code=409, content={"detail": "Superseded output saved as checkpoint", "run": result})
        return result


def add_control(conn, project_id: str, body: ControlRequest):
    config = store.project(conn, project_id)
    kind = body.kind or ("directive" if body.content.startswith("【指令】") else "suggestion")
    if body.kind == "suggestion" and body.content.startswith("【指令】"):
        kind = "suggestion"  # Explicit structured type wins over quoted text.
    for intent_id in body.scope:
        store.work(conn, project_id, intent_id)
    if body.replaces:
        old = conn.execute("SELECT control FROM hints WHERE project_id=? AND id=?", (project_id, body.replaces)).fetchone()
        if old is None or not old["control"]:
            raise HTTPException(404, "Control to replace was not found")
        previous = Control.model_validate_json(old["control"])
        previous.state = "replaced"
        conn.execute("UPDATE hints SET control=? WHERE project_id=? AND id=?", (store.dump(previous), project_id, body.replaces))
    if kind == "directive":
        config.requested_revision += 1
        config.state, config.wait_reason, config.wake_at = "active", "", None
        store.save_project(conn, project_id, config)
    control = Control(kind=kind, source="operator", state="accepted" if kind == "directive" else "received",
                      revision=config.requested_revision, scope=body.scope, replaces=body.replaces)
    hint_id = next_hint_id(conn, project_id)
    conn.execute("INSERT INTO hints(id,project_id,content,creator,created_at,control) VALUES(?,?,?,?,?,?)",
                 (hint_id, project_id, body.content, "operator", utcnow(), store.dump(control)))
    if kind == "suggestion":
        store.add_intent(conn, project_id, "Independently evaluate suggestion " + hint_id,
                         WorkPackage(role="architect", purpose="suggestion", subject=hint_id, revision=config.revision))
    store.event(conn, project_id, "control_received", hint_id, kind)
    return {"id": hint_id, "content": body.content, "control": control}


@router.post("/controls", dependencies=[Depends(require_operator)], status_code=201)
def control(project_id: str, body: ControlRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return add_control(conn, project_id, body)


@router.post("/controls/{hint_id}/revoke", dependencies=[Depends(require_operator)])
def revoke(project_id: str, hint_id: str):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.project(conn, project_id)
        row = conn.execute("SELECT control FROM hints WHERE project_id=? AND id=?", (project_id, hint_id)).fetchone()
        if not row or not row["control"]:
            raise HTTPException(404, "Control not found")
        value = Control.model_validate_json(row["control"])
        if value.state in ("revoked", "replaced"):
            return {"state": value.state}
        value.state = "revoked"
        conn.execute("UPDATE hints SET control=? WHERE project_id=? AND id=?", (store.dump(value), project_id, hint_id))
        if value.kind == "directive":
            return add_control(conn, project_id, ControlRequest(kind="directive", scope=value.scope,
                content=f"Directive {hint_id} is revoked. Reevaluate affected work using remaining active instructions."))
        return {"state": "revoked"}


@router.post("/stop", dependencies=[Depends(require_operator)])
def stop(project_id: str):
    with get_conn() as conn:
        store.project(conn, project_id)
        conn.execute("UPDATE projects SET status='stopped' WHERE id=?", (project_id,))
        store.event(conn, project_id, "stop_requested", project_id, "Cancel running work and stop new dispatch")
        return {"state": "stopping"}


@router.post("/runs/{run_id}/operations", dependencies=[Depends(require_worker)])
def reserve_operation(project_id: str, run_id: str, body: OperationRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = live_run(conn, project_id, run_id, body.token)
        store.project(conn, project_id, active=True)
        if affected(conn, project_id, row["intent_id"], row["revision"]):
            raise HTTPException(409, "New directive requires a checkpoint")
        old = conn.execute("SELECT * FROM operations WHERE project_id=? AND key=?", (project_id, body.key)).fetchone()
        if old:
            if old["request"] != store.dump(body.request) or old["kind"] != body.kind:
                raise HTTPException(409, "Operation key already names a different action")
            execute = old["state"] == "absent"
            if execute:
                conn.execute("UPDATE operations SET state='unknown',run_id=?,updated_at=? WHERE project_id=? AND key=?",
                             (run_id, utcnow(), project_id, body.key))
            return {**dict(old), "execute": execute, "state": "unknown" if execute else old["state"]}
        conn.execute("INSERT INTO operations(project_id,key,run_id,kind,request,updated_at) VALUES(?,?,?,?,?,?)",
                     (project_id, body.key, run_id, body.kind, store.dump(body.request), utcnow()))
        return {"key": body.key, "state": "unknown", "execute": True}


@router.put("/runs/{run_id}/operations/{key}", dependencies=[Depends(require_worker)])
def reconcile_operation(project_id: str, run_id: str, key: str, body: OperationResult):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        live_run(conn, project_id, run_id, body.token)
        row = conn.execute("SELECT * FROM operations WHERE project_id=? AND key=?", (project_id, key)).fetchone()
        if row is None:
            raise HTTPException(404, "Reserve an operation before recording it")
        if row["state"] == "done" and body.state != "done":
            raise HTTPException(409, "A completed operation cannot be reset for replay")
        conn.execute("UPDATE operations SET state=?,resource_id=?,evidence=?,updated_at=? WHERE project_id=? AND key=?",
                     (body.state, body.resource_id, body.evidence, utcnow(), project_id, key))
        return {"key": key, "state": body.state}
