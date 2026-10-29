"""One resumable Claude Code design turn; Cairn owns questions and publication."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import uuid

from cairn.dispatcher.design_skills import BMAD_SKILLS, prepare, run_script
from cairn.dispatcher.runtime.git_workspace import git
from cairn.dispatcher.runtime.design_artifacts import export_artifacts
from cairn.server.development_models import PlannedWork
from cairn.workspace import project_workspace


def tool_path_allowed(workspace: Path, snapshot: Path, tool: str, inputs: dict):
    if tool in ("Write", "Edit"):
        target = Path(inputs.get("file_path", ""))
        target = (workspace / target).resolve() if not target.is_absolute() else target.resolve()
        return target.is_relative_to((workspace / "artifacts").resolve())
    if tool in ("Read", "Glob", "Grep"):
        target = Path(inputs.get("file_path") or inputs.get("path") or workspace)
        target = (workspace / target).resolve() if not target.is_absolute() else target.resolve()
        return any(target.is_relative_to(root.resolve()) for root in (workspace, snapshot))
    if tool == "Skill":
        return inputs.get("skill", "").lstrip("/") in {*BMAD_SKILLS, "cairn-design", "grilling"}
    return tool in ("WebSearch", "WebFetch") or tool in ("mcp__cairn__save_draft", "mcp__cairn__ask_questions", "mcp__cairn__resolve_questions", "mcp__cairn__bmad_script", "mcp__cairn__choose_design_mode")


async def run_turn(dispatcher, project, intent, worker, run, directory, snapshot, cancellation):
    from cairn.dispatcher.design_terminal import run_console
    pid = project.project.id
    workspace = project_workspace(project.project.development, pid, dispatcher.directory) / "design-session"
    workspace.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(prepare, workspace, dispatcher.config.design)
    repository = Path(project.project.development.repository)
    message_id = intent.work.result.get('message_id')
    state = dispatcher.call(pid, 'design' + ('?input_id=' + str(message_id) if message_id else ''))
    draft = state["draft"]
    baseline = git(snapshot, "rev-parse", "HEAD")
    if draft["baseline"] and draft["baseline"] != baseline:
        raise RuntimeError("Code changed since the design baseline; reopen the design before continuing")
    session = draft["session"] or str(uuid.uuid4())
    context = {"draft":draft,"snapshot":str(snapshot),"workspace":str(workspace),
        "revision_feedback":intent.work.result.get("revision_feedback"),"independent_review":state.get("review"),
        "controls":[{"id":h.id,"content":h.content,"control":h.control.model_dump()} for h in project.hints if h.control],
        "conversation":[m for m in state["messages"] if m["kind"] in ("user","text","questions")],
        "native_work_schema":PlannedWork.model_json_schema()}
    (workspace/"context.json").write_text(json.dumps(context,ensure_ascii=False),encoding="utf-8")
    user = next((m for m in state["messages"] if m["id"] == message_id), None)
    prompt = user["data"]["content"] + "\nRead context.json and use cairn-design and installed BMAD/grilling skills. Save meaningful changes via Cairn tools." if user else None
    dispatcher.call(pid,f"design/runs/{run['id']}",{"token":run["token"],"id":"session","kind":"session","data":{"session":session,"baseline":baseline}})
    dispatcher.call(pid,f"design/runs/{run['id']}/inputs",{"token":run["token"],"action":"start","message_ids":[]})
    try:
        await run_console(dispatcher,project,worker,run,directory,snapshot,cancellation,
            workspace=workspace,prompt=prompt,session=session,message_id=message_id)
    finally:
        await asyncio.to_thread(export_artifacts,workspace/"artifacts",repository)
    return {"kind":"design","summary":"Design saved; terminal closed for candidate review"},session


def append_log(path: Path, text):
    with path.open("a", encoding="utf-8") as log:
        log.write(text + "\n")
