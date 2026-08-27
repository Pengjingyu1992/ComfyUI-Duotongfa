from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import duotongfa_gateway as gateway
import duotongfa_runtime as runtime


def _config(tmp_path, **changes):
    base = runtime.GatewayConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        upstream_url="http://127.0.0.1:65530",
        provider="custom",
        idle_seconds=300,
        start_timeout_seconds=2,
        stop_timeout_seconds=2,
        render_watchdog_seconds=120,
        monitor_interval_seconds=1,
        state_dir=tmp_path,
        allow_external_stop=False,
        resume_policy="on-demand",
        force_app_exit=False,
        require_process_exit=False,
        minimum_available_mb=0,
        stable_release_checks=1,
        max_body_bytes=1024 * 1024,
        control_token="",
        comfyui_url="http://127.0.0.1:65531",
        start_command=(),
        stop_command=(),
    )
    return replace(base, **changes)


class FakeAdapter:
    def __init__(self, healthy=True):
        self.healthy_value = healthy
        self.starts = 0
        self.stops = 0

    def healthy(self):
        return self.healthy_value

    def processes(self):
        return []

    def start(self):
        self.starts += 1
        self.healthy_value = True
        return {"ok": True, "started": True, "ownership": "gateway"}

    def stop(self, *, owned):
        self.stops += 1
        self.healthy_value = False
        return {"ok": True, "owned": owned}

    def status(self):
        return {
            "provider": "fake",
            "healthy": self.healthy_value,
            "port_open": self.healthy_value,
            "processes": [],
        }


def test_default_state_dir_follows_each_platform(tmp_path):
    assert runtime.default_state_dir(home=tmp_path, env={}, system="Darwin") == (
        tmp_path / "Library" / "Application Support" / "duotongfa"
    )
    assert runtime.default_state_dir(home=tmp_path, env={}, system="Linux") == (
        tmp_path / ".local" / "state" / "duotongfa"
    )
    assert runtime.default_state_dir(
        home=tmp_path,
        env={"LOCALAPPDATA": str(tmp_path / "Local")},
        system="Windows",
    ) == tmp_path / "Local" / "duotongfa"


def test_config_accepts_legacy_ports_but_exposes_portable_names(tmp_path):
    config = runtime.GatewayConfig.from_env(
        {
            "GW_PORT": "2468",
            "LM_PORT": "2469",
            "DUOTONGFA_STATE_DIR": str(tmp_path),
        },
        system="Linux",
    )
    assert config.listen_port == 2468
    assert config.upstream_url == "http://127.0.0.1:2469"
    assert config.state_dir == tmp_path


def test_config_reads_024_request_and_model_profile_settings(tmp_path):
    config = runtime.GatewayConfig.from_env(
        {
            "DUOTONGFA_ORPHAN_RENDER_GRACE_SECONDS": "75",
            "DUOTONGFA_MAX_CONCURRENT_REQUESTS": "2",
            "DUOTONGFA_LM_STUDIO_CONTEXT_LENGTH": "32768",
            "DUOTONGFA_LM_STUDIO_PARALLEL": "2",
            "DUOTONGFA_LM_STUDIO_MODEL_TTL_SECONDS": "360",
            "DUOTONGFA_FORCE_MODEL": "bot-model",
            "DUOTONGFA_STATE_DIR": str(tmp_path),
        },
        system="Darwin",
    )
    assert config.orphan_render_grace_seconds == 75
    assert config.max_concurrent_requests == 2
    assert config.lm_studio_context_length == 32768
    assert config.lm_studio_parallel == 2
    assert config.lm_studio_model_ttl_seconds == 360
    assert config.forced_model == "bot-model"


