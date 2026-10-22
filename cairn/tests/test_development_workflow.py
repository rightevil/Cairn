import json

from cairn.server import db
from cairn.server import development_store as store
from cairn.server.development_models import WorkPackage
from test_development_api import OPERATOR, WORKER, claim, create, http


def finish(http, project, intent, output, role, **extra):
    run = claim(http, project, intent, role=role).json()
    response = http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
                         json={"token": run["token"], "outcome": "success", "result": {"output": output, **extra}})
    assert response.status_code == 200, response.text
    advance(http, project)
    return run


def advance(http, project):
    response = http.post(f"/projects/{project}/development/advance", headers=WORKER, json={})
    assert response.status_code == 200, response.text


def works(http, project):
    return {w["id"]: w["work"] for w in http.get(f"/projects/{project}").json()["intents"]}


def plan():
    return {"kind": "plan", "summary": "Implement two independent pieces with a dependency",
            "work": [{"key": "api", "description": "Build API", "scope": "API only", "acceptance": ["API test passes"]},
                     {"key": "ui", "description": "Build UI", "scope": "UI only", "acceptance": ["UI test passes"], "dependencies": ["api"]}]}


def approve_plan(http, project, planner):
    finish(http, project, planner, plan(), "planner")
    review = next(i for i, w in works(http, project).items() if w["purpose"] == "plan" and w["role"] == "architect")
    finish(http, project, review, {"kind": "review", "verdict": "approve", "summary": "Matches design and acceptance"}, "architect")


def test_plan_requires_independent_review_before_any_implementation(http, tmp_path):
    project, planner = create(http, tmp_path)
    finish(http, project, planner, plan(), "planner")
    assert not any(w["role"] == "implementer" for w in works(http, project).values())
    reviewer = next(i for i, w in works(http, project).items() if w["role"] == "architect")
    finish(http, project, reviewer, {"kind": "review", "verdict": "approve", "summary": "Interfaces and scope are consistent"}, "architect")
    values = works(http, project)
    api = next(i for i, w in values.items() if w["key"] == "api")
    ui = next(i for i, w in values.items() if w["key"] == "ui")
    assert values[ui]["dependencies"] == [api]
    assert claim(http, project, ui, role="implementer").status_code == 409
    assert claim(http, project, api, role="implementer").status_code == 200


def test_stable_work_key_prevents_duplicate_completed_work(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    with db.get_conn() as conn:
        from cairn.server.development_workflow import install_plan
        from cairn.server.development_models import DevelopmentOutcome
        count = install_plan(conn, project, DevelopmentOutcome.model_validate(plan()), 1)
    assert count == 0
    assert len([w for w in works(http, project).values() if w["role"] == "implementer"]) == 2


def test_cyclic_plan_is_rejected_without_partial_tasks(http, tmp_path):
    project, planner = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["dependencies"] = ["ui"]
    finish(http, project, planner, candidate, "planner")
    values = works(http, project)
    assert len(values) == 1
    assert values[planner]["state"] == "waiting"
    assert "cycle" in values[planner]["checkpoint"]


def test_fake_implementation_without_raw_snapshot_and_checks_is_not_accepted(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    implementation = next(i for i, w in works(http, project).items() if w["key"] == "api")
    finish(http, project, implementation, {"kind": "implemented", "summary": "I promise all tests pass"}, "implementer")
    value = works(http, project)[implementation]
    assert value["state"] == "waiting"
    assert "actual check evidence" in value["checkpoint"]
    assert not any(w["purpose"] == "implementation" and w["role"] == "reviewer" for w in works(http, project).values())


def test_directives_apply_after_checkpoint_and_queue_new_revision(http, tmp_path):
    project, planner = create(http, tmp_path)
    run = claim(http, project, planner).json()
    http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
              json={"kind": "directive", "content": "Prioritize first-use flow"}).raise_for_status()
    advance(http, project)
    assert http.get(f"/projects/{project}").json()["project"]["development"]["revision"] == 1
    response = http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
                         json={"token": run["token"], "outcome": "success", "result": {"output": plan()}})
    assert response.status_code == 409
    advance(http, project)
    data = http.get(f"/projects/{project}").json()
    assert data["project"]["development"]["revision"] == 2
    assert data["hints"][0]["control"]["state"] == "applied"
    assert any(w["work"]["role"] == "planner" and w["work"]["revision"] == 2 and w["work"]["state"] == "ready" for w in data["intents"])


def test_bad_user_suggestion_can_be_rejected_and_never_becomes_a_directive(http, tmp_path):
    project, _ = create(http, tmp_path)
    response = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                         json={"content": "Remove tests to improve quality"}).json()
    reviewer = next(i for i, w in works(http, project).items() if w["purpose"] == "suggestion")
    finish(http, project, reviewer, {"kind": "review", "verdict": "reject", "summary": "Contradicts the defined acceptance checks"}, "architect")
    hint = http.get(f"/projects/{project}").json()["hints"][0]
    assert hint["id"] == response["id"] and hint["control"]["state"] == "rejected"
    assert hint["control"]["kind"] == "suggestion"


def test_wait_is_legal_without_inventing_new_work(http, tmp_path):
    project, planner = create(http, tmp_path)
    finish(http, project, planner, {"kind": "wait", "summary": "Required external service unavailable"}, "planner")
    advance(http, project)
    data = http.get(f"/projects/{project}").json()
    assert data["project"]["development"]["state"] == "waiting"
    assert len(data["intents"]) == 1
    assert data["intents"][0]["work"]["retry_at"] is None


def test_architecture_gap_is_sent_to_strong_role_while_other_work_remains_ready(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    implementation = next(i for i, w in works(http, project).items() if w["key"] == "api")
    finish(http, project, implementation, {"kind": "needs_design", "summary": "Existing database contract conflicts with the planned API"}, "implementer")
    values = works(http, project)
    decision = next(i for i, w in values.items() if w["purpose"] == "arbitration")
    assert values[decision]["role"] == "architect"
    finish(http, project, decision, {"kind": "decision", "summary": "Keep the database contract; add a compatibility handler inside the API"}, "architect")
    assert works(http, project)[implementation]["state"] == "ready"
    assert "compatibility" in works(http, project)[implementation]["result"]["design_decision"]
