from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from pydantic import TypeAdapter
import pytest
import yaml

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime import git_workspace as workspace_module
from cairn.dispatcher.runtime.git_workspace import GitWorkspace, GitError, IntegrationWait, git
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server.development_models import DevelopmentProject, WorkPackage
from cairn.server.models import ProjectDetail, ProjectSummary
from test_development_api import OPERATOR, WORKER, http


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "开发 repository"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@localhost")
    (repo / "README.md").write_text("Project\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Initial project")
    return repo


def test_owned_worktrees_keep_user_files_and_isolate_parallel_features(repository, tmp_path):
    (repository / "user-file.txt").write_text("keep this", encoding="utf-8")
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    first = manager.feature("i001", WorkPackage(revision=1))
    second = manager.feature("i002", WorkPackage(revision=1))
    (Path(first["worktree"]) / "feature.txt").write_text("first", encoding="utf-8")
    assert not (Path(second["worktree"]) / "feature.txt").exists()
    manager.commit_implementation(Path(first["worktree"]), "i001")
    assert (repository / "user-file.txt").read_text() == "keep this"
    assert git(repository, "branch", "--show-current") == "main"
    with pytest.raises(GitError, match="owned feature worktree"):
        manager.commit_implementation(repository, "i001")


def test_checks_record_real_failure_instead_of_accepting_worker_claim(repository, tmp_path):
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    path = manager.snapshot("review")
    passed, evidence = manager.checks(path, [[sys.executable, "-c", "raise SystemExit(7)"]], tmp_path / "artifacts", TaskCancellation())
    assert passed is False and evidence[0].exit_code == 7
    assert Path(evidence[0].path).is_file()


def test_wrapper_refuses_to_commit_if_worker_switches_its_owned_branch(repository, tmp_path):
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    owned = manager.feature("i001", WorkPackage(revision=1))
    path = Path(owned["worktree"])
    git(path, "switch", "-c", "unexpected")
    (path / "README.md").write_text("keep these uncommitted changes")
    before = git(path, "rev-parse", "HEAD")
    with pytest.raises(GitError, match="changed the owned feature branch"):
        manager.commit_implementation(path, "i001")
    assert git(path, "rev-parse", "HEAD") == before
    assert (path / "README.md").read_text() == "keep these uncommitted changes"


def test_successful_check_cannot_change_the_code_it_verifies(repository, tmp_path):
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    path = manager.snapshot("review")
    passed, evidence = manager.checks(path, [[sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('changed')"]], tmp_path / "artifacts", TaskCancellation())
    assert passed is False and evidence[0].exit_code == 0


def test_integration_sync_fast_forwards_both_sides_and_preserves_divergence(repository, tmp_path):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repository), str(bare)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(bare))
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    (repository / "local.txt").write_text("local")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Local baseline")
    manager.base = git(repository, "rev-parse", "HEAD")
    manager.sync_integration(lambda: None)
    assert git(bare, "rev-parse", "main") == manager.base
    remote_checkout = tmp_path / "remote-checkout"
    git(bare, "worktree", "add", str(remote_checkout), "main")

    def commit_remote(name):
        (remote_checkout / name).write_text("remote")
        git(remote_checkout, "add", ".")
        git(remote_checkout, "-c", "user.name=Remote", "-c", "user.email=remote@localhost", "commit", "-m", name)

    commit_remote("remote.txt")
    manager.sync_integration(lambda: None)
    assert (repository / "remote.txt").read_text() == "remote"
    (repository / "divergent-local.txt").write_text("keep local")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Divergent local")
    manager.base = git(repository, "rev-parse", "HEAD")
    commit_remote("divergent-remote.txt")
    with pytest.raises(IntegrationWait, match="diverged"):
        manager.sync_integration(lambda: None)
    assert (repository / "divergent-local.txt").read_text() == "keep local"


def test_already_present_feature_is_checked_without_creating_an_empty_pr(repository, tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repository), str(bare)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(bare))
    original = workspace_module.git

    def url(repo, *args, **kwargs):
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/example/project.git"
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "git", url)
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application", checks=[["git", "diff", "--check"]]), tmp_path / "data", "proj_001")
    value = WorkPackage(revision=1, result={"review": {"commit": manager.base, "base": manager.base, "summary": "Already implemented"}})
    result, evidence = manager.integrate("i001", value, tmp_path / "run-no-op", TaskCancellation())
    assert result["already_present"] and result["checks_passed"] and evidence