def test_lm_studio_prepare_model_reloads_with_bounded_profile(tmp_path, monkeypatch):
    config = _config(
        tmp_path,
        provider="lm-studio",
        lm_studio_context_length=32768,
        lm_studio_parallel=2,
        lm_studio_model_ttl_seconds=360,
    )
    adapter = runtime.LMStudioAdapter(config, env={})
    calls = []

    def fake_lms(*args, timeout=60):
        calls.append(args)
        stdout = ""
        if args == ("ps", "--json"):
            stdout = json.dumps(
                {
                    "models": [
                        {
                            "modelKey": "bot-model",
                            "identifier": "bot-model",
                            "contextLength": 131072,
                            "parallel": 4,
                            "ttlMs": 300000,
                        }
                    ]
                }
            )
        return runtime.subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(adapter, "_lms", fake_lms)
    result = adapter.prepare_model("bot-model")
    assert result["ok"] is True
    assert calls == [
        ("ps", "--json"),
        ("unload", "--all"),
        (
            "load",
            "bot-model",
            "--gpu",
            "max",
            "--context-length",
            "32768",
            "--parallel",
            "2",
            "--ttl",
            "360",
            "--identifier",
            "bot-model",
            "--yes",
        ),
    ]


def test_model_operation_detects_inference_embedding_and_unload():
    inference = gateway._model_operation(
        "/v1/chat/completions",
        json.dumps({"model": "vision-model"}).encode(),
    )
    assert inference == {
        "model": "vision-model",
        "exclusive": False,
        "unload": False,
    }
    embedding = gateway._model_operation(
        "/v1/embeddings",
        json.dumps({"model": "embedding-model"}).encode(),
    )
    assert embedding["model"] == "embedding-model"
    assert embedding["unload"] is False
    unload = gateway._model_operation(
        "/api/v1/models/unload",
        json.dumps({"instance_id": "vision-model"}).encode(),
    )
    assert unload == {
        "model": "vision-model",
        "exclusive": True,
        "unload": True,
    }


def test_cold_model_request_serializes_followers_then_allows_warm_parallelism(tmp_path):
    coordinator = gateway.Coordinator(
        _config(tmp_path, max_concurrent_requests=2),
        adapter=FakeAdapter(True),
    )
    first = coordinator.begin_model_request("vision-model")
    assert first["leader"] is True

    follower_ready = threading.Event()
    follower_release = threading.Event()

    def follower():
        token = coordinator.begin_model_request("vision-model")
        follower_ready.set()
        follower_release.wait(timeout=2)
        coordinator.end_model_request(token, success=True)

    thread = threading.Thread(target=follower)
    thread.start()
    assert follower_ready.wait(timeout=0.05) is False
    coordinator.end_model_request(first, success=True)
    assert follower_ready.wait(timeout=1) is True
    follower_release.set()
    thread.join(timeout=2)
    assert thread.is_alive() is False
    assert coordinator.status()["warm_model"] == "vision-model"


def test_config_rejects_cloud_or_unknown_provider(tmp_path):
    with pytest.raises(ValueError, match="DUOTONGFA_PROVIDER"):
        runtime.GatewayConfig.from_env(
            {
                "DUOTONGFA_PROVIDER": "cloud-api",
                "DUOTONGFA_STATE_DIR": str(tmp_path),
            }
        )


