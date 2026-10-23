from pathlib import Path
import sys

import pytest

from cairn.dispatcher.runtime import git_workspace as module
from cairn.dispatcher.runtime.git_workspace import GitWorkspace, GitError, IntegrationWait, git
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server.development_models import DevelopmentProject, WorkPackage
from cairn.server import db
from test_development_api import OPERATOR, WORKER, claim, create, http
from test_development_runtime import repository


def candidate(repository, tmp_path, checks=None):
    config = DevelopmentProject(repository=str(repository), design="Add a feature", delivery="local")
    manager = GitWorkspace(config, tmp_path / "data", "project")
    work = WorkPackage(revision=1, checks=checks or [[sys.executable, "-c",
        "from pathlib import Path; assert Path('feature.txt').read_text() == 'feature'"]])
    owned = manager.feature("i001", work)
    (Path(owned["worktree"]) / "feature.txt").write_text("feature", encoding="utf-8")
    head = manager.commit_implementation(Path(owned["worktree"]), "i001")
    work.branch, work.base_commit, work.head_commit = owned["branch"], owned["base"], head
    work.result = {"review": {"run_id": "independent-review", "commit": head, "base": owned["base"], "summary": "Approved"}}
    return manager, work


def test_local_merge_without_remote_has_evidence_and_recovers_without_remerging(repository, tmp_path, monkeypatch):
    manager, work = candidate(repository, tmp_path)
    original = module.command

    def no_network(argv, cwd, **kwargs):
        assert argv[0] != "gh"
        assert not (argv[0] == "git" and any(a in ("fetch", "push", "ls-remote") for a in argv))
        return original(argv, cwd, **kwargs)

    monkeypatch.setattr(module, "command", no_network)
    result, evidence = manager.integrate("i001", work, tmp_path / "first", TaskCancellation())
    assert result["checks_passed"] and result["pr_url"] is None
    assert git(repository, "show", "-s", "--format=%P", result["merge_commit"]).split() == [work.base_commit, work.head_commit]
    assert any(Path(e.path).name == "local-merge.json" for e in evidence)
    assert git(repository, "status", "--porcelain") == ""
    again = GitWorkspace(manager.config, tmp_path / "data", "project")
    recovered, _ = again.integrate("i001", work, tmp_path / "retry", TaskCancellation())
    assert recovered["merge_commit"] == result["merge_commit"]
    assert recovered["checks_passed"]
    assert git(repository, "rev-list", "--count", "--merges", "main") == "1"


def test_local_merge_recovers_crash_before_receipt_is_saved(repository, tmp_path, monkeypatch):
    manager, work = candidate(repository, tmp_path)
    save = manager.save_record

    def crash(intent, record):
        if record.get("local_merge", {}).get("merge_commit"):
            raise OSError("simulated receipt write interruption")
        save(intent, record)

    monkeypatch.setattr(manager, "save_record", crash)
    with pytest.raises(OSError, match="interruption"):
        manager.integrate("i001", work, tmp_path / "crash", TaskCancellation())
    merged = git(repository, "rev-parse", "HEAD")
    again = GitWorkspace(manager.config, tmp_path / "data", "project")
    result, _ = again.integrate("i001", work, tmp_path / "retry", TaskCancellation())
    assert result["checks_passed"] and result["merge_commit"] == merged
    assert git(repository, "rev-list", "--count", "--merges", "main") == "1"


def test_failed_post_merge_check_retries_checks_without_duplicate_merge(repository, tmp_path):
    flag = tmp_path / "fail-check"
    flag.touch()
    manager, work = candidate(repository, tmp_path, [[sys.executable, "-c",
        f"from pathlib import Path; raise SystemExit(9 if Path({str(flag)!r}).exists() else 0)"]])
    result, evidence = manager.integrate("i001", work, tmp_path / "failed", TaskCancellation())
    assert not result["checks_passed"] and evidence
    flag.unlink()
    result2, _ = manager.integrate("i001", work, tmp_path / "retry", TaskCancellation())
    assert result2["checks_passed"] and result2["merge_commit"] == result["merge_commit"]
    assert git(repository, "rev-list", "--count", "--merges", "main") == "1"


