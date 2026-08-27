# Changelog

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
