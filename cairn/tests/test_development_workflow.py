import json
import pytest

from cairn.server import db
from cairn.server import development_store as store
from cairn.server.development_models import WorkPackage
from test_development_api import OPERATOR, WORKER, claim, create, http


def finish(http, project, intent, output, role, revision=1, **extra):
    run = claim(http, project, intent, role=role, revision=revision).json()
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


def test_infrastructure_failures_preserve_work_and_do_not_escalate_design(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    subject = next(i for i,w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        value = store.work(conn, project, subject)
        value.failures = 2
        value.result["saved_progress"] = "continue existing edits"
        value.worktree = str(tmp_path / "owned-worktree")
        store.save_work(conn, project, subject, value)
    for _ in range(4):
        run = claim(http, project, subject, role="implementer").json()
        response = http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
            json={"token":run["token"], "outcome":"infrastructure", "detail":"API Error: 502 getaddrinfo ENOTFOUND gateway.example.org",
                  "retry_at":"2000-01-01T00:00:00Z"})
        assert response.status_code == 200, response.text
        advance(http, project)
        values = works(http, project)
        assert values[subject]["failures"] == 2
        assert values[subject]["result"]["saved_progress"] == "continue existing edits"
        assert values[subject]["worktree"] == str(tmp_path / "owned-worktree")
        assert not any(w["purpose"] == "arbitration" for w in values.values())
    run = claim(http, project, subject, role="implementer").json()
    http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
              json={"token":run["token"], "outcome":"failed", "detail":"Implementation checks failed"})
    advance(http, project)
    assert any(w["purpose"] == "arbitration" for w in works(http, project).values())


def test_direction_change_reuses_interrupted_arbitration(http, tmp_path):
    from cairn.server.development_workflow import all_work, request_design
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    subject = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        request_design(conn, project, subject, store.work(conn, project, subject), "Cold startup prerequisite")
        arbitration, value = next((i, w) for i, w in all_work(conn, project) if w.purpose == "arbitration")
        value.state, value.checkpoint = "waiting", "Project stopped"
        store.save_work(conn, project, arbitration, value)
        request_design(conn, project, subject, store.work(conn, project, subject), "Same prerequisite confirmed by another decision")
    values = works(http, project)
    assert [i for i, w in values.items() if w["purpose"] == "arbitration"] == [arbitration]
    assert values[subject]["result"]["pending_design"] == "Same prerequisite confirmed by another decision"


@pytest.mark.parametrize("running", [False, True])
def test_advance_retires_legacy_duplicate_arbitrations_without_losing_evidence(http, tmp_path, running):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    subject = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        first = store.add_intent(conn, project, "Interrupted arbitration", WorkPackage(
            role="architect", purpose="arbitration", subject=subject, revision=1, state="waiting",
            checkpoint="Saved diagnosis", result={"evidence": "preserved-log"}))
        second = store.add_intent(conn, project, "Duplicate arbitration", WorkPackage(
            role="architect", purpose="arbitration", subject=subject, revision=1))
        newer_revision = store.add_intent(conn, project, "Different control revision", WorkPackage(
            role="architect", purpose="arbitration", subject=subject, revision=2))
    if running:
        assert claim(http, project, second, role="architect").status_code == 200
    advance(http, project)
    values = works(http, project)
    kept, retired = (second, first) if running else (first, second)
    assert values[kept]["state"] == ("running" if running else "waiting")
    assert values[retired]["state"] == "superseded"
    assert values[first]["checkpoint"] == "Saved diagnosis"
    assert values[first]["result"]["evidence"] == "preserved-log"
    assert values[newer_revision]["state"] == "ready"
    assert claim(http, project, retired, role="architect").status_code == 409


def test_cyclic_plan_is_rejected_without_partial_tasks(http, tmp_path):
    project, planner = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["dependencies"] = ["ui"]
    finish(http, project, planner, candidate, "planner")
    values = works(http, project)
    assert len(values) == 1
    assert values[planner]["state"] == "waiting"
    assert "cycle" in values[planner]["checkpoint"]


@pytest.mark.parametrize("dependency,diagnostic", [("g15", "unknown Cairn work keys/intent IDs: g15"),
                                                 ("api", "cannot depend on itself")])
def test_invalid_plan_dependency_explains_how_to_correct_it(http, tmp_path, dependency, diagnostic):
    project, planner = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["dependencies"] = [dependency]
    finish(http, project, planner, candidate, "planner")
    values = works(http, project)
    assert len(values) == 1
    assert values[planner]["state"] == "waiting"
    assert values[planner]["retry_at"]
    assert diagnostic in values[planner]["checkpoint"]


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


