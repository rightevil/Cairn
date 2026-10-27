"""Local workspace UI: folder selection and design-first project creation."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cairn.server.access import local_browser, require_operator
from cairn.server.db import get_conn
from cairn.server.development_models import DevelopmentProject
from cairn.server.models import CreateProjectRequest, ProjectDetail
from cairn.server.routers.projects import create_project, list_projects
from cairn.server.services import get_project_or_404

router = APIRouter(prefix="/workspace", tags=["workspace"], dependencies=[Depends(require_operator)])


@router.post("/folder")
def retired_native_picker():
    raise HTTPException(410, "文件夹选择器已更新，请刷新页面后重新选择")


@router.get("/folders")
def folders(request: Request, directory: str | None = None):
    if not local_browser(request):
        raise HTTPException(403, "请在运行 Cairn 的本机浏览器中选择文件夹")
    if directory is not None and "\0" in directory:
        raise HTTPException(422, "目录路径无效")
    if directory == "":
        roots = os.listdrives() if os.name == "nt" else ["/"]
        return {"directory": None, "name": "此电脑", "parent": None, "breadcrumbs": [],
                "entries": [{"name": root, "directory": root} for root in roots]}
    path = Path(directory) if directory is not None else Path.cwd()
    if not path.is_absolute():
        raise HTTPException(422, "请选择绝对目录")
    try:
        path = path.resolve(strict=True)
        if not path.is_dir():
            raise HTTPException(422, "请选择文件夹，不能选择文件")
        with os.scandir(path) as children:
            entries = [{"name": child.name, "directory": str(path / child.name)} for child in children
                       if not child.name.startswith(".") and child.is_dir(follow_symlinks=False)]
    except FileNotFoundError as exc:
        raise HTTPException(404, "文件夹已不存在，请返回上级目录") from exc
    except ValueError as exc:
        raise HTTPException(422, "目录路径无效") from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(403, "无法访问此文件夹，请选择其他目录") from exc
    breadcrumbs = [{"name": part.name or str(part), "directory": str(part)}
                   for part in [*reversed(path.parents), path]]
    return {"directory": str(path), "name": path.name or str(path),
            "parent": str(path.parent) if path.parent != path else "", "breadcrumbs": breadcrumbs,
            "entries": sorted(entries, key=lambda child: child["name"].casefold())}


class NewFolder(BaseModel):
    directory: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=255)


@router.post("/folders", status_code=201)
def create_folder(body: NewFolder, request: Request):
    if not local_browser(request):
        raise HTTPException(403, "请在运行 Cairn 的本机浏览器中新建文件夹")
    name = body.name.strip()
    if (not name or name in {".", ".."} or name.endswith(".") or PureWindowsPath(name).is_reserved()
            or any(ord(char) < 32 or char in '<>:"/\\|?*' for char in name)):
        raise HTTPException(422, "文件夹名称无效，请勿使用路径、保留名称或特殊字符")
    parent = Path(body.directory)
    if not parent.is_absolute() or "\0" in body.directory:
        raise HTTPException(422, "请选择有效的父目录")
    try:
        parent = parent.resolve(strict=True)
        target = parent / name
        target.mkdir()
    except FileExistsError as exc:
        raise HTTPException(409, "同名文件或文件夹已存在，请使用其他名称") from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "父目录已不存在，请重新选择") from exc
    except PermissionError as exc:
        raise HTTPException(403, "没有权限在此目录新建文件夹") from exc
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(422, "无法新建文件夹，请检查目录和名称") from exc
    return folders(request, str(target))


def git(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                                encoding="utf-8", errors="replace", timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, "无法读取项目 Git 状态，请检查 Git 安装和目录访问权限") from exc
    if result.returncode:
        raise HTTPException(422, "请选择已有提交的 Git 项目根目录，或选择空文件夹创建项目")
    return result.stdout.strip()


class NewWorkspace(BaseModel):
    directory: str = Field(min_length=1)
    name: str = Field(default="", max_length=200)


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def new_project(body: NewWorkspace, request: Request):
    directory = Path(body.directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise HTTPException(422, "请选择本机存在的项目文件夹")
    directory = directory.resolve()
    with get_conn() as conn:
        for row in conn.execute("SELECT id,development FROM projects WHERE development IS NOT NULL"):
            if Path(json.loads(row["development"])["repository"]).resolve() == directory:
                raise HTTPException(409, "此文件夹已经创建项目，请从左侧项目列表打开")
    if not any(directory.iterdir()):
        # An empty baseline never stages user files or changes their global Git identity.
        git(directory, "init")
        git(directory, "-c", "user.name=Cairn", "-c", "user.email=cairn@localhost", "-c", "commit.gpgsign=false",
            "commit", "--allow-empty", "-m", "chore(project): 初始化项目")
    root = Path(git(directory, "rev-parse", "--show-toplevel")).resolve()
    if root != directory:
        raise HTTPException(422, "请选择 Git 仓库根目录，不能选择仓库内部的子文件夹")
    baseline = git(directory, "rev-parse", "HEAD")
    config = DevelopmentProject(repository=str(directory), phase="design", delivery="local",
                                design="与用户讨论并确认目标、需求、方案和验收标准后再开发。")
    return create_project(CreateProjectRequest(
        title=body.name.strip() or directory.name, origin=f"项目初始代码基线：{baseline}",
        goal="待方案设计阶段与用户共同确认。", bootstrap_enabled=False, development=config,
    ), request)


@router.get("/projects")
def overview():
    projects = list_projects()
    with get_conn() as conn:
        result = []
        for project in projects:
            work = [json.loads(row["work"]) for row in conn.execute(
                "SELECT work FROM intents WHERE project_id=? AND work IS NOT NULL", (project.id,))]
            tasks = [w for w in work if w["role"] == "implementer" and w["state"] != "superseded"]
            row = conn.execute("SELECT data FROM designs WHERE project_id=?", (project.id,)).fetchone()
            draft = json.loads(row["data"]) if row else {}
            result.append({**project.model_dump(), "progress": {
                "total": len(tasks), "verified": sum(w["state"] == "verified" for w in tasks),
                "running": conn.execute("SELECT count(*) FROM runs WHERE project_id=? AND state='running'", (project.id,)).fetchone()[0],
                "questions": sum(q.get("answer") is None for q in draft.get("questions", [])),
                "stage": draft.get("stage", "brief"),
            }})
        return result


@router.get("/projects/{project_id}/files")
def files(project_id: str, query: str = ""):
    with get_conn() as conn:
        row = get_project_or_404(conn, project_id)
        if not row["development"]:
            return []
        directory = Path(json.loads(row["development"])["repository"])
    paths = git(directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
    return [p for p in dict.fromkeys(paths) if p and query.casefold() in p.casefold()][:30]
