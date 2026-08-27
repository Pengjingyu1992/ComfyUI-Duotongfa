# Usage

Configure ComfyTV Local LLM, Codex-compatible local tooling, or another OpenAI-compatible client to use `http://127.0.0.1:1234/v1`.

Check the gateway:

```bash
python duotongfa_gateway.py check
python duotongfa_gateway.py status
```

The render integration uses one loopback endpoint:

1. `render.prepare`: stop new LLM work, drain active requests, release the backend, and return a render token.
2. Queue the ComfyUI prompt and obtain `prompt_id`.
3. `render.commit`: bind the token to `prompt_id`.
4. `render.heartbeat`: keep the lock current for long renders.
5. `render.release`: unlock on success, error, interruption, or cancellation.

Resume policies:

- `on-demand` (recommended): leave the LLM cold after rendering; restart on the next LLM request.
- `restore`: restore the pre-render LLM state.
- `never`: never restart automatically.

During rendering, `/v1/models` can return cached discovery data. Chat and embedding requests receive HTTP 423 and are never forwarded to the LLM backend.

## Model loading and request coordination

Version 0.2.4 serializes the first cold request and model switches, then allows requests for the already-warm model to run concurrently. Optional controls can bound total gateway traffic and the LM Studio model profile:

```bash
export DUOTONGFA_MAX_CONCURRENT_REQUESTS=2
export DUOTONGFA_LM_STUDIO_CONTEXT_LENGTH=32768
export DUOTONGFA_LM_STUDIO_PARALLEL=2
export DUOTONGFA_LM_STUDIO_MODEL_TTL_SECONDS=360
export DUOTONGFA_ORPHAN_RENDER_GRACE_SECONDS=60
```

All values default to `0` (provider defaults or unlimited) except the orphan render grace, which defaults to 60 seconds. A render committed without a `prompt_id` is released after ComfyUI's queue was observed and later drained; if it never appears in the queue, the orphan grace prevents a permanent lock.

## Force a deployment model

To pin generation requests to a known local model, configure the model ID before starting the gateway:

```bash
export DUOTONGFA_FORCE_MODEL="your-model-id"
python duotongfa_gateway.py serve
```

The gateway overrides `model` for `/chat/completions`, `/completions`, `/responses`, and LM Studio's `/api/v1/chat`, and returns `X-Duotongfa-Model-Policy: FORCED`. Embedding requests keep their own model. Leave the setting empty to restore general client-selected routing. Use the actual model ID returned by LM Studio `/models`; a download or display name may differ.

Do not use `lms ps`, `lms ls`, or `lms server status` as passive cold-state probes because some LM Studio releases wake their background service when those commands run. Use `python duotongfa_gateway.py status`, gateway state, port checks, or process checks instead. The cold model cache refreshes only after a successful `/models` request while the backend is READY, so a newly downloaded model appears after the next online refresh.
