from __future__ import annotations

import json
from importlib import resources

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import DriverResult, SeedSessionDriver
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env


ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    @staticmethod
    def _development_args(worker: WorkerConfig) -> list[str]:
        if not worker.roles:
            return []
        prompt = worker.env.get("CAIRN_SYSTEM_PROMPT_FILE") or str(
            resources.files("cairn.dispatcher.prompts").joinpath("development", "system.md"))
        settings = ["--settings", worker.env['CAIRN_WORKSPACE_SETTINGS']] if worker.env.get('CAIRN_WORKSPACE_SETTINGS') else []
        return [*settings, "--setting-sources", "", "--strict-mcp-config", "--disable-slash-commands",
                "--tools", "Read,Glob,Grep,Write,Edit,Bash,WebSearch,WebFetch", "--system-prompt-file", prompt,
                "--append-subagent-system-prompt-file", prompt, "--system-prompt-snapshot", "off",
                "--output-format", "stream-json", "--verbose"]

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                "-p",
                *self._development_args(worker),
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            "-p",
            *self._development_args(worker),
            "--",
            prompt,
        ]


    @staticmethod
    def _final_result(stdout: str):
        try:
            return json.loads(stdout)
        except ValueError:
            for line in reversed(stdout.splitlines()):
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict) and value.get("type") == "result":
                    return value
        return None

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        value = self._final_result(stdout)
        return value.get("session_id", session) if isinstance(value, dict) else session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        value = self._final_result(stdout)
        if isinstance(value, dict) and value.get("type") == "result":
            if value.get("is_error"):
                raise ValueError("Claude Code run reported an error: " + str(value.get("result", value.get("errors", "unknown"))))
            return value.get("result", "")
        if any(line.startswith('{"type":') for line in stdout.splitlines()):
            raise ValueError("Claude Code stream ended without a final result")
        return stdout