@pytest.mark.parametrize(
    ("system", "relative"),
    [
        ("Darwin", ".lmstudio/bin/lms"),
        ("Linux", ".lmstudio/bin/lms"),
        ("Windows", ".lmstudio/bin/lms.exe"),
    ],
)
def test_lms_discovery_is_cross_platform(tmp_path, system, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("stub", encoding="utf-8")
    target.chmod(0o755)
    found = runtime.resolve_lms_path(
        env={}, home=tmp_path, system=system, which=lambda _name: None
    )
    assert found == target


def test_json_command_form_is_portable():
    assert runtime.configured_command(
        {"DUOTONGFA_START_COMMAND": '["llama-server", "--port", "8080"]'},
        "DUOTONGFA_START_COMMAND",
        system="Windows",
    ) == ["llama-server", "--port", "8080"]


@pytest.mark.parametrize(
    ("upstream", "request_path", "expected"),
    [
        ("http://127.0.0.1:1235", "/v1/models", "http://127.0.0.1:1235/v1/models"),
        ("http://127.0.0.1:1235/v1", "/v1/models", "http://127.0.0.1:1235/v1/models"),
        (
            "http://127.0.0.1:1235/v1",
            "/v1/chat/completions?stream=true",
            "http://127.0.0.1:1235/v1/chat/completions?stream=true",
        ),
    ],
)
def test_upstream_join_accepts_root_or_v1_base(upstream, request_path, expected):
    assert gateway._upstream_request_url(upstream, request_path) == expected


def test_atomic_json_is_safe_for_concurrent_state_writes(tmp_path):
    target = tmp_path / "state.json"

    def write(index):
        gateway._atomic_json(target, {"index": index})

    threads = [threading.Thread(target=write, args=(index,)) for index in range(80)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert json.loads(target.read_text(encoding="utf-8"))["index"] in range(80)
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_lm_studio_stop_treats_closed_backend_as_off_without_claiming_gui(tmp_path, monkeypatch):
    config = _config(
        tmp_path,
        provider="lm-studio",
        upstream_url="http://127.0.0.1:65529",
        require_process_exit=False,
    )
    adapter = runtime.LMStudioAdapter(config, env={})
    monkeypatch.setattr(adapter, "healthy", lambda: False)
    monkeypatch.setattr(adapter, "processes", lambda: [{"name": "LM Studio"}])
    assert adapter.stop(owned=False) == {"ok": True, "already_off": True}


def test_lm_studio_process_filter_does_not_count_gateway_or_proxy(monkeypatch):
    monkeypatch.setattr(runtime, "_process_rows", lambda _system=None: iter([
        (100, "/usr/bin/python lmstudio_gateway.py"),
        (101, "/usr/bin/python claude-lmstudio-proxy.py"),
        (102, "/Applications/LM Studio.app/Contents/MacOS/LM Studio"),
        (103, "/home/tester/.lmstudio/.internal/utils/node worker.js"),
        (104, "/usr/bin/llmster --serve"),
        (105, "/bin/zsh -lc rg '/LM Studio.app/|/.lmstudio/.internal/'"),
        (106, "/tmp/.lmstudio/extensions/backends/llama.cpp/llama-server --model x.gguf"),
    ]))
    found = runtime.lm_studio_processes(system="Darwin")
    assert [item["pid"] for item in found] == [102, 103, 104, 106]


def test_lm_studio_stop_uses_server_stop_without_unload_wakeup(tmp_path, monkeypatch):
    config = _config(
        tmp_path,
        provider="lm-studio",
        upstream_url="http://127.0.0.1:65528",
        allow_external_stop=True,
        force_app_exit=True,
    )
    adapter = runtime.LMStudioAdapter(config, env={})
    calls = []

    def fake_lms(*args, timeout=60):
        calls.append(args)
        return runtime.subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(adapter, "_lms", fake_lms)
    monkeypatch.setattr(adapter, "healthy", lambda: False)
    monkeypatch.setattr(runtime, "port_is_open", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adapter, "_quit_app", lambda: {"attempted": True})
    monkeypatch.setattr(adapter, "processes", lambda: [])
    result = adapter.stop(owned=False)
    assert result["ok"] is True
    assert calls == [("server", "stop"), ("daemon", "down")]


def test_render_handoff_stops_backend_and_uses_one_token(tmp_path):
    adapter = FakeAdapter(healthy=True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)

    prepared = coordinator.control({
        "action": "render.prepare",
        "job_id": "job-1",
        "owner": "test",
        "workflow": "image",
    })
    assert prepared["ok"] is True
    assert prepared["state"] == "PREPARE_RENDER"
    assert adapter.stops == 1
    assert coordinator.render_lock()["backend_was_on"] is True

    committed = coordinator.control({
        "action": "render.commit",
        "token": prepared["token"],
        "prompt_id": "prompt-1",
    })
    assert committed["state"] == "RENDERING"

    released = coordinator.control({
        "action": "render.release",
        "token": prepared["token"],
    })
    assert released["state"] == "IDLE"
    assert adapter.starts == 0
    assert coordinator.render_lock() is None


def test_status_redacts_render_capability_token(tmp_path):
    adapter = FakeAdapter(healthy=True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "private-job"})
    assert prepared["token"].startswith("render|")
    assert coordinator.status()["render_lock"]["token"] == "configured"


def test_duplicate_prepare_waits_until_shutdown_is_verified(tmp_path):
    class SlowAdapter(FakeAdapter):
        def stop(self, *, owned):
            self.stops += 1
            import time
            time.sleep(0.15)
            self.healthy_value = False
            return {"ok": True, "owned": owned}

    adapter = SlowAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    results = []
    errors = []

    def prepare():
        try:
            results.append(coordinator.prepare_render({"job_id": "same-job"}))
        except Exception as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=prepare) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 2
    assert all(item.get("ready") or item.get("shutdown") for item in results)
    assert adapter.stops == 1


