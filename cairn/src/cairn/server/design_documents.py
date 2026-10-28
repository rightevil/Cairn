"""Read registered design artifacts without treating references as document bodies."""
from __future__ import annotations

import json
from pathlib import Path
import re

from cairn.server import development_store as store


def source_paths(content):
    if not content.lstrip().startswith("artifacts/"):
        return []
    return list(dict.fromkeys(re.findall(r"artifacts/[^\s<>\"'`]+?\.(?:md|html)(?![a-zA-Z])", content)))


def artifact_roots(conn, project_id):
    from cairn.workspace import project_workspace
    config = store.project(conn, project_id)
    repository = Path(config.repository)
    roots = ([project_workspace(config, project_id, repository / ".cairn" / "workspaces") / "design-session" / "artifacts"]
             if config.workspace_root else [])
    for row in conn.execute("SELECT id,streams FROM runs WHERE project_id=? ORDER BY rowid DESC", (project_id,)):
        value = json.loads(row["streams"]).get("stdout")
        if not value:
            continue
        path = Path(value)
        if path.name == "stdout.log" and path.parent.name == row["id"] and path.parent.parent.name == "runs" and path.parent.parent.parent.name == project_id:
            roots.append(path.parent.parent.parent / "design-session" / "artifacts")
    return list(dict.fromkeys([*roots, repository / ".cairn" / "artifacts"]))


def resolve_documents(conn, project_id, draft):
    documents, sources, issues = {}, dict(draft.document_sources), []
    roots = artifact_roots(conn, project_id)
    for name, registered in draft.documents.items():
        paths = source_paths(registered)
        if not paths:
            documents[name] = registered
            continue
        bodies = []
        sources[name] = registered
        for source in paths:
            relative = Path(source.removeprefix("artifacts/"))
            found = False
            for root in roots:
                target = (root / relative).resolve()
                if not target.is_relative_to(root.resolve()):
                    break
                if not target.is_file():
                    continue
                try:
                    if target.stat().st_size > 2_000_000:
                        raise ValueError("文件超过 2 MB")
                    text = target.read_text(encoding="utf-8-sig")
                    if not text.strip():
                        raise ValueError("文件为空")
                    bodies.append(f"<!-- source: {source} -->\n{text}")
                    found = True
                except (OSError, UnicodeError, ValueError) as exc:
                    issues.append(f"文档 {name} 无法读取：{source}（{exc}）")
                    found = True
                break
            if not found:
                issues.append(f"文档 {name} 的源文件不存在或越出设计产物目录：{source}")
        documents[name] = "\n\n".join(bodies)
    return documents, sources, issues
