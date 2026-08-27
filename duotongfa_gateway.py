#!/usr/bin/env python3
"""多通阀: a local resource handoff gateway for ComfyUI and local LLMs.

The public surface is intentionally small:

* Standard local OpenAI-compatible traffic is proxied unchanged.
* One loopback-only control path, ``/__duotongfa``, coordinates render
  handoff.  ComfyTV continues to use its existing MCP interface for canvas
  operations; this gateway does not add a cloud API provider or store keys.

Run directly with ``python duotongfa_gateway.py``.  Configuration is via
environment variables documented in ``docs/DUOTONGFA.md``.
"""

from __future__ import annotations

import argparse
import base64
import collections
import http.client
import json
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Deque, Dict, Optional, Tuple
from urllib.parse import urlparse, urlsplit, urlunsplit

try:
    from .duotongfa_runtime import (
        PROJECT_NAME,
        RUNTIME_VERSION,
        BackendAdapter,
        GatewayConfig,
        build_adapter,
        memory_snapshot,
        release_ready,
        wait_for_release,
    )
except ImportError:
    from duotongfa_runtime import (
        PROJECT_NAME,
        RUNTIME_VERSION,
        BackendAdapter,
        GatewayConfig,
        build_adapter,
        memory_snapshot,
        release_ready,
        wait_for_release,
    )


CONTROL_PATH = "/__duotongfa"
MODEL_PATH_SUFFIXES = ("/models",)
MODEL_SELECTION_PATH_SUFFIXES = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/api/v1/chat",
)
MODEL_REQUEST_PATH_SUFFIXES = MODEL_SELECTION_PATH_SUFFIXES + ("/embeddings",)
MODEL_UNLOAD_PATH_SUFFIXES = ("/api/v1/models/unload",)
_EMPTY_MODELS_RESPONSE = b'{"object":"list","data":[]}'
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

