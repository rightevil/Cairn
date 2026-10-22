from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from importlib import resources
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import threading
import time

from cairn.dispatcher.contracts import parse_json_output
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.git_workspace import GitError, GitWorkspace, IntegrationWait, git, write_artifact
from cairn.dispatcher.runtime.process_identity import process_identity, stop_owned_process
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.development_models import DevelopmentOutcome, DevelopmentProject

LOG = logging.getLogger(__name__)


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def executable_argv(worker, argv):
    if worker.command:
        return [*worker.command, *argv[1:]]
    executable = shutil.which(argv[0])
    if os.name == "nt" and executable and Path(executable).suffix.lower() in (".cmd", ".bat"):
        packages = {"claudecode": "@anthropic-ai/claude-code/cli.js", "codex": "@openai/codex/bin/codex.js"}
        entry = Path(executable).parent / "node_modules" / packages.get(worker.type, "missing")
        node = shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry), *argv[1:]]
        raise RuntimeError("Set worker.command to the native executable or node CLI entry; batch shims are not used for prompts")
    return argv


class DevelopmentDispatcher:
    def __init__(self, loop):
        self.loop, self.client, self.config = loop, loop.client, loop.config
        root = self.config.local.workspace_root if self.config.local else None
        self.directory = (Path(root) if root else Path.home() / ".local/share/cairn/workspaces").resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.host = socket.gethostname()
        self.owned = set()

    def call(self, project_id, action, payload=None):
        result = self.client.development(project_id, action, payload)
        if not result.ok:
            raise RuntimeError(f"Development API {action or 'state'} failed ({result.status_code}): {result.text[:500]}")
        return result.data

    def recover(self, project, state):
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=60)
        for run in state["runs"]:
            if run["id"] in self.owned:
                continue
            directory = self.directory / project.project.id / "runs" / run["id"]
            pending = directory / "finish.json"
            if pending.exists() and not (directory / "delivered").exists():
                result = self.client.development(project.project.id, f"runs/{run['id']}/finish", json.loads(pending.read_text(encoding="utf-8")))
                if result.ok or result.status_code == 409:
                    (directory / "delivered").touch()
                if result.ok:
                    continue
            if run["state"] != "running" or run["host"] != self.host:
                continue
            if datetime.fromisoformat(run["heartbeat_at"].replace("Z", "+00:00")) > cutoff:
                continue
            pid, identity = run["pid"], run.get("process_identity")
            if pid and identity:
                stopped = stop_owned_process(pid, identity)
            else:
                # Windows workers are suspended until their PID is durably recorded and job-owned.
                stopped = os.name == "nt"
            if stopped:
                self.client.development(project.project.id, f"runs/{run['id']}/recover", {
                    "host": self.host, "confirmed_stopped": True,
                    "detail": "Expired local run reconciled; owned process stopped, worktree and operations retained",
                })

    def try_dispatch(self, summary):
        if self.config.runtime.execution != "local":
            LOG.error("Development projects require runtime.execution=local project=%s", summary.id)
            return False
        if self.loop._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            return False
        try:
            project = self.client.get_project(summary.id)
            state = self.call(summary.id, "")
            self.recover(project, state)
            self.call(summary.id, "advance", {})
            project = self.client.get_project(summary.id)
            if project.project.status != "active":
                return False
            counts = self.loop._worker_counts()
            candidates = sorted(project.intents, key=lambda i: (i.work.role == "implementer" if i.work else True, i.created_at))
            for intent in candidates:
                value = intent.work
                if value is None:
                    continue
                if value.role == "implementer" and any(i.work and i.work.purpose == "direction" and
                    i.work.state in ("ready", "running", "implemented") for i in project.intents):
                    continue
                integration = value.state == "approved"
                ready = value.state == "ready" or (value.state == "waiting" and value.retry_at and value.retry_at <= _now())
                if not integration and not ready:
                    continue
                if value.retry_at and value.retry_at > _now():
                    continue
                if any(next((i.work.state for i in project.intents if i.id == dep and i.work), None) != "verified"
                       for dep in value.dependencies):
                    continue
                workers = sorted([w for w in self.config.workers if value.role in w.roles and counts.get(w.name, 0) < w.max_running],
                                 key=lambda w: (w.priority, counts.get(w.name, 0)))
                if not integration and not workers:
                    self.loop._log_changed(f"development:{summary.id}:{value.role}:unavailable", logging.WARNING,
                                           "No configured worker available project=%s role=%s", summary.id, value.role)
                    continue
                worker = None if integration else workers[0]
                name = worker.name if worker else "dispatcher.integrator"
                claim = self.client.development(summary.id, f"intents/{intent.id}/claim", {
                    "worker": name, "role": value.role, "revision": value.revision, "host": self.host,
                    "phase": "integration" if integration else "work",
                })
                if not claim.ok:
                    if claim.status_code not in (403, 409):
                        LOG.warning("Development claim failed project=%s status=%s", summary.id, claim.status_code)
                    continue
                run = claim.data
                cancellation = TaskCancellation()
                self.owned.add(run["id"])
                future = self.loop.executor.submit(self.execute, project, intent, worker, run, cancellation)
                self.loop.futures[future] = RunningTask(summary.id, "development." + ("integration" if integration else value.role),
                                                      name, cancellation, intent_id=intent.id)
                self.loop.runtime_project_ids.add(summary.id)
                return True
            return False
        except (RuntimeError, OSError) as exc:
            LOG.warning("Development dispatch deferred project=%s detail=%s", summary.id, exc)
            return False

    def context(self, project, intent, state, directory, run, workspace):
        known = {i.id: i.work.model_dump(mode="json") for i in project.intents if i.work}
        atomic_json(directory / "work-index.json", known)
        subject = next((i for i in project.intents if i.id == intent.work.subject), None)
        source_runs = [r for r in state["runs"] if subject and r["intent_id"] == subject.id]
        if intent.work.purpose == "direction":
            source_runs = [r for r in state["runs"] if r["state"] == "success" and r["role"] in ("implementer", "integration")][:20]
        if subject and not source_runs:
            source_runs = self.call(project.project.id, f"intents/{subject.id}/runs")
        evidence = [e for r in source_runs for e in r["evidence"]]
        for item in evidence:
            path = Path(item["path"]).resolve()
            if not path.is_relative_to(self.directory) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise RuntimeError("Source evidence is missing, outside the data directory, or has changed")
        hints = [{"id": h.id, "content": h.content, "control": h.control.model_dump(mode="json")}
                 for h in project.hints if h.control and h.control.state in ("accepted", "applied", "experiment", "completed")]
        candidate = next((h.content for h in project.hints if h.id == intent.work.subject), None)
        context = {
            "project": project.project.model_dump(mode="json"),
            "goal": next(f.description for f in project.facts if f.id == "goal"),
            "work_package": {"id": intent.id, "description": intent.description, **intent.work.model_dump(mode="json")},
            "current_controls": hints,
            "raw_evidence": evidence,
            "work_index_path": str(directory / "work-index.json"),
            "recent_work": {k: v for k, v in list(known.items())[-50:]},
            "operations": state["operations"],
            "subject": {"id": subject.id, "description": subject.description, "work": subject.work.model_dump(mode="json")} if subject else None,
            "suggestion_candidate": candidate,
            "working_directory": str(workspace),
            "checkpoint_path": str(directory / "checkpoint.json"),
            "operation_cli": "cairn operation reserve --key KEY --kind KIND --request-file FILE; cairn operation record --key KEY --state done --resource-id ID --evidence TEXT",
        }
        atomic_json(directory / "context.json", context)
        return directory / "context.json"

    def execute(self, project, intent, worker, run, cancellation):
        directory = self.directory / project.project.id / "runs" / run["id"]
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / "run.json", {"id": run["id"], "token": run["token"], "intent_id": intent.id})
        checkpoint = directory / "checkpoint.json"
        atomic_json(checkpoint, {"checkpoint_requested": False, "cancel": False})
        heartbeat_stop = threading.Event()
        last_success = [time.monotonic()]
        session = None
        evidence = []
        snapshot = {}
        outcome, detail, output = "failed", "Execution did not finish", {}
        retry_at = None

        def heartbeat_loop():
            while not heartbeat_stop.wait(min(self.config.runtime.interval, 5)):
                response = self.client.development(project.project.id, f"runs/{run['id']}/heartbeat", {"token": run["token"]})
                if response.ok:
                    last_success[0] = time.monotonic()
                    atomic_json(checkpoint, response.data)
                    if response.data.get("cancel"):
                        cancellation.cancel("Project stopped")
                elif response.status_code in (403, 404, 409) or time.monotonic() - last_success[0] > max(15, 3 * self.config.runtime.interval):
                    cancellation.cancel("Run lease lost")
                    return

        thread = threading.Thread(target=heartbeat_loop, daemon=True)
        thread.start()
        try:
            config = project.project.development
            repository = GitWorkspace(config, self.directory, project.project.id)
            if not config.integration_branch:
                self.call(project.project.id, "bind", {"integration_branch": repository.branch})
                config.integration_branch = repository.branch
            if worker is None:
                snapshot, evidence = repository.integrate(intent.id, intent.work, directory, cancellation)
                output = {"kind": "implemented", "summary": "PR integration and checks"}
                outcome = "success" if snapshot.get("checks_passed") or snapshot.get("needs_rebase") else "failed"
                detail = snapshot.get("detail", "Integration checks " + ("passed" if outcome == "success" else "failed"))
            else:
                state = self.call(project.project.id, "")
                if intent.work.role == "implementer":
                    owned = repository.feature(intent.id, intent.work)
                    worktree = Path(owned["worktree"])
                    intent.work.branch, intent.work.base_commit, intent.work.worktree = owned["branch"], owned["base"], str(worktree)
                    self.call(project.project.id, f"runs/{run['id']}/workspace", {"token": run["token"],
                              "branch": owned["branch"], "base_commit": owned["base"], "worktree": str(worktree)})
                else:
                    subject = next((i for i in project.intents if i.id == intent.work.subject), None)
                    commit = subject.work.head_commit if subject and intent.work.purpose == "implementation" else None
                    worktree = repository.snapshot(run["id"], commit)
                context_path = self.context(project, intent, state, directory, run, worktree)
                template = resources.files("cairn.dispatcher.prompts").joinpath("development", "task.md").read_text(encoding="utf-8")
                prompt = template.replace("{role}", intent.work.role).replace("{context_path}", str(context_path)).replace(
                    "{checkpoint_path}", str(checkpoint)).replace("{result_schema}", json.dumps(DevelopmentOutcome.model_json_schema()))
                driver = get_driver(worker.type, "local")
                session = driver.prepare_session()
                argv = executable_argv(worker, driver.build_execute(worker, prompt, session).argv)
                env = {**worker.env, "CAIRN_CONTEXT_PATH": str(context_path), "CAIRN_CHECKPOINT_PATH": str(checkpoint),
                       "CAIRN_SERVER": self.config.server, "CAIRN_PROJECT_ID": project.project.id,
                       "CAIRN_RUN_ID": run["id"], "CAIRN_RUN_TOKEN": run["token"]}
                process = self.loop.container_manager.build_exec_process(str(worktree), env, argv,
                                      timeout_seconds=self.config.tasks.development_timeout)

                def started(process):
                    identity = process_identity(process.pid)
                    atomic_json(directory / "process.json", {"pid": process.pid, "identity": identity})
                    self.call(project.project.id, f"runs/{run['id']}/heartbeat", {"token": run["token"],
                              "pid": process.pid, "process_identity": identity, "session": session})

                process.on_started = started
                process.start()
                cancellation.attach_process(process)
                try:
                    result = process.communicate(timeout=self.config.tasks.development_timeout)
                finally:
                    cancellation.attach_process(None)
                evidence.append(write_artifact(directory, "stdout.log", result.stdout))
                evidence.append(write_artifact(directory, "stderr.log", result.stderr))
                session = driver.extract_session(session, result.stdout, result.stderr)
                if result.cancelled or cancellation.is_cancelled:
                    outcome, detail = "cancelled", cancellation.reason or result.cancel_reason or "cancelled"
                elif result.timed_out:
                    outcome, detail = "failed", "Single CLI execution timed out; checkpoint and worktree retained"
                elif result.returncode:
                    outcome, detail = classify_failure(result.stdout + "\n" + result.stderr)
                else:
                    response = driver.extract_response_text(result.stdout, result.stderr)
                    parsed = DevelopmentOutcome.model_validate(parse_json_output(response))
                    output, outcome, detail = parsed.model_dump(mode="json"), "success", parsed.summary
                    snapshot = {"requested_model": worker.model or worker.env.get({"claudecode": "ANTHROPIC_MODEL", "codex": "CODEX_MODEL", "pi": "PI_MODEL"}.get(worker.type, "")),
                                "worker_type": worker.type, "tier": worker.tier}
                    if intent.work.role == "implementer" and parsed.kind == "implemented":
                        head = repository.commit_implementation(worktree, intent.id)
                        base = intent.work.base_commit
                        passed, checks = repository.checks(worktree, config.checks or intent.work.checks, directory, cancellation)
                        evidence.extend(checks)
                        evidence.append(write_artifact(directory, "changes.diff", repository.diff(base, head), kind="diff", commit=head))
                        snapshot.update(head_commit=head, base_commit=base, branch=intent.work.branch, worktree=str(worktree), checks_passed=passed)
                        if not passed:
                            outcome, detail = "failed", "Implementation checks failed; see raw check logs"
                    elif intent.work.role in ("reviewer", "architect") and intent.work.purpose in ("implementation", "milestone"):
                        subject = next(i for i in project.intents if i.id == intent.work.subject)
                        commands = config.checks or subject.work.checks
                        if not commands and intent.work.purpose == "milestone":
                            commands = []
                            for completed in project.intents:
                                if completed.work and completed.work.role == "implementer" and completed.work.state == "verified":
                                    for argv in completed.work.checks:
                                        if argv not in commands:
                                            commands.append(argv)
                        passed, checks = repository.checks(worktree, commands, directory, cancellation)
                        evidence.extend(checks)
                        snapshot.update(checks_passed=passed, reviewed_commit=subject.work.head_commit,
                                        reviewed_base=subject.work.base_commit, integration_commit=git(worktree, "rev-parse", "HEAD"))
                        if git(worktree, "diff", "HEAD", "--"):
                            outcome, detail = "failed", "Reviewer changed tracked business code in its snapshot"
                        elif not passed:
                            outcome, detail = "failed", "Independent acceptance checks failed"
                    elif git(worktree, "diff", "HEAD", "--"):
                        outcome, detail = "failed", "Planning or review role changed tracked code"
        except IntegrationWait as exc:
            outcome, detail = "blocked", str(exc)
            retry_at = _after(30)
        except Exception as exc:
            outcome, detail = classify_failure(str(exc))
            evidence.append(write_artifact(directory, "failure.log", str(exc)))
        finally:
            heartbeat_stop.set()
            thread.join(timeout=12)
            if cancellation.is_cancelled:
                outcome = "cancelled"
            if outcome == "capacity":
                retry_at = retry_at or capacity_retry_at(detail) or _after(60)
            payload = {"token": run["token"], "outcome": outcome, "session": session,
                       "result": {"output": output, "snapshot": snapshot},
                       "evidence": [e.model_dump(mode="json") for e in evidence],
                       "detail": detail, "checkpoint": detail, "retry_at": retry_at}
            atomic_json(directory / "finish.json", payload)
            result = self.client.development(project.project.id, f"runs/{run['id']}/finish", payload)
            if result.ok or result.status_code == 409:
                (directory / "delivered").touch()
            self.owned.discard(run["id"])
        return "success" if outcome == "success" else "failed"


def classify_failure(detail):
    lowered = detail.lower()
    if any(marker in lowered for marker in ("429", "rate limit", "quota", "capacity", "usage limit")):
        return "capacity", detail[-2000:]
    return "failed", detail[-2000:]


def capacity_retry_at(detail):
    import re
    match = re.search(r'retry[-_ ]after["\s:=]+(\d+)', detail, re.IGNORECASE)
    if match:
        return _after(max(1, int(match.group(1))))
    match = re.search(r'(?:reset_at|retry_at)["\s:=]+([0-9T:.+Z-]+)', detail, re.IGNORECASE)
    if match:
        try:
            value = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
            if value.tzinfo and value > datetime.now(timezone.utc):
                return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _after(seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
