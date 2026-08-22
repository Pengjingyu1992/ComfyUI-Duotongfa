# Security and privacy

Duotongfa is local-first and defaults to loopback-only networking. It does not require or store cloud API keys. It does not persist prompts, images, audio, video, request bodies, or Authorization headers.

Runtime files stored locally are limited to current lock state, counters, timestamps, local provider status, and a model-list response cache. Lock metadata may include caller-supplied job, stage, workflow, owner, and ComfyUI prompt identifiers. These files remain in the current user's state directory and should be redacted before sharing.

Non-loopback listening is rejected unless `DUOTONGFA_CONTROL_TOKEN` is configured. The token is read from the environment and is redacted from status output. The normal proxy passes client headers to the configured local upstream, so users should still avoid pointing the upstream URL at an untrusted server.

To report a vulnerability, open a private GitHub security advisory. Do not place secrets, private media, prompts, model names, or personal filesystem paths in a public issue.
