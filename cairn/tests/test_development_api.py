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
        "development": {"repository": str(tmp_path), "design": "Stay within the agreed application scope"},
    })
    assert response.status_code == 201, response.text
    data = response.json()
    return data["project"]["id"], data["intents"][0]["id"]


def claim(http, project, intent, worker="writer", role="planner", revision=1):
    return http.post(f"/projects/{project}/development/intents/{intent}/claim", headers=WORKER,
                     json={"worker": worker, "role": role, "revision": revision, "host": "local-test"})


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
