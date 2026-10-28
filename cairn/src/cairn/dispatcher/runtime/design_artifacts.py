"""Keep a user-visible project copy of design artifacts without overwriting edits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile

from cairn.dispatcher.runtime.git_workspace import ignore_project_metadata


def export_artifacts(source: Path, repository: Path):
    repository = repository.resolve()
    root = repository / ".cairn" / "artifacts"
    manifest = repository / ".cairn" / "design-export.json"
    if not root.resolve().is_relative_to(repository) or not manifest.resolve().is_relative_to(repository):
        raise RuntimeError("Project design storage must stay inside the repository")
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    contents, hashes = {}, dict(previous)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Design export cannot follow a symbolic link: {path.name}")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        target = root / relative
        if not target.resolve().is_relative_to(root.resolve()) or target.is_symlink():
            raise RuntimeError(f"Design export path escapes storage: {relative}")
        content = path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        if target.exists():
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if current != checksum and current != previous.get(relative):
                raise RuntimeError(f"Project design file was edited; preserving it: {relative}")
        contents[target] = content
        hashes[relative] = checksum
    # Local design metadata must not make an otherwise clean code baseline dirty.
    ignore_project_metadata(repository)
    contents[manifest] = json.dumps(hashes, ensure_ascii=False, indent=2).encode("utf-8")
    for target, content in contents.items():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        try:
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return root
