# Testing and known coverage

## Real machine baseline

Physical validation has covered one Apple Silicon macOS system running Comfy Desktop, PyTorch MPS, and a local LM Studio backend. Observed checks included a local LLM, render handoff, requests under lock, heartbeats, and post-render restoration. The newest application versions and other hardware configurations require full lifecycle validation.

## Automated checks

```bash
python -m pytest -q
python -m compileall -q .
```

The suite simulates macOS, Linux, and Windows service generation and tests lifecycle ownership, concurrent lock acquisition, model cache behavior, request blocking, watchdog recovery, and ComfyUI prompt-state monitoring.

## Requested community validation

Reports are especially useful for Windows + NVIDIA, Linux + NVIDIA/AMD, Intel Macs, Apple M1/M2/M4, Ollama, llama.cpp, vLLM, and llama-swap. Include OS, CPU/GPU, RAM/VRAM, Python, ComfyUI version, backend/version, model size/quantization, timings, and redacted diagnostics.