def test_commit_and_release_are_blocked_during_prepare(tmp_path):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    coordinator._render_lock = {
        "token": "pending",
        "job_id": "pending-job",
        "state": "PREPARE_RENDER",
        "created_at": 0,
        "updated_at": 0,
    }
    with pytest.raises(RuntimeError, match="still in progress"):
        coordinator.commit_render({"token": "pending"})
    with pytest.raises(RuntimeError, match="still in progress"):
        coordinator.release_render({"token": "pending"})


def test_prepare_does_not_return_a_token_after_watchdog_cancels_it(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)

    def cancel_during_stop(*, reason):
        with coordinator._render_condition:
            coordinator._render_lock = None
            coordinator._render_condition.notify_all()
        return {"ok": True, "already_off": True, "reason": reason}

    monkeypatch.setattr(coordinator, "stop_backend", cancel_during_stop)
    with pytest.raises(RuntimeError, match="cancelled"):
        coordinator.prepare_render({"job_id": "cancelled-job"})
    assert coordinator.render_lock() is None


def test_restore_policy_restarts_only_when_backend_was_previously_on(tmp_path):
    config = _config(tmp_path, resume_policy="restore")
    adapter = FakeAdapter(healthy=True)
    coordinator = gateway.Coordinator(config, adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-restore"})
    released = coordinator.release_render({"token": prepared["token"]})
    assert released["resume"]["ok"] is True
    assert adapter.starts == 1


def test_recovered_state_never_recovers_process_ownership(tmp_path):
    state = {
        "started_by_gateway": True,
        "render_lock": None,
    }
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=FakeAdapter(False))
    assert coordinator.status()["started_by_gateway"] is False


