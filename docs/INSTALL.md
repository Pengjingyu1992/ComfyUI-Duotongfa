# Installation

## Requirements

- Python 3.10 or newer
- ComfyUI and a local OpenAI-compatible LLM runtime
- Local loopback ports available (defaults: gateway `1234`, backend `1235`)

Clone the project anywhere, including `ComfyUI/custom_nodes` if you want it visible as a companion plugin. It intentionally registers no visual nodes.

```bash
git clone https://github.com/Pengjingyu1992/ComfyUI-Duotongfa.git
cd ComfyUI-Duotongfa
python tools/install_duotongfa.py install --start
```

Version 0.2.4 options can be persisted in the user service during installation, for example:

```bash
python tools/install_duotongfa.py install --start \
  --max-concurrent-requests 2 \
  --lm-studio-context-length 32768 \
  --lm-studio-parallel 2 \
  --lm-studio-model-ttl-seconds 360 \
  --force-model bot-model
```

The installer copies the two dependency-free runtime files to a stable per-user data folder, then creates:

- macOS: a user LaunchAgent
- Linux: a systemd user service
- Windows: a current-user Startup command

LM Studio low-memory example:

```bash
python tools/install_duotongfa.py install --start \
  --provider lm-studio \
  --upstream http://127.0.0.1:1235 \
  --force-app-exit \
  --allow-external-stop \
  --require-process-exit
```

Those three takeover flags are powerful: save work in LM Studio first. Without them, Duotongfa stops only a backend process that it started.

Uninstall:

```bash
python tools/install_duotongfa.py uninstall --start
```

Run without installing a service:

```bash
python duotongfa_gateway.py serve
```
