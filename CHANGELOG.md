# Changelog

## Unreleased

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