def test_watchdog_releases_stale_lock_without_starting_model(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    config = _config(tmp_path, render_watchdog_seconds=30)
    coordinator = gateway.Coordinator(config, adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-stale"})
    coordinator.commit_render({"token": prepared["token"], "prompt_id": "p"})
    with coordinator._render_lock_guard:
        coordinator._render_lock["updated_at"] -= 60
    monkeypatch.setattr(coordinator, "_prompt_terminal", lambda _prompt_id: None)
    coordinator.monitor_once()
    assert coordinator.render_lock() is None
    assert coordinator.status()["metrics"]["watchdog_releases"] == 1
    assert adapter.starts == 0


def test_promptless_render_releases_when_observed_queue_drains(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-no-prompt"})
    coordinator.commit_render({"token": prepared["token"], "prompt_id": ""})
    busy = iter((True, False))
    monkeypatch.setattr(coordinator, "_comfyui_queue_busy", lambda: next(busy))

    coordinator.monitor_once()
    assert coordinator.render_lock()["queue_seen"] is True
    coordinator.monitor_once()
    assert coordinator.render_lock() is None
    assert coordinator.status()["events"][-1]["reason"] == "queue_drained"


def test_promptless_render_without_queue_releases_after_grace(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(
        _config(tmp_path, orphan_render_grace_seconds=15),
        adapter=adapter,
    )
    prepared = coordinator.prepare_render({"job_id": "job-never-queued"})
    coordinator.commit_render({"token": prepared["token"], "prompt_id": ""})
    with coordinator._render_lock_guard:
        coordinator._render_lock["updated_at"] -= 20
    monkeypatch.setattr(coordinator, "_comfyui_queue_busy", lambda: False)

    coordinator.monitor_once()
    assert coordinator.render_lock() is None
    assert coordinator.status()["events"][-1]["reason"] == "orphan_no_queue"


def test_render_monitor_reasserts_shutdown_after_unexpected_wakeup(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-rewake"})
    assert coordinator.render_lock()["release_verified_at"] > 0
    adapter.healthy_value = True
    monkeypatch.setattr(coordinator, "_prompt_terminal", lambda _prompt_id: None)
    coordinator.monitor_once()
    assert adapter.stops == 2
    assert adapter.healthy_value is False
    assert coordinator.render_lock()["token"] == prepared["token"]
    assert coordinator.status()["metrics"]["unexpected_wakeups"] == 1


class _UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/v1/models":
            body = b'{"data":[{"id":"local-model"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _start_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def test_prompt_terminal_releases_lock_when_prompt_vanished(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-vanished"})
    coordinator.commit_render({"token": prepared["token"], "prompt_id": "gone-prompt"})

    def empty_everywhere(url, timeout=2.0):
        if "/history/" in url:
            return {}
        return {"queue_running": [], "queue_pending": []}

    monkeypatch.setattr(gateway, "_json_url", empty_everywhere)
    coordinator.monitor_once()
    assert coordinator.render_lock() is None


def test_prompt_terminal_holds_lock_when_comfyui_unreachable(tmp_path, monkeypatch):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    prepared = coordinator.prepare_render({"job_id": "job-unreachable"})
    coordinator.commit_render({"token": prepared["token"], "prompt_id": "p"})
    monkeypatch.setattr(gateway, "_json_url", lambda url, timeout=2.0: None)
    coordinator.monitor_once()
    assert coordinator.render_lock() is not None


def test_prompt_terminal_distinguishes_absent_from_unknown(tmp_path, monkeypatch):
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=FakeAdapter(True))
    responses: dict = {}

    def fake_json(url, timeout=2.0):
        return responses.get(url)

    monkeypatch.setattr(gateway, "_json_url", fake_json)
    base = coordinator.config.comfyui_url
    history_url = f"{base}/history/p"
    queue_url = f"{base}/queue"

    responses = {
        history_url: {},
        queue_url: {"queue_running": [["1", "p"]], "queue_pending": []},
    }
    assert coordinator._prompt_terminal("p") is False

    responses = {
        history_url: {},
        queue_url: {"queue_running": [], "queue_pending": []},
    }
    assert coordinator._prompt_terminal("p") is True

    responses = {history_url: None}
    assert coordinator._prompt_terminal("p") is None

    responses = {history_url: {}, queue_url: None}
    assert coordinator._prompt_terminal("p") is None

    responses = {history_url: {"p": {"status": {"completed": True}}}}
    assert coordinator._prompt_terminal("p") is True


def test_gateway_has_one_control_path_and_serves_cached_models_during_render(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    _start_server(upstream)
    upstream_port = upstream.server_address[1]
    config = _config(
        tmp_path,
        upstream_url=f"http://127.0.0.1:{upstream_port}",
    )
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(config, adapter=adapter)
    server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler, coordinator)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as response:
            assert response.headers["X-Duotongfa-State"] == "READY"
            assert response.headers["X-Duotongfa-Cache"] == "REFRESHED"
            assert json.loads(response.read())["data"][0]["id"] == "local-model"
        with urllib.request.urlopen(f"{base}{gateway.CONTROL_PATH}", timeout=3) as response:
            status = json.loads(response.read())
        assert status["control_path"] == "/__duotongfa"

        request = urllib.request.Request(
            f"{base}{gateway.CONTROL_PATH}",
            data=json.dumps({"action": "render.prepare", "job_id": "http-job"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            prepared = json.loads(response.read())
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as response:
            assert response.headers["X-Duotongfa-State"] == "RENDERING"
            assert response.headers["X-Duotongfa-Cache"] == "HIT"
            assert json.loads(response.read())["data"][0]["id"] == "local-model"

        blocked = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(blocked, timeout=3)
        assert raised.value.code == 423

        release = urllib.request.Request(
            f"{base}{gateway.CONTROL_PATH}",
            data=json.dumps({
                "action": "render.release",
                "token": prepared["token"],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(release, timeout=3) as response:
            assert json.loads(response.read())["state"] == "IDLE"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_cold_model_discovery_uses_cache_without_waking_and_normalizes_v1(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    _start_server(upstream)
    config = _config(tmp_path, upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}")
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(config, adapter=adapter)
    server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler, coordinator)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as response:
            assert json.loads(response.read())["data"][0]["id"] == "local-model"
        starts_before_cold_read = adapter.starts
        adapter.healthy_value = False

        with urllib.request.urlopen(f"{base}/models", timeout=3) as response:
            assert response.headers["X-Duotongfa-State"] == "COLD"
            assert response.headers["X-Duotongfa-Cache"] == "HIT"
            assert json.loads(response.read())["data"][0]["id"] == "local-model"
        assert adapter.starts == starts_before_cold_read
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_cold_model_discovery_returns_empty_openai_list_without_waking(tmp_path):
    adapter = FakeAdapter(False)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler, coordinator)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=3) as response:
            assert response.headers["X-Duotongfa-State"] == "COLD"
            assert response.headers["X-Duotongfa-Cache"] == "MISS"
            assert json.loads(response.read()) == {"object": "list", "data": []}
        assert adapter.starts == 0
    finally:
        server.shutdown()
        server.server_close()


def test_cold_model_cache_survives_a_gateway_restart(tmp_path):
    cached = b'{"object":"list","data":[{"id":"gemma"}]}'
    first = gateway.Coordinator(_config(tmp_path), adapter=FakeAdapter(True))
    first.cache_model_response("/v1/models", "application/json", cached)

    adapter = FakeAdapter(False)
    restarted = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    server = gateway.GatewayServer(
        ("127.0.0.1", 0), gateway.Handler, restarted)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/models", timeout=3) as response:
            assert response.headers["X-Duotongfa-State"] == "COLD"
            assert response.headers["X-Duotongfa-Cache"] == "HIT"
            assert response.read() == cached
        assert adapter.starts == 0
    finally:
        server.shutdown()
        server.server_close()


def test_inference_request_still_wakes_a_cold_backend(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    _start_server(upstream)
    config = _config(tmp_path, upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}")
    adapter = FakeAdapter(False)
    coordinator = gateway.Coordinator(config, adapter=adapter)
    server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler, coordinator)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        f"{base}/v1/embeddings", data=b"{}", headers={"Content-Type": "application/json"},
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=3)
        assert raised.value.code == 501
        assert adapter.starts == 1
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_forced_model_rewrites_generation_but_preserves_embeddings():
    original = json.dumps({"model": "client-choice", "input": "hello"}).encode()
    rewritten, applied, changed = gateway._force_request_model(
        "POST", "/v1/chat/completions", original, "pinned-model"
    )
    assert applied is True
    assert changed is True
    assert json.loads(rewritten) == {"model": "pinned-model", "input": "hello"}

    embedding_body, embedding_applied, embedding_changed = gateway._force_request_model(
        "POST", "/v1/embeddings", original, "pinned-model"
    )
    assert embedding_body == original
    assert embedding_applied is False
    assert embedding_changed is False


def test_forced_model_policy_is_forwarded_and_reported(tmp_path):
    received = []

    class CaptureHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), CaptureHandler)
    _start_server(upstream)
    config = _config(
        tmp_path,
        upstream_url=f"http://127.0.0.1:{upstream.server_address[1]}",
        forced_model="pinned-model",
    )
    coordinator = gateway.Coordinator(config, adapter=FakeAdapter(True))
    server = gateway.GatewayServer(("127.0.0.1", 0), gateway.Handler, coordinator)
    _start_server(server)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    request = urllib.request.Request(
        f"{base}/v1/chat/completions",
        data=json.dumps({"model": "client-choice", "messages": []}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.headers["X-Duotongfa-Model-Policy"] == "FORCED"
            assert json.loads(response.read()) == {"ok": True}
        assert received == [{"model": "pinned-model", "messages": []}]
        assert coordinator.status()["metrics"]["model_overrides"] == 1
        assert coordinator.status()["warm_model"] == "pinned-model"
    finally:
        server.shutdown()
        server.server_close()
        upstream.shutdown()
        upstream.server_close()


def test_forward_recovery_restarts_a_backend_that_disappeared(tmp_path):
    adapter = FakeAdapter(True)
    coordinator = gateway.Coordinator(_config(tmp_path), adapter=adapter)
    adapter.healthy_value = False

    assert coordinator.recover_backend_for_retry() is True
    assert adapter.starts == 1
    assert coordinator.status()["metrics"]["forward_recoveries"] == 1
    assert coordinator.recover_backend_for_retry() is False
