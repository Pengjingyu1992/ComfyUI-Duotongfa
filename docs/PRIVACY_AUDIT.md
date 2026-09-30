# Release privacy audit — 0.2.5 / 2026-09-30

Scope: the tracked release tree, code comments, Git author/committer metadata, source archive, source distribution, and wheel.

Results:

- No real API key, access token value, password, private key, cloud credential, personal email address, private media, prompt, model file, workflow file, log, `.env`, runtime state, or personal absolute path is included.
- Synthetic credential strings in regression tests are fixtures, not usable credentials. Technical comments contain no private deployment information.
- Deployment-specific hardware, benchmark, and model details have been replaced with generic documentation.
- Public commits use the repository owner's public GitHub username and GitHub noreply address. Existing public history has been rewritten to remove personal email metadata and deployment-specific documentation.
- The string `DUOTONGFA_CONTROL_TOKEN` is a documented environment-variable name; no token value is committed.
- Render capability tokens are generated per job, stored only in the current user's local state for crash recovery, and redacted from diagnostic responses.
- Model-list cache data may contain locally installed model identifiers at runtime, but no user's cache file is included in the repository.
- The gateway forwards client headers to the configured local upstream but does not log or persist Authorization headers or request bodies.

Automated CI rejects common private-key, access-token, email-address, and personal-path patterns. Local archive contents and metadata are reviewed before uploading release assets. This audit covers the published refs and release artifacts; removal of old commit caches is controlled by GitHub.