class Client:
    def __init__(self, http):
        self.http = http

    def close(self):
        pass

    def list_projects(self):
        return TypeAdapter(list[ProjectSummary]).validate_python(self.http.get("/projects").json())

    def get_project(self, project):
        return ProjectDetail.model_validate(self.http.get(f"/projects/{project}").json())

    def development(self, project, action="", payload=None):
        url = f"/projects/{project}/development" + ("/" + action if action else "")
        response = self.http.get(url, headers=WORKER) if payload is None else self.http.post(url, headers=WORKER, json=payload)
        return ApiResult(response.status_code, response.json(), response.text)


def test_development_failure_log_shows_reason_and_artifact_path_without_credentials(http, tmp_path, monkeypatch, caplog):
    from cairn.dispatcher import development
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from test_development_api import create, claim
    project_id, intent_id = create(http, tmp_path)
    client = Client(http)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == intent_id)
    run = claim(http, project_id, intent_id).json()
    loop = SimpleNamespace(client=client, config=SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")),
                           runtime=SimpleNamespace(interval=1)))
    dispatcher = development.DevelopmentDispatcher(loop)
    role = WorkerConfig(name="strong", type="claudecode", roles=["planner"], tier="strong", max_running=1,
                        priority=0, env={"ANTHROPIC_AUTH_TOKEN": "private-test-key"})

    def failed_repository(*args):
        raise RuntimeError("Native CLI failed; credential=private-test-key")

    monkeypatch.setattr(development, "GitWorkspace", failed_repository)
    assert dispatcher.execute(project, intent, role, run, TaskCancellation()) == "failed"
    assert "Native CLI failed" in caplog.text and run["id"] in caplog.text and "logs=" in caplog.text
    assert "private-test-key" not in caplog.text and "[redacted]" in caplog.text


class GitHubTransport:
    """Only GitHub HTTP/CLI is simulated; all Git refs, pushes, merges and checks are real."""
    def __init__(self, bare, directory, original):
        self.bare, self.directory, self.original = bare, directory, original
        self.pr = None
        self.creates, self.merges = 0, 0

    def __call__(self, argv, cwd, **kwargs):
        if argv[0] != "gh":
            return self.original(argv, cwd, **kwargs)
        command = argv[4]
        if command == "list":
            payload = [self.pr] if self.pr else []
        elif command == "create":
            self.creates += 1
            branch = argv[argv.index("--head") + 1]
            head = git(self.bare, "rev-parse", branch)
            self.pr = {"url": "https://github.com/example/project/pull/1", "state": "OPEN", "headRefOid": head,
                       "baseRefName": "main", "baseRefOid": git(self.bare, "rev-parse", "main"),
                       "mergeCommit": None, "statusCheckRollup": [{"conclusion": "SUCCESS"}], "reviewDecision": "APPROVED"}
            return subprocess.CompletedProcess(argv, 0, self.pr["url"] + "\n", "")
        elif command == "view":
            payload = self.pr
        elif command == "merge":
            assert argv[argv.index("--match-head-commit") + 1] == self.pr["headRefOid"]
            self.merges += 1
            server = self.directory / "github-merge"
            git(self.bare, "worktree", "add", str(server), "main")
            git(server, "-c", "user.name=GitHub", "-c", "user.email=github@localhost", "merge", "--no-ff", "--no-edit", self.pr["headRefOid"])
            self.pr["state"] = "MERGED"
            self.pr["mergeCommit"] = {"oid": git(server, "rev-parse", "HEAD")}
            payload = {}
        else:
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")