def test_checkpointed_package_can_be_replanned_as_independent_deliveries(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    original = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        old = store.work(conn, project, original)
        old.branch, old.worktree = "cairn/original", str(tmp_path / "original")
        old.checkpoint = "Preserved source and evidence; not yet verified"
        store.save_work(conn, project, original, old)
    http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
              json={"kind": "directive", "scope": [original], "content": "Split remaining work using the saved checkpoint"}).raise_for_status()
    advance(http, project)
    values = works(http, project)
    assert values[original]["state"] == "superseded"
    assert values[original]["branch"] == "cairn/original"
    assert values[original]["worktree"] == str(tmp_path / "original")
    assert values[original]["checkpoint"] == "Preserved source and evidence; not yet verified"
    replacement = next(i for i, w in values.items() if w["role"] == "planner" and w["state"] == "ready")
    candidate = {"kind": "plan", "summary": "Core and isolated fixtures in parallel, then native acceptance",
                 "work": [{"key": "api", "description": "Deliver API from checkpoint", "scope": "Reuse preserved API source",
                           "acceptance": ["Targeted API checks pass"]},
                          {"key": "fixtures", "description": "Isolate test data", "scope": "Independent test fixtures only",
                           "acceptance": ["Fixtures are isolated"]},
                          {"key": "native", "description": "Deliver native acceptance coverage", "scope": "Native harness only",
                           "acceptance": ["Native checks pass"], "dependencies": ["api", "fixtures"],
                           "resources": ["desktop:windows-interactive"]}]}
    finish(http, project, replacement, candidate, "planner", revision=2)
    values = works(http, project)
    assert not any(w["key"] == "fixtures" for w in values.values())
    review = next(i for i, w in values.items() if w["subject"] == replacement and w["purpose"] == "plan")
    finish(http, project, review, {"kind": "review", "verdict": "approve", "summary": "Ownership, preserved acceptance and dependencies checked"}, "architect", revision=2)
    values = works(http, project)
    api = next(i for i, w in values.items() if w["key"] == "api" and w["state"] != "superseded")
    fixtures = next(i for i, w in values.items() if w["key"] == "fixtures")
    native = next(i for i, w in values.items() if w["key"] == "native")
    ui = next(i for i, w in values.items() if w["key"] == "ui")
    assert values[ui]["dependencies"] == [api]
    assert values[native]["dependencies"] == [api, fixtures]
    assert claim(http, project, api, role="implementer", revision=2).status_code == 200
    assert claim(http, project, fixtures, role="implementer", revision=2).status_code == 200
    assert claim(http, project, native, role="implementer", revision=2).status_code == 409


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
    assert data["intents"][0]["work"]["retry_at"] is not None


def test_internal_work_wait_has_no_timer_or_repeated_claim_and_wakes_on_change(http, tmp_path):
    from cairn.server.development_workflow import ready_planner
    project, original = create(http, tmp_path)
    approve_plan(http, project, original)
    with db.get_conn() as conn:
        ready_planner(conn, project, "Wait for the existing implementation")
    values = works(http, project)
    planner = next(i for i, w in values.items() if w["role"] == "planner" and w["state"] == "ready")
    implementation = next(i for i, w in values.items() if w["key"] == "api")
    finish(http, project, planner, {"kind": "wait", "wait_for": "work", "wait_on": [implementation],
                                   "summary": "Waiting for the existing implementation"}, "planner")
    for _ in range(5):
        advance(http, project)
        value = works(http, project)[planner]
        assert value["state"] == "waiting" and value["retry_at"] is None
        assert claim(http, project, planner).status_code == 409
    finish(http, project, implementation, {"kind": "needs_design", "summary": "Contract needs a design decision"}, "implementer")
    value = works(http, project)[planner]
    assert value["state"] == "ready" and "wait_on" not in value["result"]
    assert claim(http, project, planner).status_code == 200


@pytest.mark.parametrize("target", ["self", "missing"])
def test_internal_wait_requires_other_existing_work(http, tmp_path, target):
    project, planner = create(http, tmp_path)
    finish(http, project, planner, {"kind": "wait", "wait_for": "work", "wait_on": [planner if target == "self" else "missing"],
                                   "summary": "Invalid dependency"}, "planner")
    value = works(http, project)[planner]
    assert value["retry_at"] and "validation_feedback" in value["result"]
    assert "wait_on" not in value["result"]


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


