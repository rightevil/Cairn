from __future__ import annotations

import hmac
import json
import secrets
import uuid
import hashlib
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from cairn.server.access import require_operator, require_reader, require_worker, token_hash
from cairn.server.db import get_conn
from cairn.server.development_models import (
    Control, ControlRequest, DevelopmentProject, OperationRequest, OperationResult,
    RunClaim, RunFinish, RunUpdate, WorkPackage, WorkspaceUpdate, RepositoryBinding, RecoveryRequest, DeliverySettings,
)
from cairn.server import development_store as store
from cairn.server.services import get_project_or_404, next_hint_id, utcnow

router = APIRouter(prefix="/projects/{project_id}/development", tags=["development"])


@router.put("/delivery", dependencies=[Depends(require_operator)])
def delivery(project_id: str, body: DeliverySettings):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id)
        if conn.execute("SELECT 1 FROM runs WHERE project_id=? AND role='integration' AND state='running'", (project_id,)).fetchone():
            raise HTTPException(409, "Wait for the current integration before changing delivery mode")
        if config.delivery != body.delivery:
            config.delivery = body.delivery
            store.save_project(conn, project_id, config)
            store.event(conn, project_id, "delivery_configured", project_id, "Delivery mode: " + body.delivery)
        return config


@router.post("/advance", dependencies=[Depends(require_worker)])
def advance(project_id: str):
    from cairn.server.development_workflow import advance as apply
    with get_conn() as conn:
        apply(conn, project_id)
        return {"state": store.project(conn, project_id).state}


@router.post("/bind", dependencies=[Depends(require_worker)])
def bind(project_id: str, body: RepositoryBinding):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id)
        if config.integration_branch and config.integration_branch != body.integration_branch:
            raise HTTPException(409, "Integration branch is already bound")
        config.integration_branch = body.integration_branch
        store.save_project(conn, project_id, config)
        return config


@router.post("/runs/{run_id}/workspace", dependencies=[Depends(require_worker)])
def workspace(project_id: str, run_id: str, body: WorkspaceUpdate):
    with get_conn() as conn:
        row = live_run(conn, project_id, run_id, body.token)
        value = store.work(conn, project_id, row["intent_id"])
        if value.role != "implementer":
            raise HTTPException(409, "Only implementation work owns a feature branch")
        value.branch, value.base_commit, value.worktree = body.branch, body.base_commit, body.worktree
        store.save_work(conn, project_id, row["intent_id"], value)
        return value


@router.post("/runs/{run_id}/recover", dependencies=[Depends(require_worker)])
def recover(project_id: str, run_id: str, body: RecoveryRequest):
    from datetime import datetime, timedelta, timezone
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        if row["state"] != "running":
            return public_run(row)
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(timespec="seconds").replace("+00:00", "Z")
        if row["host"] != body.host or row["heartbeat_at"] > cutoff or not body.confirmed_stopped:
            raise HTTPException(409, "Recovery requires an expired local run and confirmed process termination")
        conn.execute("UPDATE runs SET state='recovered',finished_at=?,detail=? WHERE id=?",
                     (utcnow(), body.detail, run_id))
        from cairn.server.design import settle_inputs
        settle_inputs(conn, project_id, row["intent_id"], run_id, "recovered")
        store.event(conn, project_id, "run_recovered", run_id, body.detail)
        return {"state": "recovered"}


def initialize(conn, project_id: str, config: DevelopmentProject) -> None:
    from pathlib import Path
    if not Path(config.repository).is_absolute():
        raise HTTPException(422, "Repository must be an absolute path on the dispatcher host")
    config = config.model_copy(update={"revision": 1, "requested_revision": 1, "state": "active",
                                       "wait_reason": "", "wake_at": None,
                                       "workspace_root": str(Path(config.repository) / ".cairn" / "workspaces")})
    from cairn.workspace import project_workspace
    try:
        project_workspace(config, project_id, Path(config.repository))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    store.save_project(conn, project_id, config)
    conn.execute("UPDATE projects SET bootstrap_enabled = 0 WHERE id = ?", (project_id,))
    if config.phase == "execution":
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
    for key in ("result", "evidence", "streams"):
        data[key] = json.loads(data[key])
    return data


