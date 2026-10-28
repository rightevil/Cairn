from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from cairn.server import db
from cairn.server.app import app
from cairn.server.development_models import WorkPackage
from cairn.server import development_store as store

OPERATOR = {"x-cairn-operator-token": "test-operator"}
WORKER = {"x-cairn-worker-token": "test-worker"}


@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("CAIRN_OPERATOR_TOKEN", "test-operator")
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "test-worker")
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as client:
        yield client


def create(http, tmp_path):
    response = http.post("/projects", headers=OPERATOR, json={
        "title": "development", "origin": "existing code", "goal": "release a useful application",
        "development": {"repository": str(tmp_path), "design": "Stay within the agreed application scope", "checks": [["git", "diff", "--check"]]},
    })
    assert response.status_code == 201, response.text
    data = response.json()
    return data["project"]["id"], data["intents"][0]["id"]


def claim(http, project, intent, worker="writer", role="planner", revision=1):
    return http.post(f"/projects/{project}/development/intents/{intent}/claim", headers=WORKER,
                     json={"worker": worker, "role": role, "revision": revision, "host": "local-test"})


def test_delete_project_requires_operator_and_stopped_agents_and_preserves_files(http, tmp_path):
    project, intent = create(http, tmp_path)
    marker = tmp_path / "keep.txt"
    marker.write_text("user source", encoding="utf-8")
    run = claim(http, project, intent).json()
    assert http.delete(f"/projects/{project}").status_code == 401
    assert http.delete(f"/projects/{project}", headers=OPERATOR).status_code == 409
    assert http.get(f"/projects/{project}").status_code == 200
    assert http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
                     json={"token": run["token"], "outcome": "cancelled"}).status_code == 200
    with db.get_conn() as conn:
        conn.execute("INSERT INTO operations(project_id,key,run_id,kind,request,updated_at) VALUES(?,?,?,?,?,?)",
                     (project, "operation", run["id"], "local", "{}", "2026-01-01T00:00:00Z"))
        conn.execute("INSERT INTO designs(project_id,data) VALUES(?,?)", (project, "{}"))
        mid = conn.execute("INSERT INTO design_messages(project_id,key,kind,data,created_at) VALUES(?,?,?,?,?)",
                           (project, "message", "user", "{}", "2026-01-01T00:00:00Z")).lastrowid
        conn.execute("INSERT INTO design_inputs(message_id,project_id,intent_id) VALUES(?,?,?)", (mid, project, intent))
        store.event(conn, project, "test", intent, "history")
    assert http.delete(f"/projects/{project}", headers=OPERATOR).status_code == 204
    assert http.get(f"/projects/{project}").status_code == 404
    with db.get_conn() as conn:
        for table in ("facts", "intents", "hints", "runs", "operations", "development_events", "designs", "design_messages", "design_inputs"):
            assert conn.execute(f"SELECT count(*) FROM {table} WHERE project_id=?", (project,)).fetchone()[0] == 0
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    assert marker.read_text(encoding="utf-8") == "user source"
    new_project, _ = create(http, tmp_path)
    assert new_project != project


