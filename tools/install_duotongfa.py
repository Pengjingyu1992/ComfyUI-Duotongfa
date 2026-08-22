#!/usr/bin/env python3
"""Install or remove the 多通阀 user service on macOS, Windows, or Linux."""

from __future__ import annotations

import argparse
import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
GATEWAY = ROOT / "duotongfa_gateway.py"
SERVICE_ID = "io.duotongfa.gateway"
RUNTIME_FILES = ("duotongfa_gateway.py", "duotongfa_runtime.py")


def _platform_id(value: Optional[str] = None) -> str:
    raw = str(value or platform.system()).lower()
    if raw.startswith("darwin") or raw.startswith("mac"):
        return "macos"
    if raw.startswith("win"):
        return "windows"
    if raw.startswith("linux"):
        return "linux"
    return raw


def runtime_dir(*, system: Optional[str] = None, home: Optional[Path] = None,
                env: Optional[Dict[str, str]] = None) -> Path:
    current = _platform_id(system)
    root = Path(home or Path.home())
    values = os.environ if env is None else env
    if current == "macos":
        return root / "Library" / "Application Support" / "duotongfa" / "runtime"
    if current == "windows":
        local = Path(values.get("LOCALAPPDATA", root / "AppData" / "Local"))
        return local / "duotongfa" / "runtime"
    if current == "linux":
        data = Path(values.get("XDG_DATA_HOME", root / ".local" / "share"))
        return data / "duotongfa" / "runtime"
    raise RuntimeError(f"Unsupported platform: {current}")


def stage_runtime(source_gateway: Path, *, system: Optional[str] = None,
                  target_dir: Optional[Path] = None) -> Path:
    """Install a stable runtime copy outside protected project folders."""
    source_root = source_gateway.parent
    destination = target_dir or runtime_dir(system=system)
    destination.mkdir(parents=True, exist_ok=True)
    for name in RUNTIME_FILES:
        source = source_root / name
        if not source.is_file():
            raise RuntimeError(f"Runtime file does not exist: {source}")
        shutil.copy2(source, destination / name)
    return destination / "duotongfa_gateway.py"


def _service_environment(args) -> Dict[str, str]:
    values = {
        "DUOTONGFA_HOST": args.host,
        "DUOTONGFA_PORT": str(args.port),
        "DUOTONGFA_PROVIDER": args.provider,
        "DUOTONGFA_UPSTREAM_URL": args.upstream,
        "DUOTONGFA_IDLE_SECONDS": str(args.idle_seconds),
        "DUOTONGFA_RESUME_POLICY": args.resume_policy,
    }
    if args.force_app_exit:
        values["DUOTONGFA_LM_STUDIO_FORCE_APP_EXIT"] = "1"
    if args.allow_external_stop:
        values["DUOTONGFA_ALLOW_EXTERNAL_STOP"] = "1"
    if args.require_process_exit:
        values["DUOTONGFA_REQUIRE_PROCESS_EXIT"] = "1"
    return values


def macos_plist(args, python: Path, gateway: Path) -> bytes:
    log_dir = Path.home() / "Library" / "Logs" / "duotongfa"
    log_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "Label": SERVICE_ID,
        "ProgramArguments": [str(python), str(gateway), "serve"],
        "EnvironmentVariables": _service_environment(args),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / "gateway.log"),
        "StandardErrorPath": str(log_dir / "gateway.log"),
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)


def linux_unit(args, python: Path, gateway: Path) -> str:
    env_lines = "\n".join(
        f'Environment="{key}={value.replace(chr(34), chr(92) + chr(34))}"'
        for key, value in _service_environment(args).items()
    )
    return (
        "[Unit]\n"
        "Description=Duotongfa local LLM resource gateway\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"{env_lines}\n"
        f"ExecStart={shlex.quote(str(python))} {shlex.quote(str(gateway))} serve\n"
        "Restart=on-failure\n"
        "RestartSec=2\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def windows_cmd(args, python: Path, gateway: Path) -> str:
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if pythonw.is_file() else python
    lines = ["@echo off"]
    lines.extend(f'set "{key}={value}"' for key, value in _service_environment(args).items())
    command = subprocess.list2cmdline([str(executable), str(gateway), "serve"])
    lines.append(f"start \"Duotongfa\" /min {command}")
    return "\r\n".join(lines) + "\r\n"


def _run(command) -> None:
    subprocess.run(command, check=False, capture_output=True, text=True)


def install(args) -> Path:
    current = _platform_id(args.system)
    python = Path(args.python).expanduser().resolve()
    gateway = Path(args.gateway).expanduser().resolve()
    if not python.is_file():
        raise RuntimeError(f"Python executable does not exist: {python}")
    if not gateway.is_file():
        raise RuntimeError(f"Gateway script does not exist: {gateway}")
    gateway = stage_runtime(gateway, system=current)

    if current == "macos":
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_ID}.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(macos_plist(args, python, gateway))
        if args.start:
            domain = f"gui/{os.getuid()}"
            _run(["launchctl", "bootout", domain, str(target)])
            _run(["launchctl", "bootstrap", domain, str(target)])
            _run(["launchctl", "kickstart", "-k", f"{domain}/{SERVICE_ID}"])
        return target

    if current == "linux":
        target = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_ID}.service"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(linux_unit(args, python, gateway), encoding="utf-8")
        if args.start:
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", target.name])
        return target

    if current == "windows":
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        target = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "duotongfa-gateway.cmd"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(windows_cmd(args, python, gateway), encoding="utf-8", newline="")
        if args.start:
            _run(["cmd", "/c", str(target)])
        return target

    raise RuntimeError(f"Unsupported platform: {current}")


def uninstall(args) -> Path:
    current = _platform_id(args.system)
    if current == "macos":
        target = Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_ID}.plist"
        if args.start:
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)])
    elif current == "linux":
        target = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_ID}.service"
        if args.start:
            _run(["systemctl", "--user", "disable", "--now", target.name])
            _run(["systemctl", "--user", "daemon-reload"])
    elif current == "windows":
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        target = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "duotongfa-gateway.cmd"
    else:
        raise RuntimeError(f"Unsupported platform: {current}")
    target.unlink(missing_ok=True)
    return target


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the 多通阀 user service")
    parser.add_argument("action", choices=("install", "uninstall"))
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--gateway", default=str(GATEWAY))
    parser.add_argument("--system", default="", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1234)
    parser.add_argument("--provider", default="lm-studio")
    parser.add_argument("--upstream", default="http://127.0.0.1:1235")
    parser.add_argument("--idle-seconds", type=float, default=300)
    parser.add_argument("--resume-policy", choices=("on-demand", "restore", "never"), default="on-demand")
    parser.add_argument("--force-app-exit", action="store_true")
    parser.add_argument("--allow-external-stop", action="store_true")
    parser.add_argument("--require-process-exit", action="store_true")
    parser.add_argument("--start", action="store_true", help="start/enable after writing the service file")
    args = parser.parse_args(argv)
    target = install(args) if args.action == "install" else uninstall(args)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
