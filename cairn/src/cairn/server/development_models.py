"""Versioned development metadata attached to Cairn projects and intents."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["planner", "implementer", "reviewer", "architect"]


class DevelopmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DevelopmentProject(DevelopmentModel):
    repository: str = Field(min_length=1)
    integration_branch: str = ""
    remote: str = "origin"
    design: str = Field(min_length=1)
    milestone: str = ""
    checks: list[list[str]] = Field(default_factory=list)
    check_timeout: int = Field(default=900, ge=1)
    revision: int = Field(default=1, ge=1)
    requested_revision: int = Field(default=1, ge=1)
    state: Literal["active", "waiting", "completed"] = "active"
    wait_reason: str = ""
    wake_at: str | None = None


class WorkPackage(DevelopmentModel):
    role: Role = "implementer"
    state: Literal["proposed", "ready", "running", "waiting", "implemented", "reviewing",
                   "approved", "merged", "verified", "rejected", "superseded"] = "ready"
    revision: int = Field(ge=1)
    scope: str = ""
    acceptance: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    subject: str | None = None
    purpose: Literal["plan", "implementation", "suggestion", "arbitration", "milestone"] = "implementation"
    branch: str | None = None
    base_commit: str | None = None
    head_commit: str | None = None
    worktree: str | None = None
    pr_url: str | None = None
    merge_commit: str | None = None
    retry_at: str | None = None
    failures: int = 0
    checkpoint: str = ""
    result: dict[str, Any] = Field(default_factory=dict)


class Control(DevelopmentModel):
    kind: Literal["suggestion", "directive"]
    source: Literal["operator", "agent"]
    state: Literal["received", "accepted", "experiment", "deferred", "rejected", "applied",
                   "completed", "replaced", "revoked"] = "received"
    revision: int = 1
    scope: list[str] = Field(default_factory=list)
    replaces: str | None = None
    assessment: str = ""


class RunClaim(DevelopmentModel):
    worker: str = Field(min_length=1)
    role: Role
    revision: int
    host: str = Field(min_length=1)


class RunUpdate(DevelopmentModel):
    token: str = Field(min_length=1)
    session: str | None = None
    pid: int | None = None
    checkpoint: str | None = None


class Evidence(DevelopmentModel):
    kind: Literal["diff", "test", "artifact", "log"]
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit: str | None = None
    command: list[str] | None = None
    exit_code: int | None = None


class RunFinish(RunUpdate):
    outcome: Literal["success", "failed", "cancelled", "capacity", "blocked"]
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    detail: str = ""
    retry_at: str | None = None


class ControlRequest(DevelopmentModel):
    content: str = Field(min_length=1)
    kind: Literal["suggestion", "directive"] | None = None
    scope: list[str] = Field(default_factory=list)
    replaces: str | None = None


class OperationRequest(RunUpdate):
    key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    request: dict[str, Any]


class OperationResult(RunUpdate):
    state: Literal["unknown", "done", "absent"]
    resource_id: str | None = None
    evidence: str = Field(min_length=1)
