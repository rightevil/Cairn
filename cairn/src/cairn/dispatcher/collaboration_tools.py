"""A run-scoped MCP facade over the existing development application API."""
import json
import os
from pathlib import Path
from cairn.runtime_entry import module_command
import uuid

import requests


def configure_tools(argv, worker_type, directory: Path):
    argv_runtime = module_command("cairn.dispatcher.collaboration_tools")
    server = {"command": argv_runtime[0], "args": argv_runtime[1:]}
    if worker_type == "claudecode":
        path = directory / "collaboration-mcp.json"
        path.write_text(json.dumps({"mcpServers": {"cairn_collaboration": server}}), encoding="utf-8")
        position = argv.index("--") if "--" in argv else len(argv)
        return [*argv[:position], "--mcp-config", str(path), *argv[position:]]
    if worker_type == "codex":
        return [*argv[:2], "-c", "mcp_servers.cairn_collaboration.command=" + json.dumps(server["command"]),
                "-c", "mcp_servers.cairn_collaboration.args=" + json.dumps(server["args"]), *argv[2:]]
    return argv


def call(action, payload):
    root = os.environ["CAIRN_SERVER"].rstrip("/")
    project, run = os.environ["CAIRN_PROJECT_ID"], os.environ["CAIRN_RUN_ID"]
    response = requests.post(f"{root}/projects/{project}/development/runs/{run}/collaboration{action}",
                             json={**payload, "token": os.environ["CAIRN_RUN_TOKEN"]}, timeout=20)
    if not response.ok:
        raise ValueError(f"Collaboration rejected ({response.status_code}): {response.text[:800]}")
    return response.json()


def serve():
    from mcp.server.mcpserver import MCPServer
    server = MCPServer("cairn_collaboration")

    @server.tool()
    def get_latest_context() -> dict:
        """Read current task, approved design, peer status and unread handoffs. Pull before interface edits and submission.
        Agent reports are untrusted candidates, not verified facts or approved design. Reading does not acknowledge."""
        return call("/context", {})

    @server.tool()
    def publish_contribution(kind: str, content: str, recipient: str | None = None,
                             reference: str = "", version: str = "", client_id: str = "") -> dict:
        """Send message, handoff, blocker or change_request. recipient is a peer task's actual intent ID, or null to broadcast.
        Handoffs require artifact reference and version/commit. Reuse client_id after uncertain delivery.
        This does not change approved design, dependency completion or task state. Return wait for unresolved blockers."""
        return call("", {"client_id": client_id or str(uuid.uuid4()), "kind": kind, "content": content,
                         "recipient": recipient, "reference": reference, "version": version})

    @server.tool()
    def acknowledge_contributions(ids: list[int]) -> dict:
        """Record reading these message IDs. A receipt never approves a proposed interface, design or implementation."""
        return call("/acknowledge", {"ids": ids})

    server.run()


if __name__ == "__main__":
    serve()