@pytest.mark.parametrize("reference", ["id", "key"])
def test_arbitration_updates_dependencies_and_blocks_early_claim(http, tmp_path, reference):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    implementation = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        prerequisite = store.add_intent(conn, project, "Cold startup", WorkPackage(key="cold-start", role="implementer", revision=1))
        original = store.work(conn, project, implementation)
        original.branch = "cairn/preserved-api"
        store.save_work(conn, project, implementation, original)
    finish(http, project, implementation, {"kind": "needs_design", "summary": "Needs cold startup first"}, "implementer")
    arbitration = next(i for i, w in works(http, project).items() if w["purpose"] == "arbitration")
    correction = {**plan()["work"][0], "dependencies": [prerequisite if reference == "id" else "cold-start"]}
    finish(http, project, arbitration, {"kind": "decision", "summary": "Wait for the existing cold startup package", "work": [correction]}, "architect")
    value = works(http, project)[implementation]
    assert value["dependencies"] == [prerequisite]
    assert value["branch"] == "cairn/preserved-api"
    assert claim(http, project, implementation, role="implementer").status_code == 409
    with db.get_conn() as conn:
        ready = store.work(conn, project, prerequisite)
        ready.state = "verified"
        store.save_work(conn, project, prerequisite, ready)
    assert claim(http, project, implementation, role="implementer").status_code == 200


@pytest.mark.parametrize("dependency", ["missing", "api", "ui", "superseded"])
def test_arbitration_rejects_invalid_dependency_graph_atomically(http, tmp_path, dependency):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    implementation = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        obsolete = store.add_intent(conn, project, "Obsolete", WorkPackage(key="old", state="superseded", revision=1))
    finish(http, project, implementation, {"kind": "needs_design", "summary": "Needs a prerequisite"}, "implementer")
    arbitration = next(i for i, w in works(http, project).items() if w["purpose"] == "arbitration")
    correction = {**plan()["work"][0], "scope": "Must not be installed", "dependencies": [obsolete if dependency == "superseded" else dependency]}
    finish(http, project, arbitration, {"kind": "decision", "summary": "Invalid prerequisite", "architecture": "Must roll back", "work": [correction]}, "architect")
    values = works(http, project)
    assert values[implementation]["state"] == "reviewing"
    assert values[implementation]["dependencies"] == []
    assert values[implementation]["scope"] == "API only"
    assert values[arbitration]["state"] == "waiting"
    assert "validation_feedback" in values[arbitration]["result"]
    assert http.get(f"/projects/{project}").json()["project"]["development"]["architecture"] == ""


@pytest.mark.parametrize("mode", ["omitted", "null", "empty"])
def test_arbitration_preserves_dependencies_unless_explicitly_replaced(http, tmp_path, mode):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    values = works(http, project)
    api = next(i for i, w in values.items() if w["key"] == "api")
    ui = next(i for i, w in values.items() if w["key"] == "ui")
    with db.get_conn() as conn:
        prerequisite = store.work(conn, project, api)
        prerequisite.state = "verified"
        store.save_work(conn, project, api, prerequisite)
    finish(http, project, ui, {"kind": "needs_design", "summary": "Clarify UI scope"}, "implementer")
    arbitration = next(i for i, w in works(http, project).items() if w["purpose"] == "arbitration")
    correction = {k: v for k, v in plan()["work"][1].items() if k != "dependencies"}
    if mode != "omitted":
        correction["dependencies"] = None if mode == "null" else []
    finish(http, project, arbitration, {"kind": "decision", "summary": "Clarify UI scope", "work": [correction]}, "architect")
    assert works(http, project)[ui]["dependencies"] == ([] if mode == "empty" else [api])


def test_control_received_after_finish_still_fences_unapplied_result(http, tmp_path):
    project, planner = create(http, tmp_path)
    run = claim(http, project, planner).json()
    http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
              json={"token": run["token"], "outcome": "success", "result": {"output": plan()}}).raise_for_status()
    http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
              json={"kind": "directive", "content": "Change the current milestone"}).raise_for_status()
    advance(http, project)
    assert works(http, project)[planner]["state"] == "superseded"
    assert not any(w["role"] == "architect" for w in works(http, project).values())


