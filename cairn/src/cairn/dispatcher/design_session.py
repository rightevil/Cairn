"""One resumable Claude Code design turn; Cairn owns questions and publication."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import uuid

from cairn.dispatcher.design_skills import BMAD_SKILLS, prepare, run_script
from cairn.dispatcher.runtime.git_workspace import git
from cairn.dispatcher.runtime.design_artifacts import export_artifacts
from cairn.dispatcher.runtime.process_identity import process_identity
from cairn.server.development_models import PlannedWork


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
    return tool in ("WebSearch", "WebFetch") or tool in ("mcp__cairn__save_draft", "mcp__cairn__ask_questions", "mcp__cairn__resolve_questions", "mcp__cairn__bmad_script")


async def run_turn(dispatcher, project, intent, worker, run, directory, snapshot, cancellation):
    from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, ResultMessage,
                                  SystemMessage, StreamEvent, AssistantMessage, TextBlock, tool, create_sdk_mcp_server)
    # shortcut: SDK process ownership uses its subprocess transport; review on SDK upgrades.
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    from cairn.dispatcher.development import executable_argv

    pid = project.project.id
    workspace = dispatcher.directory / pid / "design-session"
    workspace.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(prepare, workspace, dispatcher.config.design)
    repository = Path(project.project.development.repository)
    await asyncio.to_thread(export_artifacts, workspace / "artifacts", repository)
    state = dispatcher.call(pid, "design?input_id=" + str(intent.work.result["message_id"]))
    draft = state["draft"]
    baseline = git(snapshot, "rev-parse", "HEAD")
    append_log(directory / "stderr.log", "")
    if draft["baseline"] and draft["baseline"] != baseline:
        raise RuntimeError("Code changed since the design baseline; reopen the design before continuing")
    sequence = 0
    session = draft["session"] or str(uuid.uuid4())

    def emit(kind, data):
        nonlocal sequence
        sequence += 1
        return dispatcher.call(pid, f"design/runs/{run['id']}", {
            "token": run["token"], "id": str(sequence), "kind": kind, "data": data})

    def result(value, error=False):
        return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": error}

    @tool("save_draft", "Persist the native design draft using the latest version. No publication or approval.",
          {"type": "object", "properties": {"version": {"type": "integer"}, "stage": {"enum": ["brief", "requirements", "ux", "architecture", "plan"]},
           "documents": {"type": "object", "additionalProperties": {"type": "string"}}, "decisions": {"type": "array", "items": {"type": "string"}},
           "work": {"type": "array", "items": PlannedWork.model_json_schema()}}, "required": ["version", "stage", "documents", "decisions", "work"], "additionalProperties": False})
    async def save_draft(args):
        try:
            await asyncio.to_thread(export_artifacts, workspace / "artifacts", repository)
            return result(await asyncio.to_thread(emit, "draft", args))
        except RuntimeError as exc:
            return result(str(exc), True)

    @tool("ask_questions", "Persist the settled frontier and end this turn. Only the user supplies answers.",
          {"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "object", "properties": {
              "id": {"type": "string"}, "text": {"type": "string"}, "recommendation": {"type": "string"},
              "options": {"type": "array", "items": {"type": "string"}}, "depends_on": {"type": "array", "items": {"type": "string"}}},
              "required": ["id", "text"], "additionalProperties": False}}}, "required": ["questions"], "additionalProperties": False})
    async def ask_questions(args):
        try:
            saved = await asyncio.to_thread(emit, "questions", args)
            return result({**saved, "instruction": "Questions saved. End this turn and wait for explicit user answers."})
        except RuntimeError as exc:
            return result(str(exc), True)

    @tool("resolve_questions", "Associate clear answers with exact quotes from the current user message. Cannot approve a design.",
          {"type": "object", "properties": {"answers": {"type": "array", "items": {"type": "object", "properties": {
              "id": {"type": "string"}, "quote": {"type": "string"}}, "required": ["id", "quote"], "additionalProperties": False}}},
           "required": ["answers"], "additionalProperties": False})
    async def resolve_questions(args):
        try:
            return result(await asyncio.to_thread(emit, "answers", args))
        except RuntimeError as exc:
            return result(str(exc), True)

    @tool("bmad_script", "Run an installed BMAD config/render/memlog script with separate argv elements.", {"script": str, "args": list[str]})
    async def bmad_script(args):
        try:
            return result(await asyncio.to_thread(run_script, workspace, args["script"], args["args"]))
        except (ValueError, RuntimeError, OSError) as exc:
            return result(str(exc), True)

    async def guard(inputs, tool_use_id, context):
        name, values = inputs.get("tool_name", ""), inputs.get("tool_input", {})
        allowed = tool_path_allowed(workspace, snapshot, name, values)
        if allowed and name == "Skill":
            await asyncio.to_thread(emit, "activity", {"tool": "Skill", "skill": values.get("skill", "")})
        if not allowed:
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "Design can read its repository snapshot, write artifacts, invoke installed Skills and Cairn tools only. Use ask_questions for user decisions."}}
        return {}

    argv = executable_argv(worker, ["claude"])
    if len(argv) != 1:
        raise RuntimeError("Design SDK requires worker.command to name a native Claude executable")
    system = (workspace / ".claude/skills/cairn-design/SKILL.md").read_text(encoding="utf-8")
    options = ClaudeAgentOptions(cwd=workspace, cli_path=argv[0], env=worker.env,
        model=worker.model or worker.env.get("ANTHROPIC_MODEL"),
        system_prompt={"type": "preset", "preset": "claude_code", "append": system},
        setting_sources=["project"], strict_mcp_config=True, permission_mode="bypassPermissions",
        tools=["Read", "Glob", "Grep", "Write", "Edit", "Skill", "WebSearch", "WebFetch"],
        allowed_tools=["Read", "Glob", "Grep", "Write", "Edit", "Skill", "WebSearch", "WebFetch",
                       "mcp__cairn__save_draft", "mcp__cairn__ask_questions", "mcp__cairn__resolve_questions", "mcp__cairn__bmad_script"],
        resume=draft["session"], session_id=None if draft["session"] else session,
        include_partial_messages=True, hooks={"PreToolUse": [HookMatcher(hooks=[guard])]},
        mcp_servers={"cairn": create_sdk_mcp_server("cairn", tools=[save_draft, ask_questions, resolve_questions, bmad_script])},
        stderr=lambda line: append_log(directory / "stderr.log", line))

    class OwnedTransport(SubprocessCLITransport):
        job = None

        async def connect(self):
            try:
                await super().connect()
                process = self._process
                if os.name == "nt":
                    from cairn.dispatcher.runtime.windows_job import WindowsJob
                    self.job = WindowsJob()
                    self.job.assign_and_resume(process.pid)
                dispatcher.call(pid, f"runs/{run['id']}/heartbeat", {"token": run["token"], "pid": process.pid,
                    "process_identity": process_identity(process.pid), "session": session,
                    "streams": {"stdout": str(directory / "stdout.log"), "stderr": str(directory / "stderr.log")}})
            except BaseException:
                await self.close()
                raise

        async def close(self):
            try:
                await super().close()
            finally:
                if self.job:
                    self.job.close()
                    self.job = None

    context = {"draft": draft, "snapshot": str(snapshot), "workspace": str(workspace),
               "controls": [{"id": h.id, "content": h.content, "control": h.control.model_dump()} for h in project.hints if h.control],
               "conversation": [m for m in state["messages"] if m["kind"] in ("user", "text", "questions")],
               "native_work_schema": PlannedWork.model_json_schema()}
    (workspace / "context.json").write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")
    user = next((m for m in state["messages"] if m["id"] == intent.work.result["message_id"]), None)
    if user is None:
        raise RuntimeError("Design input is missing from persisted conversation")
    content = user["data"]["content"]
    prompt = (content if content.startswith("/") else "/cairn-design\n" + content)
    prompt += "\nRead context.json for persisted answers, decisions and the current draft. Save meaningful changes via Cairn tools."
    append_log(directory / "stdout.log", "Starting resumable Claude Code design session " + session)
    finished = None
    transport = OwnedTransport(prompt=None, options=options)
    async with ClaudeSDKClient(options=options, transport=transport) as client:
        await asyncio.to_thread(emit, "session", {"session": session, "baseline": baseline})
        info = await client.get_server_info() or {}
        await asyncio.to_thread(emit, "commands", {"commands": info.get("commands", [])})
        await client.query(prompt)

        owner = asyncio.current_task()
        async def watch():
            while not cancellation.is_cancelled:
                await asyncio.sleep(.2)
            try:
                await asyncio.wait_for(client.interrupt(), 5)
            finally:
                await asyncio.sleep(10)
                if finished is None:
                    owner.cancel()
        watcher = asyncio.create_task(watch())
        streamed = {}
        active_text_block = None
        text_message = 0
        try:
            async for message in client.receive_response():
                append_log(directory / "stdout.log", json.dumps(asdict(message) if is_dataclass(message) else str(message), ensure_ascii=False, default=str))
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    commands = message.data.get("slash_commands", [])
                    if commands:
                        await asyncio.to_thread(emit, "commands", {"commands": [{"name": n} if isinstance(n, str) else n for n in commands]})
                elif isinstance(message, StreamEvent):
                    event = message.event
                    if event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                        block = str(event.get("index", 0))
                        streamed[block] = streamed.get(block, "") + event["delta"]["text"]
                        await asyncio.to_thread(emit, "text", {"block": run["id"] + ":" + str(text_message) + ":" + block, "text": event["delta"]["text"]})
                    elif event.get("type") == "message_start":
                        streamed.clear()
                        active_text_block = None
                        text_message += 1
                    elif event.get("type") == "content_block_start" and event.get("content_block", {}).get("type") == "text":
                        active_text_block = str(event.get("index", 0))
                        streamed.setdefault(active_text_block, "")
                elif isinstance(message, AssistantMessage):
                    text_blocks = [block for block in message.content if isinstance(block, TextBlock)]
                    indices = list(streamed)
                    for index, block in enumerate(text_blocks):
                        if block.text in streamed.values():
                            continue
                        # SDK AssistantMessage contains a completed block, not the stream's thinking/tool indices.
                        stream_index = active_text_block if len(text_blocks) == 1 else indices[index] if index < len(indices) else None
                        if stream_index is not None:
                            key = run["id"] + ":" + str(text_message) + ":" + stream_index
                            streamed[stream_index] = block.text
                            await asyncio.to_thread(emit, "text", {"block": key, "text": block.text, "mode": "snapshot"})
                        else:
                            await asyncio.to_thread(emit, "text", {"block": run["id"] + ":" + str(sequence), "text": block.text})
                elif isinstance(message, ResultMessage):
                    finished = message
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            await asyncio.to_thread(export_artifacts, workspace / "artifacts", repository)
    if finished is None or finished.is_error:
        raise RuntimeError("Claude design turn failed: " + (getattr(finished, "result", None) or "missing result"))
    return {"kind": "design", "summary": "Design turn saved; awaiting user input or candidate review"}, session


def append_log(path: Path, text):
    with path.open("a", encoding="utf-8") as log:
        log.write(text + "\n")