def test_local_browser_session_is_origin_bound_and_cannot_authorize_workers(http):
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 5000)) as browser:
        assert browser.get("/workspace/projects").status_code == 401
        response = browser.get("/")
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "SameSite=strict" in response.headers["set-cookie"]
        assert "test-operator" not in response.headers["set-cookie"]
        assert browser.get("/workspace/projects").status_code == 200
        assert browser.get("/workspace/projects", headers={"Origin": "https://other.example"}).status_code == 401
        assert browser.get("/workspace/projects", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 401
        assert browser.get("http://127.0.0.1:8766/workspace/projects").status_code == 401
        assert browser.get("/workspace/projects", headers={"Host": "rebound.example"}).status_code == 401
        project, intent = create(http, Path.cwd())
        assert browser.get(f"/projects/{project}/development").status_code == 200
        assert claim(browser, project, intent).status_code == 200  # Explicit worker header remains supported.
        assert browser.post(f"/projects/{project}/development/advance").status_code == 401
    with TestClient(app, base_url="http://rebound.example", client=("127.0.0.1", 5000)) as untrusted:
        assert "set-cookie" not in untrusted.get("/").headers
    with TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.8", 5000)) as remote:
        assert "set-cookie" not in remote.get("/").headers


def test_folder_browser_is_local_read_only_and_repeatable(http, tmp_path):
    folder = tmp_path / "设计项目"
    folder.mkdir()
    (folder / "src").mkdir()
    (folder / "README.md").write_text("not a folder", encoding="utf-8")
    assert http.get("/workspace/folders", headers=OPERATOR).status_code == 403
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as browser:
        browser.get("/")
        for _ in range(3):
            response = browser.get("/workspace/folders", params={"directory": str(folder)})
            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "设计项目"
            assert Path(data["directory"]) == folder.resolve()
            assert [entry["name"] for entry in data["entries"]] == ["src"]
            assert Path(data["parent"]) == tmp_path.resolve()
        assert not (folder / ".git").exists()
        assert browser.post("/workspace/folder").status_code == 410
        assert browser.get("/workspace/folders", params={"directory": "relative"}).status_code == 422
        assert browser.get("/workspace/folders", params={"directory": str(folder) + "\0"}).status_code == 422
        assert browser.get("/workspace/folders", params={"directory": str(folder / "missing")}).status_code == 404
        assert browser.get("/workspace/folders", params={"directory": str(folder / "README.md")}).status_code == 422
        assert browser.get("/workspace/folders", params={"directory": ""}).json()["directory"] is None


def test_folder_browser_reports_access_errors(http, tmp_path, monkeypatch):
    from cairn.server.routers import workspace
    def denied(*args, **kwargs):
        raise PermissionError("access denied")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as browser:
        browser.get("/")
        monkeypatch.setattr(workspace.os, "scandir", denied)
        response = browser.get("/workspace/folders", params={"directory": str(tmp_path)})
        assert response.status_code == 403
        assert "无法访问" in response.json()["detail"]


def test_create_folder_is_local_preserves_existing_files_and_rejects_paths(http, tmp_path):
    body = {"directory": str(tmp_path), "name": "新项目"}
    assert http.post("/workspace/folders", json=body).status_code == 401
    assert http.post("/workspace/folders", headers=OPERATOR, json=body).status_code == 403
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as browser:
        browser.get("/")
        response = browser.post("/workspace/folders", json=body)
        assert response.status_code == 201, response.text
        target = tmp_path / "新项目"
        assert target.is_dir() and list(target.iterdir()) == []
        assert Path(response.json()["directory"]) == target.resolve()
        assert response.json()["entries"] == []
        assert browser.post("/workspace/folders", json=body).status_code == 409
        existing = tmp_path / "existing.txt"
        existing.write_text("Keep existing data", encoding="utf-8")
        assert browser.post("/workspace/folders", json={**body, "name": "existing.txt"}).status_code == 409
        assert existing.read_text(encoding="utf-8") == "Keep existing data"
        for name in (".", "..", "../escape", "sub/name", "sub\\name", "CON", "nul.txt", "bad.", "bad?name", "   "):
            assert browser.post("/workspace/folders", json={**body, "name": name}).status_code == 422
        assert browser.post("/workspace/folders", json={**body, "directory": "relative"}).status_code == 422
        assert browser.post("/workspace/folders", json={**body, "directory": str(tmp_path / "missing")}).status_code == 404


def test_create_folder_reports_permission_error(http, tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("access denied")
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 5000)) as browser:
        browser.get("/")
        monkeypatch.setattr(Path, "mkdir", denied)
        response = browser.post("/workspace/folders", json={"directory": str(tmp_path), "name": "new-project"})
        assert response.status_code == 403
        assert "没有权限" in response.json()["detail"]


