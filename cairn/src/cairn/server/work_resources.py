"""Worktree-local paths do not lock unrelated checkouts; external resources do."""
from pathlib import PurePosixPath, PureWindowsPath


def shared_resources(names, project_id):
    shared = set()
    for name in names:
        if name.startswith("workspace:"):
            continue
        if name.startswith("project:"):
            shared.add((project_id, name))
        elif name.startswith("global:"):
            shared.add((None, name.removeprefix("global:")))
        elif ":" not in name and not PurePosixPath(name).is_absolute() and not PureWindowsPath(name).is_absolute() and (
                "/" in name or "\\" in name or PurePosixPath(name).suffix):
            continue  # Legacy relative file resources refer to each run's private checkout.
        else:
            shared.add((None, name))
    return shared


def resource_conflicts(names, project_id, held, held_project):
    return sorted(name for _, name in shared_resources(names, project_id) & shared_resources(held, held_project))


def scheduling(conn, project_id):
    from cairn.server import development_store as store
    from cairn.server.services import utcnow
    rows = conn.execute("SELECT project_id,id,work FROM intents WHERE work IS NOT NULL").fetchall()
    work = {(r["project_id"], r["id"]): store.WorkPackage.model_validate_json(r["work"]) for r in rows}
    running = conn.execute("SELECT project_id,intent_id,role FROM runs WHERE state='running'").fetchall()
    result = {}
    for (pid, iid), value in work.items():
        if pid != project_id or value.state not in ("ready", "waiting", "approved", "running", "reviewing", "implemented"):
            continue
        unmet = [dep for dep in value.dependencies if not work.get((pid, dep)) or work[pid, dep].state != "verified"]
        conflicts = sorted({name for run in running if run["intent_id"] != iid or run["project_id"] != pid
                            for name in resource_conflicts(value.resources, pid, work[run["project_id"], run["intent_id"]].resources, run["project_id"])})
        if value.state == "running":
            status, detail = "running", "执行中"
        elif unmet:
            status, detail = "dependencies", "等待前置任务：" + "、".join(unmet)
        elif conflicts:
            status, detail = "resources", "等待共享资源：" + "、".join(conflicts)
        elif value.retry_at and value.retry_at > utcnow():
            status, detail = "retry", "等待重试：" + value.retry_at
        elif value.state in ("reviewing", "implemented"):
            status, detail = "review", "等待审查／输出校验"
        elif value.state == "waiting":
            status, detail = "waiting", value.checkpoint or "等待外部条件"
        elif value.state == "approved":
            status, detail = "integration", "等待串行合并与验证"
        else:
            status, detail = "capacity", "依赖已满足，等待执行器分配／可用模型容量"
        result[iid] = {"status": status, "detail": detail}
    return result
