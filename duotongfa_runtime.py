"""Portable runtime primitives for the Duotongfa local-model gateway.

This module deliberately has no ComfyUI or ComfyTV imports.  It can be used
by the bundled gateway, tests, or another local client on macOS, Windows, and
Linux.  No cloud provider or stored API key is involved.
"""

from __future__ import annotations

import csv
import ctypes
import http.client
import io
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


PROJECT_ID = "duotongfa"
PROJECT_NAME = "多通阀"
RUNTIME_VERSION = "0.2.4"
SUPPORTED_PROVIDERS = (
    "lm-studio",
    "ollama",
    "llama.cpp",
    "vllm",
    "llama-swap",
    "custom",
)


def _env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = str(env.get(name, "1" if default else "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(env.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except (TypeError, ValueError):
        return int(default)


def platform_id(system: Optional[str] = None) -> str:
    value = str(system or platform.system()).strip().lower()
    if value.startswith("darwin") or value.startswith("mac"):
        return "macos"
    if value.startswith("win"):
        return "windows"
    if value.startswith("linux"):
        return "linux"
    return value or "unknown"


def default_state_dir(
    *,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    system: Optional[str] = None,
) -> Path:
    values = os.environ if env is None else env
    override = str(values.get("DUOTONGFA_STATE_DIR", "")).strip()
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override)))
    root = Path(home or Path.home())
    current = platform_id(system)
    if current == "windows":
        base = str(values.get("LOCALAPPDATA", "")).strip()
        return (Path(base) if base else root / "AppData" / "Local") / PROJECT_ID
    if current == "macos":
        return root / "Library" / "Application Support" / PROJECT_ID
    xdg_state = str(values.get("XDG_STATE_HOME", "")).strip()
    return (Path(xdg_state) if xdg_state else root / ".local" / "state") / PROJECT_ID


def _split_command(raw: str, *, system: Optional[str] = None) -> List[str]:
    value = str(raw or "").strip()
    if not value:
        return []
    if value.startswith("["):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON command: {exc}") from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("A JSON command must be an array of strings")
        return [item for item in parsed if item]
    return shlex.split(value, posix=platform_id(system) != "windows")


def configured_command(
    env: Mapping[str, str],
    name: str,
    *,
    system: Optional[str] = None,
) -> List[str]:
    return _split_command(str(env.get(name, "")), system=system)


def resolve_lms_path(
    *,
    env: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
    system: Optional[str] = None,
    which=shutil.which,
) -> Optional[Path]:
    values = os.environ if env is None else env
    root = Path(home or Path.home())
    current = platform_id(system)
    executable = "lms.exe" if current == "windows" else "lms"
    candidates: List[Optional[str]] = [
        str(values.get("DUOTONGFA_LMS_PATH", "")).strip(),
        str(values.get("LMSTUDIO_LMS_PATH", "")).strip(),
        which(executable),
        str(root / ".lmstudio" / "bin" / executable),
    ]
    if current == "macos":
        candidates.extend([
            "/Applications/LM Studio.app/Contents/Resources/app/.webpack/lms",
            str(root / "Applications" / "LM Studio.app" / "Contents" / "Resources" / "app" / ".webpack" / "lms"),
        ])
    elif current == "windows":
        local_app = Path(str(values.get("LOCALAPPDATA", root / "AppData" / "Local")))
        program_files = Path(str(values.get("ProgramFiles", "C:/Program Files")))
        candidates.extend([
            str(local_app / "Programs" / "LM Studio" / "resources" / "app" / ".webpack" / executable),
            str(local_app / "LM Studio" / "bin" / executable),
            str(program_files / "LM Studio" / "resources" / "app" / ".webpack" / executable),
        ])
    else:
        candidates.extend(["/usr/local/bin/lms", "/usr/bin/lms"])

    seen = set()
    for raw in candidates:
        candidate = str(raw or "").strip().strip('"')
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        path = Path(os.path.expandvars(os.path.expanduser(candidate)))
        if path.is_file() and (current == "windows" or os.access(path, os.X_OK)):
            return path
    return None


