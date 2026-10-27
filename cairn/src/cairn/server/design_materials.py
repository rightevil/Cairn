"""Freeze source materials before review; materialize them in owned feature trees."""
from __future__ import annotations

import hashlib
from pathlib import Path, PureWindowsPath
import re

from cairn.server import development_store as store
from cairn.server.design_documents import artifact_roots, resolve_documents


def confined(root, relative):
    if not relative or "\\" in relative or PureWindowsPath(relative).is_absolute():
        raise ValueError("材料路径必须为相对路径：" + relative)
    path = (root / relative).resolve()
    if path == root.resolve() or not path.is_relative_to(root.resolve()) or ".git" in Path(relative).parts or ".cairn" in Path(relative).parts:
        raise ValueError("材料路径越界：" + relative)
    return path


def prepare_materials(conn, project_id, draft, write=True):
    repository = Path(store.project(conn, project_id).repository).resolve()
    if not (repository / ".cairn").resolve().is_relative_to(repository):
        raise ValueError("项目材料目录越界")
    roots = artifact_roots(conn, project_id)
    mappings = dict(draft.materials)
    text = "\n".join(resolve_documents(conn, project_id, draft)[0].values()) + store.dump([w.model_dump() for w in draft.work])
    # Compatibility with plans that already name UX fixtures but did not register mappings.
    if "test/fixtures/ux/" in text:
        names = set(re.findall(r"test/fixtures/ux/([\w.-]+\.html)", text))
        names.update(re.findall(r"\bkey-[\w.-]+\.html", text))
        for name in names:
            target = "test/fixtures/ux/" + name
            if target in mappings:
                continue
            matches = list(dict.fromkeys(p for root in roots if root.is_dir() for p in root.rglob(name)
                                        if p.is_file() and p.resolve().is_relative_to(root.resolve())))
            if not matches:
                raise ValueError("缺少 UX 源材料：" + name)
            hashes = {hashlib.sha256(p.read_bytes()).hexdigest() for p in matches}
            if len(hashes) != 1:
                raise ValueError("UX 源材料有多个不同版本，请登记目标映射：" + name)
            source = next("artifacts/" + matches[0].relative_to(root).as_posix() for root in roots if matches[0].is_relative_to(root))
            mappings[target] = source
    entries = []
    for target, source in sorted(mappings.items()):
        destination = confined(repository, target)
        if not source.startswith("artifacts/"):
            raise ValueError("材料来源须在 artifacts/：" + source)
        found = next((p for root in roots if (p := confined(root, source.removeprefix("artifacts/"))).is_file()), None)
        if found is None:
            raise ValueError("材料源文件不存在：" + source)
        if found.stat().st_size > 32_000_000:
            raise ValueError("材料超过 32 MB：" + source)
        content = found.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        if destination.exists() and (not destination.is_file() or destination.read_bytes() != content):
            raise ValueError("目标已有不同内容，保留原文件：" + target)
        relative = "design-materials/" + checksum + "/" + target
        archive_root = repository / ".cairn"
        archive = confined(archive_root, relative)
        if archive.exists() and archive.read_bytes() != content:
            raise ValueError("冻结材料哈希不匹配：" + target)
        if write:
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                with archive.open("xb") as stream:
                    stream.write(content)
        entries.append({"source": source, "target": target, "sha256": checksum, "archive": relative})
    return entries


def materialize_materials(repository, workspace, materials):
    if not (repository / ".cairn").resolve().is_relative_to(repository.resolve()):
        raise ValueError("Project material storage escapes repository")
    pending = []
    for item in materials:
        source = confined(repository / ".cairn", item["archive"])
        target = confined(workspace, item["target"])
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Frozen material hash changed: " + item["target"])
        if target.exists() and target.read_bytes() != content:
            raise ValueError("Preserving changed material: " + item["target"])
        pending.append((target, content))
    for target, content in pending:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as stream:
                stream.write(content)
