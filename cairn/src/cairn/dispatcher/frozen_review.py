"""Independent design review with enforced artifact-only writes and no shell tools."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import uuid

from cairn.dispatcher.design_session import append_log
from cairn.dispatcher.runtime.sdk_transport import owned_transport


def review_tool_allowed(snapshot, reports, readable, name, values):
    if name not in ("Read", "Glob", "Grep", "Write", "Edit"):
        return False
    path = Path(values.get("file_path") or values.get("path") or snapshot)
    path = (snapshot / path).resolve() if not path.is_absolute() else path.resolve()
    if name in ("Write", "Edit"):
        return path.is_relative_to(reports.resolve()) and path != reports.resolve()
    return path.is_relative_to(snapshot.resolve()) or path.is_relative_to(reports.resolve()) or path in readable


async def run_review(dispatcher, project, worker, run, directory, snapshot, prompt, context_path, cancellation):
    from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, HookMatcher, ResultMessage
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
        if not review_tool_allowed(snapshot, reports, readable, inputs.get("tool_name"), inputs.get("tool_input", {})):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "Frozen review: read only the supplied snapshot/context/evidence; write reports and patch proposals only under " + str(reports) + ". No shell, subagents, source edits, copying or publication."}}
        return {}

    argv = executable_argv(worker, ["claude"])
    if len(argv) != 1:
        raise RuntimeError("Controlled design review requires a native Claude executable")
    system = ("You independently review a frozen Cairn design. Read the entire frozen design_snapshot, including all "
              "documents and prepared_materials, in the context. Examine requirement coverage, dependencies, edge cases "
              "and verifiable facts. Report concrete findings and optional patch proposals; do not apply them, rewrite "
              "approved user decisions, move source materials or request manual copies. A suggested product/scope/acceptance "
              "change must be identified as needing user decision. Future tests are planned checks, not existing evidence. "
              "Use Chinese summaries and return exactly the supplied DevelopmentOutcome JSON with kind=review and an "
              "honest verdict. You may write your report/proposals under " + str(reports) + ".")
    options = ClaudeAgentOptions(cwd=snapshot, cli_path=argv[0], env=worker.env,
        model=worker.model or worker.env.get("ANTHROPIC_MODEL"), session_id=session,
        system_prompt=system, setting_sources=[], strict_mcp_config=True, mcp_servers={},
        tools=["Read", "Glob", "Grep", "Write", "Edit"], allowed_tools=["Read", "Glob", "Grep", "Write", "Edit"],
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
    if final is None or final.is_error or cancellation.is_cancelled:
        raise RuntimeError("Independent design review did not complete successfully")
    return final.result or "", session
