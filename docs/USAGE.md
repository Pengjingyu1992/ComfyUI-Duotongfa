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