def test_workspace_creates_design_first_without_origin_goal_or_planner(http, tmp_path):
    from cairn.server.routers.workspace import git
    directory = tmp_path / "new-project"
    directory.mkdir()
    response = http.post("/workspace/projects", headers=OPERATOR,
                         json={"directory": str(directory), "name": "New project"})
    assert response.status_code == 201, response.text
    payload = response.json()
    project = payload["project"]["id"]
    config = payload["project"]["development"]
    assert config["phase"] == "design" and config["delivery"] == "local"
    assert payload["intents"] == [] and not payload["project"]["bootstrap_enabled"]
    baseline = git(directory, "rev-parse", "HEAD")
    assert baseline in payload["facts"][0]["description"]
    assert git(directory, "status", "--porcelain") == ""
    assert http.post("/workspace/projects", headers=OPERATOR, json={"directory": str(directory)}).status_code == 409
    # A direct user message starts the existing design workflow, without invoking a skill manually.
    turn = http.post(f"/projects/{project}/development/design/messages", headers=OPERATOR,
                     json={"id": "first-message", "content": "Help design the application"})
    assert turn.status_code == 201, turn.text
    intents = http.get(f"/projects/{project}").json()["intents"]
    assert len(intents) == 1 and intents[0]["work"]["purpose"] == "design"


def test_workspace_preserves_existing_code_and_counts_only_verified_implementation(http, tmp_path):
    from cairn.server.routers.workspace import git
    directory = tmp_path / "existing-project"
    directory.mkdir()
    (directory / "source.txt").write_text("Keep this code", encoding="utf-8")
    assert http.post("/workspace/projects", headers=OPERATOR, json={"directory": str(directory)}).status_code == 422
    assert not (directory / ".git").exists()
    git(directory, "init")
    git(directory, "add", "source.txt")
    git(directory, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-m", "test baseline")
    baseline = git(directory, "rev-parse", "HEAD")
    (directory / "source.txt").write_text("Uncommitted work", encoding="utf-8")
    child = directory / "child"
    child.mkdir()
    (child / "keep.txt").write_text("Keep", encoding="utf-8")
    assert http.post("/workspace/projects", headers=OPERATOR, json={"directory": str(child)}).status_code == 422
    response = http.post("/workspace/projects", headers=OPERATOR, json={"directory": str(directory)})
    assert response.status_code == 201, response.text
    project = response.json()["project"]["id"]
    assert git(directory, "rev-parse", "HEAD") == baseline
    assert (directory / "source.txt").read_text() == "Uncommitted work"
    assert http.get(f"/workspace/projects/{project}/files?query=source", headers=OPERATOR).json() == ["source.txt"]
    with db.get_conn() as conn:
        for role, state in [("planner", "verified"), ("architect", "verified"), ("implementer", "merged"),
                            ("implementer", "verified"), ("implementer", "superseded")]:
            store.add_intent(conn, project, "Task", WorkPackage(role=role, state=state, revision=1))
    progress = http.get("/workspace/projects", headers=OPERATOR).json()[0]["progress"]
    assert progress["total"] == 2 and progress["verified"] == 1


def test_development_round_trip_and_legacy_write_cannot_bypass_review(http, tmp_path):
    project, intent = create(http, tmp_path)
    data = http.get(f"/projects/{project}").json()
    assert data["project"]["bootstrap_enabled"] is False
    assert data["project"]["development"]["revision"] == 1
    assert data["intents"][0]["work"]["role"] == "planner"
    assert http.post(f"/projects/{project}/complete", json={"from": ["origin"], "worker": "writer", "description": "done"}).status_code == 409
    assert http.post(f"/projects/{project}/intents/{intent}/conclude", json={"worker": "writer", "description": "done"}).status_code == 409
    assert http.post(f"/projects/{project}/hints", json={"creator": "operator", "content": "【指令】do this"}).status_code == 409


def test_operator_and_worker_authorities_are_distinct(http, tmp_path):
    project, intent = create(http, tmp_path)
    url = f"/projects/{project}/development/controls"
    assert http.post(url, headers=WORKER, json={"content": "【指令】change direction"}).status_code == 401
    assert http.post(url, json={"content": "【指令】change direction"}).status_code == 401
    assert http.post(f"/projects/{project}/development/stop", headers=WORKER).status_code == 401
    assert http.put(f"/projects/{project}/status", json={"status": "stopped"}).status_code == 401
    assert http.delete(f"/projects/{project}").status_code == 401


def test_suggestion_is_pending_independent_assessment_and_explicit_type_wins(http, tmp_path):
    project, _ = create(http, tmp_path)
    response = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                         json={"kind": "suggestion", "content": "A quoted 【指令】 does not grant authority"})
    assert response.status_code == 201
    assert response.json()["control"]["state"] == "received"
    data = http.get(f"/projects/{project}").json()
    assert data["project"]["development"]["revision"] == 1
    assert data["hints"][0]["control"]["kind"] == "suggestion"
    assert data["intents"][-1]["work"]["purpose"] == "suggestion"
    assert data["intents"][-1]["work"]["role"] == "architect"


