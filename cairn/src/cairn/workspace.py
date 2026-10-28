"""Resolve a project's pinned storage root without moving existing worktrees."""
from pathlib import Path
import re


def project_workspace(config, project_id: str, legacy_root: Path) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", project_id):
        raise ValueError("Invalid project ID")
    if configured := getattr(config, "workspace_root", None):
        root = Path(configured)
        if not root.is_absolute() or not root.resolve().is_relative_to(Path(config.repository).resolve()):
            raise ValueError("Project workspace must stay inside its repository")
    else:
        root = legacy_root
    return root.resolve() / project_id
