# Contributing

Issues and pull requests are welcome, especially for platforms not yet tested on physical hardware.

Before submitting:

1. Run `python -m pytest -q` and `python -m compileall -q .`.
2. Keep the default network boundary on loopback.
3. Do not add cloud API providers, telemetry, model downloads, or destructive model/workflow operations.
4. Never commit `.env`, API keys, prompts, private media, model files, logs, state files, or personal absolute paths.
5. Describe platform, hardware, backend, reproduction steps, expected behavior, actual behavior, and redacted timings.

By contributing, you agree that your contribution is licensed under GPL-3.0.