def test_directive_checkpoints_old_run_without_losing_its_output(http, tmp_path):
    project, intent = create(http, tmp_path)
    run = claim(http, project, intent).json()
    control = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                        json={"content": "【指令】Prioritize the first-use experience"}).json()
    assert control["control"]["state"] == "accepted"
    heartbeat = http.post(f"/projects/{project}/development/runs/{run['id']}/heartbeat", headers=WORKER,
                          json={"token": run["token"], "checkpoint": "saved current plan"}).json()
    assert heartbeat["checkpoint_requested"] is True
    assert heartbeat["cancel"] is False
    result = http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
                       json={"token": run["token"], "outcome": "success", "result": {"old": "plan"}})
    assert result.status_code == 409
    saved = http.get(f"/projects/{project}/development", headers=OPERATOR).json()["runs"][0]
    assert saved["state"] == "superseded" and saved["result"] == {"old": "plan"}
    assert "token_hash" not in saved


def test_atomic_claim_and_run_token_fencing(http, tmp_path):
    project, intent = create(http, tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: claim(http, project, intent, name), ["writer1", "writer2"]))
    assert sorted(r.status_code for r in results) == [200, 409]
    run = next(r.json() for r in results if r.status_code == 200)
    url = f"/projects/{project}/development/runs/{run['id']}/finish"
    assert http.post(url, headers=WORKER, json={"token": "wrong", "outcome": "success"}).status_code == 403
    payload = {"token": run["token"], "outcome": "success", "result": {"plan": "candidate"}}
    assert http.post(url, headers=WORKER, json=payload).status_code == 200
    assert http.post(url, headers=WORKER, json=payload).status_code == 200
    assert http.post(url, headers=WORKER, json={**payload, "result": {"plan": "different"}}).status_code == 409
    assert http.post(f"/projects/{project}/development/runs/{run['id']}/heartbeat", headers=WORKER,
                     json={"token": run["token"]}).status_code == 409


def test_unknown_operation_requires_reconciliation_and_done_never_replays(http, tmp_path):
    project, intent = create(http, tmp_path)
    run = claim(http, project, intent).json()
    url = f"/projects/{project}/development/runs/{run['id']}/operations"
    payload = {"token": run["token"], "key": "scheduler:project-watch", "kind": "create-scheduler",
               "request": {"target": "project-watch"}}
    assert http.post(url, headers=WORKER, json=payload).json()["execute"] is True
    assert http.post(url, headers=WORKER, json=payload).json()["execute"] is False
    assert http.post(url, headers=WORKER, json={**payload, "request": {"target": "other"}}).status_code == 409
    result_url = url + "/scheduler:project-watch"
    assert http.put(result_url, headers=WORKER, json={"token": run["token"], "state": "absent", "evidence": "queried scheduler; not found"}).status_code == 200
    assert http.post(url, headers=WORKER, json=payload).json()["execute"] is True
    assert http.post(url, headers=WORKER, json=payload).json()["execute"] is False
    assert http.put(result_url, headers=WORKER, json={"token": run["token"], "state": "done", "resource_id": "job-123", "evidence": "queried matching active job"}).status_code == 200
    assert http.post(url, headers=WORKER, json=payload).json()["execute"] is False
    assert http.put(result_url, headers=WORKER, json={"token": run["token"], "state": "absent", "evidence": "try replay"}).status_code == 409