def endpoint_host_port(url: str) -> Tuple[str, int]:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid local endpoint URL: {url}")
    return parsed.hostname, int(parsed.port or (443 if parsed.scheme == "https" else 80))


def port_is_open(host: str, port: int, timeout: float = 0.75) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def endpoint_healthy(url: str, timeout: float = 2.0) -> bool:
    base = str(url or "").rstrip("/")
    target = base if base.endswith("/models") else f"{base}/v1/models"
    if base.endswith("/v1"):
        target = f"{base}/models"
    connection = None
    try:
        parsed = urlparse(target)
        if not parsed.hostname:
            return False
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
        connection.request("GET", request_path, headers={"Accept": "application/json"})
        response = connection.getresponse()
        response.read()
        return 200 <= int(response.status) < 300
    except Exception:
        return False
    finally:
        if connection is not None:
            connection.close()


def _run(
    command: Sequence[str],
    *,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise RuntimeError("Lifecycle command is empty")
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"Could not run {command[0]}: {exc}") from exc


def _completed_detail(result: subprocess.CompletedProcess[str]) -> str:
    return str(result.stderr or result.stdout or "").strip()


def _process_rows(system: Optional[str] = None) -> Iterable[Tuple[int, str]]:
    current = platform_id(system)
    try:
        if current == "windows":
            result = _run(["tasklist", "/FO", "CSV", "/NH"], timeout=5.0)
            reader = csv.reader(io.StringIO(result.stdout or ""))
            for row in reader:
                if len(row) < 2:
                    continue
                try:
                    yield int(str(row[1]).replace(",", "")), str(row[0])
                except ValueError:
                    continue
            return
        result = _run(["ps", "-axo", "pid=,command="], timeout=5.0)
        for line in (result.stdout or "").splitlines():
            match = re.match(r"\s*(\d+)\s+(.+)", line)
            if match:
                yield int(match.group(1)), match.group(2)
    except Exception:
        return


def matching_processes(
    fragments: Sequence[str],
    *,
    system: Optional[str] = None,
) -> List[Dict[str, Any]]:
    needles = [str(item).lower() for item in fragments if str(item).strip()]
    if not needles:
        return []
    own_pid = os.getpid()
    rows = []
    for pid, command in _process_rows(system):
        lowered = command.lower()
        if pid != own_pid and any(needle in lowered for needle in needles):
            rows.append({"pid": pid, "command": command})
    return rows


