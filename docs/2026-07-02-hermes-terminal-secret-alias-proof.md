# Hermes Terminal Secret Alias Proof

Date: 2026-07-02

Purpose: prove that a Tinyhat encrypted secret save can make a provider-style
secret name immediately available to Hermes local terminal/code children without
returning or printing the plaintext value. `EXA_API_KEY` is the real external
API used for this proof; the runtime code generates `_HERMES_FORCE_<ENV_NAME>`
aliases dynamically for every valid secret name returned by the Tinyhat
platform, not only for Exa.

Hermes source under test:
`NousResearch/hermes-agent@88d1d6206f399c134d1f4c0b7db27733aaa3c50c`

Runtime checkout under test: PR #68 head after the terminal alias scoping fix.

Plugin path under test:

1. `tinyhat.secret_handoff._set_hermes_secret("EXA_API_KEY", value)`
2. `tinyhat.secret_handoff._register_terminal_env_secret("EXA_API_KEY")`
3. Fresh Hermes process loads `~/.hermes/.env`
4. Hermes local terminal env builder creates a child env
5. Child process checks only whether `EXA_API_KEY` is present, then sends one
   Exa search request without printing the key.

Sanitized command shape:

```bash
EXA_API_KEY="<redacted>" docker run --rm \
  -e EXA_API_KEY \
  -e HOME=/tmp/plain-home \
  -e HERMES_HOME=/tmp/plain-hermes-home \
  -v /tmp/hermes-agent-source:/src/hermes-agent:ro \
  -v /path/to/tinyhat-plugin:/src/tinyhat:ro \
  -v /path/to/tinyhat--runtimes--hermes:/opt/tinyhat-hermes-runtime:ro \
  -v /tmp/hermes_plain_secret_probe.py:/probe.py:ro \
  python:3.12-bookworm \
  bash -lc 'python -m pip install -e /src/hermes-agent >/tmp/pip.log 2>&1; python /probe.py'
```

Sanitized output:

```json
{
  "child_returncode": 0,
  "child_stdout": {
    "all_passthrough": [],
    "blocklisted": true,
    "config_file_exists": true,
    "config_has_secret_name": true,
    "force_alias_leaked": false,
    "force_alias_set": true,
    "loaded_env_files": ["/tmp/plain-hermes-home/.env"],
    "main_process_env_set": true,
    "passthrough_allowed": false,
    "probe_returncode": 0,
    "probe_stdout": {
      "env_set": true,
      "http_status": 200,
      "title": "Introducing the Voice Agent Builder | xAI"
    },
    "terminal_env_set": true
  },
  "config_file_exists": true,
  "env_file_exists": true,
  "registration_ok": true
}
```

Interpretation:

- Hermes still treats `EXA_API_KEY` as a blocked provider/tool credential.
- Normal `terminal.env_passthrough` does not allow this name by itself.
- Hermes loads `_HERMES_FORCE_EXA_API_KEY` from the env file.
- The force-prefixed alias does not leak into the child env.
- The child receives only `EXA_API_KEY` and can use it successfully.
- No secret value is printed or returned in command output.