def test_immediate_stop_cancels_without_waiting_for_a_reviewer(http, tmp_path):
    project, intent = create(http, tmp_path)
    run = claim(http, project, intent).json()
    assert http.post(f"/projects/{project}/development/stop", headers=OPERATOR).json()["state"] == "stopping"
    heartbeat = http.post(f"/projects/{project}/development/runs/{run['id']}/heartbeat", headers=WORKER,
                          json={"token": run["token"]}).json()
    assert heartbeat["cancel"] is True


def test_scoped_directive_does_not_invalidate_unaffected_feature(http, tmp_path):
    project, _ = create(http, tmp_path)
    with db.get_conn() as conn:
        first = store.add_intent(conn, project, "first", WorkPackage(revision=1))
        second = store.add_intent(conn, project, "second", WorkPackage(revision=1))
    run = claim(http, project, second, role="implementer").json()
    http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
              json={"kind": "directive", "content": "Change first feature", "scope": [first]}).raise_for_status()
    url = f"/projects/{project}/development/runs/{run['id']}/finish"
    assert http.post(url, headers=WORKER, json={"token": run["token"], "outcome": "success"}).status_code == 200


def test_controls_replace_revoke_and_database_restart_preserves_state(http, tmp_path):
    project, _ = create(http, tmp_path)
    first = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                      json={"kind": "directive", "content": "first direction"}).json()
    second = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                       json={"kind": "directive", "content": "second direction", "replaces": first["id"]}).json()
    assert second["control"]["revision"] == 3
    http.post(f"/projects/{project}/development/controls/{second['id']}/revoke", headers=OPERATOR).raise_for_status()
    db._db_path = None
    db.configure(tmp_path / "cairn.db")
    result = http.get(f"/projects/{project}").json()
    assert result["project"]["development"]["requested_revision"] == 4
    assert [h["control"]["state"] for h in result["hints"][:2]] == ["replaced", "revoked"]
    assert result["hints"][-1]["control"]["kind"] == "directive"


def test_export_retains_work_and_control_ids_and_revisions(http, tmp_path):
    import yaml
    project, intent = create(http, tmp_path)
    http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
              json={"content": "Consider a simpler interface"}).raise_for_status()
    result = yaml.safe_load(http.get(f"/projects/{project}/export").text)
    assert result["project"]["development"]["revision"] == 1
    assert result["intents"][0]["id"] == intent
    assert result["intents"][0]["work"]["role"] == "planner"
    assert result["hints"][0]["id"]
    assert result["hints"][0]["control"]["state"] == "received"


def test_stop_then_resume_does_not_revive_the_old_attempt(http, tmp_path):
    project, intent = create(http, tmp_path)
    run = claim(http, project, intent).json()
    http.put(f"/projects/{project}/status", headers=OPERATOR, json={"status": "stopped"}).raise_for_status()
    http.put(f"/projects/{project}/status", headers=OPERATOR, json={"status": "active"}).raise_for_status()
    url = f"/projects/{project}/development/runs/{run['id']}"
    assert http.post(url + "/heartbeat", headers=WORKER, json={"token": run["token"]}).json()["cancel"] is True
    assert http.post(url + "/operations", json={"token": run["token"], "key": "new-job", "kind": "scheduler", "request": {}}).status_code == 409
    result = http.post(url + "/finish", headers=WORKER, json={"token": run["token"], "outcome": "success"})
    assert result.json()["state"] == "cancelled"