def test_real_subprocess_git_and_review_pipeline_reaches_verified_completion(http, repository, tmp_path, monkeypatch):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repository), str(bare)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(bare))
    original_command, original_git = workspace_module.command, workspace_module.git
    transport = GitHubTransport(bare, tmp_path, original_command)
    monkeypatch.setattr(workspace_module, "command", transport)

    def network_url(repo, *args, **kwargs):
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/example/project.git"
        return original_git(repo, *args, **kwargs)

    monkeypatch.setattr(workspace_module, "git", network_url)
    fake = tmp_path / "agent.py"
    fake.write_text('''import json,os,sys
from pathlib import Path
c=json.loads(Path(os.environ["CAIRN_CONTEXT_PATH"]).read_text(encoding="utf-8"))
w=c["work_package"]
assert "CAIRN_OPERATOR_TOKEN" not in os.environ
assert "CAIRN_WORKER_TOKEN" not in os.environ
if w["role"]=="planner":
    verified=any(x["role"]=="implementer" and x["state"]=="verified" for x in c["recent_work"].values())
    if verified:
        result={"kind":"complete","summary":"The agreed feature is merged and verified","project_complete":True}
    else:
        result={"kind":"plan","summary":"Add the requested feature","work":[{"key":"feature","description":"Add feature.txt","scope":"One feature file","acceptance":["feature.txt contains implemented"]}]}
elif w["role"]=="implementer":
    Path("feature.txt").write_text("implemented",encoding="utf-8")
    result={"kind":"implemented","summary":"Feature implemented"}
else:
    if w["purpose"]=="implementation":
        assert c["raw_evidence"]
        assert Path("feature.txt").read_text()=="implemented"
    result={"kind":"review","verdict":"approve","summary":"Checked the independent evidence and design"}
print(json.dumps({"type":"result","session_id":"session-"+os.environ["CAIRN_RUN_ID"],"result":json.dumps(result)}))
''', encoding="utf-8")
    config = {
        "server": "in-process", "runtime": {"execution": "local", "worker_healthcheck": "disabled", "interval": 1,
            "max_workers": 3, "max_project_workers": 3, "max_running_projects": 1, "healthcheck_timeout": 5, "prompt_group": "development"},
        "local": {"workspace_root": str(tmp_path / "data"), "completed_action": "keep"},
        "tasks": {"bootstrap": {"timeout": 10, "conclude_timeout": 2}, "reason": {"timeout": 10},
                  "explore": {"timeout": 10, "conclude_timeout": 2}, "development_timeout": 20},
        "workers": [
            {"name": "strong", "type": "claudecode", "roles": ["planner", "architect"], "tier": "strong", "command": [sys.executable, str(fake)], "max_running": 1, "priority": 0},
            {"name": "standard", "type": "claudecode", "roles": ["implementer", "reviewer"], "command": [sys.executable, str(fake)], "max_running": 2, "priority": 0},
        ],
    }
    path = tmp_path / "dispatch.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    project = http.post("/projects", headers=OPERATOR, json={"title": "project", "origin": "existing repository",
        "goal": "Add the requested feature", "development": {"repository": str(repository), "design": "Only add feature.txt",
            "checks": [[sys.executable, "-c", "from pathlib import Path; assert Path('feature.txt').read_text()=='implemented'"]]}}).json()["project"]["id"]
    loop = DispatcherLoop(path)
    loop.client.close()
    loop.client = Client(http)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            loop._reap_futures()
            summaries = loop.client.list_projects()
            loop._dispatch_available(summaries)
            current = loop.client.get_project(project)
            if current.project.status == "completed":
                break
            time.sleep(0.03)
        else:
            state = loop.client.development(project).data
            pytest.fail(json.dumps({"project": current.model_dump(mode="json"), "state": state}, indent=2))
    finally:
        loop.close()
    assert (repository / "feature.txt").read_text() == "implemented"
    assert git(repository, "branch", "--show-current") == "main"
    assert transport.creates == transport.merges == 1
    implementation = next(i for i in current.intents if i.work.role == "implementer")
    assert implementation.work.state == "verified"
    assert implementation.work.merge_commit
    assert implementation.work.pr_url.endswith("/pull/1")
    assert current.project.development.state == "completed"
    # A lost merge receipt must reconcile the existing PR, not create or merge it again.
    manager = GitWorkspace(current.project.development, tmp_path / "data", project)
    reconciled, evidence = manager.integrate(implementation.id, implementation.work, tmp_path / "reconcile-run", TaskCancellation())
    assert reconciled["checks_passed"] and reconciled["merge_commit"] == implementation.work.merge_commit
    assert evidence and transport.creates == transport.merges == 1
