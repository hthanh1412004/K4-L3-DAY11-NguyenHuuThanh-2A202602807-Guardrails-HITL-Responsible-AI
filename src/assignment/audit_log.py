"""
Assignment 11 — Audit Log.

Records every interaction for forensics. Never blocks by itself —
other layers catch attacks; this layer makes them reviewable.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4


def default_audit_log_path() -> str:
    """Always resolve to <repo>/outputs/… (safe when cwd is src/)."""
    repo_root = Path(__file__).resolve().parents[2]
    return str(repo_root / "outputs" / "audit_log.json")


class AuditLogPlugin:
    """Framework-agnostic audit logger (wire into ADK callbacks or your pipeline)."""

    def __init__(self):
        self.name = "audit_log"
        self.logs: list[dict] = []
        self._open: dict[str, dict] = {}

    def record_input(self, *, user_id: str, text: str, request_id: str | None = None):
        """Store input metadata and return the request identifier used."""
        key = request_id or f"{user_id}-{uuid4().hex}"
        self._open[key] = {
            "request_id": key,
            "user_id": user_id,
            "input": text,
            "timestamp": utc_now_iso(),
            "started": time.perf_counter(),
        }
        return key

    def record_output(
        self,
        *,
        user_id: str,
        text: str,
        blocked: bool = False,
        layer: str | None = None,
        request_id: str | None = None,
    ):
        """Complete a pending record with the decision and elapsed time."""
        key = request_id
        pending = self._open.pop(key, None) if key else None
        if pending is None and request_id is None:
            # Backward-compatible convenience for callers that omit request_id:
            # pair with the most recent pending request for this user.
            candidates = [
                (pending_id, item)
                for pending_id, item in self._open.items()
                if item["user_id"] == user_id
            ]
            if candidates:
                key, pending = candidates[-1]
                self._open.pop(key, None)

        now = time.perf_counter()
        pending = pending or {
            "request_id": request_id or f"{user_id}-{uuid4().hex}",
            "user_id": user_id,
            "input": "",
            "timestamp": utc_now_iso(),
            "started": now,
        }
        entry = {
            "request_id": pending["request_id"],
            "timestamp": pending["timestamp"],
            "user_id": user_id,
            "input": pending["input"],
            "output": text,
            "blocked": bool(blocked),
            "layer": layer,
            "latency_ms": round(max(0.0, now - pending["started"]) * 1000, 3),
        }
        self.logs.append(entry)
        return entry

    def export_json(self, filepath: str | None = None):
        """Write logs to disk (JSON array) under repo-root ``outputs/`` by default."""
        path = Path(filepath or default_audit_log_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.logs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