logging.basicConfig(
    level=getattr(logging, os.environ.get("DUOTONGFA_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
_log = logging.getLogger("duotongfa")


def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A fixed ``.tmp`` sibling is unsafe here: the gateway uses a threaded
    # HTTP server, so concurrent heartbeats/status requests can otherwise
    # replace or remove each other's temporary file.  A unique file in the
    # same directory preserves atomic rename semantics without serializing
    # unrelated requests.
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_loopback(host: str) -> bool:
    return str(host or "").strip("[]").lower() in {"127.0.0.1", "localhost", "::1"}


def _model_path(path: str) -> bool:
    clean = urlparse(str(path or "")).path.rstrip("/")
    return any(clean.endswith(suffix) for suffix in MODEL_PATH_SUFFIXES)


def _model_selection_path(path: str) -> bool:
    clean = urlparse(str(path or "")).path.rstrip("/")
    return any(clean.endswith(suffix) for suffix in MODEL_SELECTION_PATH_SUFFIXES)


def _force_request_model(
    method: str,
    path: str,
    body: Optional[bytes],
    forced_model: str,
) -> Tuple[Optional[bytes], bool, bool]:
    """Apply the optional local model policy to generation requests only.

    Embedding requests deliberately keep their own model selection.  The
    policy is configuration-driven so a deployment can pin today's known
    model without making the gateway itself model-specific.
    """
    selected = str(forced_model or "").strip()
    if method != "POST" or not selected or not _model_selection_path(path):
        return body, False, False
    if not body:
        raise ValueError("forced model policy requires a JSON request body")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("forced model policy requires a valid JSON request body") from exc
    if not isinstance(payload, dict):
        raise ValueError("forced model policy requires a JSON object request body")
    changed = payload.get("model") != selected
    payload["model"] = selected
    rewritten = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return rewritten, True, changed


def _model_operation(path: str, body: Optional[bytes]) -> Dict[str, Any]:
    """Identify model-bound requests for safe cold loads and model switches."""
    clean = urlparse(str(path or "")).path.rstrip("/")
    if not body:
        return {"model": "", "exclusive": False, "unload": False}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"model": "", "exclusive": False, "unload": False}
    if not isinstance(payload, dict):
        return {"model": "", "exclusive": False, "unload": False}
    if any(clean.endswith(suffix) for suffix in MODEL_UNLOAD_PATH_SUFFIXES):
        model = str(payload.get("instance_id") or payload.get("model") or "").strip()
        return {"model": model, "exclusive": bool(model), "unload": bool(model)}
    if any(clean.endswith(suffix) for suffix in MODEL_REQUEST_PATH_SUFFIXES):
        model = str(payload.get("model") or "").strip()
        return {"model": model, "exclusive": False, "unload": False}
    return {"model": "", "exclusive": False, "unload": False}


def _model_cache_key(path: str) -> str:
    """Normalize the two supported OpenAI model-discovery paths.

    Clients may be configured with either a gateway root or a ``/v1`` base.
    Both paths describe the same upstream model list, so keeping separate
    cache entries would turn a valid cold-cache hit into an unnecessary miss.
    """
    clean = urlparse(str(path or "")).path.rstrip("/")
    if clean in {"/models", "/v1/models"}:
        return "/models"
    return clean


def _upstream_request_url(upstream: str, request_path: str) -> str:
    """Join a gateway request to either a server root or a ``/v1`` base.

    ComfyTV normally stores an OpenAI-compatible base ending in ``/v1`` while
    LM Studio is often configured as a host root.  Supporting both avoids the
    accidental ``/v1/v1`` requests that otherwise look like backend outages.
    """
    base = urlsplit(str(upstream or "").rstrip("/"))
    incoming = urlsplit(str(request_path or "/"))
    base_path = base.path.rstrip("/")
    incoming_path = incoming.path or "/"
    if base_path.endswith("/v1") and incoming_path.startswith("/v1/"):
        path = f"{base_path[:-3]}{incoming_path}"
    else:
        path = f"{base_path}{incoming_path}"
    return urlunsplit((base.scheme, base.netloc, path or "/", incoming.query, ""))


def _json_url(url: str, timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    connection = None
    try:
        connection, response = _open_upstream_response(
            "GET", url, {"Accept": "application/json"}, None, timeout,
        )
        if not 200 <= int(response.status) < 300:
            return None
        payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None
    finally:
        if connection is not None:
            connection.close()


def _open_upstream_response(
    method: str,
    url: str,
    headers: Dict[str, str],
    body: Optional[bytes],
    timeout: Optional[float],
) -> Tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    """Use a standard HTTP client that interoperates reliably with LM Studio.

    LM Studio's local Express server can close CPython ``urllib`` connections,
    while accepting equivalent ``http.client``/browser/aiohttp requests.
    This remains dependency-free and works across supported platforms.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid upstream URL: {url}")
    connection_type = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = connection_type(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        timeout=timeout,
    )
    request_path = parsed.path or "/"
    if parsed.query:
        request_path = f"{request_path}?{parsed.query}"
    connection.request(method, request_path, body=body, headers=headers)
    return connection, connection.getresponse()


class Coordinator:
    def __init__(self, config: GatewayConfig, adapter: Optional[BackendAdapter] = None):
        self.config = config
        self.adapter = adapter or build_adapter(config)
        self.state_file = config.state_dir / "state.json"
        self.cache_file = config.state_dir / "model_cache.json"

        self._condition = threading.Condition(threading.RLock())
        self._render_lock_guard = threading.RLock()
        self._render_condition = threading.Condition(self._render_lock_guard)
        self._cache_guard = threading.RLock()
        self._events_guard = threading.Lock()
        self._state = "off"
        self._active_requests = 0
        self._queued_requests = 0
        self._last_activity = time.monotonic()
        self._started_by_gateway = False
        self._request_slots = (
            threading.BoundedSemaphore(config.max_concurrent_requests)
            if config.max_concurrent_requests > 0
            else None
        )
        self._model_condition = threading.Condition(threading.RLock())
        self._active_model: Optional[str] = None
        self._model_active_requests = 0
        self._model_warming = False
        self._warm_model: Optional[str] = None
        self._render_lock: Optional[Dict[str, Any]] = None
        self._model_cache: Dict[str, Tuple[str, bytes]] = {}
        self._events: Deque[Dict[str, Any]] = collections.deque(maxlen=100)
        self._metrics: Dict[str, Any] = {
            "requests": 0,
            "starts": 0,
            "stops": 0,
            "render_handoffs": 0,
            "blocked_requests": 0,
            "watchdog_releases": 0,
            "unexpected_wakeups": 0,
            "model_overrides": 0,
            "forward_recoveries": 0,
            "model_warmups": 0,
        }
        self._stop_event = threading.Event()
        self._load_state()
        self._load_cache()
        self._record("gateway_initialized", provider=config.provider)

    def _record(self, event: str, **fields: Any) -> None:
        entry = {"time": time.time(), "event": event, **fields}
        with self._events_guard:
            self._events.append(entry)
        _log.info("%s: %s %s", PROJECT_NAME, event, fields or "")

    def _state_snapshot(self) -> Dict[str, Any]:
        with self._condition:
            state = self._state
            active = self._active_requests
            queued = self._queued_requests
            owned = self._started_by_gateway
        with self._model_condition:
            active_model = self._active_model
            model_active_requests = self._model_active_requests
            warm_model = self._warm_model
            model_warming = self._model_warming
        with self._render_lock_guard:
            render_lock = dict(self._render_lock) if self._render_lock else None
        return {
            "project": PROJECT_NAME,
            "version": RUNTIME_VERSION,
            "updated_at": time.time(),
            "gateway_state": state,
            "active_requests": active,
            "queued_requests": queued,
            "started_by_gateway": owned,
            "render_lock": render_lock,
            "active_model": active_model,
            "model_active_requests": model_active_requests,
            "warm_model": warm_model,
            "model_warming": model_warming,
        }

    def _persist_state(self) -> None:
        try:
            _atomic_json(self.state_file, self._state_snapshot())
        except Exception as exc:
            _log.warning("could not persist state: %s", exc)

    def _load_state(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            _log.warning("ignored invalid state file: %s", exc)
            return
        lock = payload.get("render_lock")
        if isinstance(lock, dict) and lock.get("token"):
            age = time.time() - float(lock.get("updated_at") or lock.get("created_at") or 0)
            if age < self.config.render_watchdog_seconds:
                self._render_lock = lock
                self._record("render_lock_recovered", job_id=lock.get("job_id"))
        # Process ownership never survives a gateway restart.  Adopting it
        # would let a new process stop a service it did not start.
        self._started_by_gateway = False

    def _persist_cache(self) -> None:
        try:
            with self._cache_guard:
                payload = {
                    path: {
                        "content_type": content_type,
                        "payload": base64.b64encode(body).decode("ascii"),
                    }
                    for path, (content_type, body) in self._model_cache.items()
                }
            _atomic_json(self.cache_file, payload)
        except Exception as exc:
            _log.warning("could not persist model cache: %s", exc)

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            _log.warning("ignored invalid model cache: %s", exc)
            return
        if not isinstance(payload, dict):
            return
        for path, item in payload.items():
            if not isinstance(item, dict):
                continue
            try:
                body = base64.b64decode(str(item.get("payload") or ""), validate=True)
            except Exception:
                continue
            if body:
                self._model_cache[_model_cache_key(str(path))] = (
                    str(item.get("content_type") or "application/json"),
                    body,
                )

    def cached_model_response(self, path: str) -> Optional[Tuple[str, bytes]]:
        clean = _model_cache_key(path)
        with self._cache_guard:
            return self._model_cache.get(clean)

    def cache_model_response(self, path: str, content_type: str, body: bytes) -> None:
        clean = _model_cache_key(path)
        with self._cache_guard:
            self._model_cache[clean] = (content_type, body)
        self._persist_cache()

    def render_lock(self) -> Optional[Dict[str, Any]]:
        with self._render_lock_guard:
            return dict(self._render_lock) if self._render_lock else None

    def begin_request(self) -> None:
        if self._request_slots is not None:
            with self._condition:
                self._queued_requests += 1
            self._request_slots.acquire()
            with self._condition:
                self._queued_requests = max(0, self._queued_requests - 1)
        with self._condition:
            self._active_requests += 1
            self._last_activity = time.monotonic()
            self._metrics["requests"] += 1

    def end_request(self) -> None:
        with self._condition:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_activity = time.monotonic()
            self._condition.notify_all()
        if self._request_slots is not None:
            self._request_slots.release()

    def begin_model_request(
        self,
        model: str,
        *,
        exclusive: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Serialize cold loads/model switches while allowing warm-model traffic."""
        model = str(model or "").strip()
        if not model:
            return None
        with self._model_condition:
            while True:
                if exclusive:
                    if self._model_active_requests == 0:
                        self._active_model = model
                        self._model_active_requests = 1
                        return {"model": model, "leader": False, "exclusive": True}
                elif self._active_model not in {None, model} or self._model_warming:
                    pass
                else:
                    if self._active_model is None:
                        self._active_model = model
                    leader = self._warm_model != model
                    if leader:
                        self._model_warming = True
                        with self._condition:
                            self._metrics["model_warmups"] += 1
                    self._model_active_requests += 1
                    return {
                        "model": model,
                        "leader": leader,
                        "exclusive": False,
                    }
                self._model_condition.wait(timeout=0.25)

    def end_model_request(
        self,
        token: Optional[Dict[str, Any]],
        *,
        success: bool,
    ) -> None:
        if not token:
            return
        model = str(token.get("model") or "")
        with self._model_condition:
            if token.get("leader"):
                if success:
                    self._warm_model = model
                self._model_warming = False
            elif not success and self._warm_model == model:
                self._warm_model = None
            self._model_active_requests = max(0, self._model_active_requests - 1)
            if self._model_active_requests == 0:
                self._active_model = None
            self._model_condition.notify_all()

    def prepare_model(self, token: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not token or not token.get("leader"):
            return {"ok": True, "prepared": False}
        prepare = getattr(self.adapter, "prepare_model", None)
        if not callable(prepare):
            return {"ok": True, "prepared": False}
        result = prepare(str(token.get("model") or ""))
        self._record(
            "model_profile_ready",
            model=token.get("model"),
            already_loaded=bool(result.get("already_loaded")),
        )
        return result

    def mark_model_cold(self, model: str = "") -> None:
        model = str(model or "").strip()
        with self._model_condition:
            if not model or self._warm_model == model:
                self._warm_model = None
            self._model_condition.notify_all()

    def mark_blocked(self) -> None:
        with self._condition:
            self._metrics["blocked_requests"] += 1

    def mark_model_override(self) -> None:
        with self._condition:
            self._metrics["model_overrides"] += 1

    def recover_backend_for_retry(self) -> bool:
        """Restart a backend that vanished after the initial readiness check."""
        if self.adapter.healthy():
            return False
        self.ensure_on()
        with self._condition:
            self._metrics["forward_recoveries"] += 1
        self._record("backend_recovered", reason="forward_retry")
        return True

    def ensure_on(self) -> Dict[str, Any]:
        if self.render_lock():
            raise RuntimeError("Local LLM is reserved for an active render")
        with self._condition:
            while self._state in {"starting", "stopping"}:
                self._condition.wait(timeout=1.0)
            if self._state == "on" and self.adapter.healthy():
                return {"ok": True, "started": False}
            if self.config.resume_policy == "never":
                raise RuntimeError("Local LLM resume policy is 'never'; start the service manually")
            self._state = "starting"
            self._condition.notify_all()
        try:
            result = self.adapter.start()
            self.mark_model_cold()
            with self._condition:
                self._state = "on"
                self._started_by_gateway = bool(result.get("started"))
                self._last_activity = time.monotonic()
                if result.get("started"):
                    self._metrics["starts"] += 1
            self._record("backend_ready", ownership=result.get("ownership", "unknown"))
            return result
        except Exception:
            with self._condition:
                self._state = "off"
                self._started_by_gateway = False
            raise
        finally:
            with self._condition:
                self._condition.notify_all()
            self._persist_state()

    def stop_backend(self, *, reason: str) -> Dict[str, Any]:
        with self._condition:
            while self._state == "stopping":
                self._condition.wait(timeout=0.25)
                if self._state != "stopping" and not self.adapter.healthy():
                    return {"ok": True, "already_stopped": True}
            deadline = time.monotonic() + self.config.stop_timeout_seconds
            while self._active_requests > 0 and time.monotonic() < deadline:
                self._condition.wait(timeout=0.25)
            if self._active_requests > 0:
                return {
                    "ok": False,
                    "reason": "active_request",
                    "message": "a Local LLM request is still active",
                }
            while self._state == "starting" and time.monotonic() < deadline:
                self._condition.wait(timeout=0.25)
            owned = self._started_by_gateway
            self._state = "stopping"
            self._condition.notify_all()
        try:
            stopped = self.adapter.stop(owned=owned)
            if not stopped.get("ok"):
                return stopped
            released = wait_for_release(self.adapter, self.config)
            if not released.get("ok"):
                return released
            with self._condition:
                self._metrics["stops"] += 1
            self._record("backend_stopped", reason=reason)
            return {"ok": True, "stop": stopped, "release": released}
        finally:
            self.mark_model_cold()
            with self._condition:
                self._state = "off"
                self._started_by_gateway = False
                self._condition.notify_all()
            self._persist_state()

    def prepare_render(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        job_id = str(payload.get("job_id") or "").strip()
        if not job_id:
            raise ValueError("job_id is required")
        with self._render_condition:
            if self._render_lock:
                if self._render_lock.get("job_id") == job_id:
                    token = str(self._render_lock.get("token") or "")
                    deadline = time.monotonic() + self.config.stop_timeout_seconds
                    while (
                        self._render_lock
                        and self._render_lock.get("token") == token
                        and not self._render_lock.get("release_verified_at")
                    ):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise RuntimeError("render prepare is still in progress")
                        self._render_condition.wait(timeout=min(remaining, 0.25))
                    if not self._render_lock or self._render_lock.get("token") != token:
                        raise RuntimeError("render prepare was cancelled")
                    return {
                        "ok": True,
                        "token": token,
                        "state": self._render_lock.get("state"),
                        "reused": True,
                        "ready": True,
                    }
                raise RuntimeError("a render lock is already held")
            was_on = self.adapter.healthy()
            token = f"render|{job_id}|{uuid.uuid4().hex}"
            self._render_lock = {
                "token": token,
                "job_id": job_id,
                "owner": str(payload.get("owner") or "local-client"),
                "stage": str(payload.get("stage") or ""),
                "workflow": str(payload.get("workflow") or ""),
                "state": "PREPARE_RENDER",
                "backend_was_on": was_on,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        self._persist_state()
        stopped = self.stop_backend(reason="render_handoff")
        if not stopped.get("ok"):
            with self._render_condition:
                self._render_lock = None
                self._render_condition.notify_all()
            self._persist_state()
            raise RuntimeError(str(stopped.get("message") or stopped.get("reason") or stopped))
        cancelled = False
        with self._render_condition:
            if not self._render_lock or self._render_lock.get("token") != token:
                cancelled = True
            else:
                self._render_lock["release_verified_at"] = time.time()
            self._render_condition.notify_all()
        self._persist_state()
        if cancelled:
            raise RuntimeError("render prepare was cancelled")
        with self._condition:
            self._metrics["render_handoffs"] += 1
        self._record("render_prepared", job_id=job_id)
        return {
            "ok": True,
            "token": token,
            "state": "PREPARE_RENDER",
            "shutdown": stopped,
        }

    def commit_render(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(payload.get("token") or "")
        with self._render_lock_guard:
            if not self._render_lock or token != self._render_lock.get("token"):
                raise RuntimeError("unknown or expired render token")
            if not self._render_lock.get("release_verified_at"):
                raise RuntimeError("render prepare is still in progress")
            self._render_lock["state"] = "RENDERING"
            self._render_lock["prompt_id"] = str(payload.get("prompt_id") or "")
            self._render_lock["updated_at"] = time.time()
            job_id = self._render_lock.get("job_id")
        self._persist_state()
        self._record("render_committed", job_id=job_id)
        return {"ok": True, "token": token, "state": "RENDERING"}

    def heartbeat_render(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        token = str(payload.get("token") or "")
        with self._render_lock_guard:
            if not self._render_lock or token != self._render_lock.get("token"):
                raise RuntimeError("unknown or expired render token")
            self._render_lock["updated_at"] = time.time()
            state = self._render_lock.get("state")
        self._persist_state()
        return {"ok": True, "state": state}

    def release_render(
        self,
        payload: Dict[str, Any],
        *,
        watchdog: bool = False,
    ) -> Dict[str, Any]:
        token = str(payload.get("token") or "")
        with self._render_condition:
            if not self._render_lock:
                return {"ok": True, "state": "IDLE", "already_released": True}
            if not watchdog and token != self._render_lock.get("token"):
                raise RuntimeError("unknown or expired render token")
            if not watchdog and not self._render_lock.get("release_verified_at"):
                raise RuntimeError("render prepare is still in progress")
            lock = dict(self._render_lock)
            self._render_lock = None
            self._render_condition.notify_all()
        self._persist_state()
        if watchdog:
            with self._condition:
                self._metrics["watchdog_releases"] += 1
        self._record(
            "render_released",
            job_id=lock.get("job_id"),
            reason="watchdog" if watchdog else str(payload.get("reason") or "client"),
        )
        resumed: Dict[str, Any] = {"attempted": False}
        if self.config.resume_policy == "restore" and lock.get("backend_was_on"):
            try:
                resumed = {"attempted": True, **self.ensure_on()}
            except Exception as exc:
                resumed = {"attempted": True, "ok": False, "error": str(exc)}
        return {
            "ok": True,
            "state": "IDLE",
            "job_id": lock.get("job_id"),
            "resume": resumed,
        }

    def status(self) -> Dict[str, Any]:
        state = self._state_snapshot()
        # The persisted lock needs its capability token for crash recovery, but
        # diagnostics never need to disclose it to a caller.
        public_lock = state.get("render_lock")
        if isinstance(public_lock, dict) and public_lock.get("token"):
            public_lock = dict(public_lock)
            public_lock["token"] = "configured"
            state["render_lock"] = public_lock
        with self._events_guard:
            events = list(self._events)[-20:]
        with self._condition:
            metrics = dict(self._metrics)
        state.update({
            "control_path": CONTROL_PATH,
            "backend": self.adapter.status(),
            "memory": memory_snapshot(),
            "metrics": metrics,
            "events": events,
            "config": self.config.public_dict(),
        })
        return state

    def control(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        action = str(payload.get("action") or "status").strip().lower().replace("/", ".")
        aliases = {
            "render.prepare": "render.prepare",
            "prepare_render": "render.prepare",
            "render.commit": "render.commit",
            "commit_render": "render.commit",
            "render.heartbeat": "render.heartbeat",
            "heartbeat_render": "render.heartbeat",
            "render.release": "render.release",
            "release_render": "render.release",
            "service.up": "service.up",
            "service.start": "service.up",
            "service.down": "service.down",
            "service.stop": "service.down",
            "status": "status",
        }
        action = aliases.get(action, action)
        if action == "status":
            return self.status()
        if action == "render.prepare":
            return self.prepare_render(payload)
        if action == "render.commit":
            return self.commit_render(payload)
        if action == "render.heartbeat":
            return self.heartbeat_render(payload)
        if action == "render.release":
            return self.release_render(payload)
        if action == "service.up":
            return self.ensure_on()
        if action == "service.down":
            return self.stop_backend(reason="manual")
        raise ValueError(f"unknown control action: {action}")

    def _prompt_terminal(self, prompt_id: str) -> Optional[bool]:
        if not prompt_id:
            return None
        history = _json_url(f"{self.config.comfyui_url}/history/{prompt_id}")
        if history is None:
            # ComfyUI did not answer; keep the lock conservative.
            return None
        if prompt_id in history:
            entry = history.get(prompt_id) or {}
            status = entry.get("status") or {}
            if status.get("completed") is True:
                return True
            status_text = str(status.get("status_str") or "").lower()
            if status_text in {"success", "error", "interrupted"}:
                return True
            return False
        queue = _json_url(f"{self.config.comfyui_url}/queue")
        if queue is None:
            # ComfyUI did not answer; keep the lock conservative.
            return None
        for key in ("queue_running", "queue_pending"):
            for item in queue.get(key) or []:
                if isinstance(item, (list, tuple)) and prompt_id in item:
                    return False
        # ComfyUI answered both queries and tracks the prompt in neither
        # history nor the queue: the render can no longer be observed, so
        # release the lock now instead of waiting for the watchdog.
        return True

    def _comfyui_queue_busy(self) -> Optional[bool]:
        """Return whether ComfyUI has queued work, or None when unreachable."""
        queue = _json_url(f"{self.config.comfyui_url}/queue")
        if queue is None:
            return None
        return any(queue.get(key) for key in ("queue_running", "queue_pending"))

    def monitor_once(self) -> None:
        lock = self.render_lock()
        if lock:
            if lock.get("release_verified_at"):
                ready, snapshot = release_ready(self.adapter, self.config)
                if not ready:
                    with self._condition:
                        self._metrics["unexpected_wakeups"] += 1
                    self._record(
                        "backend_reappeared_during_render",
                        job_id=lock.get("job_id"),
                        backend=snapshot.get("backend"),
                    )
                    stopped = self.stop_backend(reason="render_lock_reassert")
                    if not stopped.get("ok"):
                        self._record(
                            "render_lock_reassert_failed",
                            job_id=lock.get("job_id"),
                            error=stopped,
                        )
            prompt_id = str(lock.get("prompt_id") or "")
            terminal = self._prompt_terminal(prompt_id)
            age = time.time() - float(lock.get("updated_at") or lock.get("created_at") or 0)
            release_reason = ""
            if terminal is True:
                release_reason = "prompt_terminal"
            elif not prompt_id:
                queue_busy = self._comfyui_queue_busy()
                if queue_busy is True:
                    changed = False
                    with self._render_lock_guard:
                        current = self._render_lock
                        if (
                            current
                            and current.get("token") == lock.get("token")
                            and not current.get("queue_seen")
                        ):
                            current["queue_seen"] = True
                            current["queue_seen_at"] = time.time()
                            changed = True
                    if changed:
                        self._record(
                            "render_queue_detected",
                            job_id=lock.get("job_id"),
                        )
                        self._persist_state()
                elif queue_busy is False:
                    if lock.get("queue_seen"):
                        release_reason = "queue_drained"
                    elif age >= self.config.orphan_render_grace_seconds:
                        release_reason = "orphan_no_queue"
            if not release_reason and age >= self.config.render_watchdog_seconds:
                release_reason = "timeout"
            if release_reason:
                self.release_render(
                    {"token": lock.get("token"), "reason": release_reason},
                    watchdog=(release_reason == "timeout"),
                )
            return
        with self._condition:
            idle = time.monotonic() - self._last_activity
            should_stop = (
                self._state == "on"
                and self._active_requests == 0
                and self._started_by_gateway
                and self.config.idle_seconds > 0
                and idle >= self.config.idle_seconds
            )
        if should_stop:
            result = self.stop_backend(reason="idle_timeout")
            if not result.get("ok"):
                self._record("idle_stop_failed", error=result)

    def monitor_forever(self) -> None:
        while not self._stop_event.wait(self.config.monitor_interval_seconds):
            try:
                self.monitor_once()
            except Exception as exc:
                _log.exception("monitor error: %s", exc)

    def stop_monitor(self) -> None:
        self._stop_event.set()


class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, coordinator: Coordinator):
        self.coordinator = coordinator
        super().__init__(address, handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def coordinator(self) -> Coordinator:
        return self.server.coordinator  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        _log.info("%s %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length") or "0"
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > self.coordinator.config.max_body_bytes:
            raise ValueError("request body exceeds DUOTONGFA_MAX_BODY_BYTES")
        return self.rfile.read(length) if length else b""

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _control_authorized(self) -> bool:
        remote = str(self.client_address[0] or "")
        if _is_loopback(remote):
            return True
        token = self.coordinator.config.control_token
        supplied = str(self.headers.get("X-Duotongfa-Token") or "")
        return bool(token and supplied == token)

    def _handle_control(self, method: str) -> bool:
        if urlparse(self.path).path.rstrip("/") != CONTROL_PATH:
            return False
        if not self._control_authorized():
            self._send_json({"ok": False, "error": "control access denied"}, status=403)
            return True
        try:
            if method == "GET":
                payload: Dict[str, Any] = {"action": "status"}
            elif method == "POST":
                payload = json.loads(self._read_body().decode("utf-8") or "{}")
                if not isinstance(payload, dict):
                    raise ValueError("control payload must be an object")
            else:
                self._send_json({"ok": False, "error": "method not allowed"}, status=405)
                return True
            result = self.coordinator.control(payload)
            status = 200 if result.get("ok", True) else 409
            self._send_json(result, status=status)
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            self._send_json({"ok": False, "error": str(exc)}, status=409)
        return True

    def _send_render_busy(self, lock: Dict[str, Any]) -> None:
        self.coordinator.mark_blocked()
        self._send_json({
            "error": {
                "message": "Local LLM is temporarily unavailable while 多通阀 is rendering",
                "type": "duotongfa_render_lock",
                "job_id": lock.get("job_id"),
                "state": lock.get("state"),
            }
        }, status=423)

    def _send_model_discovery(
        self,
        *,
        state: str,
        cached: Optional[Tuple[str, bytes]],
    ) -> None:
        """Serve model discovery without ever waking a cold backend."""
        if cached:
            content_type, payload = cached
            cache_state = "HIT"
        else:
            content_type = "application/json; charset=utf-8"
            payload = _EMPTY_MODELS_RESPONSE
            cache_state = "MISS"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Duotongfa-State", state)
        self.send_header("X-Duotongfa-Cache", cache_state)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _forward(self, method: str, body: Optional[bytes]) -> None:
        lock = self.coordinator.render_lock()
        if lock:
            if method == "GET" and _model_path(self.path):
                self._send_model_discovery(
                    state="RENDERING",
                    cached=self.coordinator.cached_model_response(self.path),
                )
                return
            self._send_render_busy(lock)
            return

        # Model discovery is a UI/readiness concern, not inference.  A cold
        # gateway must answer from its persisted cache (or an explicit empty
        # OpenAI-compatible list) instead of starting LM Studio for a poll.
        if method == "GET" and _model_path(self.path) \
                and not self.coordinator.adapter.healthy():
            self._send_model_discovery(
                state="COLD",
                cached=self.coordinator.cached_model_response(self.path),
            )
            return

        request_body = body if method in {"POST", "PUT", "PATCH"} else None
        try:
            request_body, model_policy_applied, model_changed = _force_request_model(
                method,
                self.path,
                request_body,
                self.coordinator.config.forced_model,
            )
        except ValueError as exc:
            self._send_json({"error": {"message": str(exc)}}, status=400)
            return
        if model_changed:
            self.coordinator.mark_model_override()

        operation = _model_operation(self.path, request_body)
        model_token: Optional[Dict[str, Any]] = None
        request_ok = False
        unload_ok = False
        self.coordinator.begin_request()
        try:
            self.coordinator.ensure_on()
            model_token = self.coordinator.begin_model_request(
                str(operation.get("model") or ""),
                exclusive=bool(operation.get("exclusive")),
            )
            self.coordinator.prepare_model(model_token)
            url = _upstream_request_url(
                self.coordinator.config.upstream_url, self.path,
            )
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in HOP_BY_HOP_HEADERS
                and name.lower() not in {"host", "content-length"}
            }
            response = None
            connection = None
            last_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    connection, response = _open_upstream_response(
                        method, url, headers, request_body, None,
                    )
                    if int(response.status) in {502, 503, 504} and not attempt:
                        last_error = RuntimeError(
                            f"upstream returned HTTP {response.status}"
                        )
                        response.read()
                        connection.close()
                        response = None
                        connection = None
                        self.coordinator.recover_backend_for_retry()
                        time.sleep(0.5)
                        continue
                    break
                except Exception as exc:
                    last_error = exc
                    if connection is not None:
                        connection.close()
                    response = None
                    connection = None
                    if attempt:
                        break
                    try:
                        self.coordinator.recover_backend_for_retry()
                    except Exception as recovery_exc:
                        last_error = recovery_exc
                        break
                    time.sleep(0.5)
            if response is None:
                self._send_json({
                    "error": {"message": f"upstream error: {last_error or 'request failed'}"}
                }, status=502)
                return

            response_status = int(response.status)
            request_ok = response_status < 500
            unload_ok = bool(operation.get("unload")) and 200 <= response_status < 300
            content_type = response.headers.get("Content-Type", "application/json")
            streaming = "text/event-stream" in content_type.lower()
            model_discovery = method == "GET" and _model_path(self.path)
            self.send_response(response_status)
            self.send_header("Content-Type", content_type)
            if model_discovery:
                self.send_header("X-Duotongfa-State", "READY")
                self.send_header("X-Duotongfa-Cache", "REFRESHED")
            if model_policy_applied:
                self.send_header("X-Duotongfa-Model-Policy", "FORCED")
            self.send_header("Connection", "close")
            if streaming:
                self.end_headers()
                try:
                    while True:
                        chunk = response.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:
                    if connection is not None:
                        connection.close()
                self.close_connection = True
                return

            payload = response.read()
            if connection is not None:
                connection.close()
            if model_discovery and response_status == 200:
                self.coordinator.cache_model_response(self.path, content_type, payload)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True
        except ValueError as exc:
            self._send_json({"error": {"message": str(exc)}}, status=400)
        except Exception as exc:
            self._send_json({"error": {"message": str(exc)}}, status=503)
        finally:
            self.coordinator.end_model_request(model_token, success=request_ok)
            if unload_ok:
                self.coordinator.mark_model_cold(str(operation.get("model") or ""))
            self.coordinator.end_request()

    def do_GET(self) -> None:
        if not self._handle_control("GET"):
            self._forward("GET", None)

    def do_POST(self) -> None:
        if not self._handle_control("POST"):
            try:
                body = self._read_body()
            except ValueError as exc:
                self._send_json({"error": {"message": str(exc)}}, status=413)
                return
            self._forward("POST", body)

    def do_PUT(self) -> None:
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json({"error": {"message": str(exc)}}, status=413)
            return
        self._forward("PUT", body)

    def do_PATCH(self) -> None:
        try:
            body = self._read_body()
        except ValueError as exc:
            self._send_json({"error": {"message": str(exc)}}, status=413)
            return
        self._forward("PATCH", body)

    def do_DELETE(self) -> None:
        self._forward("DELETE", None)

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True


def serve(config: GatewayConfig) -> int:
    if not _is_loopback(config.listen_host) and not config.control_token:
        raise RuntimeError(
            "A non-loopback DUOTONGFA_HOST requires DUOTONGFA_CONTROL_TOKEN"
        )
    coordinator = Coordinator(config)
    monitor = threading.Thread(target=coordinator.monitor_forever, daemon=True)
    monitor.start()
    server = GatewayServer((config.listen_host, config.listen_port), Handler, coordinator)

    def stop_server(_signum=None, _frame=None):
        coordinator.stop_monitor()
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        signum = getattr(signal, name, None)
        if signum is not None:
            try:
                signal.signal(signum, stop_server)
            except (OSError, ValueError):
                pass
    _log.info(
        "%s %s listening on http://%s:%d -> %s (%s)",
        PROJECT_NAME,
        RUNTIME_VERSION,
        config.listen_host,
        config.listen_port,
        config.upstream_url,
        config.provider,
    )
    try:
        server.serve_forever()
    finally:
        coordinator.stop_monitor()
        server.server_close()
    return 0


def fetch_status(config: GatewayConfig) -> Dict[str, Any]:
    url = f"http://{config.listen_host}:{config.listen_port}{CONTROL_PATH}"
    request = urllib.request.Request(url, headers={"X-Duotongfa-Token": config.control_token})
    with urllib.request.urlopen(request, timeout=5.0) as response:
        return json.loads(response.read().decode("utf-8", "replace") or "{}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="多通阀 local resource gateway")
    parser.add_argument("command", nargs="?", choices=("serve", "status", "check"), default="serve")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args(argv)
    config = GatewayConfig.from_env()
    if args.command == "serve":
        return serve(config)
    if args.command == "status":
        payload = fetch_status(config)
    else:
        adapter = build_adapter(config)
        payload = {
            "ok": True,
            "version": RUNTIME_VERSION,
            "config": config.public_dict(),
            "backend": adapter.status(),
            "memory": memory_snapshot(),
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
