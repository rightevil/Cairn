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
    passed, evidence = manager.checks(path, [[sys.executable, "-c", "raise SystemExit(7)"],
        [sys.executable, "-c", "from pathlib import Path; Path('should-not-run').touch()"]], tmp_path / "artifacts", TaskCancellation())
    assert passed is False and evidence[0].exit_code == 7
    assert Path(evidence[0].path).is_file()
    assert not (path / "should-not-run").exists()
    reports = json.loads((tmp_path / "artifacts/check-summary.json").read_text(encoding="utf-8"))
    assert len(reports) == 1 and reports[0]["exit_code"] == 7 and not reports[0]["passed"]


def test_new_project_storage_is_in_selected_repository_and_metadata_is_ignored(repository, tmp_path):
    config = DevelopmentProject(repository=str(repository), design="Approved design",
                                workspace_root=str(repository / ".cairn" / "workspaces"))
    manager = GitWorkspace(config, tmp_path / "legacy", "project")
    owned = manager.feature("task", WorkPackage(revision=1))
    path = Path(owned["worktree"])
    assert path.is_relative_to(repository / ".cairn" / "workspaces")
    snapshot = manager.snapshot("review")
    assert snapshot.is_relative_to(repository / ".cairn" / "workspaces")
    (path / "feature.txt").write_text("implementation", encoding="utf-8")
    head = manager.commit_implementation(path, "task")
    assert git(path, "show", f"{head}:feature.txt") == "implementation"
    assert not git(repository, "status", "--porcelain")
    assert not (tmp_path / "legacy" / "project").exists()
    with pytest.raises(ValueError, match="inside its repository"):
        GitWorkspace(config.model_copy(update={"workspace_root":str(tmp_path / "outside")}), tmp_path, "project")


def test_review_reuses_exact_check_result_and_rejects_undeclared_commands(repository, tmp_path):
    from cairn.dispatcher.frozen_review import ReviewChecks
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Inspect candidate"), tmp_path / "data", "project")
    argv = [sys.executable, "-c", "print('verified')"]
    checks = ReviewChecks(manager, repository, [argv, argv], tmp_path / "reports", TaskCancellation())
    first = checks.run(0)
    second = checks.run(0)
    assert first["passed"] and not first["reused"] and second["reused"]
    assert first["evidence"] == second["evidence"]
    assert first["evidence"][0]["command"] == argv
    assert first["evidence"][0]["exit_code"] == 0
    with pytest.raises(ValueError, match="Unknown declared"):
        checks.run(1)


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
    report = json.loads((tmp_path / "artifacts/check-summary.json").read_text(encoding="utf-8"))[0]
    assert report["changed_tracked_files"] == ["README.md"] and not report["passed"]


def test_check_artifacts_can_change_outside_the_tracked_source(repository, tmp_path):
    manager = GitWorkspace(DevelopmentProject(repository=str(repository), design="Build application"), tmp_path / "data", "proj_001")
    path = manager.snapshot("artifact-check")
    artifact_dir = tmp_path / "artifacts"
    passed, evidence = manager.checks(path, [[sys.executable, "-c",
        "import os; from pathlib import Path; Path(os.environ['CAIRN_ARTIFACT_DIR'], 'dynamic.json').write_text('temporary evidence')"]],
        artifact_dir, TaskCancellation())
    assert passed and (artifact_dir / "dynamic.json").is_file()
    assert not git(path, "status", "--porcelain")
    assert any(Path(item.path).name == "check-summary.json" for item in evidence)


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