def test_live_output_is_separate_from_hash_verified_evidence(http, tmp_path):
    import hashlib
    project, intent = create(http, tmp_path)
    run = claim(http, project, intent).json()
    directory = tmp_path / "runs" / run["id"]
    directory.mkdir(parents=True)
    log = directory / "stdout.log"
    log.write_text("working", encoding="utf-8")
    url = f"/projects/{project}/development/runs/{run['id']}"
    http.post(url + "/heartbeat", headers=WORKER, json={"token": run["token"], "streams": {"stdout": str(log)}}).raise_for_status()
    assert http.get(url + "/stream/stdout").status_code == 401
    assert "not acceptance evidence" in http.get(url + "/stream/stdout", headers=OPERATOR).text
    artifact = {"kind": "log", "path": str(log), "sha256": hashlib.sha256(log.read_bytes()).hexdigest()}
    http.post(url + "/finish", headers=WORKER, json={"token": run["token"], "outcome": "success", "evidence": [artifact]}).raise_for_status()
    assert http.get(url + "/evidence/0", headers=OPERATOR).text == "working"
    log.write_text("tampered", encoding="utf-8")
    assert http.get(url + "/evidence/0", headers=OPERATOR).status_code == 409


def test_declared_shared_resources_serialize_across_projects_until_run_ends(http, tmp_path):
    project, _ = create(http, tmp_path)
    other, _ = create(http, tmp_path)
    with db.get_conn() as conn:
        first = store.add_intent(conn, project, "Use shared database", WorkPackage(revision=1, resources=["database:local-test"]))
        second = store.add_intent(conn, other, "Use same database", WorkPackage(revision=1, resources=["database:local-test"]))
        independent = store.add_intent(conn, other, "Independent feature", WorkPackage(revision=1, resources=["database:separate"]))
    run = claim(http, project, first, role="implementer").json()
    assert claim(http, other, second, role="implementer").status_code == 409
    assert claim(http, other, independent, role="implementer").status_code == 200
    http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
              json={"token": run["token"], "outcome": "cancelled"}).raise_for_status()
    assert claim(http, other, second, role="implementer").status_code == 200


def test_uncertain_action_cannot_be_replayed_while_its_original_owner_is_live(http, tmp_path):
    project, planner = create(http, tmp_path)
    first = claim(http, project, planner).json()
    with db.get_conn() as conn:
        second_intent = store.add_intent(conn, project, "Another feature", WorkPackage(revision=1))
    second = claim(http, project, second_intent, role="implementer").json()
    prefix = f"/projects/{project}/development/runs"
    payload = {"key": "scheduler:watch", "kind": "create", "request": {"name": "watch"}}
    assert http.post(f"{prefix}/{first['id']}/operations", json={**payload, "token": first["token"]}).json()["execute"]
    assert not http.post(f"{prefix}/{second['id']}/operations", json={**payload, "token": second["token"]}).json()["execute"]
    record = {"token": second["token"], "state": "absent", "evidence": "Service has not shown the pending create yet"}
    assert http.put(f"{prefix}/{second['id']}/operations/scheduler:watch", json=record).status_code == 409
    http.post(f"{prefix}/{first['id']}/finish", headers=WORKER, json={"token": first["token"], "outcome": "cancelled"}).raise_for_status()
    assert http.put(f"{prefix}/{second['id']}/operations/scheduler:watch", json=record).status_code == 200


def test_private_files_parallelize_but_dependencies_and_explicit_locks_still_gate(http, tmp_path):
    project, _ = create(http, tmp_path)
    with db.get_conn() as conn:
        a = store.add_intent(conn, project, "A", WorkPackage(revision=1, resources=["anvil.html", "test/shared.mjs"]))
        b = store.add_intent(conn, project, "B", WorkPackage(revision=1, resources=["anvil.html", "test/shared.mjs"]))
        c = store.add_intent(conn, project, "C", WorkPackage(revision=1, dependencies=[a]))
        d = store.add_intent(conn, project, "D", WorkPackage(revision=1, resources=["global:port:8080"]))
        e = store.add_intent(conn, project, "E", WorkPackage(revision=1, resources=["port:8080"]))
    assert claim(http, project, a, role="implementer").status_code == 200
    assert claim(http, project, b, role="implementer").status_code == 200
    assert claim(http, project, c, role="implementer").status_code == 409
    assert claim(http, project, d, role="implementer").status_code == 200
    assert claim(http, project, e, role="implementer").status_code == 409
    state = http.get(f"/projects/{project}/development", headers=OPERATOR).json()
    assert state["scheduling"][c]["status"] == "dependencies"
    assert state["scheduling"][e]["status"] == "resources"
