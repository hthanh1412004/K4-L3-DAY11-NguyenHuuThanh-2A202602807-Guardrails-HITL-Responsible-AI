"""Local web demo for the VinBank defense-in-depth guardrails.

Run from the repository root:

    python demo_app.py

The default local mode never calls an LLM. Enable "Live OpenRouter" in the UI
to send safe, allowed prompts to the Blue agent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
STATIC_DIR = ROOT / "demo"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from google.genai import types  # noqa: E402

from agents.agent import create_blue_agent, create_red_agent_default  # noqa: E402
from agents.guards_agent import create_red_agent_advance  # noqa: E402
from assignment.audit_log import AuditLogPlugin  # noqa: E402
from assignment.monitoring import MonitoringAlert  # noqa: E402
from assignment.pipeline import is_egress_allowed  # noqa: E402
from assignment.rate_limiter import RateLimitPlugin  # noqa: E402
from core.config import (  # noqa: E402
    blue_provider_label,
    get_openrouter_api_key,
    red_provider_label,
)
from core.utils import chat_with_agent  # noqa: E402
from attacks.attacks import classify_attack_outcome  # noqa: E402
from guardrails.input_guardrails import detect_injection, topic_filter  # noqa: E402
from guardrails.output_guardrails import content_filter  # noqa: E402


def _content_text(content: types.Content | None) -> str:
    if not content or not content.parts:
        return ""
    return "".join(part.text for part in content.parts if getattr(part, "text", None))


def _local_response(prompt: str) -> str:
    """Return an explicit, non-LLM response for the zero-cost demo mode."""
    lower = prompt.casefold()
    if "interest" in lower or "lãi suất" in lower or "lai suat" in lower:
        return (
            "[LOCAL DEMO] Yêu cầu về lãi suất đã qua toàn bộ guardrails. "
            "Bật Live OpenRouter để nhận câu trả lời từ Blue LLM."
        )
    if "transfer" in lower or "chuyển" in lower or "chuyen" in lower:
        return (
            "[LOCAL DEMO] Yêu cầu chuyển khoản đã được phân loại là banking. "
            "Không có giao dịch thật nào được thực hiện."
        )
    return (
        "[LOCAL DEMO] Prompt banking hợp lệ và đã được phép đi qua. "
        "Bật Live OpenRouter nếu bạn muốn gọi model thật."
    )


class DemoEngine:
    """Stateful, thread-safe adapter around the lab's real guardrail functions."""

    def __init__(self, max_requests: int = 5, window_seconds: int = 60):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> dict:
        with getattr(self, "_lock", threading.RLock()):
            self.rate = RateLimitPlugin(self.max_requests, self.window_seconds)
            self.audit = AuditLogPlugin()
            self.monitor = MonitoringAlert(
                block_rate_threshold=0.5,
                rate_limit_hit_threshold=2,
            )
        return self.state()

    def state(self) -> dict:
        with self._lock:
            self.monitor.check_metrics()
            return {
                "model": blue_provider_label(),
                "agents": {
                    "blue": {
                        "label": "Blue · Guarded",
                        "provider": blue_provider_label(),
                    },
                    "red_default": {
                        "label": "Red · Unguarded",
                        "provider": red_provider_label("default"),
                    },
                    "red_advance": {
                        "label": "Red Advance · Strong guards",
                        "provider": red_provider_label("advance"),
                    },
                },
                "openrouter_configured": bool(get_openrouter_api_key()),
                "rate_limit": {
                    "max_requests": self.max_requests,
                    "window_seconds": self.window_seconds,
                },
                "metrics": self.monitor.snapshot(),
                "audit": list(reversed(self.audit.logs[-12:])),
            }

    def evaluate(
        self,
        prompt: str,
        user_id: str,
        live: bool,
        target: str = "blue",
    ) -> dict:
        started = time.perf_counter()
        request_id = f"demo-{uuid4().hex[:10]}"
        prompt = str(prompt or "")
        user_id = str(user_id or "demo-user").strip() or "demo-user"
        target = target if target in {"blue", "red_default", "red_advance"} else "blue"
        trace: list[dict] = []

        with self._lock:
            audit_id = self.audit.record_input(
                user_id=user_id,
                text=prompt,
                request_id=request_id,
            )
            self.monitor.total_requests += 1

            if target != "blue":
                return self._evaluate_red(
                    prompt=prompt,
                    user_id=user_id,
                    request_id=audit_id,
                    target=target,
                    live=live,
                    trace=trace,
                    started=started,
                )

            message = types.Content(
                role="user",
                parts=[types.Part.from_text(text=prompt)],
            )
            rate_result = asyncio.run(self.rate.on_user_message_callback(
                invocation_context=SimpleNamespace(user_id=user_id),
                user_message=message,
            ))
            if rate_result is not None:
                response = _content_text(rate_result)
                trace.extend([
                    {"name": "Rate limiter", "status": "blocked", "detail": "Too many requests"},
                    {"name": "Input guardrail", "status": "skipped", "detail": "Stopped upstream"},
                    {"name": "Blue LLM", "status": "skipped", "detail": "No API call"},
                    {"name": "Output guardrail", "status": "skipped", "detail": "No model output"},
                ])
                self.monitor.blocked_requests += 1
                self.monitor.rate_limit_hits += 1
                return self._finish(
                    audit_id, user_id, response, True, "rate_limiter", trace,
                    started, live, target="blue"
                )

            trace.append({
                "name": "Rate limiter",
                "status": "passed",
                "detail": f"Within {self.max_requests}/{self.window_seconds}s quota",
            })

            injection = detect_injection(prompt)
            topic = topic_filter(prompt)
            if injection == "BLOCK" or topic == "BLOCK":
                layer = (
                    "input_guardrail_injection"
                    if injection == "BLOCK"
                    else "input_guardrail_topic"
                )
                reason = (
                    "Prompt-injection signal detected"
                    if injection == "BLOCK"
                    else "Outside the allowed banking scope"
                )
                response = (
                    "Yêu cầu đã bị chặn trước khi gọi LLM. "
                    "VinBank Assistant chỉ xử lý câu hỏi ngân hàng an toàn."
                )
                trace.extend([
                    {"name": "Input guardrail", "status": "blocked", "detail": reason},
                    {"name": "Blue LLM", "status": "skipped", "detail": "No API call"},
                    {"name": "Output guardrail", "status": "skipped", "detail": "No model output"},
                ])
                self.monitor.blocked_requests += 1
                return self._finish(
                    audit_id, user_id, response, True, layer, trace,
                    started, live, target="blue"
                )

            trace.append({
                "name": "Input guardrail",
                "status": "passed",
                "detail": "Injection: ALLOW · Topic: ALLOW",
            })

            if live:
                if not get_openrouter_api_key():
                    raise RuntimeError("OPENROUTER_API_KEY chưa được cấu hình trong .env")
                # Input/rate checks already ran above. The live runner only needs the
                # real Blue instruction; output is filtered deterministically below.
                agent, runner = create_blue_agent([])
                response, _ = asyncio.run(chat_with_agent(agent, runner, prompt))
                llm_detail = blue_provider_label()
            else:
                response = _local_response(prompt)
                llm_detail = "Local simulation · zero API calls"
            trace.append({"name": "Blue LLM", "status": "passed", "detail": llm_detail})

            filtered = content_filter(response)
            if filtered["safe"]:
                trace.append({
                    "name": "Output guardrail",
                    "status": "passed",
                    "detail": "No PII or secret detected",
                })
                layer = None
                blocked = False
            else:
                response = filtered["redacted"]
                trace.append({
                    "name": "Output guardrail",
                    "status": "redacted",
                    "detail": " · ".join(filtered["issues"]),
                })
                layer = "output_guardrail"
                blocked = True
                self.monitor.blocked_requests += 1

            return self._finish(
                audit_id, user_id, response, blocked, layer, trace, started, live,
                issues=filtered["issues"], target="blue",
            )

    def _evaluate_red(
        self,
        *,
        prompt: str,
        user_id: str,
        request_id: str,
        target: str,
        live: bool,
        trace: list[dict],
        started: float,
    ) -> dict:
        """Run a prompt against Red or Red Advance with their actual lab policy."""
        is_advance = target == "red_advance"
        label = "Red Advance" if is_advance else "Red"
        trace.append({
            "name": "Target",
            "status": "passed",
            "detail": (
                "Strong input/output guardrails enabled"
                if is_advance else "Deliberately unguarded lab target"
            ),
        })

        if live:
            if is_advance:
                agent, runner = create_red_agent_advance()
            else:
                agent, runner = create_red_agent_default()
            response, _ = asyncio.run(chat_with_agent(agent, runner, prompt))
            provider_detail = red_provider_label("advance" if is_advance else "default")
        else:
            response = (
                f"[LOCAL DEMO] {label} simulation only. "
                "Enable Live API to run the actual red-team target."
            )
            provider_detail = "Local simulation · no API call"

        trace.append({
            "name": f"{label} LLM",
            "status": "passed",
            "detail": provider_detail,
        })

        if live:
            outcome = classify_attack_outcome(prompt, response, target_name=target)
        else:
            outcome = {
                "leaked": False,
                "blocked": False,
                "layer": None,
                "blocked_at": "Local simulation",
            }

        if outcome["leaked"]:
            trace.append({
                "name": "Leak detector",
                "status": "leaked",
                "detail": "Protected demo value found in response",
            })
            decision = "LEAKED"
        elif outcome["blocked"]:
            trace.append({
                "name": "Guardrail outcome",
                "status": "blocked",
                "detail": outcome["blocked_at"],
            })
            decision = "BLOCKED"
        else:
            trace.append({
                "name": "Leak detector",
                "status": "passed",
                "detail": outcome["blocked_at"],
            })
            decision = "PASSED"

        blocked = bool(outcome["blocked"])
        if blocked:
            self.monitor.blocked_requests += 1
        result = self._finish(
            request_id,
            user_id,
            response,
            blocked,
            outcome["layer"],
            trace,
            started,
            live,
            target=target,
            leaked=bool(outcome["leaked"]),
        )
        result["decision"] = decision
        return result

    def _finish(
        self,
        request_id: str,
        user_id: str,
        response: str,
        blocked: bool,
        layer: str | None,
        trace: list[dict],
        started: float,
        live: bool,
        issues: list[str] | None = None,
        target: str = "blue",
        leaked: bool = False,
    ) -> dict:
        entry = self.audit.record_output(
            user_id=user_id,
            text=response,
            blocked=blocked,
            layer=layer,
            request_id=request_id,
        )
        entry["target"] = target
        self.monitor.check_metrics()
        return {
            "request_id": request_id,
            "decision": "BLOCKED" if blocked else "ALLOWED",
            "blocked": blocked,
            "layer": layer,
            "response": response,
            "issues": issues or [],
            "target": target,
            "leaked": leaked,
            "trace": trace,
            "live": live,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "audit_entry": entry,
            "metrics": self.monitor.snapshot(),
        }

    def inspect_output(self, text: str) -> dict:
        result = content_filter(str(text or ""))
        return {**result, "original": str(text or "")}

    def inspect_egress(self, destination: str, payload: str) -> dict:
        destination = str(destination or "")
        payload = str(payload or "")
        allowed = is_egress_allowed(destination, payload)
        parsed = urlparse(destination)
        filtered = content_filter(payload)
        reasons = []
        if parsed.scheme.lower() != "https":
            reasons.append("Destination must use HTTPS")
        if parsed.hostname not in {"api.vinbank.example", "cases.vinbank.example"}:
            reasons.append("Hostname is not on the VinBank allowlist")
        if not filtered["safe"]:
            reasons.extend(filtered["issues"])
        if not allowed and not reasons:
            reasons.append("Payload contains protected internal data")
        return {
            "allowed": allowed,
            "decision": "ALLOW" if allowed else "DENY",
            "destination": destination,
            "host": parsed.hostname,
            "reasons": reasons,
        }