def test_direction_barrier_is_dispatched_before_older_planning_and_then_releases_writer(http, tmp_path):
    from concurrent.futures import Future
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.server import db, development_store as store
    from test_development_api import create
    from test_development_workflow import advance

    project, planner = create(http, tmp_path)
    with db.get_conn() as conn:
        writer = store.add_intent(conn, project, "Ready implementation", WorkPackage(key="feature", revision=1))
        direction = store.add_intent(conn, project, "Review direction", WorkPackage(
            role="architect", purpose="direction", revision=1))
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.client = Client(http)
    loop.config = SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")),
        runtime=SimpleNamespace(execution="local", max_workers=3, max_project_workers=3, max_running_projects=1),
        workers=[WorkerConfig(name="strong", type="claudecode", roles=["planner", "architect"], tier="strong", max_running=1, priority=0),
                 WorkerConfig(name="standard", type="claudecode", roles=["implementer"], max_running=2, priority=0)])
    loop.futures, loop.runtime_project_ids, loop._log_state = {}, set(), {}
    loop.project_cursor, loop.development = 0, None
    submissions = []

    def submit(*args):
        future = Future()
        submissions.append((args, future))
        return future

    loop.executor = SimpleNamespace(submit=submit)
    loop._dispatch_available(loop.client.list_projects())
    assert [args[2].id for args, _ in submissions] == [direction]
    run = submissions[0][0][4]
    http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER, json={
        "token": run["token"], "outcome": "success", "result": {"output": {
            "kind": "review", "verdict": "approve", "summary": "Direction remains aligned"}}}).raise_for_status()
    advance(http, project)
    submissions[0][1].set_result("success")
    loop._reap_futures()
    loop._dispatch_available(loop.client.list_projects())
    assert {args[2].id for args, _ in submissions[1:]} == {planner, writer}


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


def test_resume_dispatch_reports_rejected_plan_wait_and_restarts_when_due(http, tmp_path, caplog):
    from concurrent.futures import Future
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.server import db, development_store as store
    from test_development_api import create
    from test_development_workflow import finish, plan

    project, planner = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["dependencies"] = ["g15"]
    finish(http, project, planner, candidate, "planner")
    with db.get_conn() as conn:
        value = store.work(conn, project, planner)
        value.checkpoint += " credential=private-test-key"
        store.save_work(conn, project, planner, value)
    http.put(f"/projects/{project}/status", headers=OPERATOR, json={"status": "stopped"}).raise_for_status()
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.client = Client(http)
    loop.config = SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")),
        runtime=SimpleNamespace(execution="local", max_workers=1, max_project_workers=1, max_running_projects=1),
        workers=[WorkerConfig(name="strong", type="claudecode", roles=["planner"], tier="strong",
                              max_running=1, priority=0, env={"ANTHROPIC_AUTH_TOKEN": "private-test-key"})])
    loop.futures, loop.runtime_project_ids, loop._log_state = {}, set(), {}
    loop.project_cursor, loop.development = 0, None
    # Record a submission without launching a model or modifying the user's repository.
    loop.executor = SimpleNamespace(submit=lambda *args: Future())
    caplog.set_level("INFO")
    loop._dispatch_available(loop.client.list_projects())
    assert "no active projects" in caplog.text
    http.put(f"/projects/{project}/status", headers=OPERATOR, json={"status": "active"}).raise_for_status()
    caplog.clear()
    loop._dispatch_available(loop.client.list_projects())
    assert "active projects=" in caplog.text and "state=waiting" in caplog.text
    assert "unknown Cairn work keys/intent IDs: g15" in caplog.text and "retry_at=" in caplog.text
    assert "private-test-key" not in caplog.text and "[redacted]" in caplog.text
    assert not loop.futures
    caplog.clear()
    loop._dispatch_available(loop.client.list_projects())
    assert not any(record.name.startswith("cairn.dispatcher") for record in caplog.records)
    with db.get_conn() as conn:
        value = store.work(conn, project, planner)
        value.retry_at = "2000-01-01T00:00:00Z"
        store.save_work(conn, project, planner, value)
    loop._dispatch_available(loop.client.list_projects())
    assert len(loop.futures) == 1
    assert "Development task started" in caplog.text and f"intent={planner}" in caplog.text


