# Changelog

## Unreleased

- Report Hermes gateway readiness on every assigned heartbeat, run gateway
  reconcile periodically instead of once per runtime process, and reset a
  failed/start-limited gateway unit before retrying `hermes gateway start`.

## 0.0.41 - 2026-07-07

- Use the running Hermes package version when a Computer installed from a
  channel ref checks a newer final LTS/latest tag, so channel-installed
  runtimes correctly report available updates.

## 0.0.40 - 2026-07-07

- Keep the Tinyhat heartbeat loop alive when platform HTTP reads time out,
  install the Hermes gateway service before falling back to a foreground
  gateway, and add `heal_hermes` for repairing already-configured Computers
  whose Telegram gateway stopped after a runtime restart.

## 0.0.39 - 2026-07-07

- Restore OpenRouter `openai/gpt-4o-transcribe` as the default Computer /
  Telegram STT primary and move Whisper variants back to the end of the
  fallback chain.

## 0.0.38 - 2026-07-07

- Make OpenRouter `openai/whisper-large-v3` the default Computer / Telegram
  STT primary and keep Whisper variants first in the OpenRouter fallback chain
  ahead of the gpt-4o transcribe models. Codex-auth multimedia still keeps
  voice transcription on the OpenRouter STT chain.

## 0.0.37 - 2026-07-06

- Retry transient post-install Hermes status probes with bounded diagnostics so
  first-run lazy dependency setup does not falsely mark fresh provisioning as
  broken, while still surfacing command timeouts and permanent status failures.

## 0.0.36 - 2026-07-04

- Accept a real OpenClaw import when `hermes claw migrate` applies changes
  after printing its preview / dry-run section, using the `Migration Results`
  marker or a matching execute-mode `report.json` with zero errors so a
  successful migration is not false-failed as preview-only.

## 0.0.35 - 2026-07-03

- Make `import_openclaw_state` fail when Hermes exits 0 after printing only a
  dry-run preview, so Tinyhat does not mark OpenClaw memory/persona import as
  completed when no Hermes files were written.
- Give apt-based provisioning a default dpkg lock timeout so a background
  unattended upgrade does not immediately fail fresh Hermes installs or
  OpenClaw takeover installs.

## 0.0.34 - 2026-07-03

- Treat logged-out OpenAI Codex auth status as unavailable during OpenClaw to
  Hermes migration, detect legacy OpenClaw OpenAI auth without returning token
  values, and start the normal Hermes Codex reconnect flow when the user needs
  to sign in again.

## 0.0.33 - 2026-07-03

- Add `activate_codex_auth_models`, a non-interactive runtime command that
  reuses the `/codex_auth` Hermes model picker and multimedia configuration
  when OpenClaw migration imported existing OpenAI Codex auth.

## 0.0.32 - 2026-07-03

- Accept the redaction-safe `include_private_values` OpenClaw migration flag as
  an alias for private-value import, while preserving the legacy
  `migrate_secrets` flag and returning a redaction-safe confirmation field.

## 0.0.31 - 2026-07-03

- Add runtime commands for OpenClaw to Hermes in-place migration, including
  verified OpenClaw state import and legacy Tinyhat secret-name import so the
  monorepo takeover flow can preserve migration data without exposing
  plaintext secrets.

## 0.0.30 - 2026-07-02

