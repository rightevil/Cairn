"""Transactional state transitions on Cairn Intents, not an additional task queue."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

from fastapi import HTTPException

from cairn.server import development_store as store
from cairn.server.development_models import Control, DevelopmentOutcome, WorkPackage
from cairn.server.services import next_fact_id, next_hint_id, utcnow


def all_work(conn, project_id):
    return [(r["id"], WorkPackage.model_validate_json(r["work"])) for r in conn.execute(
        "SELECT id,work FROM intents WHERE project_id=? AND work IS NOT NULL ORDER BY rowid", (project_id,))]


def fact(conn, project_id, intent_id, summary):
    fact_id = next_fact_id(conn, project_id)
    conn.execute("INSERT INTO facts(id,project_id,description) VALUES(?,?,?)", (fact_id, project_id, summary))
    conn.execute("UPDATE intents SET to_fact_id=?,concluded_at=?,worker=NULL WHERE project_id=? AND id=?",
                 (fact_id, utcnow(), project_id, intent_id))
    return fact_id


def add_review(conn, project_id, subject, purpose, revision, summary):
    return store.add_intent(conn, project_id, summary, WorkPackage(
        role="reviewer" if purpose == "implementation" else "architect",
        purpose=purpose, subject=subject, revision=revision))


def ready_planner(conn, project_id, reason):
    config = store.project(conn, project_id)
    existing = all_work(conn, project_id)
    if not any(w.role == "planner" and w.state in ("ready", "running", "implemented") for _, w in existing):
        store.add_intent(conn, project_id, reason, WorkPackage(role="planner", purpose="plan", revision=config.revision))


def retry(conn, project_id, intent_id, value, detail, retry_at=None, *, escalate=True):
    if escalate:
        value.failures += 1
    value.state = "waiting"
    value.checkpoint = detail
    value.retry_at = retry_at or (datetime.now(timezone.utc) + timedelta(
        seconds=min(300, 5 * 2 ** min(value.failures, 6)))).isoformat(timespec="seconds").replace("+00:00", "Z")
    store.save_work(conn, project_id, intent_id, value)
    if escalate and value.role == "implementer" and value.failures >= 3:
        if not any(w.purpose == "arbitration" and w.subject == intent_id and w.state in ("ready", "running")
                   for _, w in all_work(conn, project_id)):
            value.state, value.retry_at = "reviewing", None
            store.save_work(conn, project_id, intent_id, value)
            add_review(conn, project_id, intent_id, "arbitration", value.revision,
                       "Decide a new approach using repeated failure evidence")


def validate_plan(conn, project_id, outcome):
    keys = [w.key for w in outcome.work]
    if len(keys) != len(set(keys)) or not keys:
        raise ValueError("Plan must have nonempty unique work keys")
    existing = {key for intent_id, w in all_work(conn, project_id) for key in (intent_id, w.key) if key}
    dependencies = {w.key: set(w.dependencies) for w in outcome.work}
    config = store.project(conn, project_id)
    for proposed in outcome.work:
        commands = config.checks or proposed.checks
        if not commands or any(not argv or not all(arg.strip() for arg in argv) for argv in commands):
            raise ValueError("Every work package needs runnable project or planned check commands")
        if proposed.supersedes:
            prior = store.work(conn, project_id, proposed.supersedes)
            if prior.state not in ("verified", "superseded") or prior.key != proposed.key:
                raise ValueError("A revision must explicitly supersede a completed work package with the same key")
    for key, deps in dependencies.items():
        if key in deps or deps - set(keys) - existing:
            raise ValueError(f"Invalid dependencies for {key}")
    resolved = set(existing) - set(keys)
    pending = set(keys)
    while pending:
        ready = {key for key in pending if dependencies[key] <= resolved}
        if not ready:
            raise ValueError("Work dependency cycle")
        resolved.update(ready)
        pending -= ready


def install_plan(conn, project_id, outcome, revision):
    validate_plan(conn, project_id, outcome)
    existing = all_work(conn, project_id)
    by_key = {w.key: (i, w) for i, w in existing if w.key and w.state != "superseded"}
    mapping = {i: i for i, _ in existing}
    mapping.update({key: i for key, (i, _) in by_key.items()})
    created = []
    for proposed in outcome.work:
        if proposed.key in by_key and not proposed.supersedes:
            continue  # A stable completed/active work key never silently becomes a new action.
        if proposed.supersedes:
            previous = store.work(conn, project_id, proposed.supersedes)
            previous.state = "superseded"
            store.save_work(conn, project_id, proposed.supersedes, previous)
        value = WorkPackage(key=proposed.key, scope=proposed.scope, acceptance=proposed.acceptance,
                            checks=proposed.checks, revision=revision)
        intent_id = store.add_intent(conn, project_id, proposed.description, value)
        mapping[proposed.key] = intent_id
        created.append((intent_id, proposed))
        if proposed.supersedes:
            for old_id, old in existing:
                if old.state not in ("verified", "superseded") and proposed.supersedes in old.dependencies:
                    old.dependencies = [intent_id if d == proposed.supersedes else d for d in old.dependencies]
                    store.save_work(conn, project_id, old_id, old)
    for intent_id, proposed in created:
        value = store.work(conn, project_id, intent_id)
        value.dependencies = [mapping[d] for d in proposed.dependencies]
        store.save_work(conn, project_id, intent_id, value)
    return len(created)


def apply_review(conn, project_id, run, value, outcome):
    if outcome.kind != "review" or outcome.verdict is None:
        raise ValueError("Review requires an explicit verdict and supporting summary")
    config = store.project(conn, project_id)
    if value.purpose == "direction":
        if outcome.verdict != "approve":
            if not outcome.affected:
                raise ValueError("A direction objection must identify affected work packages")
            for intent_id in outcome.affected:
                subject = store.work(conn, project_id, intent_id)
                if subject.state in ("verified", "superseded"):
                    continue
                subject.state = "reviewing"
                subject.checkpoint = "Direction review: " + outcome.summary
                store.save_work(conn, project_id, intent_id, subject)
                add_review(conn, project_id, intent_id, "arbitration", config.revision, subject.checkpoint)
        value.state = "verified"
        return
    if value.purpose == "suggestion":
        row = conn.execute("SELECT control FROM hints WHERE project_id=? AND id=?", (project_id, value.subject)).fetchone()
        control = Control.model_validate_json(row["control"])
        if control.kind != "suggestion" or control.state != "received":
            value.state = "superseded"
        else:
            control.state = {"approve": "accepted", "request_changes": "deferred", "needs_design": "deferred",
                             "reject": "rejected", "defer": "deferred", "experiment": "experiment"}[outcome.verdict]
            if not outcome.within_scope:
                control.state = "deferred"
            control.assessment = outcome.summary
            conn.execute("UPDATE hints SET control=? WHERE project_id=? AND id=?",
                         (store.dump(control), project_id, value.subject))
            if control.state in ("accepted", "experiment") and outcome.within_scope:
                ready_planner(conn, project_id, "Incorporate evaluated suggestion " + value.subject)
            value.state = "verified"
        store.event(conn, project_id, "suggestion_evaluated", value.subject, outcome.summary)
        return
    subject = store.work(conn, project_id, value.subject)
    author = conn.execute("SELECT session FROM runs WHERE project_id=? AND intent_id=? AND state='success' ORDER BY rowid DESC LIMIT 1",
                          (project_id, value.subject)).fetchone()
    if author and author["session"] and run["session"] == author["session"]:
        raise ValueError("Review must use an independent session")
    if subject.state in ("superseded", "verified"):
        value.state = "superseded"
        return
    if not outcome.within_scope:
        outcome.verdict = "reject"
    if outcome.verdict != "approve":
        subject.checkpoint = "Review: " + outcome.summary
        if outcome.verdict == "needs_design":
            subject.state = "reviewing"
            add_review(conn, project_id, value.subject, "arbitration", config.revision, "Resolve design dispute: " + outcome.summary)
        elif outcome.verdict in ("request_changes", "reject"):
            subject.state = "ready"
            subject.result["review_feedback"] = outcome.summary
        else:
            subject.state, subject.retry_at = "waiting", outcome.wake_at
        value.state = "verified"
        store.save_work(conn, project_id, value.subject, subject)
        return
    if value.purpose == "plan":
        candidate = DevelopmentOutcome.model_validate(subject.result["output"])
        count = install_plan(conn, project_id, candidate, config.revision)
        subject.state = "verified"
        if not count:
            config.state, config.wait_reason = "waiting", "Plan adds no new work; existing work keys are preserved"
            store.save_project(conn, project_id, config)
    elif value.purpose == "implementation":
        reviewed = subject.result.get("snapshot", {})
        reported = run_result(run).get("snapshot", {})
        if not reviewed.get("head_commit") or reported.get("reviewed_commit") != reviewed["head_commit"]:
            raise ValueError("Review must identify the exact implementation commit")
        if reported.get("reviewed_base") != reviewed.get("base_commit"):
            raise ValueError("Review base does not match implementation base")
        evidence = json.loads(run["evidence"])
        if not evidence or not reported.get("checks_passed"):
            raise ValueError("Independent checks and evidence are required before approval")
        subject.state = "approved"
        subject.result["review"] = {"run_id": run["id"], "commit": reviewed["head_commit"],
                                    "base": reviewed.get("base_commit"), "summary": outcome.summary}
    elif value.purpose == "milestone":
        if any(w.role == "implementer" and w.state not in ("verified", "superseded") for _, w in all_work(conn, project_id)):
            raise ValueError("Milestone has unfinished implementation work")
        if not run_result(run).get("snapshot", {}).get("checks_passed") or not json.loads(run["evidence"]):
            raise ValueError("Milestone completion requires independent integration checks")
        candidate = DevelopmentOutcome.model_validate(subject.result["output"])
        subject.state = "verified"
        for hint in conn.execute("SELECT id,control FROM hints WHERE project_id=? AND control IS NOT NULL", (project_id,)):
            control = Control.model_validate_json(hint["control"])
            if control.kind == "directive" and control.state == "applied":
                control.state = "completed"
                control.assessment = "Independently verified at milestone: " + outcome.summary
                conn.execute("UPDATE hints SET control=? WHERE project_id=? AND id=?", (store.dump(control), project_id, hint["id"]))
                store.event(conn, project_id, "control_completed", hint["id"], control.assessment)
        if candidate.project_complete:
            conn.execute("UPDATE projects SET status='completed' WHERE id=?", (project_id,))
            config.state = "completed"
            store.save_project(conn, project_id, config)
            conn.execute("UPDATE intents SET to_fact_id='goal',concluded_at=? WHERE project_id=? AND id=?",
                         (utcnow(), project_id, value.subject))
        else:
            ready_planner(conn, project_id, "Plan the next milestone within the approved project design")
    value.state = "verified"
    store.save_work(conn, project_id, value.subject, subject)


def run_result(run):
    return json.loads(run["result"])


def apply_success(conn, project_id, run, value):
    result = run_result(run)
    outcome = DevelopmentOutcome.model_validate(result.get("output", result))
    value.result = result
    value.checkpoint = outcome.summary
    if outcome.kind in ("wait", "proposal"):
        config = store.project(conn, project_id)
        value.state, value.retry_at = "waiting", outcome.wake_at
        if value.role == "planner":
            config.state, config.wait_reason, config.wake_at = "waiting", outcome.summary, outcome.wake_at
            store.save_project(conn, project_id, config)
        if outcome.kind == "proposal":
            hint_id = next_hint_id(conn, project_id)
            control = Control(kind="suggestion", source="agent", state="deferred", revision=config.revision,
                              assessment="Outside-scope proposal; requires a user decision")
            conn.execute("INSERT INTO hints(id,project_id,content,creator,created_at,control) VALUES(?,?,?,?,?,?)",
                         (hint_id, project_id, outcome.summary, "planner", utcnow(), store.dump(control)))
        return
    if value.purpose == "arbitration":
        if value.role != "architect" or outcome.kind != "decision":
            raise ValueError("Architecture arbitration requires an independent strong-model decision")
        if not outcome.within_scope:
            raise ValueError("Architecture decisions cannot expand the approved project scope")
        subject = store.work(conn, project_id, value.subject)
        if outcome.work:
            if len(outcome.work) != 1 or outcome.work[0].key != subject.key:
                raise ValueError("A design correction may update only the specified work package")
            correction = outcome.work[0]
            subject.scope, subject.acceptance, subject.checks = correction.scope, correction.acceptance, correction.checks
        subject.result["design_decision"] = outcome.summary
        subject.checkpoint = outcome.summary
        subject.state, subject.retry_at = "ready", None
        store.save_work(conn, project_id, value.subject, subject)
        value.state = "verified"
    elif value.role in ("reviewer", "architect"):
        apply_review(conn, project_id, run, value, outcome)
    elif value.role == "planner":
        if outcome.kind == "plan":
            validate_plan(conn, project_id, outcome)
            add_review(conn, project_id, run["intent_id"], "plan", value.revision, "Independently review the proposed plan and project fit")
        elif outcome.kind == "complete":
            add_review(conn, project_id, run["intent_id"], "milestone", value.revision, "Verify the milestone against raw integration evidence")
        else:
            raise ValueError("Planner must propose a plan, completion candidate, scoped proposal, or wait")
        value.state = "reviewing"
    elif value.role == "implementer":
        if outcome.kind == "needs_design":
            add_review(conn, project_id, run["intent_id"], "arbitration", value.revision, "Decide implementation design gap: " + outcome.summary)
            value.state = "reviewing"
        elif outcome.kind == "implemented":
            snapshot = result.get("snapshot", {})
            if not snapshot.get("head_commit") or not snapshot.get("checks_passed") or not json.loads(run["evidence"]):
                raise ValueError("Implementation must include committed code and actual check evidence")
            value.head_commit, value.base_commit = snapshot["head_commit"], snapshot["base_commit"]
            value.branch, value.worktree = snapshot["branch"], snapshot["worktree"]
            add_review(conn, project_id, run["intent_id"], "implementation", value.revision, "Independently inspect the diff, acceptance and check evidence")
            value.state = "reviewing"
        else:
            raise ValueError("Implementer must report implementation, design gap, proposal or wait")
    if value.state not in ("waiting", "superseded"):
        fact_id = fact(conn, project_id, run["intent_id"], outcome.summary)
        for review_id, review in all_work(conn, project_id):
            if review.subject == run["intent_id"] and review.state == "ready":
                conn.execute("INSERT OR IGNORE INTO intent_sources(intent_id,project_id,fact_id) VALUES(?,?,?)",
                             (review_id, project_id, fact_id))
    store.event(conn, project_id, "work_updated", run["intent_id"], value.state + ": " + outcome.summary)


def checkpoint(conn, project_id):
    from cairn.server.routers.development import affected
    config = store.project(conn, project_id)
    if config.requested_revision == config.revision:
        return
    for row in conn.execute("SELECT intent_id,revision FROM runs WHERE project_id=? AND state='running'", (project_id,)):
        if affected(conn, project_id, row["intent_id"], row["revision"]):
            return
    for intent_id, value in all_work(conn, project_id):
        if value.state in ("verified", "superseded"):
            continue
        if affected(conn, project_id, intent_id, value.revision):
            value.state = "superseded"
        else:
            value.revision = config.requested_revision
        store.save_work(conn, project_id, intent_id, value)
    config.revision, config.state, config.wait_reason = config.requested_revision, "active", ""
    store.save_project(conn, project_id, config)
    for row in conn.execute("SELECT id,control FROM hints WHERE project_id=? AND control IS NOT NULL", (project_id,)):
        control = Control.model_validate_json(row["control"])
        if control.kind == "directive" and control.state == "accepted":
            control.state = "applied"
            conn.execute("UPDATE hints SET control=? WHERE project_id=? AND id=?", (store.dump(control), project_id, row["id"]))
            store.event(conn, project_id, "control_applied", row["id"], f"revision {config.revision}")
        elif control.kind == "suggestion" and control.state == "received":
            if not any(w.purpose == "suggestion" and w.subject == row["id"] and w.state not in ("verified", "superseded")
                       for _, w in all_work(conn, project_id)):
                add_review(conn, project_id, row["id"], "suggestion", config.revision,
                           "Evaluate the pending suggestion against the updated project direction")
    ready_planner(conn, project_id, "Replan affected work using current user directives")


def advance(conn, project_id):
    conn.execute("BEGIN IMMEDIATE")
    store.project(conn, project_id, active=True)
    rows = conn.execute("SELECT * FROM runs WHERE project_id=? AND state!='running' AND applied=0 ORDER BY rowid", (project_id,)).fetchall()
    for run in rows:
        value = store.work(conn, project_id, run["intent_id"])
        from cairn.server.routers.development import affected
        if run["state"] == "success" and affected(conn, project_id, run["intent_id"], run["revision"]):
            value.state = "superseded"
            value.merge_commit = run_result(run).get("snapshot", {}).get("merge_commit", value.merge_commit)
            store.save_work(conn, project_id, run["intent_id"], value)
            conn.execute("UPDATE runs SET state='superseded',applied=1 WHERE id=?", (run["id"],))
            store.event(conn, project_id, "stale_result_rejected", run["id"], "Controls changed before result application")
            continue
        if run["role"] == "integration":
            snapshot = run_result(run).get("snapshot", {})
            if run["state"] == "superseded":
                value.state = "superseded"
                value.merge_commit = snapshot.get("merge_commit", value.merge_commit)
            elif snapshot.get("needs_rebase"):
                value.state, value.retry_at = "ready", None
                value.result.pop("review", None)
                value.result["review_feedback"] = snapshot.get("detail", "Rebase and recheck against current integration branch")
            elif snapshot.get("merge_commit"):
                value.merge_commit, value.pr_url = snapshot["merge_commit"], snapshot.get("pr_url")
                if run["state"] == "success" and snapshot.get("checks_passed") and json.loads(run["evidence"]):
                    value.state = "verified"
                    value.result["integration"] = snapshot
                    fact(conn, project_id, run["intent_id"], "Merged and verified: " + value.merge_commit)
                else:
                    value.state = "merged"
                    value.result["integration_failure"] = snapshot
                    add_review(conn, project_id, run["intent_id"], "arbitration", value.revision,
                               "Resolve failed integration checks without undoing completed external actions")
                store.event(conn, project_id, "integration_updated", run["intent_id"], value.state)
            else:
                value.state = "approved"
                value.retry_at = run["retry_at"] or (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(timespec="seconds").replace("+00:00", "Z")
                value.checkpoint = run["detail"]
        elif run["state"] == "success":
            conn.execute("SAVEPOINT apply_result")
            try:
                apply_success(conn, project_id, run, value)
            except (ValueError, KeyError, HTTPException) as exc:
                conn.execute("ROLLBACK TO apply_result")
                value = store.work(conn, project_id, run["intent_id"])
                retry(conn, project_id, run["intent_id"], value, "Invalid result: " + str(exc))
                store.event(conn, project_id, "result_rejected", run["id"], str(exc))
            finally:
                conn.execute("RELEASE apply_result")
        elif run["state"] != "superseded":
            retry(conn, project_id, run["intent_id"], value, run["detail"] or run["state"], run["retry_at"],
                  escalate=run["state"] == "failed")
        store.save_work(conn, project_id, run["intent_id"], value)
        conn.execute("UPDATE runs SET applied=1 WHERE id=?", (run["id"],))
    checkpoint(conn, project_id)
    config = store.project(conn, project_id)
    work = all_work(conn, project_id)
    verified_count = sum(w.role == "implementer" and w.state == "verified" for _, w in work)
    if verified_count - config.direction_checkpoint >= config.direction_review_every and not conn.execute(
        "SELECT 1 FROM runs WHERE project_id=? AND state='running'", (project_id,)
    ).fetchone():
        add_review(conn, project_id, None, "direction", config.revision, "Review progress and remaining work against the agreed project design")
        config.direction_checkpoint = verified_count
        store.save_project(conn, project_id, config)
    if config.state == "active" and not any(
        w.state in ("ready", "running", "implemented", "reviewing", "approved", "merged", "waiting") for _, w in work
    ):
        ready_planner(conn, project_id, "Choose the next useful milestone within the approved design")