@pytest.mark.parametrize("legacy", [False, True])
def test_rejected_plan_is_supplied_to_retry_then_passes_independent_review(http, tmp_path, legacy):
    from cairn.dispatcher.development import DevelopmentDispatcher
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.server import db, development_store as store
    from test_development_api import create, claim
    from test_development_workflow import advance, finish, plan, works

    project_id, intent_id = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["dependencies"] = ["g15"]
    rejected = finish(http, project_id, intent_id, candidate, "planner")
    value = works(http, project_id)[intent_id]
    assert "output" not in value["result"] and "source_run_id" not in value["result"]
    assert value["result"]["validation_feedback"]["output"] == candidate
    with db.get_conn() as conn:
        stored = store.work(conn, project_id, intent_id)
        stored.retry_at = "2000-01-01T00:00:00Z"
        if legacy:
            stored.result.pop("validation_feedback")
            stored.checkpoint = "Invalid result: Invalid dependencies for api"
        store.save_work(conn, project_id, intent_id, stored)
    response = claim(http, project_id, intent_id)
    assert response.status_code == 200, response.text
    retry = response.json()
    client = Client(http)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == intent_id)
    dispatcher = DevelopmentDispatcher(SimpleNamespace(client=client,
        config=SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")))))
    worker = WorkerConfig(name="strong", type="claudecode", roles=["planner"], tier="strong", max_running=1, priority=0)
    context = json.loads(dispatcher.context(project, intent, client.development(project_id).data,
        tmp_path / "retry", retry, tmp_path, worker).read_text(encoding="utf-8"))
    assert context["retry_feedback"]["run_id"] == rejected["id"]
    assert context["retry_feedback"]["output"] == candidate
    assert "Invalid dependencies for api" in context["retry_feedback"]["error"]
    history = json.loads(Path(context["attempt_history_path"]).read_text(encoding="utf-8"))
    assert history[0]["run_id"] == rejected["id"] and history[0]["result"]["output"] == candidate
    assert "token" not in history[0]
    assert Path(context["progress_path"]).parent == tmp_path / "retry"
    # Model output is simulated here; the real reducer still gates the corrected plan on independent review.
    corrected = json.loads(json.dumps(context["retry_feedback"]["output"]))
    corrected["work"][0]["dependencies"] = []
    http.post(f"/projects/{project_id}/development/runs/{retry['id']}/finish", headers=WORKER,
        json={"token": retry["token"], "outcome": "success", "result": {"output": corrected}}).raise_for_status()
    advance(http, project_id)
    values = works(http, project_id)
    assert values[intent_id]["state"] == "reviewing"
    assert "validation_feedback" not in values[intent_id]["result"]
    assert not any(w["role"] == "implementer" for w in values.values())
    reviewer = next(i for i, w in values.items() if w["role"] == "architect")
    finish(http, project_id, reviewer, {"kind": "review", "verdict": "approve", "summary": "Checked baseline and repaired plan"}, "architect")
    values = works(http, project_id)
    assert len([w for w in values.values() if w["role"] == "implementer"]) == 2
    assert next(w for w in values.values() if w["key"] == "api")["dependencies"] == []
    assert not any(w["key"] == "g15" for w in values.values())


@pytest.mark.parametrize("role,kind,expected", [("implementer", "wait", "success"),
    ("implementer", "needs_design", "success"), ("planner", "wait", "failed")])
def test_dirty_checkpoint_is_allowed_for_implementation_but_not_readonly_roles(http, repository, tmp_path, role, kind, expected):
    from cairn.dispatcher.development import DevelopmentDispatcher
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from test_development_api import claim
    from test_development_workflow import approve_plan, advance, works

    data = http.post("/projects", headers=OPERATOR, json={"title": "checkpoint", "origin": "existing code", "goal": "Build feature",
        "development": {"repository": str(repository), "design": "Add the feature", "checks": [["git", "diff", "--check"]]}}).json()
    project_id, intent_id = data["project"]["id"], data["intents"][0]["id"]
    if role == "implementer":
        approve_plan(http, project_id, intent_id)
        intent_id = next(i for i, w in works(http, project_id).items() if w["key"] == "api")
    run = claim(http, project_id, intent_id, role=role).json()
    fake = tmp_path / "checkpoint-worker.py"
    fake.write_text('''import json, os
from pathlib import Path
c = json.loads(Path(os.environ["CAIRN_CONTEXT_PATH"]).read_text(encoding="utf-8"))
assert Path(c["attempt_history_path"]).is_file()
Path("README.md").write_text("Uncommitted implementation checkpoint", encoding="utf-8")
Path(c["progress_path"]).write_text(json.dumps({"next_step":"Resolve recorded blocker","checks":[]}), encoding="utf-8")
print(json.dumps({"type":"result","result":json.dumps({"kind":"''' + kind + '''","summary":"Saved current work"})}))
''', encoding="utf-8")
    client = Client(http)
    local = LocalConfig(workspace_root=str(tmp_path / "data"))
    loop = SimpleNamespace(client=client, config=SimpleNamespace(server="in-process", local=local, runtime=SimpleNamespace(interval=1),
        tasks=SimpleNamespace(development_timeout=10)), container_manager=LocalBackend(local))
    dispatcher = DevelopmentDispatcher(loop)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == intent_id)
    worker = WorkerConfig(name="checkpoint", type="claudecode", roles=[role], tier="strong" if role == "planner" else "standard",
                          command=[sys.executable, str(fake)], max_running=1, priority=0)
    assert dispatcher.execute(project, intent, worker, run, TaskCancellation()) == expected
    result = client.development(project_id).data["runs"][0]
    assert result["state"] == expected
    assert result["result"]["snapshot"]["cli"]["exit_code"] == 0
    if role == "implementer":
        assert "changed tracked code" not in result["detail"]
        assert any(Path(item["path"]).name == "progress.json" for item in result["evidence"])
        worktree = repository / ".cairn" / "workspaces" / project_id / "worktrees" / intent_id
        assert (worktree / "README.md").read_text() == "Uncommitted implementation checkpoint"
        assert git(worktree, "rev-parse", "HEAD") == git(repository, "rev-parse", "HEAD")
        advance(http, project_id)
        assert works(http, project_id)[intent_id]["state"] == ("waiting" if kind == "wait" else "reviewing")
    else:
        assert "Planning or review role changed tracked code" in result["detail"]


@pytest.mark.parametrize("purpose,verdict,within_scope,dirty,check_fails", [
    ("implementation", "request_changes", True, False, True),
    ("implementation", "reject", True, False, True),
    ("implementation", "needs_design", True, False, True),
    ("implementation", "wait", True, False, True),
    ("implementation", "approve", False, False, True),
    ("implementation", "approve", True, False, False),
    ("implementation", "approve", True, False, True),
    ("implementation", "request_changes", True, True, True),
    ("milestone", "request_changes", True, False, True),
    ("milestone", "approve", True, False, False),
    ("milestone", "approve", True, False, True),
])
def test_only_in_scope_approval_runs_automatic_acceptance_checks(http, repository, tmp_path,
        purpose, verdict, within_scope, dirty, check_fails):
    from cairn.dispatcher.development import DevelopmentDispatcher
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from test_development_api import claim
    from test_development_workflow import approve_plan, advance, finish, works

    fail_flag = tmp_path / "fail-check"
    checks = [[sys.executable, "-c", "import os; from pathlib import Path; "
        "Path(os.environ['CAIRN_ARTIFACT_DIR'], 'acceptance-ran').touch(); "
        f"raise SystemExit(7 if Path({str(fail_flag)!r}).exists() else 0)"]]
    data = http.post("/projects", headers=OPERATOR, json={"title": "review checks", "origin": "existing code",
        "goal": "Verify the feature", "development": {"repository": str(repository),
        "design": "Only change README.md", "checks": checks}}).json()
    project_id, planner = data["project"]["id"], data["intents"][0]["id"]
    client = Client(http)
    local = LocalConfig(workspace_root=str(tmp_path / "data"))
    loop = SimpleNamespace(client=client, config=SimpleNamespace(server="in-process", local=local,
        runtime=SimpleNamespace(interval=1), tasks=SimpleNamespace(development_timeout=10)),
        container_manager=LocalBackend(local))
    dispatcher = DevelopmentDispatcher(loop)
    fake = tmp_path / "review-worker.py"
    fake.write_text('''import json, os
from pathlib import Path
c = json.loads(Path(os.environ["CAIRN_CONTEXT_PATH"]).read_text(encoding="utf-8"))
if c["work_package"]["role"] == "implementer":
    Path("README.md").write_text("Implementation", encoding="utf-8")
    result = {"kind":"implemented","summary":"Implementation ready"}
else:
    if os.environ["DIRTY_REVIEW"] == "1":
        Path("README.md").write_text("Unauthorized review change", encoding="utf-8")
    result = json.loads(os.environ["REVIEW_RESULT"])
print(json.dumps({"type":"result","session_id":os.environ["CAIRN_RUN_ID"],"result":json.dumps(result)}))
''', encoding="utf-8")
    output = {"kind": "wait" if verdict == "wait" else "review", "summary": "Recorded review conclusion",
              "within_scope": within_scope}
    if verdict != "wait":
        output["verdict"] = verdict

    def execute(intent_id, role):
        run = claim(http, project_id, intent_id, role=role).json()
        project = client.get_project(project_id)
        intent = next(i for i in project.intents if i.id == intent_id)
        worker = WorkerConfig(name=role, type="claudecode", roles=[role],
            tier="strong" if role == "architect" else "standard", command=[sys.executable, str(fake)],
            env={"REVIEW_RESULT": json.dumps(output), "DIRTY_REVIEW": str(int(dirty))}, max_running=1, priority=0)
        outcome = dispatcher.execute(project, intent, worker, run, TaskCancellation())
        advance(http, project_id)
        return run, outcome

    if purpose == "implementation":
        approve_plan(http, project_id, planner)
        subject = next(i for i, w in works(http, project_id).items() if w["key"] == "api")
        _, outcome = execute(subject, "implementer")
        assert outcome == "success"
    else:
        subject = planner
        finish(http, project_id, planner, {"kind": "complete", "summary": "Baseline satisfies the goal",
            "project_complete": True}, "planner")
    review = next(i for i, w in works(http, project_id).items() if w["purpose"] == purpose and w["subject"] == subject)
    role = works(http, project_id)[review]["role"]
    if check_fails:
        fail_flag.touch()
    run, outcome = execute(review, role)
    artifact_dir = repository / ".cairn" / "workspaces" / project_id / "runs" / run["id"]
    checks_required = verdict == "approve" and within_scope
    assert (artifact_dir / "acceptance-ran").exists() == checks_required
    assert (artifact_dir / "check-summary.json").exists() == checks_required
    assert outcome == ("failed" if dirty or checks_required and check_fails else "success")
    values = works(http, project_id)
    if checks_required and not check_fails:
        assert values[subject]["state"] == ("approved" if purpose == "implementation" else "verified")
        result = next(r for r in client.development(project_id).data["runs"] if r["id"] == run["id"])
        assert result["result"]["snapshot"]["checks_passed"]
        assert any(e["kind"] == "test" for e in result["evidence"])
    else:
        assert values[subject]["state"] not in ("approved", "verified")
        if not dirty and not checks_required:
            assert values[review]["state"] == ("waiting" if verdict == "wait" else "verified")


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


@pytest.mark.parametrize("delivery", ["github", "local"])
def test_real_subprocess_git_and_review_pipeline_reaches_verified_completion(http, repository, tmp_path, monkeypatch, delivery):
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(repository), str(bare)], check=True, capture_output=True)
    git(repository, "remote", "add", "origin", str(bare))
    original_command, original_git = workspace_module.command, workspace_module.git
    transport = GitHubTransport(bare, tmp_path, original_command)
    def delivery_command(argv, cwd, **kwargs):
        if delivery == "local":
            assert argv[0] != "gh"
            assert not (argv[0] == "git" and any(arg in ("push", "fetch", "ls-remote") for arg in argv))
        return transport(argv, cwd, **kwargs)

    monkeypatch.setattr(workspace_module, "command", delivery_command)

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
assert c["execution"]["role"]==w["role"]
assert c["execution"]["worker_type"]=="claudecode"
assert "--safe-mode" not in sys.argv
assert "--strict-mcp-config" in sys.argv
system=Path(os.environ["CAIRN_SYSTEM_PROMPT_FILE"])
assert system.parent.name==os.environ["CAIRN_RUN_ID"]
assert sys.argv[sys.argv.index("--system-prompt-file")+1]==str(system)
assert "Cairn development worker" in system.read_text(encoding="utf-8")
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
        "goal": "Add the requested feature", "development": {"repository": str(repository), "delivery": delivery, "design": "Only add feature.txt",
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
    assert transport.creates == transport.merges == (1 if delivery == "github" else 0)
    implementation = next(i for i in current.intents if i.work.role == "implementer")
    assert implementation.work.state == "verified"
    assert implementation.work.merge_commit
    assert implementation.work.pr_url.endswith("/pull/1") if delivery == "github" else implementation.work.pr_url is None
    assert current.project.development.state == "completed"
    state = loop.client.development(project).data
    assert any(Path(e["path"]).name == "system-prompt.md" for r in state["runs"] for e in r["evidence"])
    assert all("CAIRN_SYSTEM_PROMPT_FILE" not in w.env for w in loop.config.workers)
    # A lost merge receipt must reconcile the existing PR, not create or merge it again.
    manager = GitWorkspace(current.project.development, tmp_path / "data", project)
    reconciled, evidence = manager.integrate(implementation.id, implementation.work, tmp_path / "reconcile-run", TaskCancellation())
    assert reconciled["checks_passed"] and reconciled["merge_commit"] == implementation.work.merge_commit
    assert evidence and transport.creates == transport.merges == (1 if delivery == "github" else 0)
