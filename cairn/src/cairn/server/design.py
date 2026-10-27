"""Design drafts share Cairn's native plan and review protocol."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field, ValidationError

from cairn.server import development_store as store
from cairn.server.access import require_operator, require_reader, require_worker
from cairn.server.db import get_conn
from cairn.server.development_models import DevelopmentModel, DevelopmentOutcome, PlannedWork, WorkPackage
from cairn.server.services import utcnow

router = APIRouter(prefix="/projects/{project_id}/development/design", tags=["design"])

SCHEMA = """
CREATE TABLE IF NOT EXISTS designs (
 project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS design_messages (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 key TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(project_id,key)
);
CREATE TABLE IF NOT EXISTS design_inputs (
 message_id INTEGER PRIMARY KEY REFERENCES design_messages(id) ON DELETE CASCADE,
 project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 intent_id TEXT NOT NULL, run_id TEXT,
 state TEXT NOT NULL DEFAULT 'queued'
);
"""


class Question(DevelopmentModel):
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    recommendation: str = ""
    options: list[str] = []
    depends_on: list[str] = []
    answer: str | None = None
    answer_message_id: int | None = None
    asked_after: int = 0


class Draft(DevelopmentModel):
    version: int = 0
    stage: Literal["brief", "requirements", "ux", "architecture", "plan"] = "brief"
    documents: dict[str, str] = {}
    document_sources: dict[str, str] = {}
    decisions: list[str] = []
    questions: list[Question] = []
    work: list[PlannedWork] = []
    baseline: str = ""
    control_revision: int = 1
    session: str | None = None
    candidate: dict = {}
    releases: list[dict] = []
    stage_reviews: list[dict] = []
    commands: list[dict] = []
    materials: dict[str, str] = {}  # repository target -> artifacts/source
    prepared_materials: list[dict] = []


class Message(DevelopmentModel):
    id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    answers: dict[str, str] = {}
    session: str | None = None


class InputDelivery(DevelopmentModel):
    token: str
    action: Literal["start", "take", "delivered", "close"]
    message_ids: list[int] = []


class AgentUpdate(DevelopmentModel):
    token: str
    id: str = Field(min_length=1, max_length=200)
    kind: Literal["text", "activity", "commands", "session", "draft", "questions", "answers"]
    data: dict


class Version(DevelopmentModel):
    version: int
    digest: str = ""


def load(conn, project_id):
    store.project(conn, project_id)
    row = conn.execute("SELECT data FROM designs WHERE project_id=?", (project_id,)).fetchone()
    return Draft.model_validate_json(row["data"]) if row else Draft()


def save(conn, project_id, draft):
    conn.execute("INSERT INTO designs(project_id,data) VALUES(?,?) ON CONFLICT(project_id) DO UPDATE SET data=excluded.data",
                 (project_id, store.dump(draft)))


def validated(model, data):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]) from exc


def record(conn, project_id, key, kind, data):
    encoded = store.dump(data)
    prior = conn.execute("SELECT * FROM design_messages WHERE project_id=? AND key=?", (project_id, key)).fetchone()
    if prior:
        if prior["kind"] != kind or prior["data"] != encoded:
            raise HTTPException(409, "Message ID was already used for different content")
        return prior["id"]
    return conn.execute("INSERT INTO design_messages(project_id,key,kind,data,created_at) VALUES(?,?,?,?,?)",
                        (project_id, key, kind, encoded, utcnow())).lastrowid


def messages(conn, project_id, after=0):
    return [{**dict(r), "data": json.loads(r["data"])} for r in conn.execute(
        "SELECT * FROM design_messages WHERE project_id=? AND id>? ORDER BY id LIMIT 500", (project_id, after))]


def review_state(conn, project_id, draft):
    from cairn.server.development_workflow import all_work
    from cairn.server.routers.development import public_run
    work = all_work(conn, project_id)
    subject = draft.candidate.get("subject")
    if not subject:
        subject = next((iid for iid, value in reversed(work) if value.result.get("design_candidate")), None)
    if not subject:
        return None
    author = next(value for iid, value in work if iid == subject)
    reviews = [(iid, value) for iid, value in work if value.subject == subject and value.purpose == "plan"]
    if not reviews:
        return None
    iid, value = reviews[-1]
    runs = conn.execute("SELECT * FROM runs WHERE project_id=? AND intent_id=? ORDER BY rowid DESC", (project_id, iid)).fetchall()
    run = public_run(runs[0]) if runs else None
    output = value.result.get("output", {})
    frozen = author.result.get("design_snapshot", {})
    current = draft.candidate.get("subject") == subject
    revision = next(({"intent_id": rid, "state": item.state} for rid, item in reversed(work)
                     if item.purpose == "design" and item.result.get("revision_feedback", {}).get("digest") == author.result["design_candidate"]), None)
    from cairn.server.design_documents import resolve_documents
    current_docs = resolve_documents(conn, project_id, draft)[0]
    old_tasks = {w["key"]: w for w in frozen.get("work", [])}
    new_tasks = {w.key: w.model_dump() for w in draft.work}
    changes = {"documents": [key for key in set(current_docs) | set(frozen.get("documents", {})) if current_docs.get(key) != frozen.get("documents", {}).get(key)],
               "tasks": [key for key in set(old_tasks) | set(new_tasks) if old_tasks.get(key) != new_tasks.get(key)],
               "decisions_added": [d for d in draft.decisions if d not in frozen.get("decisions", [])],
               "decisions_removed": [d for d in frozen.get("decisions", []) if d not in draft.decisions]}
    return {"subject": subject, "intent_id": iid, "version": frozen.get("version"), "stage": frozen.get("stage"),
            "digest": author.result["design_candidate"], "current": current, "work_state": value.state,
            "summary": draft.candidate.get("summary", "") if current else output.get("summary", ""),
            "verdict": draft.candidate.get("review", "pending") if current else output.get("verdict"),
            "checkpoint": value.checkpoint, "retry_at": value.retry_at, "run": run, "attempts": len(runs), "revision": revision, "changes": changes}


@router.get("", dependencies=[Depends(require_reader)])
def state(project_id: str, after: int = 0, input_id: int | None = None):
    with get_conn() as conn:
        draft = load(conn, project_id)
        if input_id is not None:
            history = [{**dict(r), "data": json.loads(r["data"])} for r in conn.execute(
                "SELECT * FROM (SELECT * FROM design_messages WHERE project_id=? AND kind='user' AND id<=? ORDER BY id DESC LIMIT 20) ORDER BY id", (project_id, input_id))]
        else:
            history = messages(conn, project_id, after)
        from cairn.server.design_documents import resolve_documents
        from pathlib import Path
        documents, _, document_errors = resolve_documents(conn, project_id, draft)
        return {"draft": draft, "messages": history, "readiness": readiness(conn, project_id, draft),
                "deliveries": {str(r["message_id"]): r["state"] for r in conn.execute(
                    "SELECT message_id,state FROM design_inputs WHERE project_id=?", (project_id,))},
                "document_views": documents, "document_errors": document_errors,
                "review": review_state(conn, project_id, draft),
                "artifact_directory": str(Path(store.project(conn, project_id).repository) / ".cairn" / "artifacts")}


def ensure_idle(conn, project_id):
    if conn.execute("SELECT 1 FROM runs WHERE project_id=? AND state='running'", (project_id,)).fetchone():
        raise HTTPException(409, "Wait for the active run to finish or interrupt it first")
    from cairn.server.development_workflow import all_work
    if any(w.purpose == "design" and w.state in ("ready", "running", "implemented")
           for _, w in all_work(conn, project_id)):
        raise HTTPException(409, "A design message is already queued")


def document_id(documents, reference):
    name = reference.partition("#")[0]
    if name in documents:
        return name
    # Older drafts referenced file stems; accept only a unique registered source.
    matches = [key for key, content in documents.items()
               if any(PurePosixPath(path).stem == name for path in re.findall(r"artifacts/[^\s<>\"'`]+?\.md", content))]
    return matches[0] if len(matches) == 1 else None


def readiness(conn, project_id, draft):
    from cairn.server.development_workflow import validate_plan
    from cairn.server.design_documents import resolve_documents
    config = store.project(conn, project_id)
    issues = []
    issues.extend(resolve_documents(conn, project_id, draft)[2])
    from cairn.server.design_materials import prepare_materials
    try:
        prepare_materials(conn, project_id, draft, write=False)
    except (ValueError, OSError) as exc:
        issues.append("交付材料：" + str(exc))
    if config.phase != "design":
        issues.append("项目已进入开发阶段")
    if conn.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()["status"] != "active":
        issues.append("请先继续项目")
    try:
        ensure_idle(conn, project_id)
    except HTTPException:
        issues.append("请等待当前任务完成并应用结果")
    if not draft.documents:
        issues.append("尚缺方案文档")
    if not draft.baseline:
        issues.append("尚缺代码基线，请启动设计 Agent 核对当前代码")
    if not draft.session:
        issues.append("尚缺设计会话，请启动或恢复设计 Agent")
    if any(q.answer is None for q in draft.questions):
        issues.append("请先回答所有待决问题")
    if config.revision != config.requested_revision or draft.control_revision != config.revision:
        issues.append("黑板指令已变化，请先让设计 Agent 核对草案")
    if draft.stage == "plan":
        try:
            validate_plan(conn, project_id, DevelopmentOutcome(kind="plan", summary="Design readiness", work=draft.work))
        except ValueError as exc:
            issues.append(str(exc))
    for task in draft.work if draft.stage == "plan" else []:
        if not task.spec_refs:
            issues.append(f"任务 {task.key} 未引用设计文档")
        for ref in task.spec_refs:
            document, separator, section = ref.partition("#")
            if document_id(draft.documents, ref) is None or separator and not section.strip():
                issues.append(f"任务 {task.key} 的文档引用无效：{ref}（使用已登记文档 ID 或 ID#章节）")
    author = conn.execute("SELECT r.applied FROM runs r JOIN intents i ON i.project_id=r.project_id AND i.id=r.intent_id "
                          "WHERE r.project_id=? AND r.state='success' AND json_extract(i.work,'$.purpose')='design' "
                          "ORDER BY r.rowid DESC LIMIT 1", (project_id,)).fetchone()
    if not author or not author["applied"]:
        issues.append("设计尚无已完成并应用的输出")
    return {"ready": not issues, "issues": issues, "version": draft.version}


@router.post("/messages", dependencies=[Depends(require_operator)], status_code=201)
def send(project_id: str, body: Message):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id, active=True)
        encoded = body.model_dump()
        prior = conn.execute("SELECT id,data FROM design_messages WHERE project_id=? AND key=?", (project_id, body.id)).fetchone()
        if prior:
            previous = json.loads(prior["data"])
            previous.setdefault("session", None)
            if previous != encoded:
                raise HTTPException(409, "Message ID was already used for different content")
            return {"id": prior["id"]}
        if config.phase != "design":
            raise HTTPException(409, "Execution changes belong in blackboard controls")
        from cairn.server.development_workflow import all_work
        for iid, w in all_work(conn, project_id):
            if w.purpose == "design" and w.state == "waiting":
                w.state = "superseded"
                store.save_work(conn, project_id, iid, w)
        if config.revision != config.requested_revision:
            config.revision = config.requested_revision
            store.save_project(conn, project_id, config)
        draft = load(conn, project_id)
        if body.session and body.session != draft.session:
            raise HTTPException(409, "Design session changed; reload before sending")
        if draft.candidate:
            raise HTTPException(409, "Reopen the frozen candidate before changing its design")
        for key, answer in body.answers.items():
            question = next((q for q in draft.questions if q.id == key), None)
            if question is None or not answer.strip():
                raise HTTPException(422, "Answer must reference an existing question and contain text")
            if any(not next((q.answer for q in draft.questions if q.id == dep), None) for dep in question.depends_on):
                raise HTTPException(409, "Question prerequisites remain unanswered")
            question.answer = answer
        if body.answers:
            draft.version += 1
            save(conn, project_id, draft)
        mid = record(conn, project_id, body.id, "user", encoded)
        for question in draft.questions:
            if question.id in body.answers:
                question.answer_message_id = mid
        save(conn, project_id, draft)
        remaining = [q.id for q in draft.questions if q.answer is None]
        if body.answers and remaining:
            return {"id": mid, "intent_id": None, "remaining_questions": remaining}
        active = next(((iid, w) for iid, w in reversed(all_work(conn, project_id)) if w.purpose == "design"
                       and w.state in ("ready", "running", "implemented") and not w.result.get("input_closed")), None)
        intent = active[0] if active else store.add_intent(conn, project_id, "Design conversation", WorkPackage(
            role="planner", purpose="design", revision=config.revision, result={"message_id": mid}))
        conn.execute("INSERT INTO design_inputs(message_id,project_id,intent_id) VALUES(?,?,?)", (mid, project_id, intent))
        # Only unsent inputs can be replayed after interruption; uncertain sends stay visible for user reconciliation.
        if not active:
            conn.execute("UPDATE design_inputs SET intent_id=?,run_id=NULL WHERE project_id=? AND state='queued'",
                         (intent, project_id))
            first = conn.execute("SELECT MIN(message_id) FROM design_inputs WHERE project_id=? AND intent_id=? AND state='queued'", (project_id, intent)).fetchone()[0]
            value = store.work(conn, project_id, intent)
            value.result["message_id"] = first
            store.save_work(conn, project_id, intent, value)
        return {"id": mid, "intent_id": intent, "delivery": "queued"}


@router.post("/runs/{run_id}/inputs", dependencies=[Depends(require_worker)])
def inputs(project_id: str, run_id: str, body: InputDelivery):
    from cairn.server.routers.development import live_run
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = live_run(conn, project_id, run_id, body.token)
        value = store.work(conn, project_id, run["intent_id"])
        store.project(conn, project_id, active=True)
        if value.purpose != "design" or run["cancel_requested"]:
            raise HTTPException(409, "Design input lease is no longer active")
        if body.action == "delivered":
            for mid in body.message_ids:
                conn.execute("UPDATE design_inputs SET state='delivered' WHERE project_id=? AND intent_id=? AND run_id=? AND message_id=? AND state='sending'",
                             (project_id, run["intent_id"], run_id, mid))
            return {"items": []}
        initial = value.result["message_id"]
        if body.action == "start":
            conn.execute("UPDATE design_inputs SET state='sending',run_id=? WHERE project_id=? AND intent_id=? AND message_id=? AND state='queued'",
                         (run_id, project_id, run["intent_id"], initial))
            return {"items": []}
        if value.result.get("input_closed"):
            return {"items": [], "closed": True}
        rows = conn.execute("SELECT d.message_id,m.data FROM design_inputs d JOIN design_messages m ON m.id=d.message_id "
                            "WHERE d.project_id=? AND d.intent_id=? AND d.state='queued' ORDER BY d.message_id", (project_id, run["intent_id"])).fetchall()
        if body.action == "close" and not rows:
            value.result["input_closed"] = True
            store.save_work(conn, project_id, run["intent_id"], value)
            return {"items": [], "closed": True}
        for row in rows:
            conn.execute("UPDATE design_inputs SET state='sending',run_id=? WHERE message_id=?", (run_id, row["message_id"]))
        return {"items": [{"id": r["message_id"], "data": json.loads(r["data"])} for r in rows], "closed": False}


def settle_inputs(conn, project_id, intent_id, run_id, outcome):
    value = store.work(conn, project_id, intent_id)
    if outcome == "success" and value.result.get("message_id"):
        conn.execute("UPDATE design_inputs SET state='delivered',run_id=? WHERE project_id=? AND message_id=? AND state IN ('queued','sending')",
                     (run_id, project_id, value.result["message_id"]))
    conn.execute("UPDATE design_inputs SET state='uncertain' WHERE project_id=? AND run_id=? AND state='sending'", (project_id, run_id))
    pending = conn.execute("SELECT message_id FROM design_inputs WHERE project_id=? AND intent_id=? AND state='queued' ORDER BY message_id",
                           (project_id, intent_id)).fetchall()
    if pending and outcome == "success":
        config = store.project(conn, project_id)
        iid = store.add_intent(conn, project_id, "Continue queued design guidance", WorkPackage(
            role="planner", purpose="design", revision=config.revision, result={"message_id": pending[0]["message_id"]}))
        conn.execute("UPDATE design_inputs SET intent_id=?,run_id=NULL WHERE project_id=? AND intent_id=? AND state='queued'", (iid, project_id, intent_id))


@router.post("/runs/{run_id}", dependencies=[Depends(require_worker)])
def update(project_id: str, run_id: str, body: AgentUpdate):
    from cairn.server.routers.development import live_run
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        run = live_run(conn, project_id, run_id, body.token)
        value = store.work(conn, project_id, run["intent_id"])
        config = store.project(conn, project_id, active=True)
        if value.purpose != "design" or config.phase != "design" or run["cancel_requested"]:
            raise HTTPException(409, "Design writer lease is no longer active")
        event_key = run_id + ":" + body.id
        prior = conn.execute("SELECT 1 FROM design_messages WHERE project_id=? AND key=?", (project_id, event_key)).fetchone()
        if prior:
            return {"id": record(conn, project_id, event_key, body.kind, body.data)}
        draft = load(conn, project_id)
        if body.kind == "draft":
            if body.data.get("version") != draft.version or draft.candidate:
                raise HTTPException(409, "Draft version changed; reload before saving")
            if value.result.get("revision_feedback") and "decisions" in body.data:
                if not isinstance(body.data["decisions"], list) or any(not isinstance(d, str) for d in body.data["decisions"]):
                    raise HTTPException(422, "Expected a decisions array of strings")
                removed = set(draft.decisions) - set(body.data["decisions"])
                approvals = body.data.get("decision_changes", {})
                if not isinstance(approvals, dict):
                    raise HTTPException(422, "Expected confirmed decision to question ID mappings")
                for decision in removed:
                    question = next((q for q in draft.questions if q.id == approvals.get(decision)), None)
                    if not question or question.answer is None or (question.answer_message_id or 0) <= value.result["message_id"]:
                        raise HTTPException(409, "Changing a confirmed decision requires a new explicit user answer: " + decision)
            fields = {k: body.data[k] for k in ("stage", "documents", "decisions", "work", "materials") if k in body.data}
            draft = validated(Draft, {**draft.model_dump(), **fields, "version": draft.version + 1,
                                          "control_revision": config.revision})
        elif body.kind == "questions":
            old = {q.id: q for q in draft.questions}
            if not isinstance(body.data.get("questions"), list):
                raise HTTPException(422, "Expected a questions array")
            incoming = [validated(Question, q) for q in body.data["questions"]]
            if not incoming or len({q.id for q in incoming}) != len(incoming):
                raise HTTPException(422, "Questions require unique IDs")
            for q in incoming:
                if q.answer is not None or q.id in old:
                    raise HTTPException(409, "Only the operator can answer; question IDs cannot be reused")
                if any(dep not in old or not old[dep].answer for dep in q.depends_on):
                    raise HTTPException(409, "Ask only questions on the settled frontier")
                q.asked_after = conn.execute("SELECT COALESCE(MAX(id),0) FROM design_messages WHERE project_id=?", (project_id,)).fetchone()[0]
                q.answer_message_id = None
            draft.questions.extend(incoming)
            draft.version += 1
        elif body.kind == "answers":
            input_id = body.data.get("message_id", value.result["message_id"])
            if input_id != value.result["message_id"] and not conn.execute(
                "SELECT 1 FROM design_inputs WHERE project_id=? AND message_id=? AND run_id=? AND state='delivered'",
                (project_id, input_id, run_id)).fetchone():
                raise HTTPException(409, "Answer source must be a delivered input in this session")
            message = conn.execute("SELECT id,data FROM design_messages WHERE project_id=? AND id=? AND kind='user'",
                                   (project_id, input_id)).fetchone()
            user_message = json.loads(message["data"]) if message else {}
            content = user_message.get("content", "")
            answers = body.data.get("answers", [])
            if not isinstance(answers, list) or not answers or any(not isinstance(a, dict) or not isinstance(a.get("quote"), str) for a in answers):
                raise HTTPException(422, "Provide exact quotes from the current user message")
            for answer in answers:
                q = next((q for q in draft.questions if q.id == answer.get("id")), None)
                quote = answer.get("quote", "")
                if user_message.get("answers") and answer.get("id") not in user_message["answers"]:
                    raise HTTPException(409, "A card answer applies only to its explicitly selected question")
                if not q or q.answer is not None or not quote.strip() or quote not in content or message["id"] <= q.asked_after:
                    raise HTTPException(409, "Answers must quote current user input after the question was asked")
                if any(not next((prior.answer for prior in draft.questions if prior.id == dep), None) for dep in q.depends_on):
                    raise HTTPException(409, "Question prerequisites remain unanswered")
                q.answer, q.answer_message_id = quote, message["id"]
            draft.version += 1
        elif body.kind == "session":
            if not isinstance(body.data.get("session"), str) or not body.data["session"]:
                raise HTTPException(422, "Expected a session ID")
            draft.session = body.data["session"]
            baseline = body.data.get("baseline")
            if not isinstance(baseline, str) or len(baseline) not in (40, 64) or any(c not in "0123456789abcdef" for c in baseline):
                raise HTTPException(422, "Expected an exact Git commit")
            if draft.baseline and draft.baseline != baseline:
                raise HTTPException(409, "Code baseline changed; reopen against the new baseline")
            draft.baseline = baseline
        elif body.kind == "commands":
            draft.commands = body.data.get("commands", [])
        save(conn, project_id, draft)
        return {"id": record(conn, project_id, event_key, body.kind, body.data), "version": draft.version}


def digest(draft):
    payload = draft.model_dump(exclude={"candidate", "releases", "commands", "session"})
    for field in ("materials", "prepared_materials"):
        if not payload[field]:
            payload.pop(field)  # Preserve hashes of candidates frozen before material packaging existed.
    return hashlib.sha256(store.dump(payload).encode()).hexdigest()


@router.post("/candidate", dependencies=[Depends(require_operator)])
def freeze(project_id: str, body: Version):
    from cairn.server.development_workflow import add_review
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id, active=True)
        draft = load(conn, project_id)
        if body.version != draft.version or config.phase != "design":
            raise HTTPException(409, "Design version or phase changed")
        if draft.candidate:
            return draft
        checks = readiness(conn, project_id, draft)
        if not checks["ready"]:
            raise HTTPException(422, "；".join(checks["issues"]))
        for task in draft.work if draft.stage == "plan" else []:
            task.spec_refs = [document_id(draft.documents, ref) + ("#" + ref.partition("#")[2] if "#" in ref else "")
                              for ref in task.spec_refs]
        from cairn.server.design_documents import resolve_documents
        draft.documents, draft.document_sources, document_errors = resolve_documents(conn, project_id, draft)
        if document_errors:
            raise HTTPException(422, "；".join(document_errors))
        from cairn.server.design_materials import prepare_materials
        try:
            draft.prepared_materials = prepare_materials(conn, project_id, draft)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, "交付材料：" + str(exc)) from exc
        outcome = DevelopmentOutcome(kind="plan", summary=f"Design stage: {draft.stage}", work=draft.work)
        author = conn.execute("SELECT r.* FROM runs r JOIN intents i ON i.project_id=r.project_id AND i.id=r.intent_id "
                              "WHERE r.project_id=? AND r.state='success' AND json_extract(i.work,'$.purpose')='design' "
                              "ORDER BY r.rowid DESC LIMIT 1", (project_id,)).fetchone()
        if not author or not author["applied"]:
            raise HTTPException(409, "Wait for the design turn to finish and be applied")
        candidate_hash = digest(draft)
        subject = store.work(conn, project_id, author["intent_id"])
        subject.purpose, subject.state = "plan", "reviewing"
        subject.result = {"output": outcome.model_dump(mode="json"), "source_run_id": author["id"],
                          "design_candidate": candidate_hash, "design_snapshot": draft.model_dump()}
        store.save_work(conn, project_id, author["intent_id"], subject)
        add_review(conn, project_id, author["intent_id"], "plan", config.revision,
                   "Independently review the frozen design, native task dependencies, acceptance and baseline; do not install tasks")
        draft.candidate = {"digest": candidate_hash, "subject": author["intent_id"], "review": "pending",
                           "stage": draft.stage, "kind": "final" if draft.stage == "plan" else "stage"}
        save(conn, project_id, draft)
        return draft


def apply_review(conn, project_id, run, value, subject, outcome):
    draft = load(conn, project_id)
    if draft.candidate.get("digest") != subject.result["design_candidate"] or digest(draft) != draft.candidate["digest"]:
        raise ValueError("Frozen design candidate changed")
    if outcome.verdict == "approve" and outcome.within_scope:
        snapshot = json.loads(run["result"]).get("snapshot", {})
        if snapshot.get("design_baseline") != draft.baseline or not snapshot.get("checks_passed") or snapshot.get("check_scope") != "design_baseline" or not json.loads(run["evidence"]):
            raise ValueError("Design approval requires independent baseline checks and raw evidence")
    draft.candidate.update(review=outcome.verdict if outcome.within_scope else "reject", summary=outcome.summary, run_id=run["id"])
    save(conn, project_id, draft)
    value.state = "verified"
    subject.state = "reviewing"  # Human approval remains required even after a positive review.
    store.save_work(conn, project_id, value.subject, subject)


@router.post("/continue", dependencies=[Depends(require_operator)])
def continue_stage(project_id: str, body: Version):
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id, active=True)
        draft = load(conn, project_id)
        candidate = draft.candidate
        if config.phase != "design" or body.version != draft.version or body.digest != digest(draft):
            raise HTTPException(409, "Confirm the exact frozen design version")
        if candidate.get("digest") != body.digest or candidate.get("review") != "approve" or candidate.get("kind") != "stage":
            raise HTTPException(409, "This stage must pass independent review before continuing")
        if config.revision != draft.control_revision or config.requested_revision != config.revision:
            raise HTTPException(409, "Controls changed after design review")
        ensure_idle(conn, project_id)
        stages = ["brief", "requirements", "ux", "architecture", "plan"]
        next_stage = stages[stages.index(draft.stage) + 1]
        subject = store.work(conn, project_id, candidate["subject"])
        subject.state = "verified"
        store.save_work(conn, project_id, candidate["subject"], subject)
        draft.stage_reviews.append({**candidate, "version": draft.version, "confirmed_at": utcnow()})
        draft.stage, draft.candidate = next_stage, {}
        draft.version += 1
        save(conn, project_id, draft)
        mid = record(conn, project_id, "continue:" + body.digest, "user", {"content": f"当前阶段评审已确认，继续 {next_stage} 方案设计。"})
        store.add_intent(conn, project_id, "Continue reviewed design stage", WorkPackage(
            role="planner", purpose="design", revision=config.revision, result={"message_id": mid}))
        return draft


@router.post("/reopen", dependencies=[Depends(require_operator)])
def reopen(project_id: str, body: Version):
    from cairn.server.development_workflow import all_work
    from cairn.dispatcher.runtime.git_workspace import git, GitError
    from pathlib import Path
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        config = store.project(conn, project_id, active=True)
        draft = load(conn, project_id)
        work = all_work(conn, project_id)
        if any(w.purpose == "design" and w.result.get("reopened_from") == body.model_dump() for _, w in work):
            return draft
        if body.version != draft.version:
            raise HTTPException(409, "Design version changed")
        ensure_idle(conn, project_id)
        if config.phase != "design":
            raise HTTPException(409, "Use blackboard directives to revise executing work")
        review = review_state(conn, project_id, draft)
        subject = draft.candidate.get("subject") or (review or {}).get("subject")
        if not subject:
            raise HTTPException(409, "No frozen design or previous review is available to revise")
        author = store.work(conn, project_id, subject)
        frozen = author.result["design_snapshot"]
        if body.digest and body.digest != author.result["design_candidate"]:
            raise HTTPException(409, "Review candidate changed; reload before requesting revision")
        try:
            repository = Path(config.repository)
            branch = config.integration_branch or git(repository, "symbolic-ref", "--short", "HEAD")
            baseline = git(repository, "rev-parse", "refs/heads/" + branch)
        except (GitError, OSError) as exc:
            raise HTTPException(409, "Cannot inspect repository baseline for design revision") from exc
        feedback = {"digest": author.result["design_candidate"], "version": frozen["version"],
                    "stage": frozen["stage"], "baseline": frozen["baseline"],
                    "review_intent": (review or {}).get("intent_id"), "review_run": ((review or {}).get("run") or {}).get("id"),
                    "verdict": (review or {}).get("verdict"), "summary": (review or {}).get("summary", ""),
                    "output": store.work(conn, project_id, review["intent_id"]).result.get("output", {}) if review else {},
                    "baseline_changed": baseline != frozen["baseline"]}
        for iid, w in work:
            if iid == subject or w.subject == subject or w.purpose == "design" and w.state == "waiting":
                w.state = "superseded"
                store.save_work(conn, project_id, iid, w)
        config.revision = config.requested_revision
        store.save_project(conn, project_id, config)
        draft.candidate, draft.baseline = {}, baseline
        draft.session = draft.session or frozen.get("session")
        draft.documents.update({key: source for key, source in draft.document_sources.items() if key in draft.documents})
        draft.version += 1
        save(conn, project_id, draft)
        mid = record(conn, project_id, f"revise:{subject}:{body.version}", "user", {
            "content": "请根据上一轮独立评审修订当前方案，逐项核对意见并保存新草案；完成后等待我重新送审，不得自行发布。"})
        iid = store.add_intent(conn, project_id, "Revise design using independent review", WorkPackage(
            role="planner", purpose="design", revision=config.revision, result={"message_id": mid,
                "revision_feedback": feedback, "reopened_from": body.model_dump()}))
        store.event(conn, project_id, "design_revision_requested", iid, f"Revise frozen v{frozen['version']} using review {feedback['review_intent']}")
        return draft


@router.post("/interrupt", dependencies=[Depends(require_operator)])
def interrupt(project_id: str):
    from cairn.server.development_workflow import all_work
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        store.project(conn, project_id)
        for iid, w in all_work(conn, project_id):
            if w.purpose != "design" or w.state in ("verified", "superseded"):
                continue
            running = conn.execute("SELECT id FROM runs WHERE project_id=? AND intent_id=? AND state='running'", (project_id, iid)).fetchone()
            if running:
                conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (running["id"],))
            else:
                w.state = "superseded"
                store.save_work(conn, project_id, iid, w)
        store.event(conn, project_id, "design_interrupted", project_id, "User interrupted the design turn; draft retained")
        return {"state": "interrupt_requested"}


@router.post("/publish", dependencies=[Depends(require_operator)])
def publish(project_id: str, body: Version):
    from cairn.server.development_workflow import install_plan
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        draft = load(conn, project_id)
        prior = next((r for r in draft.releases if r["digest"] == body.digest and r["version"] == body.version), None)
        if prior:
            return prior
        config = store.project(conn, project_id, active=True)
        if draft.stage != "plan" or draft.candidate.get("kind", "final") != "final":
            raise HTTPException(409, "Only the final plan can be published for development")
        if config.phase != "design" or body.version != draft.version or body.digest != digest(draft):
            raise HTTPException(409, "Confirm the exact frozen design version")
        candidate = draft.candidate
        if candidate.get("digest") != body.digest or candidate.get("review") != "approve":
            raise HTTPException(409, "Independent review must approve this exact candidate")
        if config.revision != draft.control_revision or config.requested_revision != config.revision:
            raise HTTPException(409, "Controls changed after design review")
        ensure_idle(conn, project_id)
        # Publishing is local-only: never accept a review of yesterday's code.
        from cairn.dispatcher.runtime.git_workspace import git, GitError
        from pathlib import Path
        try:
            branch = config.integration_branch or git(Path(config.repository), "symbolic-ref", "--short", "HEAD")
            if git(Path(config.repository), "rev-parse", "refs/heads/" + branch) != draft.baseline:
                raise HTTPException(409, "Repository baseline changed; reopen and review the updated code")
            if git(Path(config.repository), "status", "--porcelain"):
                raise HTTPException(409, "Commit or set aside repository changes before publishing")
        except (GitError, OSError) as exc:
            raise HTTPException(409, "Design publication requires access to the local Git repository") from exc
        review = conn.execute("SELECT result FROM runs WHERE id=?", (candidate["run_id"],)).fetchone()
        snapshot = json.loads(review["result"]).get("snapshot", {}) if review else {}
        if snapshot.get("design_baseline") != draft.baseline or not snapshot.get("checks_passed") or snapshot.get("check_scope") != "design_baseline":
            raise HTTPException(409, "Independent baseline and runnable acceptance checks are required")
        config.design = "\n\n".join(f"# {key}\n{content}" for key, content in draft.documents.items())
        config.architecture = draft.documents.get("architecture", config.architecture)
        config.design_revision += 1
        config.design_materials = draft.prepared_materials
        config.phase = "execution"
        conn.execute("UPDATE facts SET description=? WHERE project_id=? AND id='goal'",
                     (draft.documents.get("requirements") or draft.documents.get("brief") or config.design, project_id))
        store.save_project(conn, project_id, config)
        try:
            count = install_plan(conn, project_id, DevelopmentOutcome(kind="plan", summary="Approved design", work=draft.work), config.revision)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        subject = store.work(conn, project_id, candidate["subject"])
        subject.state = "verified"
        store.save_work(conn, project_id, candidate["subject"], subject)
        release = {"version": draft.version, "digest": body.digest, "baseline": draft.baseline,
                   "created": count, "published_at": utcnow()}
        draft.releases.append(release)
        save(conn, project_id, draft)
        store.event(conn, project_id, "design_published", body.digest, f"Design v{draft.version}: {count} native tasks")
        return release


@router.get("/events", dependencies=[Depends(require_reader)])
async def events(project_id: str, request: Request, after: int = 0):
    import asyncio
    from fastapi.responses import StreamingResponse
    with get_conn() as conn:
        load(conn, project_id)

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            with get_conn() as conn:
                batch = messages(conn, project_id, cursor)
            for item in batch:
                cursor = item["id"]
                yield "id: " + str(cursor) + "\ndata: " + json.dumps(item, ensure_ascii=False) + "\n\n"
            if not batch:
                yield ": heartbeat\n\n"
            await asyncio.sleep(.25 if batch else 1)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
