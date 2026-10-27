"""Group actual runtime sessions without conflating workers that share a model name."""
import json


def sessions(conn, project_id):
    from cairn.server.design import load
    from cairn.server.routers.development import public_run
    draft = load(conn, project_id)
    groups = {}
    for row in conn.execute("SELECT r.*,i.work FROM runs r JOIN intents i ON i.project_id=r.project_id AND i.id=r.intent_id "
                            "WHERE r.project_id=? ORDER BY r.rowid", (project_id,)):
        work = json.loads(row["work"] or "{}")
        design = work.get("purpose") == "design" or bool(work.get("result", {}).get("design_candidate"))
        actual = row["session"] or (draft.session if design else None)
        key = "session:" + actual if actual else "run:" + row["id"]
        group = groups.setdefault(key, {"key": key, "session": actual, "main": False, "runs": [], "title": ""})
        group["main"] |= design and (not draft.session or actual == draft.session)
        group["title"] = "方案设计主会话" if group["main"] else "历史方案设计" if design else (
            "独立评审" if row["role"] in ("architect", "reviewer") else "实施" if row["role"] == "implementer" else "集成" if row["role"] == "integration" else "规划")
        public = public_run(row)
        run = {key: public[key] for key in ("id", "intent_id", "worker", "role", "state", "session", "pid",
            "heartbeat_at", "started_at", "streams", "cancel_requested", "checkpoint", "detail")}
        run["result"] = {"snapshot": {"tier": (public.get("result", {}).get("snapshot") or {}).get("tier")}}
        run["design_conversation"] = design
        group["runs"].append(run)
        group["state"] = "running" if any(r["state"] == "running" for r in group["runs"]) else row["state"]
    result = sorted(groups.values(), key=lambda g: (not g["main"], g["runs"][-1]["started_at"]), reverse=False)
    if not any(g["main"] for g in result):
        result.insert(0, {"key": "main", "main": True, "title": "方案设计主会话", "session": draft.session, "runs": [], "state": "ready"})
    return result