ENGINE = DemoEngine()


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "VinBankGuardrailDemo/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[demo] {self.address_string()} - {fmt % args}")

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid JSON request body") from exc

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            self._json(ENGINE.state())
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = static_files.get(path)
        if item is None:
            self._json({"error": "Not found"}, 404)
            return
        filename, content_type = item
        target = STATIC_DIR / filename
        if not target.is_file():
            self._json({"error": f"Missing demo asset: {filename}"}, 500)
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json()
            if self.path == "/api/evaluate":
                result = ENGINE.evaluate(
                    body.get("prompt", ""),
                    body.get("user_id", "demo-user"),
                    bool(body.get("live", False)),
                    body.get("target", "blue"),
                )
            elif self.path == "/api/output-filter":
                result = ENGINE.inspect_output(body.get("text", ""))
            elif self.path == "/api/egress":
                result = ENGINE.inspect_egress(
                    body.get("destination", ""), body.get("payload", "")
                )
            elif self.path == "/api/reset":
                result = ENGINE.reset()
            else:
                self._json({"error": "Not found"}, 404)
                return
            self._json(result)
        except Exception as exc:  # Keep provider failures readable in the demo UI.
            self._json(
                {"error": str(exc), "type": type(exc).__name__},
                status=502 if self.path == "/api/evaluate" else 400,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="VinBank guardrail web demo")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DemoHandler)
    print("\nVinBank Guardrail Demo")
    print(f"Open: http://{args.host}:{args.port}")
    print("Live API is enabled by default in the UI; switch it off for local mode.")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping demo server...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
