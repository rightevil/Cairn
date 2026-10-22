from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.server.development_models import DevelopmentProject, Evidence, WorkPackage


class GitError(RuntimeError):
    pass


class IntegrationWait(RuntimeError):
    pass


def command(argv, cwd: Path, *, check=True, timeout=120):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=timeout,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if check and result.returncode:
        detail = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", result.stderr)
        raise GitError(f"{argv[0]} {argv[1]} failed ({result.returncode}): {detail.strip()}")
    return result


def git(repository: Path, *args, check=True):
    return command(["git", "-C", str(repository), *args], repository, check=check).stdout.strip()


def write_artifact(directory: Path, name: str, content: str, kind="log", **metadata) -> Evidence:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return Evidence(kind=kind, path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **metadata)


class GitWorkspace:
    def __init__(self, config: DevelopmentProject, directory: Path, project_id: str):
        self.config = config
        self.repository = Path(config.repository).resolve()
        if not self.repository.is_dir():
            raise GitError("Configured repository does not exist on this host")
        top = Path(git(self.repository, "rev-parse", "--show-toplevel")).resolve()
        if top != self.repository:
            raise GitError("Configure the repository root, not a subdirectory")
        self.directory = directory.resolve() / project_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.branch = config.integration_branch or git(self.repository, "symbolic-ref", "--quiet", "--short", "HEAD")
        git(self.repository, "check-ref-format", "--branch", self.branch)
        self.base = git(self.repository, "rev-parse", f"refs/heads/{self.branch}")

    def _record_path(self, intent_id):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", intent_id):
            raise GitError("Invalid internal work package ID")
        return self.directory / (intent_id + ".json")

    def record(self, intent_id):
        path = self._record_path(intent_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save_record(self, intent_id, record):
        path = self._record_path(intent_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(path)

    def feature(self, intent_id: str, value: WorkPackage):
        path = self.directory / "worktrees" / intent_id
        record = self.record(intent_id)
        branch = value.branch or record.get("branch") or f"cairn/{self.directory.name}/{intent_id}"
        git(self.repository, "check-ref-format", "--branch", branch)
        if not path.exists():
            if git(self.repository, "show-ref", "--verify", f"refs/heads/{branch}", check=False):
                git(self.repository, "worktree", "add", str(path), branch)
            else:
                git(self.repository, "worktree", "add", "-b", branch, str(path), self.branch)
        rebasing = Path(git(path, "rev-parse", "--git-path", "rebase-merge")).exists()
        if not rebasing and git(path, "symbolic-ref", "--short", "HEAD") != branch:
            raise GitError("Owned worktree has an unexpected branch; preserving it")
        if Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve() != Path(
            git(self.repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve():
            raise GitError("Worktree belongs to another repository")
        base = value.base_commit or record.get("base") or self.base
        if self.base != base and not git(path, "status", "--porcelain"):
            # Conflicts are deliberately left intact for the implementation worker to resolve.
            command(["git", "-C", str(path), "rebase", "--onto", self.base, base, branch], path, check=False)
            base = self.base
        record.update(branch=branch, base=base, worktree=str(path))
        self.save_record(intent_id, record)
        return record

    def snapshot(self, run_id: str, commit: str | None = None) -> Path:
        commit = commit or self.base
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise GitError("Snapshot requires an exact commit ID")
        path = self.directory / "snapshots" / run_id
        git(self.repository, "worktree", "add", "--detach", str(path), commit)
        return path

    def commit_implementation(self, path: Path, intent_id: str):
        # Never commit from the user's integration checkout, only our owned feature worktree.
        if self.directory / "worktrees" / intent_id != path:
            raise GitError("Cannot commit outside the owned feature worktree")
        if Path(git(path, "rev-parse", "--git-path", "rebase-merge")).exists():
            raise GitError("Worker must finish or resolve the pending rebase before implementation can complete")
        git(path, "add", "--all")
        changed = command(["git", "-C", str(path), "diff", "--cached", "--quiet"], path, check=False)
        if changed.returncode == 1:
            git(path, "-c", "user.name=Cairn", "-c", "user.email=cairn@localhost", "commit", "-m", f"Implement {intent_id}")
        elif changed.returncode:
            raise GitError("Cannot inspect staged changes")
        return git(path, "rev-parse", "HEAD")

    def checks(self, path: Path, commands: list[list[str]], artifact_dir: Path, cancellation):
        if not commands or any(not argv or not all(isinstance(a, str) and a for a in argv) for argv in commands):
            raise GitError("Configure nonempty project or reviewed work-package check commands")
        evidence = []
        passed = True
        for index, argv in enumerate(commands):
            env = {k: v for k, v in os.environ.items() if k.upper() not in ("CAIRN_OPERATOR_TOKEN", "CAIRN_WORKER_TOKEN")}
            process = LocalProcess(argv, str(path), env, timeout_seconds=self.config.check_timeout)
            process.start()
            cancellation.attach_process(process)
            try:
                result = process.communicate(timeout=self.config.check_timeout)
            finally:
                cancellation.attach_process(None)
            evidence.append(write_artifact(artifact_dir, f"check-{index}.log", result.stdout + "\n" + result.stderr,
                                           kind="test", commit=git(path, "rev-parse", "HEAD"),
                                           command=argv, exit_code=result.returncode))
            passed = passed and result.returncode == 0 and not result.cancelled and not result.timed_out
            if cancellation.is_cancelled:
                return False, evidence
        return passed, evidence

    def diff(self, base, head):
        return git(self.repository, "diff", "--binary", base, head, "--")

    def push_feature(self, intent_id, head):
        record = self.record(intent_id)
        branch = record["branch"]
        remote = self.config.remote
        existing = git(self.repository, "ls-remote", remote, f"refs/heads/{branch}")
        remote_head = existing.split()[0] if existing else ""
        if remote_head == head:
            return
        if remote_head and remote_head != record.get("pushed_commit"):
            raise GitError("Remote feature branch has an unrecognized change; refusing to overwrite it")
        git(Path(record["worktree"]), "push", f"--force-with-lease=refs/heads/{branch}:{remote_head}", remote,
            f"{head}:refs/heads/{branch}")
        record["pushed_commit"] = head
        self.save_record(intent_id, record)

    def integrate(self, intent_id, value: WorkPackage, artifact_dir: Path, cancellation):
        review = value.result["review"]
        head, base = review["commit"], review["base"]
        if git(self.repository, "status", "--porcelain"):
            raise IntegrationWait("Integration checkout has uncommitted files; preserving them")
        if git(self.repository, "symbolic-ref", "--short", "HEAD") != self.branch:
            raise IntegrationWait("Integration checkout is on another branch; no automatic checkout performed")
        remote = self.config.remote
        remote_url = git(self.repository, "remote", "get-url", remote)
        match = re.search(r"(?:github\.com[:/])([^/]+/[^/]+?)(?:\.git)?$", remote_url)
        if not match:
            raise IntegrationWait("Automatic PR integration requires a configured GitHub remote")
        repository = match.group(1)
        gh = ["gh", "--repo", repository]
        branch = value.branch
        prs = json.loads(command([*gh, "pr", "list", "--head", branch, "--base", self.branch, "--state", "all",
                                   "--json", "url,state,headRefOid,mergeCommit"], self.repository).stdout)
        existing = next((p for p in prs if p["headRefOid"] == head and p["state"] in ("OPEN", "MERGED")), None)
        if existing is None:
            remote_base = git(self.repository, "ls-remote", remote, f"refs/heads/{self.branch}").split()[0]
            if remote_base != base or self.base != base:
                return {"needs_rebase": True, "detail": "Integration branch advanced; update implementation and review"}, []
            self.push_feature(intent_id, head)
            body = artifact_dir / "pr-body.md"
            body.parent.mkdir(parents=True, exist_ok=True)
            body.write_text(f"Implements {intent_id}.\n\n{value.scope}\n\n"
                            f"Independent review: {review['summary']}\nReviewed head: {head}\nReviewed base: {base}\n", encoding="utf-8")
            url = command([*gh, "pr", "create", "--head", branch, "--base", self.branch,
                           "--title", f"Implement {intent_id}", "--body-file", str(body)], self.repository).stdout.strip()
        else:
            url = existing["url"]
        fields = "url,state,headRefOid,baseRefOid,baseRefName,mergeCommit,mergeStateStatus,statusCheckRollup,reviewDecision"
        pr = json.loads(command([*gh, "pr", "view", url, "--json", fields], self.repository).stdout)
        if pr["headRefOid"] != head or pr["baseRefName"] != self.branch:
            raise GitError("PR differs from the independently reviewed commit or target")
        if pr["state"] != "MERGED":
            if pr["baseRefOid"] != base:
                return {"needs_rebase": True, "pr_url": url, "detail": "PR base advanced; a new review is required"}, []
            for check in pr.get("statusCheckRollup") or []:
                if check.get("conclusion", check.get("state")) not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
                    raise IntegrationWait("Waiting for required PR checks")
            if pr.get("reviewDecision") == "CHANGES_REQUESTED":
                raise IntegrationWait("GitHub review requests changes")
            command([*gh, "pr", "merge", url, "--merge", "--match-head-commit", head], self.repository)
            pr = json.loads(command([*gh, "pr", "view", url, "--json", fields], self.repository).stdout)
            if pr["state"] != "MERGED":
                raise IntegrationWait("PR is queued; waiting for the confirmed merge")
        merged = (pr.get("mergeCommit") or {}).get("oid")
        if not merged:
            raise IntegrationWait("GitHub has not supplied the merge commit")
        git(self.repository, "fetch", remote, f"refs/heads/{self.branch}:refs/remotes/{remote}/{self.branch}")
        git(self.repository, "merge", "--ff-only", f"refs/remotes/{remote}/{self.branch}")
        command(["git", "-C", str(self.repository), "merge-base", "--is-ancestor", merged, "HEAD"], self.repository)
        command(["git", "-C", str(self.repository), "merge-base", "--is-ancestor", head, merged], self.repository)
        actual = git(self.repository, "rev-parse", "HEAD")
        snapshot = self.snapshot("integration-" + artifact_dir.name, actual)
        passed, evidence = self.checks(snapshot, self.config.checks or value.checks, artifact_dir, cancellation)
        return {"merge_commit": merged, "integration_commit": actual, "head_commit": head,
                "pr_url": url, "checks_passed": passed}, evidence