@router.get("/runs/{run_id}", dependencies=[Depends(require_reader)])
def get_run(project_id: str, run_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        return public_run(row)


@router.get("/intents/{intent_id}/runs", dependencies=[Depends(require_reader)])
def intent_runs(project_id: str, intent_id: str):
    with get_conn() as conn:
        store.work(conn, project_id, intent_id)
        return [public_run(r) for r in conn.execute(
            "SELECT * FROM runs WHERE project_id=? AND intent_id=? ORDER BY rowid DESC", (project_id, intent_id))]


@router.get("/intents/{intent_id}/activity", dependencies=[Depends(require_reader)])
def task_activity(project_id: str, intent_id: str, before: int | None = None, limit: int = Query(30, ge=1, le=100)):
    from cairn.server.agent_sessions import task_records, task_identity
    with get_conn() as conn:
        store.work(conn, project_id, intent_id)
        records = task_records(conn, project_id)
        task = task_identity(records, intent_id)
        ids = [i for i in records if task_identity(records, i) == task] if task else [intent_id]
        marks = ",".join("?" for _ in ids)
        rows = conn.execute(f"SELECT rowid AS cursor,* FROM runs WHERE project_id=? AND intent_id IN ({marks}) "
                            "AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                            (project_id, *ids, before, before, limit+1)).fetchall()
        output = []
        for row in rows[:limit]:
            value = public_run(row)
            item = {k:value[k] for k in ("id","intent_id","worker","role","state","started_at","finished_at",
                                         "heartbeat_at","pid","checkpoint","detail","streams","evidence")}
            item["verdict"] = value["result"].get("output", {}).get("verdict")
            output.append(item)
        return {"task":task, "runs":output, "next":rows[limit-1]["cursor"] if len(rows)>limit else None}


@router.get("/runs/{run_id}/evidence/{index}", dependencies=[Depends(require_reader)])
def evidence(project_id: str, run_id: str, index: int):
    from fastapi.responses import PlainTextResponse
    with get_conn() as conn:
        row = conn.execute("SELECT evidence,streams FROM runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Run not found")
        items = json.loads(row["evidence"])
        if index < 0 or index >= len(items):
            raise HTTPException(404, "Evidence not found")
        item = items[index]
        path = Path(item["path"]).resolve()
        stdout = json.loads(row["streams"]).get("stdout")
        owned = Path(stdout).resolve().parent if stdout else None
        in_reports = owned is not None and owned.name == run_id and path.is_relative_to(owned / "reports")
        if (path.parent.name != run_id and not in_reports) or not path.is_file():
            raise HTTPException(409, "Evidence is not in its owned run directory")
        with path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != item["sha256"]:
                raise HTTPException(409, "Evidence hash changed")
            source.seek(0)
            text = source.read(2_000_000).decode("utf-8", errors="replace")
        if path.stat().st_size > 2_000_000:
            text += "\n[Preview truncated; full evidence retained on disk]"
        return PlainTextResponse(text)


@router.get("/runs/{run_id}/stream/{channel}", dependencies=[Depends(require_reader)])
def stream(project_id: str, run_id: str, channel: str, tail: int = Query(default=2_000_000, ge=1024, le=2_000_000)):
    from fastapi.responses import PlainTextResponse
    if channel not in ("stdout", "stderr"):
        raise HTTPException(404, "Unknown stream")
    with get_conn() as conn:
        row = conn.execute("SELECT streams FROM runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
        if not row:
            raise HTTPException(404, "Run not found")
        value = json.loads(row["streams"]).get(channel)
        if not value:
            raise HTTPException(404, "No stream registered")
        path = Path(value).resolve()
        if path.parent.name != run_id or not path.is_file():
            raise HTTPException(404, "No output yet")
        with path.open("rb") as source:
            size = path.stat().st_size
            source.seek(max(0, size - tail))
            text = source.read(tail).decode("utf-8", errors="replace")
        return PlainTextResponse("[Live output; not acceptance evidence]\n" + text)


@router.get("", dependencies=[Depends(require_reader)])
def state(project_id: str):
    from cairn.server.work_resources import scheduling
    from cairn.server.agent_sessions import sessions
    with get_conn() as conn:
        config = store.project(conn, project_id)
        return {
            "project": config,
            "scheduling": scheduling(conn, project_id),
            "sessions": sessions(conn, project_id),
            "runs": [public_run(r) for r in conn.execute(
                "SELECT * FROM runs WHERE project_id=? AND (state='running' OR id IN "
                "(SELECT id FROM runs WHERE project_id=? ORDER BY rowid DESC LIMIT 100)) ORDER BY rowid DESC", (project_id, project_id))],
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
        if config.phase == "design" and not (value.purpose == "design" or (
            value.purpose == "plan" and value.subject and
            store.work(conn, project_id, value.subject).result.get("design_candidate"))):
            raise HTTPException(409, "Design phase permits conversation and frozen-candidate review only")
        if value.role != body.role or value.revision != body.revision:
            raise HTTPException(409, "Role or control revision changed")
        if value.purpose == "design":
            if conn.execute("SELECT 1 FROM runs r JOIN intents i ON i.id=r.intent_id AND i.project_id=r.project_id "
                            "WHERE r.project_id=? AND json_extract(i.work,'$.purpose')='design' AND "
                            "(r.state='running' OR (r.state='success' AND r.applied=0))", (project_id,)).fetchone():
                raise HTTPException(409, "The existing design session is still active or awaiting application")
        if store.stale_review(conn, project_id, value):
            value.state = "superseded"
            store.save_work(conn, project_id, intent_id, value)
            return JSONResponse(status_code=409, content={"detail": "Review candidate changed; stale review superseded"})
        if affected(conn, project_id, intent_id, body.revision):
            raise HTTPException(409, "New directive requires a checkpoint before dispatch")
        allowed = ("approved", "merged") if body.phase == "integration" else ("ready", "waiting")
        if value.state not in allowed or (value.retry_at and value.retry_at > utcnow()):
            raise HTTPException(409, "Work package is not ready")
        if value.state == "waiting" and value.result.get("wait_on"):
            raise HTTPException(409, "Waiting for observed work to change; advance before claiming")
        if body.phase == "integration" and (value.role != "implementer" or not value.result.get("review")):
            raise HTTPException(409, "Integration requires a reviewed implementation")
        if body.phase == "integration" and value.result.get("pending_design"):
            raise HTTPException(409, "Integration awaits the affected design decision")
        if body.phase == "integration" and conn.execute(
            "SELECT 1 FROM runs WHERE project_id=? AND role='integration' AND state='running'", (project_id,)
        ).fetchone():
            raise HTTPException(409, "Another merge is in progress")
        if body.phase == "integration":
            repository = os.path.normcase(str(Path(config.repository).resolve()))
            for row in conn.execute("SELECT p.development FROM runs r JOIN projects p ON p.id=r.project_id "
                                    "WHERE r.role='integration' AND r.state='running'"):
                other = DevelopmentProject.model_validate_json(row["development"])
                if os.path.normcase(str(Path(other.repository).resolve())) == repository:
                    raise HTTPException(409, "Another project is merging into this repository")
        for dependency in value.dependencies:
            if store.work(conn, project_id, dependency).state != "verified":
                raise HTTPException(409, f"Dependency {dependency} is not verified")
            source = conn.execute("SELECT to_fact_id FROM intents WHERE project_id=? AND id=?", (project_id, dependency)).fetchone()
            if source and source["to_fact_id"]:
                conn.execute("INSERT OR IGNORE INTO intent_sources(intent_id,project_id,fact_id) VALUES(?,?,?)",
                             (intent_id, project_id, source["to_fact_id"]))
        if conn.execute("SELECT 1 FROM runs WHERE project_id=? AND intent_id=? AND state='running'",
                        (project_id, intent_id)).fetchone():
            raise HTTPException(409, "Work package already has a live run")
        from cairn.server.work_resources import resource_conflicts
        for held in conn.execute("SELECT r.project_id,i.work FROM runs r JOIN intents i ON i.project_id=r.project_id AND i.id=r.intent_id WHERE r.state='running'"):
            conflict = resource_conflicts(value.resources, project_id, WorkPackage.model_validate_json(held["work"]).resources, held["project_id"])
            if conflict:
                raise HTTPException(409, "Shared resource is in use: " + ", ".join(sorted(conflict)))
        run_id, token = str(uuid.uuid4()), secrets.token_urlsafe(32)
        if value.role == "planner":
            config.state, config.wait_reason, config.wake_at = "active", "", None
            store.save_project(conn, project_id, config)
        now = utcnow()
        conn.execute("INSERT INTO runs(id,project_id,intent_id,worker,role,revision,token_hash,host,started_at,heartbeat_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (run_id, project_id, intent_id, body.worker, "integration" if body.phase == "integration" else body.role,
                      body.revision, token_hash(token), body.host, now, now))
        value.state, value.retry_at = (value.state if body.phase == "integration" else "running"), None
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
        conn.execute("UPDATE runs SET heartbeat_at=?,session=COALESCE(?,session),pid=COALESCE(?,pid),process_identity=COALESCE(?,process_identity),"
                     "checkpoint=COALESCE(?,checkpoint) WHERE id=?",
                     (utcnow(), body.session, body.pid, body.process_identity, body.checkpoint, run_id))
        conn.execute("UPDATE intents SET last_heartbeat_at=? WHERE project_id=? AND id=?", (utcnow(), project_id, row["intent_id"]))
        if body.streams:
            if any(key not in ("stdout", "stderr", "terminal") or Path(path).resolve().parent.name != run_id
                   for key, path in body.streams.items()):
                raise HTTPException(422, "Streams must be in their owned run directory")
            conn.execute("UPDATE runs SET streams=? WHERE id=?", (store.dump(body.streams), run_id))
        value = store.work(conn, project_id, row["intent_id"])
        superseded = affected(conn, project_id, row["intent_id"], row["revision"])
        superseded = superseded or bool(value.result.get("pending_design")) or value.state == "superseded" or store.stale_review(conn, project_id, value)
        return {"cancel": project_row["status"] != "active" or bool(row["cancel_requested"]), "checkpoint_requested": superseded,
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
        value = store.work(conn, project_id, row["intent_id"])
        stale = affected(conn, project_id, row["intent_id"], row["revision"]) or value.state == "superseded" or store.stale_review(conn, project_id, value)
        cancelled = get_project_or_404(conn, project_id)["status"] != "active" or bool(row["cancel_requested"])
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
        if value.purpose == "design":
            from cairn.server.design import settle_inputs
            settle_inputs(conn, project_id, row["intent_id"], run_id, outcome)
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
        if get_project_or_404(conn, project_id)["status"] == "completed":
            conn.execute("UPDATE projects SET status='active' WHERE id=?", (project_id,))
            store.event(conn, project_id, "project_reopened", project_id, "New user directive requires further work")
    control = Control(kind=kind, source="operator", state="accepted" if kind == "directive" else "received",
                      revision=config.requested_revision, scope=body.scope, replaces=body.replaces)
    hint_id = next_hint_id(conn, project_id)
    conn.execute("INSERT INTO hints(id,project_id,content,creator,created_at,control) VALUES(?,?,?,?,?,?)",
                 (hint_id, project_id, body.content, "operator", utcnow(), store.dump(control)))
    if kind == "suggestion" and config.phase != "design":
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
        conn.execute("BEGIN IMMEDIATE")
        return {"state": store.stop_project(conn, project_id)}


@router.post("/runs/{run_id}/operations")
def reserve_operation(project_id: str, run_id: str, body: OperationRequest):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = live_run(conn, project_id, run_id, body.token)
        store.project(conn, project_id, active=True)
        value = store.work(conn, project_id, row["intent_id"])
        if row["cancel_requested"] or value.result.get("pending_design") or value.state == "superseded" or affected(conn, project_id, row["intent_id"], row["revision"]):
            raise HTTPException(409, "Stopped or revised work requires a checkpoint before another external action")
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


@router.put("/runs/{run_id}/operations/{key:path}")
def reconcile_operation(project_id: str, run_id: str, key: str, body: OperationResult):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        live_run(conn, project_id, run_id, body.token)
        row = conn.execute("SELECT * FROM operations WHERE project_id=? AND key=?", (project_id, key)).fetchone()
        if row is None:
            raise HTTPException(404, "Reserve an operation before recording it")
        if row["run_id"] != run_id and body.state == "absent" and conn.execute(
            "SELECT 1 FROM runs WHERE id=? AND state='running'", (row["run_id"],)
        ).fetchone():
            raise HTTPException(409, "Another live run still owns the uncertain external action")
        if row["state"] == "done" and body.state != "done":
            raise HTTPException(409, "A completed operation cannot be reset for replay")
        if row["state"] == "done" and row["resource_id"] and body.resource_id != row["resource_id"]:
            raise HTTPException(409, "A completed operation cannot name a different resource")
        conn.execute("UPDATE operations SET state=?,resource_id=?,evidence=?,updated_at=? WHERE project_id=? AND key=?",
                     (body.state, body.resource_id, body.evidence, utcnow(), project_id, key))
        return {"key": key, "state": body.state}
