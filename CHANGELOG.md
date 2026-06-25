# Changelog

## 0.0.3 - 2026-06-25

- Fix runtime updates so `stage_update` prepares the target runtime package
  code, and activation swaps that code into the install prefix before the
  runtime process re-executes.
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
