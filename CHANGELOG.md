# Changelog

## Unreleased

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
