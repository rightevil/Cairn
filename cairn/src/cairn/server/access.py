"""Separate operator and dispatcher credentials; creator text confers no authority."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from ipaddress import ip_address
from pathlib import Path

from fastapi import HTTPException, Request

from cairn.server import db

OPERATOR_COOKIE = "cairn_operator"


def local_browser(request: Request) -> bool:
    """Only literal loopback origins can bootstrap a local browser session."""
    try:
        return bool(request.client and ip_address(request.client.host).is_loopback
                    and request.url.hostname in {"localhost", "127.0.0.1", "::1"})
    except ValueError:
        return False


def browser_session(request: Request) -> str:
    # Bind to the exact origin so another local service cannot reuse this cookie.
    return hmac.new(credential("operator").encode(), str(request.base_url).encode(), "sha256").hexdigest()


def same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    return (request.headers.get("sec-fetch-site") != "cross-site"
            and (not origin or origin == str(request.base_url).rstrip("/")))


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
    if candidate and hmac.compare_digest(candidate, credential("operator")):
        return
    if (local_browser(request) and same_origin(request)
            and hmac.compare_digest(request.cookies.get(OPERATOR_COOKIE, ""), browser_session(request))):
        return
    raise HTTPException(401, "Operator token required")


def require_worker(request: Request) -> None:
    candidate = request.headers.get("x-cairn-worker-token", "")
    if not hmac.compare_digest(candidate, credential("worker")):
        raise HTTPException(401, "Dispatcher token required")


def require_reader(request: Request) -> None:
    try:
        require_operator(request)
        return
    except HTTPException:
        pass
    require_worker(request)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
