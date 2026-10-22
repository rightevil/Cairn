"""Separate operator and dispatcher credentials; creator text confers no authority."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request

from cairn.server import db


def credential_path(role: str) -> Path:
    assert db._db_path is not None
    return db._db_path.with_suffix(f".{role}-token")


def credential(role: str) -> str:
    override = os.environ.get(f"CAIRN_{role.upper()}_TOKEN")
    if override:
        return override
    path = credential_path(role)
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(token)
        return token


def require_operator(request: Request) -> None:
    candidate = request.headers.get("x-cairn-operator-token", "")
    if not hmac.compare_digest(candidate, credential("operator")):
        raise HTTPException(401, "Operator token required")


def require_worker(request: Request) -> None:
    candidate = request.headers.get("x-cairn-worker-token", "")
    if not hmac.compare_digest(candidate, credential("worker")):
        raise HTTPException(401, "Dispatcher token required")


def require_reader(request: Request) -> None:
    if hmac.compare_digest(request.headers.get("x-cairn-operator-token", ""), credential("operator")):
        return
    require_worker(request)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