def lm_studio_processes(*, system: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return LM Studio-owned processes without matching gateways/proxies.

    A substring search for ``lmstudio`` also catches scripts such as
    ``lmstudio_gateway.py``.  Treating those as model workers makes a strict
    release gate impossible to satisfy, so match only known application,
    daemon, and internal-worker signatures.
    """
    current = platform_id(system)
    rows: List[Dict[str, Any]] = []
    own_pid = os.getpid()
    for pid, command in _process_rows(system):
        if pid == own_pid:
            continue
        raw_command = command.strip().strip('"')
        executable_source = raw_command if current == "windows" else raw_command.split(" ", 1)[0]
        normalized_executable = executable_source.lower().replace("\\", "/")
        executable = Path(executable_source).name.lower()
        normalized_command = raw_command.lower().replace("\\", "/")
        matched = (
            normalized_command.startswith("/applications/lm studio.app/")
            or "/.lmstudio/.internal/" in normalized_executable
            or (
                "/.lmstudio/extensions/backends/" in normalized_executable
                and executable == "llama-server"
            )
            or "llmster" in executable
        )
        if current == "windows":
            matched = matched or executable in {
                "lm studio.exe", "lmstudio.exe", "llmster.exe"
            }
        elif current == "linux":
            matched = matched or executable in {"lm-studio", "lmstudio"}
        if matched:
            rows.append({"pid": pid, "command": command})
    return rows


def terminate_lm_studio_processes(*, timeout: float = 5.0,
                                  system: Optional[str] = None) -> Dict[str, Any]:
    """Terminate only processes positively identified as LM Studio-owned."""
    current = platform_id(system)
    targets = lm_studio_processes(system=current)
    if not targets:
        return {"attempted": False, "remaining": []}
    attempted: List[int] = []
    for item in targets:
        pid = int(item.get("pid") or 0)
        if pid <= 0:
            continue
        attempted.append(pid)
        try:
            if current == "windows":
                _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=10.0)
            else:
                os.kill(pid, signal.SIGTERM)
        except (OSError, RuntimeError):
            pass
    deadline = time.monotonic() + max(0.0, timeout)
    remaining = lm_studio_processes(system=current)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.25)
        remaining = lm_studio_processes(system=current)
    if remaining and current != "windows":
        for item in remaining:
            try:
                os.kill(int(item.get("pid") or 0), signal.SIGKILL)
            except OSError:
                pass
        time.sleep(0.25)
        remaining = lm_studio_processes(system=current)
    return {"attempted": True, "pids": attempted, "remaining": remaining}


def memory_snapshot(system: Optional[str] = None) -> Dict[str, Any]:
    current = platform_id(system)
    try:
        import psutil  # type: ignore

        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        return {
            "source": "psutil",
            "total_bytes": int(memory.total),
            "available_bytes": int(memory.available),
            "available_percent": round(float(memory.available) * 100.0 / max(1, float(memory.total)), 2),
            "swap_used_bytes": int(swap.used),
        }
    except Exception:
        pass

    if current == "windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        try:
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = int(status.ullTotalPhys)
                available = int(status.ullAvailPhys)
                return {
                    "source": "GlobalMemoryStatusEx",
                    "total_bytes": total,
                    "available_bytes": available,
                    "available_percent": round(available * 100.0 / max(1, total), 2),
                    "swap_used_bytes": max(0, int(status.ullTotalPageFile - status.ullAvailPageFile)),
                }
        except Exception:
            return {"source": "unavailable"}

    if current == "linux":
        try:
            values: Dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                match = re.search(r"(\d+)", raw)
                if match:
                    values[key] = int(match.group(1)) * 1024
            total = values.get("MemTotal", 0)
            available = values.get("MemAvailable", values.get("MemFree", 0))
            return {
                "source": "/proc/meminfo",
                "total_bytes": total,
                "available_bytes": available,
                "available_percent": round(available * 100.0 / max(1, total), 2),
                "swap_used_bytes": max(0, values.get("SwapTotal", 0) - values.get("SwapFree", 0)),
            }
        except Exception:
            return {"source": "unavailable"}

    if current == "macos":
        try:
            page_size = int(_run(["sysctl", "-n", "hw.pagesize"], timeout=3.0).stdout.strip())
            total = int(_run(["sysctl", "-n", "hw.memsize"], timeout=3.0).stdout.strip())
            vm_text = _run(["vm_stat"], timeout=3.0).stdout
            pages: Dict[str, int] = {}
            for line in vm_text.splitlines():
                if ":" not in line:
                    continue
                key, raw = line.split(":", 1)
                match = re.search(r"(\d+)", raw)
                if match:
                    pages[key.strip()] = int(match.group(1))
            available_pages = sum(
                pages.get(name, 0)
                for name in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
            )
            available = available_pages * page_size
            swap_used = 0
            swap_text = _run(["sysctl", "-n", "vm.swapusage"], timeout=3.0).stdout
            match = re.search(r"used\s*=\s*([0-9.]+)([MG])", swap_text)
            if match:
                factor = 1024 ** (3 if match.group(2) == "G" else 2)
                swap_used = int(float(match.group(1)) * factor)
            return {
                "source": "vm_stat",
                "total_bytes": total,
                "available_bytes": available,
                "available_percent": round(available * 100.0 / max(1, total), 2),
                "swap_used_bytes": swap_used,
            }
        except Exception:
            return {"source": "unavailable"}

    return {"source": "unavailable"}


@dataclass(frozen=True)
class GatewayConfig:
    listen_host: str
    listen_port: int
    upstream_url: str
    provider: str
    idle_seconds: float
    start_timeout_seconds: float
    stop_timeout_seconds: float
    render_watchdog_seconds: float
    monitor_interval_seconds: float
    state_dir: Path
    allow_external_stop: bool
    resume_policy: str
    force_app_exit: bool
    require_process_exit: bool
    minimum_available_mb: int
    stable_release_checks: int
    max_body_bytes: int
    control_token: str
    comfyui_url: str
    start_command: Tuple[str, ...]
    stop_command: Tuple[str, ...]
    forced_model: str = ""
    orphan_render_grace_seconds: float = 60.0
    max_concurrent_requests: int = 0
    lm_studio_context_length: int = 0
    lm_studio_parallel: int = 0
    lm_studio_model_ttl_seconds: int = 0

    @classmethod
    def from_env(
        cls,
        env: Optional[Mapping[str, str]] = None,
        *,
        home: Optional[Path] = None,
        system: Optional[str] = None,
    ) -> "GatewayConfig":
        values = os.environ if env is None else env
        provider = str(values.get("DUOTONGFA_PROVIDER", "lm-studio")).strip().lower()
        if provider not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"DUOTONGFA_PROVIDER must be one of: {', '.join(SUPPORTED_PROVIDERS)}"
            )
        legacy_port = _env_int(values, "LM_PORT", 1235)
        upstream = str(
            values.get("DUOTONGFA_UPSTREAM_URL", f"http://127.0.0.1:{legacy_port}")
        ).strip().rstrip("/")
        endpoint_host_port(upstream)
        resume = str(values.get("DUOTONGFA_RESUME_POLICY", "on-demand")).strip().lower()
        if resume not in {"on-demand", "restore", "never"}:
            raise ValueError("DUOTONGFA_RESUME_POLICY must be on-demand, restore, or never")
        current = platform_id(system)
        start = configured_command(values, "DUOTONGFA_START_COMMAND", system=current)
        stop = configured_command(values, "DUOTONGFA_STOP_COMMAND", system=current)
        return cls(
            listen_host=str(values.get("DUOTONGFA_HOST", values.get("GW_HOST", "127.0.0.1"))).strip(),
            listen_port=_env_int(values, "DUOTONGFA_PORT", _env_int(values, "GW_PORT", 1234)),
            upstream_url=upstream,
            provider=provider,
            idle_seconds=max(0.0, _env_float(values, "DUOTONGFA_IDLE_SECONDS", _env_float(values, "IDLE_S", 300.0))),
            start_timeout_seconds=max(1.0, _env_float(values, "DUOTONGFA_START_TIMEOUT_SECONDS", _env_float(values, "START_TIMEOUT_S", 240.0))),
            stop_timeout_seconds=max(1.0, _env_float(values, "DUOTONGFA_STOP_TIMEOUT_SECONDS", _env_float(values, "DUOTONGFA_RENDER_STOP_TIMEOUT_S", 45.0))),
            render_watchdog_seconds=max(30.0, _env_float(values, "DUOTONGFA_RENDER_WATCHDOG_SECONDS", _env_float(values, "DUOTONGFA_RENDER_WATCHDOG_S", 4 * 60 * 60))),
            monitor_interval_seconds=max(1.0, _env_float(values, "DUOTONGFA_MONITOR_INTERVAL_SECONDS", 10.0)),
            state_dir=default_state_dir(env=values, home=home, system=current),
            allow_external_stop=_env_bool(values, "DUOTONGFA_ALLOW_EXTERNAL_STOP", False),
            resume_policy=resume,
            force_app_exit=_env_bool(values, "DUOTONGFA_LM_STUDIO_FORCE_APP_EXIT", False),
            require_process_exit=_env_bool(values, "DUOTONGFA_REQUIRE_PROCESS_EXIT", False),
            minimum_available_mb=max(0, _env_int(values, "DUOTONGFA_MIN_AVAILABLE_MB", 0)),
            stable_release_checks=max(1, _env_int(values, "DUOTONGFA_STABLE_RELEASE_CHECKS", 3)),
            max_body_bytes=max(1024, _env_int(values, "DUOTONGFA_MAX_BODY_BYTES", 128 * 1024 * 1024)),
            control_token=str(values.get("DUOTONGFA_CONTROL_TOKEN", "")).strip(),
            comfyui_url=str(values.get("DUOTONGFA_COMFYUI_URL", "http://127.0.0.1:8188")).strip().rstrip("/"),
            start_command=tuple(start),
            stop_command=tuple(stop),
            forced_model=str(values.get("DUOTONGFA_FORCE_MODEL", "")).strip(),
            orphan_render_grace_seconds=max(
                15.0,
                _env_float(values, "DUOTONGFA_ORPHAN_RENDER_GRACE_SECONDS", 60.0),
            ),
            max_concurrent_requests=max(
                0, _env_int(values, "DUOTONGFA_MAX_CONCURRENT_REQUESTS", 0)
            ),
            lm_studio_context_length=max(
                0, _env_int(values, "DUOTONGFA_LM_STUDIO_CONTEXT_LENGTH", 0)
            ),
            lm_studio_parallel=max(
                0, _env_int(values, "DUOTONGFA_LM_STUDIO_PARALLEL", 0)
            ),
            lm_studio_model_ttl_seconds=max(
                0, _env_int(values, "DUOTONGFA_LM_STUDIO_MODEL_TTL_SECONDS", 0)
            ),
        )

    def public_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        result["state_dir"] = str(self.state_dir)
        result["control_token"] = "configured" if self.control_token else ""
        return result


class BackendAdapter:
    """Provider-neutral local lifecycle contract."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self._owned_process: Optional[subprocess.Popen[Any]] = None

    @property
    def provider(self) -> str:
        return self.config.provider

    def healthy(self) -> bool:
        return endpoint_healthy(self.config.upstream_url)

    def processes(self) -> List[Dict[str, Any]]:
        return []

    def start(self) -> Dict[str, Any]:
        if self.healthy():
            return {"ok": True, "started": False, "ownership": "external"}
        if not self.config.start_command:
            raise RuntimeError(
                f"{self.provider} is offline and has no DUOTONGFA_START_COMMAND"
            )
        creationflags = 0
        if platform_id() == "windows":
            creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            self._owned_process = subprocess.Popen(
                list(self.config.start_command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=platform_id() != "windows",
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"Could not start {self.provider}: {exc}") from exc
        self._wait_healthy(self.config.start_timeout_seconds)
        return {"ok": True, "started": True, "ownership": "gateway"}

    def stop(self, *, owned: bool) -> Dict[str, Any]:
        if not self.healthy() and not self.processes():
            return {"ok": True, "already_off": True}
        if not owned and not self.config.allow_external_stop:
            return {
                "ok": False,
                "reason": "external_owner",
                "message": f"{self.provider} is externally owned and was left running",
            }
        if self.config.stop_command:
            result = _run(self.config.stop_command, timeout=self.config.stop_timeout_seconds)
            if result.returncode != 0:
                raise RuntimeError(_completed_detail(result) or "stop command failed")
        elif self._owned_process is not None:
            self._owned_process.terminate()
            try:
                self._owned_process.wait(timeout=self.config.stop_timeout_seconds)
            except subprocess.TimeoutExpired:
                self._owned_process.kill()
                self._owned_process.wait(timeout=5.0)
        else:
            return {
                "ok": False,
                "reason": "no_stop_adapter",
                "message": f"{self.provider} has no safe stop command",
            }
        return {"ok": True}

    def status(self) -> Dict[str, Any]:
        host, port = endpoint_host_port(self.config.upstream_url)
        return {
            "provider": self.provider,
            "healthy": self.healthy(),
            "port_open": port_is_open(host, port),
            "processes": self.processes(),
        }

    def _wait_healthy(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"{self.provider} did not become healthy within {timeout:.0f}s"
        )


class LMStudioAdapter(BackendAdapter):
    def __init__(self, config: GatewayConfig, *, env: Optional[Mapping[str, str]] = None):
        super().__init__(config)
        self.env = os.environ if env is None else env
        self.lms_path = resolve_lms_path(env=self.env)

    def _lms(self, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
        if not self.lms_path:
            raise RuntimeError(
                "LM Studio CLI (lms) was not found. Set DUOTONGFA_LMS_PATH."
            )
        return _run([str(self.lms_path), *args], timeout=timeout)

    def processes(self) -> List[Dict[str, Any]]:
        return lm_studio_processes()

    def _loaded_models(self) -> List[Dict[str, Any]]:
        result = self._lms("ps", "--json", timeout=30.0)
        if result.returncode != 0:
            raise RuntimeError(
                _completed_detail(result) or "Could not inspect loaded LM Studio models"
            )
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("LM Studio returned invalid model status JSON") from exc
        if isinstance(payload, dict):
            payload = payload.get("models") or payload.get("data") or []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def prepare_model(self, model: str) -> Dict[str, Any]:
        """Load one model with the configured bounded context/concurrency profile."""
        model = str(model or "").strip()
        if not model:
            return {"ok": True, "configured": False, "reason": "no_model"}
        context_length = self.config.lm_studio_context_length
        parallel = self.config.lm_studio_parallel
        ttl_seconds = self.config.lm_studio_model_ttl_seconds
        if not any((context_length, parallel, ttl_seconds)):
            return {"ok": True, "configured": False, "reason": "no_profile"}

        loaded = self._loaded_models()
        selected = next(
            (
                item
                for item in loaded
                if model
                in {
                    str(item.get("identifier") or "").strip(),
                    str(item.get("modelKey") or "").strip(),
                }
            ),
            None,
        )
        matches = bool(selected)
        if selected and context_length:
            matches = matches and int(selected.get("contextLength") or 0) == context_length
        if selected and parallel:
            matches = matches and int(selected.get("parallel") or 0) == parallel
        if selected and ttl_seconds:
            matches = matches and int(selected.get("ttlMs") or 0) == ttl_seconds * 1000
        if matches and len(loaded) == 1:
            return {
                "ok": True,
                "configured": True,
                "already_loaded": True,
                "model": model,
            }

        if loaded:
            unloaded = self._lms("unload", "--all", timeout=90.0)
            if unloaded.returncode != 0:
                raise RuntimeError(
                    _completed_detail(unloaded)
                    or "Could not unload the previous LM Studio model"
                )

        args = ["load", model, "--gpu", "max"]
        if context_length:
            args.extend(["--context-length", str(context_length)])
        if parallel:
            args.extend(["--parallel", str(parallel)])
        if ttl_seconds:
            args.extend(["--ttl", str(ttl_seconds)])
        args.extend(["--identifier", model, "--yes"])
        loaded_result = self._lms(*args, timeout=self.config.start_timeout_seconds)
        if loaded_result.returncode != 0:
            raise RuntimeError(
                _completed_detail(loaded_result)
                or f"Could not load LM Studio model: {model}"
            )
        return {
            "ok": True,
            "configured": True,
            "already_loaded": False,
            "model": model,
            "context_length": context_length,
            "parallel": parallel,
            "ttl_seconds": ttl_seconds,
        }

    def start(self) -> Dict[str, Any]:
        if self.healthy():
            return {"ok": True, "started": False, "ownership": "external"}
        if self.config.start_command:
            return super().start()
        host, port = endpoint_host_port(self.config.upstream_url)
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("Automatic LM Studio lifecycle requires a loopback upstream")
        daemon = self._lms("daemon", "up", "--json", timeout=30.0)
        if daemon.returncode != 0:
            status = self._lms("daemon", "status", timeout=10.0)
            if status.returncode != 0 or "not running" in _completed_detail(status).lower():
                raise RuntimeError(_completed_detail(daemon) or "LM Studio daemon start failed")
        server = self._lms("server", "start", "-p", str(port), timeout=30.0)
        if server.returncode != 0 and not self.healthy():
            raise RuntimeError(_completed_detail(server) or "LM Studio server start failed")
        self._wait_healthy(self.config.start_timeout_seconds)
        return {"ok": True, "started": True, "ownership": "gateway"}

    def _quit_app(self) -> Dict[str, Any]:
        if not self.config.force_app_exit:
            return {"attempted": False}
        custom = configured_command(
            self.env, "DUOTONGFA_LM_STUDIO_APP_STOP_COMMAND", system=platform_id()
        )
        if custom:
            result = _run(custom, timeout=20.0)
            return {"attempted": True, "returncode": result.returncode, "mode": "configured"}
        current = platform_id()
        command: List[str] = []
        if current == "macos" and shutil.which("osascript"):
            command = ["osascript", "-e", 'tell application "LM Studio" to quit']
        elif current == "windows" and shutil.which("taskkill"):
            command = ["taskkill", "/IM", "LM Studio.exe", "/T"]
        elif current == "linux" and shutil.which("pkill"):
            command = ["pkill", "-TERM", "-f", "LM Studio|lm-studio"]
        if not command:
            return {"attempted": False, "reason": "unsupported_platform"}
        result = _run(command, timeout=20.0)
        return {"attempted": True, "returncode": result.returncode, "mode": current}

    def stop(self, *, owned: bool) -> Dict[str, Any]:
        host, port = endpoint_host_port(self.config.upstream_url)
        if not self.healthy() and not port_is_open(host, port):
            if not self.config.require_process_exit or not self.processes():
                return {"ok": True, "already_off": True}
        if not owned and not self.config.allow_external_stop:
            return {
                "ok": False,
                "reason": "external_owner",
                "message": "LM Studio is externally owned and was left running",
            }
        if self.config.stop_command:
            return super().stop(owned=owned)
        # `server stop` unloads the active model. Calling `lms unload --all`
        # first can wake the LM Studio desktop when no model is loaded and may
        # spend a full CLI timeout doing so, defeating the render handoff.
        server = self._lms("server", "stop", timeout=30.0)
        app_before_daemon = self._quit_app()
        daemon = self._lms("daemon", "down", timeout=30.0)
        # Some LM Studio desktop builds reopen while `daemon down` reconciles
        # its service. Quit once more after the final LMS command, then use a
        # PID-scoped fallback only when strict application exit was requested.
        app_after_daemon = self._quit_app()
        forced = {"attempted": False}
        if self.config.force_app_exit and self.processes():
            forced = terminate_lm_studio_processes(timeout=5.0)
        details = {
            "server_returncode": server.returncode,
            "daemon_returncode": daemon.returncode,
            "app_exit_before_daemon": app_before_daemon,
            "app_exit_after_daemon": app_after_daemon,
            "forced_process_exit": forced,
        }
        if self.healthy():
            details.update({"ok": False, "reason": "backend_still_healthy"})
            return details
        details["ok"] = True
        return details

    def status(self) -> Dict[str, Any]:
        result = super().status()
        result.update({
            "lms_path": str(self.lms_path) if self.lms_path else "",
            "force_app_exit": self.config.force_app_exit,
        })
        return result


def build_adapter(config: GatewayConfig) -> BackendAdapter:
    if config.provider == "lm-studio":
        return LMStudioAdapter(config)
    return BackendAdapter(config)


def release_ready(
    adapter: BackendAdapter,
    config: GatewayConfig,
) -> Tuple[bool, Dict[str, Any]]:
    backend = adapter.status()
    memory = memory_snapshot()
    processes = backend.get("processes") or []
    available = int(memory.get("available_bytes") or 0)
    minimum = int(config.minimum_available_mb) * 1024 * 1024
    ready = not backend.get("healthy") and not backend.get("port_open")
    if config.require_process_exit:
        ready = ready and not processes
    if minimum:
        ready = ready and available >= minimum
    return ready, {"backend": backend, "memory": memory}


def wait_for_release(
    adapter: BackendAdapter,
    config: GatewayConfig,
) -> Dict[str, Any]:
    deadline = time.monotonic() + config.stop_timeout_seconds
    stable = 0
    latest: Dict[str, Any] = {}
    while time.monotonic() < deadline:
        ready, latest = release_ready(adapter, config)
        stable = stable + 1 if ready else 0
        if stable >= config.stable_release_checks:
            return {"ok": True, "stable_checks": stable, **latest}
        time.sleep(1.0)
    return {
        "ok": False,
        "reason": "resource_not_released",
        "stable_checks": stable,
        **latest,
    }
