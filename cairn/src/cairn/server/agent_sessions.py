"""Group actual runtime sessions without conflating workers that share a model name."""
import json


def task_records(conn, project_id):
    return {r["id"]: {"description":r["description"], "work":json.loads(r["work"] or "{}")}
            for r in conn.execute("SELECT id,description,work FROM intents WHERE project_id=?", (project_id,))}


def task_identity(records, intent_id):
    seen = set()
    while intent_id in records and intent_id not in seen:
        seen.add(intent_id)
        item = records[intent_id]
        work = item["work"]
        if work.get("role") == "implementer":
            return {"id":intent_id, "key":work.get("key"), "title":item["description"]}
        intent_id = work.get("subject")
    return None


def sessions(conn, project_id):
    from cairn.server.design import load
    from cairn.server.routers.development import public_run
    draft = load(conn, project_id)
    records = task_records(conn, project_id)
    groups = {}
    for row in conn.execute("SELECT r.*,i.work FROM runs r JOIN intents i ON i.project_id=r.project_id AND i.id=r.intent_id "
                            "WHERE r.project_id=? ORDER BY r.rowid", (project_id,)):
        work = json.loads(row["work"] or "{}")
        subject = records.get(work.get("subject"), {}).get("work", {})
        author = work.get('purpose')=='design' or bool(work.get('result',{}).get('design_candidate'))
        design = author or bool(subject.get("result", {}).get("design_candidate"))
        actual = row["session"] or (draft.session if design else None)
        key = "session:" + actual if actual else "run:" + row["id"]
        group = groups.setdefault(key, {"key": key, "session": actual, "main": False, "runs": [], "title": ""})
        group["main"] |= author and (not draft.session or actual == draft.session)
        group["title"] = "方案设计主会话" if group["main"] else "独立方案评审" if design and row["role"] in ("architect", "reviewer") else "历史方案设计" if design else (
            "独立评审" if row["role"] in ("architect", "reviewer") else "实施" if row["role"] == "implementer" else "集成" if row["role"] == "integration" else "规划")
        public = public_run(row)
        run = {key: public[key] for key in ("id", "intent_id", "worker", "role", "state", "session", "pid",
            "heartbeat_at", "started_at", "streams", "cancel_requested", "checkpoint", "detail")}
        run["result"] = {"snapshot": {"tier": (public.get("result", {}).get("snapshot") or {}).get("tier")}}
        run["design_conversation"] = design
        run["task"] = task_identity(records, row["intent_id"])
        group["runs"].append(run)
        group["state"] = "running" if any(r["state"] == "running" for r in group["runs"]) else row["state"]
    result = sorted(groups.values(), key=lambda g: (not g["main"], g["runs"][-1]["started_at"]), reverse=False)
    if not any(g["main"] for g in result):
        result.insert(0, {"key": "main", "main": True, "title": "方案设计主会话", "session": draft.session, "runs": [], "state": "ready"})
    return result
