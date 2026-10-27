from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import shutil
import sys

from cairn.dispatcher.runtime.git_workspace import command, git

BMAD_SKILLS = ("bmad", "bmod-core-tools", "bmod-method", "bmad-product-brief", "bmad-prd",
               "bmad-ux", "bmad-architecture", "bmad-agent-po", "bmad-advanced-elicitation", "bmad-review")


def refresh_orchestration(directory):
    path = directory / ".claude" / "skills" / "cairn-design" / "SKILL.md"
    content = resources.files("cairn.dispatcher.prompts").joinpath("design", "SKILL.md").read_text(encoding="utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")


def prepare(directory: Path, config):
    if config is None:
        raise RuntimeError("Set design.bmad_repository and design.grilling_repository in the dispatcher configuration")
    bmad, grilling = Path(config.bmad_repository).resolve(), Path(config.grilling_repository).resolve()
    versions = {"bmad": git(bmad, "rev-parse", "HEAD"), "grilling": git(grilling, "rev-parse", "HEAD")}
    marker = directory / "skill-versions.json"
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != versions:
            raise RuntimeError("Design Skill sources changed; restore the pinned commits or start a new design session")
        refresh_orchestration(directory)
        return
    skills = directory / ".claude" / "skills"
    for name in BMAD_SKILLS:
        source = bmad / "skills" / name
        if not (source / "SKILL.md").is_file():
            raise RuntimeError(f"BMAD Skill is missing: {name}")
        shutil.copytree(source, skills / name, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    shutil.copytree(grilling / "skills" / "productivity" / "grilling", skills / "grilling", dirs_exist_ok=True)
    refresh_orchestration(directory)
    # Keep upstream text intact; local adaptation is explicit and versioned.
    overlay = ("\n## Cairn host adaptation\nThis web session is interactive even when SDK/Skill-invoked. "
               "Follow cairn-design for questions, native draft storage, and PO handoff. "
               "Use mcp__cairn__bmad_script instead of shell script commands. "
               "Only write documents under artifacts/. No game-studio redirect.\n\n")
    for name in BMAD_SKILLS:
        path = skills / name / "SKILL.md"
        content = path.read_text(encoding="utf-8")
        end = content.index("---", 3) + 3
        path.write_text(content[:end] + overlay + content[end:], encoding="utf-8")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("BMAD requires uv; install uv before starting design")
    setup = skills / "bmad" / "scripts" / "setup.py"
    command([uv, "run", "--python", sys.executable, "--no-cache", str(setup),
             "--project-root", str(directory), "--skill", str(skills / "bmad"), "--root", str(skills)], directory)
    artifacts = directory / "artifacts"
    artifacts.mkdir(exist_ok=True)
    core = directory / "_bmad" / "config.toml"
    # The isolated host owns these paths; never edit the user's repository setup.
    core.write_text('[core]\nproject_name = "Cairn design"\noutput_folder = "' + artifacts.as_posix() + '"\nactive_initiative = "design"\n', encoding="utf-8")
    custom = directory / "_bmad" / "custom"
    custom.mkdir(exist_ok=True)
    for name in BMAD_SKILLS:
        section = "agent" if name == "bmad-agent-po" else "workflow"
        (custom / (name + ".toml")).write_text('[' + section + ']\nactivation_steps_prepend = ["Read cairn-design and the supplied native context. This is a human-involved web session; do not use headless defaults. PO writes native Cairn drafts instead of a ticket store."]\n', encoding="utf-8")
    marker.write_text(json.dumps(versions), encoding="utf-8")


def run_script(directory: Path, script: str, args: list[str]):
    allowed_flags = {"resolve_config.py": {"--project-root", "--key"},
        "resolve_customization.py": {"--project-root", "--skill", "--key"},
        "render_skill.py": {"--project-root", "--skill"},
        "memlog.py": {"--workspace", "--type", "--text", "--field"}}
    if script not in allowed_flags:
        raise ValueError("Only BMAD configuration, rendering and document memlog scripts are available")
    if any("\x00" in arg for arg in args):
        raise ValueError("Invalid script argument")
    if any(arg.startswith("--") and arg not in allowed_flags[script] for arg in args):
        raise ValueError("Unsupported BMAD script flag; host configuration cannot be overridden")
    for flag in ("--project-root", "--skill", "--workspace", "--output"):
        for index, arg in enumerate(args):
            if arg == flag:
                if index + 1 >= len(args):
                    raise ValueError("Missing script path")
                target = Path(args[index + 1]).resolve()
                if flag == "--project-root":
                    allowed = target == directory.resolve()
                elif flag == "--skill":
                    allowed = target in {(directory / ".claude/skills" / name).resolve() for name in BMAD_SKILLS}
                else:
                    allowed = target.is_relative_to((directory / "artifacts").resolve())
                if not allowed:
                    raise ValueError("BMAD script paths must stay in the design workspace")
            elif arg.startswith(flag + "="):
                raise ValueError("Use separate argv elements for path arguments")
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("BMAD requires uv")
    output = command([uv, "run", "--python", sys.executable, "--no-cache",
                      str(directory / "_bmad" / "scripts" / script), *args], directory).stdout
    if script == "render_skill.py":
        output += ("\nCairn host mapping: retain this workflow's requirements/slicing/readiness reasoning. "
                   "Replace ticket-store scripts, files and publication steps with the native draft and save_draft tool. "
                   "No separate ticket store, git commit or tracker publication. Questions use ask_questions. "
                   "Only the operator can approve the Cairn candidate.\n")
    return output
