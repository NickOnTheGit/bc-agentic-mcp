"""Server-issued integrity tokens for approvals, evidence, and knowledge reads.

The filesystem remains the durable transport for lifecycle artifacts, but it is no
longer the authority. Tokens are HMAC signed with ``BC_MCP_SECURITY_SECRET`` when
configured, or with a process-local random key otherwise. The latter deliberately
invalidates old approvals/evidence after a server restart instead of trusting files
that cannot be authenticated.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, Optional


ENV_SECURITY_SECRET = "BC_MCP_SECURITY_SECRET"
_PROCESS_SECRET = secrets.token_bytes(32)
_TOKEN_SEPARATOR = "."
_APPROVAL_STATE: Dict[tuple[str, str, str], str] = {}
_APPROVAL_STATE_LOCK = Lock()


def security_mode() -> str:
    """Describe whether tokens survive a server restart without exposing secrets."""
    return "configured" if os.environ.get(ENV_SECURITY_SECRET) else "process-local"


def _secret() -> bytes:
    configured = os.environ.get(ENV_SECURITY_SECRET)
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()
    return _PROCESS_SECRET


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_covers(covers: Any) -> str:
    """Normalize acceptance-criterion coverage for token binding."""
    return _canonical(covers)


def digest_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_fingerprint(project_root: Path) -> str:
    return digest_text(str(Path(project_root).resolve()))


def issue(kind: str, payload: Dict[str, Any]) -> str:
    body = _canonical({"kind": kind, "payload": payload}).encode("utf-8")
    encoded = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac.new(_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}{_TOKEN_SEPARATOR}{signature}"


def verify(
    token: str,
    kind: str,
    expected: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return the signed payload when valid and matching ``expected`` fields."""
    if not isinstance(token, str) or token.count(_TOKEN_SEPARATOR) != 1:
        return None
    encoded, signature = token.split(_TOKEN_SEPARATOR, 1)
    try:
        expected_signature = hmac.new(
            _secret(), encoded.encode("ascii"), hashlib.sha256
        ).hexdigest()
    except UnicodeEncodeError:
        return None
    if not encoded or not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        envelope = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict) or envelope.get("kind") != kind:
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    for key, value in (expected or {}).items():
        if payload.get(key) != value:
            return None
    return payload


def issue_approval(
    *,
    project_root: Path,
    spec_name: str,
    phase: str,
    status: str,
    artifact_path: str,
    artifact_sha256: str,
    summary: str,
    idempotency_key: str,
    recorded_at: Optional[str] = None,
) -> str:
    payload = {
        "v": 1,
        "project": project_fingerprint(project_root),
        "spec_name": spec_name,
        "phase": phase,
        "status": status,
        "artifact_path": str(Path(artifact_path).resolve()) if artifact_path else "",
        "artifact_sha256": artifact_sha256,
        "summary_sha256": digest_text(summary),
        "idempotency_key": idempotency_key,
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }
    token = issue("approval", payload)
    register_approval_token(token)
    return token


def verify_approval(
    token: str,
    *,
    project_root: Path,
    spec_name: str,
    phase: str,
    status: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    expected: Dict[str, Any] = {
        "v": 1,
        "project": project_fingerprint(project_root),
        "spec_name": spec_name,
        "phase": phase,
    }
    if status is not None:
        expected["status"] = status
    payload = verify(token, "approval", expected)
    if payload is None:
        return None
    state_key = (
        str(payload.get("project") or ""),
        str(payload.get("spec_name") or ""),
        str(payload.get("phase") or ""),
    )
    with _APPROVAL_STATE_LOCK:
        current = _APPROVAL_STATE.get(state_key)
    if current is not None and not hmac.compare_digest(current, token):
        return None
    return payload


def register_approval_token(token: str) -> bool:
    """Make the latest approval token authoritative for this running server."""
    payload = verify(token, "approval")
    if payload is None:
        return False
    state_key = (
        str(payload.get("project") or ""),
        str(payload.get("spec_name") or ""),
        str(payload.get("phase") or ""),
    )
    with _APPROVAL_STATE_LOCK:
        _APPROVAL_STATE[state_key] = token
    return True


def issue_evidence(
    *,
    project_root: Path,
    spec_name: str,
    producer: str,
    name: str,
    result: str,
    covers: Any,
    layer: str,
    evidence: str,
) -> str:
    payload = {
        "v": 1,
        "project": project_fingerprint(project_root),
        "spec_name": spec_name,
        "producer": producer,
        "name": name,
        "result": str(result),
        "covers": canonical_covers(covers),
        "layer": layer,
        "evidence_sha256": digest_text(evidence),
        "issued_at": datetime.now(timezone.utc).isoformat(),
    }
    return issue("evidence", payload)


def verify_evidence(
    token: str,
    *,
    project_root: Path,
    spec_name: str,
    name: str,
    result: str,
    covers: Any,
    layer: str,
    evidence: str,
) -> Optional[Dict[str, Any]]:
    return verify(
        token,
        "evidence",
        {
            "v": 1,
            "project": project_fingerprint(project_root),
            "spec_name": spec_name,
            "name": name,
            "result": str(result),
            "covers": canonical_covers(covers),
            "layer": layer,
            "evidence_sha256": digest_text(evidence),
        },
    )


def issue_knowledge_read(
    *,
    project_root: Path,
    spec_name: str,
    packet_id: str,
    path: str,
    article_sha256: str,
    vendor_commit: str,
) -> str:
    return issue(
        "knowledge-read",
        {
            "v": 1,
            "project": project_fingerprint(project_root),
            "spec_name": spec_name,
            "packet_id": packet_id,
            "path": path,
            "article_sha256": article_sha256,
            "vendor_commit": vendor_commit,
            "read_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def verify_knowledge_read(
    token: str,
    *,
    project_root: Path,
    spec_name: str,
    packet_id: str,
    path: str,
    article_sha256: str,
    vendor_commit: str,
) -> Optional[Dict[str, Any]]:
    return verify(
        token,
        "knowledge-read",
        {
            "v": 1,
            "project": project_fingerprint(project_root),
            "spec_name": spec_name,
            "packet_id": packet_id,
            "path": path,
            "article_sha256": article_sha256,
            "vendor_commit": vendor_commit,
        },
    )
