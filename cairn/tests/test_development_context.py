import json
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import LocalConfig, WorkerConfig
from cairn.dispatcher.development import DevelopmentDispatcher
from cairn.server import db, development_store as store
from cairn.server.development_models import WorkPackage
from test_development_api import create, http
from test_development_runtime import Client


@pytest.mark.parametrize("review", [False, True])
def test_context_keeps_full_current_evidence_without_inlining_unrelated_history(http, tmp_path, review):
    project_id, planner = create(http, tmp_path)
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
    if review:
        assert context["subject"]["work"] == index[old_id]
    else:
        assert len(path.read_bytes()) < len(json.dumps(large_result)) // 10