def test_expired_run_requires_confirmed_stop_and_old_token_stays_fenced(http, tmp_path):
    project, planner = create(http, tmp_path)
    run = claim(http, project, planner).json()
    url = f"/projects/{project}/development/runs/{run['id']}/recover"
    body = {"host": "local-test", "confirmed_stopped": True, "detail": "owned process exited"}
    assert http.post(url, headers=WORKER, json=body).status_code == 409
    with db.get_conn() as conn:
        conn.execute("UPDATE runs SET heartbeat_at='2000-01-01T00:00:00Z' WHERE id=?", (run["id"],))
    assert http.post(url, headers=WORKER, json={**body, "confirmed_stopped": False}).status_code == 409
    assert http.post(url, headers=WORKER, json=body).status_code == 200
    assert http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER,
                     json={"token": run["token"], "outcome": "success", "result": {"output": plan()}}).status_code == 409
    advance(http, project)
    assert works(http, project)[planner]["state"] == "waiting"


def test_completed_feature_requires_explicit_reviewed_revision_to_reimplement(http, tmp_path):
    from cairn.server.development_models import DevelopmentOutcome
    from cairn.server.development_workflow import install_plan
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    old = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        prior = store.work(conn, project, old)
        prior.state = "verified"
        store.save_work(conn, project, old, prior)
        candidate = plan()
        candidate["work"] = [{**candidate["work"][0], "supersedes": old, "description": "Revise the verified API under the new decision"}]
        assert install_plan(conn, project, DevelopmentOutcome.model_validate(candidate), 1) == 1
    values = works(http, project)
    assert values[old]["state"] == "superseded"
    assert len([w for w in values.values() if w["key"] == "api"]) == 2


def test_approved_architecture_is_durable_but_unreviewed_plan_is_not(http, tmp_path):
    project, planner = create(http, tmp_path)
    candidate = {**plan(), "architecture": "Preserve the database contract; expose a compatibility API", "milestone": "First usable API"}
    finish(http, project, planner, candidate, "planner")
    assert http.get(f"/projects/{project}").json()["project"]["development"]["architecture"] == ""
    review = next(i for i, w in works(http, project).items() if w["role"] == "architect")
    finish(http, project, review, {"kind": "review", "verdict": "approve", "summary": "Design fits"}, "architect")
    db._db_path = None
    db.configure(tmp_path / "cairn.db")
    config = http.get(f"/projects/{project}").json()["project"]["development"]
    assert config["architecture"] == candidate["architecture"] and config["design_revision"] == 2
    assert config["milestone"] == candidate["milestone"]
    assert all(w["design_revision"] == 2 for w in works(http, project).values() if w["role"] == "implementer")


def test_live_direction_dispute_waits_for_affected_checkpoint_and_fences_old_review(http, tmp_path):
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    values = works(http, project)
    implementation = next(i for i, w in values.items() if w["key"] == "api")
    unrelated = next(i for i, w in values.items() if w["key"] == "ui")
    run = claim(http, project, implementation, role="implementer").json()
    with db.get_conn() as conn:
        direction = store.add_intent(conn, project, "Direction audit", WorkPackage(role="architect", purpose="direction", revision=1))
    finish(http, project, direction, {"kind": "review", "verdict": "needs_design", "summary": "API conflicts with the design", "affected": [implementation]}, "architect")
    url = f"/projects/{project}/development/runs/{run['id']}"
    assert http.post(url + "/heartbeat", headers=WORKER, json={"token": run["token"]}).json()["checkpoint_requested"] is True
    assert not any(w["purpose"] == "arbitration" for w in works(http, project).values())
    http.post(url + "/finish", headers=WORKER, json={"token": run["token"], "outcome": "success", "result": {"output": {"kind": "implemented", "summary": "Saved checkpoint"}}}).raise_for_status()
    advance(http, project)
    values = works(http, project)
    arbitration = next(i for i, w in values.items() if w["purpose"] == "arbitration")
    assert values[unrelated]["state"] == "ready"
    with db.get_conn() as conn:
        obsolete = store.add_intent(conn, project, "Old review", WorkPackage(role="reviewer", purpose="implementation", subject=implementation, subject_run=run["id"], revision=1))
    finish(http, project, arbitration, {"kind": "decision", "summary": "Use a compatibility endpoint", "architecture": "Compatibility endpoint preserves storage"}, "architect")
    assert works(http, project)[implementation]["state"] == "ready"
    assert works(http, project)[obsolete]["state"] == "superseded"
    assert claim(http, project, obsolete, role="reviewer").status_code == 409