- Cap OpenRouter vision `extra_body.models` fallbacks at three entries so
  fresh Computers do not hit OpenRouter's `models array must have 3 items or
  fewer` validation error.

## 0.0.29 - 2026-07-02

- Let the OpenRouter STT command bridge resolve credentials from Hermes env
  files when the gateway process has not exported them, and switch the fresh
  Computer STT primary to `openai/gpt-4o-transcribe` with explicit sequential
  OpenRouter model fallbacks before local `small` faster-whisper.
- Configure fresh Computer image understanding with OpenRouter
  `google/gemini-2.5-flash`, OpenRouter same-provider model fallbacks, and an
  OpenRouter provider fallback chain for Codex-auth vision.
- After `/codex_auth`, use the selected Codex chat model for image
  understanding by default, with OpenRouter vision as the fallback.

## 0.0.28 - 2026-07-02

- Configure fresh installs and Telegram assignments with OpenRouter
  `openai/whisper-large-v3-turbo` command-provider STT, a warmed local `medium`
  faster-whisper model prepared for explicit local-mode selection, and
  OpenRouter `google/gemini-2.5-flash-lite` auxiliary vision.
- After `/codex_auth`, switch image understanding to the Codex/GPT vision
  provider while keeping OpenRouter Whisper STT active.
- Add `multimodal_status`, a read-only runtime command that reports the active
  Hermes voice and image providers/models without exposing secret values.

## 0.0.27 - 2026-07-02

- Stop exporting Hermes env-file secrets through login-shell hooks. Tinyhat now
  records secret names through the documented `terminal.env_passthrough` config
  and writes Tinyhat-managed `_HERMES_FORCE_<NAME>` aliases into the first local
  Hermes env file that already defines each saved name, so Hermes' terminal
  backend can expose the saved name without leaking the alias itself.
- Add `python3 -m hermes_runtime.terminal_env_passthrough register <NAME>` so
  the Tinyhat plugin can make encrypted private-handoff secrets available to
  terminal/code subprocesses after the gateway reloads, including provider/tool
  names such as `EXA_API_KEY`.
- Persist `TINYHAT_COMPUTER_TOKEN_AUDIENCE` during installer setup, including
  the `--token-audience` override, so non-default GCE identity-token audiences
  reach the running runtime service.

## 0.0.26 - 2026-07-01

- Export Hermes env files into fresh terminal sessions through Hermes'
  `terminal.shell_init_files` config so newly saved secrets are available to
  shell/exec tools after gateway restart.

## 0.0.25 - 2026-07-01

- Remove `list_hermes_secrets_masked` from runtime command dispatch because
  Hermes provider credentials are not safe to model as generic shell-visible
  secrets.
- Restart `hermes gateway` after Tinyhat settings Mini App secret add/update
  applies so Hermes reloads the refreshed env file.

## 0.0.24 - 2026-06-30

- Add `apply_config`, a runtime command that writes Tinyhat settings Mini App
  secrets into Hermes env files, reloads those keys into the runtime process,
  sends the owner an availability notice, and restarts the Telegram gateway
  only when a previously managed secret was removed and must be cleared from
  the running process environment.
- Add `list_hermes_secrets_masked`, a read-only runtime command that lists
  Tinyhat-managed Hermes secret names with masked values and source-file
  metadata without returning plaintext secrets.
- Send a pre-restart Telegram note during `/codex_auth` so the owner knows the
  gateway is restarting to load the new OpenAI Codex model configuration.

## 0.0.23 - 2026-06-30

- Install Hermes' voice extra with messaging dependencies, configure fresh
  Telegram assignments with local STT and automatic vision routing, and
  register a Tinyhat `openai-codex-stt` Hermes provider after `/codex_auth`
  without replacing the local STT fallback by default.
- Install Tinyhat's recommended Linux machine packages during runtime
  provisioning, including `ffmpeg` for TTS voice bubbles, `ripgrep`,
  `build-essential`, and Linux clipboard helpers for image paste.

## 0.0.22 - 2026-06-30

- Add `/tinyhat_settings` as a Tinyhat-managed Hermes Telegram command so an
  assigned agent can send the Tinyhat settings Mini App button from chat.
- Set Telegram's default `configure` Mini App button to the same Tinyhat
  settings page during `configure_telegram`, while still letting Hermes own the
  full Telegram command menu.

## 0.0.21 - 2026-06-29

- Add Tinyhat plugin lifecycle commands for Hermes Computers:
  `install_tinyhat_plugin`, `update_tinyhat_plugin`,
  `tinyhat_plugin_status`, and `check_tinyhat_plugin_update`.
- Include Tinyhat plugin freshness in the daily runtime update check and
  `update_status` payload so Hat admin can see the installed plugin version,
  target channel version, and whether a plugin update is available.
- Harden plugin update diagnostics by recording the resolved channel commit,
  cleaning temporary checkouts on failure, and reporting a clear error if
  Hermes reports plugin installation success but no plugin manifest is readable
  from the documented Hermes plugin directory.

## 0.0.20 - 2026-06-28

- Register the Tinyhat Codex Telegram commands through a small Hermes user
  plugin so Hermes can include them in the Telegram command menu. The quick
  commands still own the zero-token behavior; the plugin only exposes the same
  commands to Hermes' slash-command registry.

## 0.0.19 - 2026-06-28

- Let Hermes own Telegram command-menu registration by configuring Hermes'
  documented `platforms.telegram.extra.command_menu` priority instead of
  overwriting Telegram BotCommands from the Tinyhat runtime.

## 0.0.18 - 2026-06-28

- Install the Codex CLI during runtime provisioning, including a supported
  Node.js toolchain on apt-based Linux hosts, so Codex auth and usage commands
  are ready on warm Computers before Telegram assignment.
- Render OpenAI Codex usage limits as Telegram-friendly progress bars while
  storing the structured Codex app-server response in runtime state instead of
  scraping CLI logs.

## 0.0.17 - 2026-06-28

- Add `codex_limits`, a read-only runtime command and Telegram quick command
  that asks `codex app-server --listen stdio://` for OpenAI Codex subscription
  windows and credits without reading or returning auth tokens.

## 0.0.16 - 2026-06-28

- Add Telegram quick commands that start OpenAI Codex device-code auth from the
  Hermes chat, send the auth link/code back to Telegram, and restart the gateway
  after Hermes writes the local auth store.
