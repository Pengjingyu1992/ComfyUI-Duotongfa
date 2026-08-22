# Release privacy audit — 2026-08-22

Scope: every file included in the initial public repository and source distribution.

Results:

- No API key, access token value, password, private key, cloud credential, email address, private media, prompt, model file, workflow file, log, `.env`, runtime state, or personal absolute path is included.
- The string `DUOTONGFA_CONTROL_TOKEN` is a documented environment-variable name; no token value is committed.
- Render capability tokens are generated per job, stored only in the current user's local state for crash recovery, and redacted from diagnostic responses.
- Model-list cache data may contain locally installed model identifiers at runtime, but no user's cache file is included in the repository.
- The gateway forwards client headers to the configured local upstream but does not log or persist Authorization headers or request bodies.

Automated CI rejects common private-key, access-token, and personal-path patterns. This is defense in depth, not a substitute for contributor review.