def test_replacement_cannot_create_a_cycle_through_existing_work(http, tmp_path):
    from cairn.server.development_models import DevelopmentOutcome
    from cairn.server.development_workflow import install_plan
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    values = works(http, project)
    api = next(i for i, w in values.items() if w["key"] == "api")
    with db.get_conn() as conn:
        prior = store.work(conn, project, api)
        prior.state = "verified"
        store.save_work(conn, project, api, prior)
        candidate = plan()
        candidate["work"] = [{**candidate["work"][0], "supersedes": api, "dependencies": ["ui"]}]
        with pytest.raises(ValueError, match="cycle"):
            install_plan(conn, project, DevelopmentOutcome.model_validate(candidate), 1)
    assert len(works(http, project)) == 4


def test_invalid_extra_checks_are_not_hidden_by_project_checks(http, tmp_path):
    project, planner = create(http, tmp_path)
    candidate = plan()
    candidate["work"][0]["checks"] = [[]]
    finish(http, project, planner, candidate, "planner")
    assert "runnable" in works(http, project)[planner]["checkpoint"]


def test_user_wait_does_not_busy_retry_and_times_normalize_to_utc(http, tmp_path):
    from cairn.server.development_models import DevelopmentOutcome
    project, planner = create(http, tmp_path)
    finish(http, project, planner, {"kind": "wait", "summary": "Need user product decision", "wait_for": "user"}, "planner")
    assert works(http, project)[planner]["retry_at"] is None
    assert DevelopmentOutcome(kind="wait", summary="Service reset", wake_at="2030-01-01T08:00:00+08:00").wake_at == "2030-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="time zone"):
        DevelopmentOutcome(kind="wait", summary="Ambiguous", wake_at="2030-01-01T08:00:00")


def test_author_session_cannot_approve_its_own_plan(http, tmp_path):
    project, planner = create(http, tmp_path)
    finish(http, project, planner, plan(), "planner")
    reviewer = next(i for i, w in works(http, project).items() if w["role"] == "architect")
    with db.get_conn() as conn:
        conn.execute("UPDATE runs SET session='author-session' WHERE project_id=? AND intent_id=?", (project, planner))
    run = claim(http, project, reviewer, role="architect").json()
    http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER, json={
        "token": run["token"], "session": "author-session", "outcome": "success",
        "result": {"output": {"kind": "review", "verdict": "approve", "summary": "Trust my own work"}},
    }).raise_for_status()
    advance(http, project)
    assert "independent session" in works(http, project)[reviewer]["checkpoint"]
    assert not any(w["role"] == "implementer" for w in works(http, project).values())


def test_repeated_review_disagreement_escalates_instead_of_looping_forever(http, tmp_path):
    from cairn.server.development_workflow import apply_review
    from cairn.server.development_models import DevelopmentOutcome
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    implementation = next(i for i, w in works(http, project).items() if w["key"] == "api")
    with db.get_conn() as conn:
        for index in range(2):
            value = WorkPackage(role="reviewer", purpose="implementation", subject=implementation, revision=1)
            apply_review(conn, project, {"session": None, "intent_id": "review"}, value,
                         DevelopmentOutcome(kind="review", verdict="request_changes", summary="Acceptance still fails; reproduce API test"))
        subject = store.work(conn, project, implementation)
        assert subject.state == "reviewing" and subject.review_rounds == 2
    assert len([w for w in works(http, project).values() if w["purpose"] == "arbitration"]) == 1


def test_useful_suggestion_is_accepted_then_applied_by_a_reviewed_plan(http, tmp_path):
    project, planner = create(http, tmp_path)
    hint = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                     json={"content": "Make API errors actionable for first-time users"}).json()
    reviewer = next(i for i, w in works(http, project).items() if w["purpose"] == "suggestion")
    finish(http, project, reviewer, {"kind": "review", "verdict": "approve", "summary": "Fits the agreed first-use acceptance"}, "architect")
    assert http.get(f"/projects/{project}").json()["hints"][0]["control"]["state"] == "accepted"
    candidate = {**plan(), "addressed_controls": [hint["id"]]}
    finish(http, project, planner, candidate, "planner")
    reviewer = next(i for i, w in works(http, project).items() if w["role"] == "architect" and w["purpose"] == "plan")
    finish(http, project, reviewer, {"kind": "review", "verdict": "approve", "summary": "Suggestion is covered by the API acceptance"}, "architect")
    assert http.get(f"/projects/{project}").json()["hints"][0]["control"]["state"] == "applied"