- Drive Codex model selection through Hermes' formal `hermes model --no-browser`
  picker, including the unnumbered-menu fallback, and redact picker output
  before it is stored in local status.

## 0.0.15 - 2026-06-27

- Add `stop_hermes`, a runtime command that stops Hermes Agent messaging before
  Tinyhat parks or reassigns a Telegram bot.
- Add `start_hermes`, a runtime command that starts the already-configured
  Hermes Agent gateway again without fetching credentials or changing webhooks.

## 0.0.14 - 2026-06-27

- Document that `configure_telegram` revokes the platform setup grant after
  Hermes has been configured, so Computers cannot keep fetching Telegram
  credentials after assignment succeeds.

## 0.0.13 - 2026-06-27

- Add `configure_telegram`, a whitelisted runtime command that fetches the
  platform-granted Telegram setup payload, writes Hermes Telegram/OpenRouter
  configuration through public files and CLI commands, clears the bot webhook,
  and starts the Hermes gateway without returning secrets in command results.
- Warm Hermes messaging dependencies during `install_hermes`, and make the
  dependency repair path work on hosts whose system `pip` does not support
  `pip --python`.

## 0.0.12 - 2026-06-26

- Keep heartbeat polling alive while any runtime command is running, so slow
  commands such as Hermes installation do not make the Computer look offline.
- Preserve the one-active-command invariant while heartbeats continue, and
  report restart-triggering command results before the runtime exits.

## 0.0.11 - 2026-06-26

- Add `install_hermes`, an idempotent runtime command that installs upstream
  Hermes Agent through the official public installer and reports whether the
  command actually changed the machine.
- Add `hermes_status`, a read-only runtime command that runs
  `hermes --version`, `hermes status`, and `hermes status --all` through the
  public Hermes CLI so Hat admin can verify the framework after setup.
- Add shared Hermes CLI probing helpers with bounded output capture and
  timeout cleanup, plus focused tests for install failure paths and timeout
  child-process reaping.

## 0.0.10 - 2026-06-26

- Tune heartbeat cadence from platform state: unassigned/provisioning Computers
  check in quickly for faster assignment, while assigned/active Computers use a
  slower default cadence.
- Add explicit environment overrides for assigned and unassigned heartbeat
  intervals while preserving the legacy fixed-interval override.

## 0.0.9 - 2026-06-26

- Use Google Cloud identity tokens for production platform API calls so GCloud
  Computers authenticate through instance attestation instead of static runtime
  tokens.
- Keep local development on the explicit `local-dev` path while preserving the
  same command and heartbeat contract used by GCloud Computers.

## 0.0.8 - 2026-06-26

- Make update decisions explicit when a channel selector is unresolved:
  `channels/lts` or `channels/latest` alone no longer reports an update as
  available unless the platform supplies a concrete final tag that is newer
  than the installed final version.

## 0.0.7 - 2026-06-26

- Add `install.sh --run-foreground` so local Docker machines can use the same
  public installer surface as production machines while letting Docker supervise
  the long-running process; foreground mode now forwards TERM and INT cleanly to
  the runtime child.

## 0.0.6 - 2026-06-26

- Treat legacy cached update-check results that do not record the checked
  runtime version or sha as stale, so `update_status` does not surface an
  unprovable `update_available` decision.
- Normalize the live runtime version before comparing it with cached update
  checks.

## 0.0.5 - 2026-06-26

- Mark cached update-check results as stale in `update_status` when the runtime
  has changed since the check was computed, preventing old decisions from being
  treated as actionable.

## 0.0.4 - 2026-06-26

- Add `running_version`, a read-only command that proves which runtime package
  version the running Python process imported.
- Guard releases so `VERSION` and `hermes_runtime.__version__` stay aligned,
  keeping the runtime's post-update proof honest.

## 0.0.3 - 2026-06-25

- Fix runtime updates so `stage_update` prepares the target runtime package
  code, and activation swaps that code into the install prefix before the
  runtime process re-executes.
- Fetch staged updates by immutable `target_sha` when the platform provides
  one, reject unsafe archive paths, and recover interrupted package swaps on
  startup.
- Run the service through an import-safe bootstrap outside the runtime package
  and report startup activation failures through heartbeat/update status
  instead of crash-looping silently.
- Make Docker builds record the selected runtime ref instead of always
  recording `local-dev`.

## 0.0.2 - 2026-06-25

- Document the restart-activated update flow.
- Add `setup_snapshot`, `update_status`, and `recent_commands` commands for
  inspecting runtime state and the local command ledger from Hat admin.
- Add `restart_runtime_service` so operators can restart the Tinyhat runtime
  process after activating a staged update.

## 0.0.1 - 2026-06-25

- Establish the public Tinyhat Hermes runtime repository.
- Add repo-local development guidance, code ownership, CI, release notes, and
  development-skill adapters.
