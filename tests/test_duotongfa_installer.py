from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_duotongfa", ROOT / "tools" / "install_duotongfa.py"
)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(installer)


def _args(**changes):
    values = {
        "host": "127.0.0.1",
        "port": 1234,
        "provider": "lm-studio",
        "upstream": "http://127.0.0.1:1235",
        "idle_seconds": 300,
        "resume_policy": "on-demand",
        "orphan_render_grace_seconds": 60,
        "max_concurrent_requests": 0,
        "lm_studio_context_length": 0,
        "lm_studio_parallel": 0,
        "lm_studio_model_ttl_seconds": 0,
        "force_model": "",
        "force_app_exit": False,
        "allow_external_stop": False,
        "require_process_exit": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_macos_service_uses_portable_gateway_and_no_cloud_key(tmp_path):
    data = installer.macos_plist(_args(), tmp_path / "python", tmp_path / "gateway.py")
    text = data.decode("utf-8")
    assert "io.duotongfa.gateway" in text
    assert "DUOTONGFA_UPSTREAM_URL" in text
    assert "API_KEY" not in text


def test_linux_service_has_user_restart_policy(tmp_path):
    text = installer.linux_unit(_args(), tmp_path / "python", tmp_path / "gateway.py")
    assert "Restart=on-failure" in text
    assert "DUOTONGFA_PROVIDER=lm-studio" in text
    assert "WantedBy=default.target" in text


def test_service_environment_includes_024_model_profile_and_force_policy():
    values = installer._service_environment(
        _args(
            max_concurrent_requests=2,
            lm_studio_context_length=32768,
            lm_studio_parallel=2,
            lm_studio_model_ttl_seconds=360,
            force_model="bot-model",
        )
    )
    assert values["DUOTONGFA_MAX_CONCURRENT_REQUESTS"] == "2"
    assert values["DUOTONGFA_LM_STUDIO_CONTEXT_LENGTH"] == "32768"
    assert values["DUOTONGFA_LM_STUDIO_PARALLEL"] == "2"
    assert values["DUOTONGFA_LM_STUDIO_MODEL_TTL_SECONDS"] == "360"
    assert values["DUOTONGFA_FORCE_MODEL"] == "bot-model"


def test_windows_startup_uses_pythonw_when_available(tmp_path):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    text = installer.windows_cmd(_args(), python, tmp_path / "gateway.py")
    assert "pythonw.exe" in text
    assert "DUOTONGFA_PORT=1234" in text
    assert "API_KEY" not in text


def test_runtime_is_staged_as_two_portable_files(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "installed"
    source.mkdir()
    for name in installer.RUNTIME_FILES:
        (source / name).write_text(f"# {name}\n", encoding="utf-8")
    gateway = installer.stage_runtime(
        source / "duotongfa_gateway.py", system="Linux", target_dir=target,
    )
    assert gateway == target / "duotongfa_gateway.py"
    assert (target / "duotongfa_runtime.py").is_file()