@pytest.mark.parametrize("change", ["revoke", "replace"])
@pytest.mark.parametrize("stage", ["proposal", "approval"])
def test_inactive_suggestion_cannot_install_a_plan(http, tmp_path, change, stage):
    project, planner = create(http, tmp_path)
    hint = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                     json={"content": "Old direction"}).json()
    assessment = next(i for i, w in works(http, project).items() if w["purpose"] == "suggestion")
    finish(http, project, assessment, {"kind": "review", "verdict": "approve", "summary": "Initially useful"}, "architect")
    candidate = {**plan(), "architecture": "Must not be adopted after withdrawal", "addressed_controls": [hint["id"]]}
    if stage == "approval":
        finish(http, project, planner, candidate, "planner")
    if change == "revoke":
        http.post(f"/projects/{project}/development/controls/{hint['id']}/revoke", headers=OPERATOR).raise_for_status()
    else:
        http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                  json={"content": "Replacement direction", "replaces": hint["id"]}).raise_for_status()
    rejected = planner
    if stage == "proposal":
        finish(http, project, planner, candidate, "planner")
    else:
        rejected = next(i for i, w in works(http, project).items() if w["purpose"] == "plan" and w["role"] == "architect")
        finish(http, project, rejected, {"kind": "review", "verdict": "approve", "summary": "Approve the old plan"}, "architect")
    values = works(http, project)
    assert values[rejected]["state"] == "waiting"
    assert "inactive control" in values[rejected]["checkpoint"].lower()
    assert hint["id"] in values[rejected]["checkpoint"]
    assert not any(w["role"] == "implementer" for w in values.values())
    data = http.get(f"/projects/{project}").json()
    assert data["project"]["development"]["architecture"] == ""
    assert next(h for h in data["hints"] if h["id"] == hint["id"])["control"]["state"] == ("revoked" if change == "revoke" else "replaced")


@pytest.mark.parametrize("reference", ["missing", "unassessed"])
def test_plan_cannot_adopt_unknown_or_unassessed_advice(http, tmp_path, reference):
    project, planner = create(http, tmp_path)
    hint_id = "h999"
    if reference == "unassessed":
        hint_id = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                            json={"content": "An unreviewed idea"}).json()["id"]
    finish(http, project, planner, {**plan(), "addressed_controls": [hint_id]}, "planner")
    values = works(http, project)
    assert values[planner]["state"] == "waiting"
    assert hint_id in values[planner]["checkpoint"]
    assert not any(w["role"] == "implementer" for w in values.values())


def test_arbitration_cannot_restore_withdrawn_advice(http, tmp_path):
    from cairn.server.development_workflow import request_design
    project, planner = create(http, tmp_path)
    approve_plan(http, project, planner)
    hint = http.post(f"/projects/{project}/development/controls", headers=OPERATOR,
                     json={"content": "Reconsider a prerequisite"}).json()
    assessment = next(i for i, w in works(http, project).items() if w["purpose"] == "suggestion")
    finish(http, project, assessment, {"kind": "review", "verdict": "approve", "summary": "Initially justified"}, "architect")
    values = works(http, project)
    subject = next(i for i, w in values.items() if w["key"] == "ui")
    previous = values[subject]
    with db.get_conn() as conn:
        request_design(conn, project, subject, store.work(conn, project, subject), "Review the prerequisite")
    http.post(f"/projects/{project}/development/controls/{hint['id']}/revoke", headers=OPERATOR).raise_for_status()
    arbitration = next(i for i, w in works(http, project).items() if w["purpose"] == "arbitration")
    correction = {"key": "ui", "description": "Adjust UI", "scope": "Changed contract", "acceptance": ["Changed acceptance"], "dependencies": []}
    finish(http, project, arbitration, {"kind": "decision", "summary": "Apply the withdrawn advice", "architecture": "Must not change",
                                       "addressed_controls": [hint["id"]], "work": [correction]}, "architect")
    values = works(http, project)
    assert values[arbitration]["state"] == "waiting"
    assert "inactive control" in values[arbitration]["checkpoint"].lower()
    assert values[subject]["dependencies"] == previous["dependencies"]
    assert values[subject]["scope"] == previous["scope"]
    assert http.get(f"/projects/{project}").json()["project"]["development"]["architecture"] == ""
