# Changelog

## 0.2.4

- Added model-aware cold-load and model-switch coordination while preserving warm same-model concurrency.
- Added optional request concurrency limits and bounded LM Studio context, parallel-slot, and model-TTL profiles.
- Added promptless render recovery based on observed ComfyUI queue drain plus a configurable orphan grace period.
- Added LM Studio `llama-server` worker detection and installer flags for the new runtime settings.
- Preserved the 0.2.1 cold `/models` cache, forced generation-model policy, backend retry, prompt-state checks, and public render-token redaction.

## 0.2.1

- Added cold-state model discovery from the persisted cache without waking the local backend.
- Added optional forced model routing for chat/completions/responses while leaving embeddings untouched.
- Added one retry path for a backend that disappears after readiness probing.
- Made prompt-state monitoring release a render lock when ComfyUI confirms a prompt has vanished, while remaining conservative when ComfyUI is unreachable.
- Added regression coverage for cache normalization, cold discovery, forced model routing, backend recovery, and prompt terminal-state handling.

## 0.2.0

- Initial standalone open-source release.
- Provider-neutral local lifecycle gateway.
- Verified render lock, cached model discovery, ComfyUI prompt monitoring, watchdog recovery, and portable user-service installer.
- Added privacy, security, architecture, installation, testing, and contribution documentation.
