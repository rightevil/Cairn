from __future__ import annotations

import json

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, RegexSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


class CodexDriver(RegexSessionDriver):
    type_name = "codex"

    def __init__(self, local: bool = False):
        self.local = local

    def local_binary(self) -> str | None:
        return "codex"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['CODEX_BASE_URL']}/responses",
            headers={
                "Authorization": f"Bearer {env['OPENAI_API_KEY']}",
                "content-type": "application/json",
            },
            json_body={
                "model": env["CODEX_MODEL"],
                "input": [{"role": "user", "content": "ping"}],
                "stream": False,
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['CODEX_BASE_URL']}/responses (model={worker.env['CODEX_MODEL']})"

    def _options(self, worker: WorkerConfig) -> list[str]:
        env = worker.env
        roots = env.get('CAIRN_WRITABLE_ROOTS')
        options = (["--sandbox", "workspace-write", "-c", 'approval_policy="never"',
                    "-c", 'sandbox_workspace_write.writable_roots=' + roots] if roots else ["--dangerously-bypass-approvals-and-sandbox"])
        if worker.roles:
            options.append("--json")
        if env.get("CODEX_MODEL"):
            options.extend(["--model", env["CODEX_MODEL"]])
        if not self.local or env.get("CODEX_BASE_URL"):
            if not env.get("CODEX_BASE_URL") or not env.get("OPENAI_API_KEY"):
                raise ValueError("Codex provider overrides require CODEX_BASE_URL and OPENAI_API_KEY")
            options.extend([
                "-c", 'model_provider="cairn"',
                "-c", 'model_providers.cairn.name="cairn"',
                "-c", 'model_providers.cairn.wire_api="responses"',
                "-c", 'model_providers.cairn.base_url=' + json.dumps(env["CODEX_BASE_URL"]),
                "-c", 'model_providers.cairn.env_key="OPENAI_API_KEY"',
            ])
        if env.get("CODEX_REASONING_EFFORT"):
            options.extend(["-c", "model_reasoning_effort=" + json.dumps(env["CODEX_REASONING_EFFORT"])])
        return options

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        return DriverResult(argv=["codex", "exec", *self._options(worker), "--", prompt])

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return ["codex", "exec", "resume", session, *self._options(worker), "--", prompt]

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started" and event.get("thread_id"):
                return event["thread_id"]
        return super().extract_session(session, stdout, stderr)

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        final = None
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") in ("error", "turn.failed"):
                raise ValueError("Codex run reported an error: " + str(event.get("message", event.get("error", "unknown"))))
            item = event.get("item", {})
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                final = item.get("text")
        return final if isinstance(final, str) else stdout
