import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import LocalConfig, WorkerConfig
from cairn.dispatcher.development import DevelopmentDispatcher
from cairn.server import db, development_store as store
from cairn.server.development_models import WorkPackage
from test_development_api import create, http, claim, WORKER, OPERATOR
from test_development_runtime import Client


@pytest.mark.parametrize("review", [False, True])
def test_context_keeps_full_current_evidence_without_inlining_unrelated_history(http, tmp_path, review):
    project_id, planner = create(http, tmp_path)
    old_hint = http.post(f"/projects/{project_id}/development/controls", headers=OPERATOR,
                         json={"content": "Withdrawn advice"}).json()
    new_hint = http.post(f"/projects/{project_id}/development/controls", headers=OPERATOR,
                         json={"content": "Pending correction", "replaces": old_hint["id"]}).json()
    large_result = {"output": {"summary": "Historical evidence " * 10000}, "source_run_id": "old-run"}
    with db.get_conn() as conn:
        old = WorkPackage(key="old", state="verified", revision=1, scope="Original ownership",
                          acceptance=["Original acceptance"], result=large_result)
        old_id = store.add_intent(conn, project_id, "Historical delivery", old)
        current_id = store.add_intent(conn, project_id, "Review current candidate",
            WorkPackage(role="architect", purpose="plan", subject=old_id, revision=1)) if review else planner
    client = Client(http)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == current_id)
    dispatcher = DevelopmentDispatcher(SimpleNamespace(client=client,
        config=SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")))))
    worker = WorkerConfig(name="strong", type="claudecode", tier="strong", roles=[intent.work.role],
                          max_running=1, priority=0)
    path = dispatcher.context(project, intent, client.development(project_id).data,
                              tmp_path / "context", {"id": "new-run"}, tmp_path, worker)
    context = json.loads(path.read_text(encoding="utf-8"))
    index = json.loads((path.parent / "work-index.json").read_text(encoding="utf-8"))
    assert context["recent_work"][old_id]["state"] == "verified"
    assert "result" not in context["recent_work"][old_id]
    assert "scope" not in context["recent_work"][old_id]
    assert index[old_id] == old.model_dump(mode="json")
    assert context["work_package"]["id"] == current_id
    assert context["work_package"]["scope"] == intent.work.scope
    assert context["current_controls"] == []
    assert context["control_status"][old_hint["id"]]["state"] == "replaced"
    assert context["control_status"][new_hint["id"]]["state"] == "received"
    assert context["control_status"][new_hint["id"]]["replaces"] == old_hint["id"]
    assert "content" not in context["control_status"][old_hint["id"]]
    if review:
        assert context["subject"]["work"] == index[old_id]
    else:
        assert len(path.read_bytes()) < len(json.dumps(large_result)) // 10


@pytest.mark.parametrize("current_revision", [1, 2])
def test_attempt_history_survives_project_run_list_pagination(http, tmp_path, current_revision):
    project_id, planner = create(http, tmp_path)
    previous = claim(http, project_id, planner).json()
    http.post(f"/projects/{project_id}/development/runs/{previous['id']}/finish", headers=WORKER,
        json={"token": previous["token"], "outcome": "cancelled", "detail": "Preserved diagnosis",
              "result": {"next_step": "Repair the targeted check"}}).raise_for_status()
    client = Client(http)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == planner)
    intent.work.revision = current_revision
    intent.work.result["validation_feedback"] = {"run_id": previous["id"], "revision": 1,
        "error": "Prior validation error", "output": {"summary": "Prior candidate"}}
    dispatcher = DevelopmentDispatcher(SimpleNamespace(client=client,
        config=SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")))))
    saved_progress = tmp_path / ".cairn" / "workspaces" / project_id / "runs" / previous["id"] / "progress.json"
    saved_progress.parent.mkdir(parents=True)
    saved_progress.write_text('{"next_step":"Continue existing edits"}', encoding="utf-8")
    state = client.development(project_id).data
    state["runs"] = []  # Other packages' runs have displaced this task from the project window.
    worker = WorkerConfig(name="strong", type="claudecode", tier="strong", roles=["planner"], max_running=1, priority=0)
    path = dispatcher.context(project, intent, state, tmp_path / "retry", {"id": "new-run"}, tmp_path, worker)
    context = json.loads(path.read_text(encoding="utf-8"))
    history = json.loads((path.parent / "attempt-history.json").read_text(encoding="utf-8"))
    assert history[0]["run_id"] == previous["id"]
    assert history[0]["revision"] == 1
    assert history[0]["result"]["next_step"] == "Repair the targeted check"
    assert history[0]["detail"] == "Preserved diagnosis"
    assert context["resume_progress_path"] == str(saved_progress)
    assert context["previous_attempt"]["run_id"] == previous["id"]
    assert "token" not in history[0]
    if current_revision == 1:
        assert context["retry_feedback"]["error"] == "Prior validation error"
    else:
        assert context["retry_feedback"] is None


@pytest.mark.parametrize("same_design", [True, False])
def test_authoritative_design_is_not_duplicated_and_distinct_goal_survives(http, tmp_path, same_design):
    project_id, planner = create(http, tmp_path)
    approved = "Accepted requirement " * 3000
    goal = approved if same_design else "Original project goal"
    with db.get_conn() as conn:
        config = store.project(conn, project_id)
        config.design = approved
        store.save_project(conn, project_id, config)
        conn.execute("UPDATE facts SET description=? WHERE project_id=? AND id='goal'", (goal, project_id))
    client = Client(http)
    project = client.get_project(project_id)
    intent = next(i for i in project.intents if i.id == planner)
    dispatcher = DevelopmentDispatcher(SimpleNamespace(client=client,
        config=SimpleNamespace(local=LocalConfig(workspace_root=str(tmp_path / "data")))))
    worker = WorkerConfig(name="strong", type="claudecode", tier="strong", roles=["planner"], max_running=1, priority=0)
    path = dispatcher.context(project, intent, client.development(project_id).data,
                              tmp_path / "context", {"id":"new"}, tmp_path, worker)
    context = json.loads(path.read_text(encoding="utf-8"))
    assert "design" not in context["project"]["development"]
    assert context["goal"] == goal
    assert context["accepted_design"] == ({"ref":"goal"} if same_design else {"text":approved})
    assert path.read_text(encoding="utf-8").count(approved) == 1
