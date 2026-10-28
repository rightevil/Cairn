"""Independent review with artifact-only writes and bounded declared checks."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import uuid

from cairn.dispatcher.design_session import append_log
from cairn.dispatcher.runtime.sdk_transport import owned_transport


class ReviewChecks:
    def __init__(self, repository, snapshot, commands, directory, cancellation):
        self.repository, self.snapshot, self.directory, self.cancellation = repository, snapshot, directory, cancellation
        self.commands = list(dict.fromkeys(tuple(argv) for argv in commands))
        self.results = {}

    def run(self, index):
        if not 0 <= index < len(self.commands):
            raise ValueError("Unknown declared check index")
        if index in self.results:
            return {**self.results[index], "reused": True}
        passed, evidence = self.repository.checks(self.snapshot, [list(self.commands[index])],
            self.directory / f"review-check-{index}", self.cancellation)
        result = {"passed": passed, "command": self.commands[index], "reused": False,
                  "evidence": [e.model_dump(mode="json") for e in evidence]}
        self.results[index] = result
        return result


def review_tool_allowed(snapshot, reports, readable, name, values):
    if name not in ("Read", "Glob", "Grep", "Write", "Edit"):
        return False
    path = Path(values.get("file_path") or values.get("path") or snapshot)
    path = (snapshot / path).resolve() if not path.is_absolute() else path.resolve()
    if name in ("Write", "Edit"):
        return path.is_relative_to(reports.resolve()) and path != reports.resolve()
    return path.is_relative_to(snapshot.resolve()) or path.is_relative_to(reports.resolve()) or path in readable


async def run_review(dispatcher, project, worker, run, directory, snapshot, prompt, context_path, cancellation, checks=None):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, ResultMessage, tool, create_sdk_mcp_server
    from cairn.dispatcher.development import executable_argv
    session = str(uuid.uuid4())
    reports = directory / "reports"
    reports.mkdir(exist_ok=True)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    readable = {p.resolve() for p in directory.iterdir() if p.name in (
        "context.json", "work-index.json", "attempt-history.json", "checkpoint.json", "system-prompt.md")}
    readable.update(Path(e["path"]).resolve() for e in context.get("raw_evidence", []))
    frozen = (context.get("subject") or {}).get("work", {}).get("result", {}).get("design_snapshot", {})
    root = Path(project.project.development.repository) / ".cairn"
    from cairn.server.design_materials import confined
    for item in frozen.get("prepared_materials", []):
        path = confined(root, item["archive"])
        import hashlib
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise RuntimeError("Frozen review material changed: " + item["target"])
        readable.add(path)

    async def guard(inputs, tool_use_id, hook_context):
        if checks is not None and inputs.get("tool_name") == "mcp__cairn_review__run_check":
            return {}
        if not review_tool_allowed(snapshot, reports, readable, inputs.get("tool_name"), inputs.get("tool_input", {})):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Frozen review: read only the supplied snapshot/context/evidence; write reports and patch proposals only under " + str(reports) + ". No shell, subagents, source edits, copying or publication."}}
        return {}

    argv = executable_argv(worker, ["claude"])
    if len(argv) % 2 != 1 or any(arg not in ("--fallback-model", "--model", "--effort") for arg in argv[1::2]):
        raise RuntimeError("Controlled review requires a native Claude executable with only model/effort options")
    model_options = dict(zip(argv[1::2], argv[2::2]))
    system = ("You independently review a frozen Cairn design. Read the entire frozen design_snapshot, including all "
              "documents and prepared_materials, in the context. Examine requirement coverage, dependencies, edge cases "
              "and verifiable facts. Report concrete findings and optional patch proposals; do not apply them, rewrite "
              "approved user decisions, move source materials or request manual copies. A suggested product/scope/acceptance "
              "change must be identified as needing user decision. Future tests are planned checks, not existing evidence. "
              "Use Chinese summaries and return exactly the supplied DevelopmentOutcome JSON with kind=review and an "
              "honest verdict. You may write your report/proposals under " + str(reports) + ".")
    servers = {}
    allowed = ["Read", "Glob", "Grep", "Write", "Edit"]
    if checks is not None:
        lock = asyncio.Lock()
        @tool("run_check", "Run a declared check once on the fixed candidate; repeated calls reuse its recorded result", {"index": int})
        async def run_check(values):
            async with lock:
                try:
                    result = await asyncio.to_thread(checks.run, values["index"])
                except ValueError as exc:
                    return {"isError":True, "content":[{"type":"text","text":str(exc)}]}
                readable.update(Path(e["path"]).resolve() for e in result["evidence"])
                return {"content":[{"type":"text","text":json.dumps(result, ensure_ascii=False)}]}
        servers["cairn_review"] = create_sdk_mcp_server(name="cairn_review", version="1", tools=[run_check])
        allowed.append("mcp__cairn_review__run_check")
        system = ("Independently review this exact Cairn implementation snapshot using the approved design, effective user "
            "controls, full base..head diff and raw evidence. Source and acceptance cannot be changed. Write findings or "
            "patch proposals only under " + str(reports) + ". Use run_check with the zero-based declared check index: "
            + json.dumps(checks.commands) + ". Each check runs once; repeats return the same evidence. No shell or arbitrary "
            "execution, stress loops, extra agents or ad-hoc business edits. Investigate a concrete hypothesis and its "
            "release condition; once a decisive blocker is established, return request_changes without unrelated checks. "
            "An unrelated timing flake should be documented with its source and uncertainty, not chased by hundreds of "
            "full runs or silently fixed by loosening acceptance. Required independent acceptance remains in force: the "
            "dispatcher runs the declared checks on approval. Never claim a check ran if its evidence is absent. Missing "
            "checks or uncertain design require explicit findings. Return the supplied DevelopmentOutcome JSON in Chinese.")
    options = ClaudeAgentOptions(cwd=snapshot, cli_path=argv[0], env=worker.env,
        model=model_options.get("--model", worker.model or worker.env.get("ANTHROPIC_MODEL")), session_id=session,
        fallback_model=model_options.get("--fallback-model"), effort=model_options.get("--effort"),
        system_prompt=system, setting_sources=[], strict_mcp_config=True, mcp_servers=servers,
        tools=["Read", "Glob", "Grep", "Write", "Edit"], allowed_tools=allowed,
        permission_mode="bypassPermissions", include_partial_messages=True,
        hooks={"PreToolUse": [HookMatcher(hooks=[guard])]},
        stderr=lambda line: append_log(directory / "stderr.log", line))
    append_log(directory / "stderr.log", "")
    transport = owned_transport(dispatcher, project.project.id, run, directory, options, session)
    async with ClaudeSDKClient(options=options, transport=transport) as client:
        async def watch():
            while not cancellation.is_cancelled:
                await asyncio.sleep(.2)
            await client.interrupt()
        watcher = asyncio.create_task(watch())
        final = None
        try:
            await client.query(prompt)
            async for message in client.receive_response():
                append_log(directory / "stdout.log", json.dumps(asdict(message), ensure_ascii=False, default=str))
                if isinstance(message, ResultMessage):
                    final = message
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
    if final is not None and final.is_error:
        raise RuntimeError("Independent review request failed: " + str(final.result or getattr(final, "errors", None) or "unknown"))
    if final is None or cancellation.is_cancelled:
        raise RuntimeError("Independent design review did not complete successfully")
    return final.result or "", session