def test_recorded_merge_removed_from_main_is_not_silently_replayed(repository, tmp_path):
    manager, work = candidate(repository, tmp_path)
    manager.integrate("i001", work, tmp_path / "first", TaskCancellation())
    git(repository, "switch", "-c", "retain-merged-history")
    git(repository, "branch", "-f", "main", work.base_commit)
    git(repository, "switch", "main")
    with pytest.raises(IntegrationWait, match="Recorded local merge is absent"):
        manager.integrate("i001", work, tmp_path / "retry", TaskCancellation())
    assert git(repository, "rev-parse", "HEAD") == work.base_commit


def test_noop_local_delivery_checks_exact_reviewed_baseline(repository, tmp_path):
    manager, work = candidate(repository, tmp_path, [["git", "diff", "--check"]])
    work.result["review"]["commit"] = work.base_commit
    result, evidence = manager.integrate("i001", work, tmp_path / "noop", TaskCancellation())
    assert result["checks_passed"] and result["merge_commit"] == work.base_commit and evidence
    assert git(repository, "rev-list", "--count", "--merges", "main") == "0"


@pytest.mark.parametrize("condition", ["dirty", "wrong_branch", "cancelled", "base_advanced", "feature_advanced"])
def test_local_integration_preserves_unreviewed_or_user_changes(repository, tmp_path, condition):
    manager, work = candidate(repository, tmp_path)
    cancellation = TaskCancellation()
    if condition == "dirty":
        (repository / "user.txt").write_text("keep")
    elif condition == "wrong_branch":
        git(repository, "switch", "-c", "user-branch")
    elif condition == "cancelled":
        cancellation.cancel("stop")
    elif condition == "base_advanced":
        (repository / "user.txt").write_text("user change")
        git(repository, "add", ".")
        git(repository, "commit", "-m", "User advanced main")
    else:
        path = Path(manager.record("i001")["worktree"])
        (path / "unreviewed.txt").write_text("unreviewed")
        manager.commit_implementation(path, "i001")
    before = git(repository, "rev-parse", "HEAD")
    if condition == "base_advanced":
        result, _ = manager.integrate("i001", work, tmp_path / "run", cancellation)
        assert result["needs_rebase"]
    else:
        with pytest.raises((IntegrationWait, GitError)):
            manager.integrate("i001", work, tmp_path / "run", cancellation)
    assert git(repository, "rev-parse", "HEAD") == before
    assert git(repository, "rev-list", "--count", "--merges", "main") == "0"
    if condition == "dirty":
        assert (repository / "user.txt").read_text() == "keep"


def test_delivery_mode_is_explicit_operator_only_and_cannot_change_mid_merge(http, tmp_path):
    project, planner = create(http, tmp_path)
    url = f"/projects/{project}/development/delivery"
    assert http.put(url, json={"delivery": "local"}).status_code == 401
    assert http.put(url, headers=WORKER, json={"delivery": "local"}).status_code == 401
    assert http.put(url, headers=OPERATOR, json={"delivery": "automatic"}).status_code == 422
    response = http.put(url, headers=OPERATOR, json={"delivery": "local"})
    assert response.status_code == 200
    assert response.json()["delivery"] == "local" and response.json()["revision"] == 1
    assert http.put(url, headers=OPERATOR, json={"delivery": "local"}).status_code == 200
    run = claim(http, project, planner).json()
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM development_events WHERE kind='delivery_configured'").fetchone()[0] == 1
        conn.execute("UPDATE runs SET role='integration' WHERE id=?", (run["id"],))
    assert http.put(url, headers=OPERATOR, json={"delivery": "github"}).status_code == 409


def test_integration_claims_serialize_across_projects_sharing_a_repository(http, tmp_path):
    from cairn.server import development_store as store
    first, first_intent = create(http, tmp_path)
    second, second_intent = create(http, tmp_path)
    for project, intent in [(first, first_intent), (second, second_intent)]:
        with db.get_conn() as conn:
            work = store.work(conn, project, intent)
            work.role, work.state = "implementer", "approved"
            work.result = {"review": {"run_id": "review", "commit": "1" * 40, "base": "2" * 40}}
            store.save_work(conn, project, intent, work)
    def integrate(project, intent):
        return http.post(f"/projects/{project}/development/intents/{intent}/claim", headers=WORKER,
            json={"worker": "integrator", "role": "implementer", "revision": 1, "host": "local", "phase": "integration"})
    assert integrate(first, first_intent).status_code == 200
    response = integrate(second, second_intent)
    assert response.status_code == 409 and "repository" in response.text
