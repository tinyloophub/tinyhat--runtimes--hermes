# AGENTS.md - Tinyhat Hermes runtime

This public repo installs and supervises Tinyhat-managed Hermes Computers,
including heartbeat, command dispatch, configuration, diagnostics, and updates.
It does not vendor upstream Hermes Agent or copy OpenClaw runtime behavior.

## Official Interfaces Only

Use documented Hermes installers, CLI commands, public configuration files, and
runtime interfaces. Do not depend on private implementation details, cache
layouts, database files, or undocumented on-disk state. If an interface is
missing, request or build it upstream instead of reaching into Hermes internals.

## Contribution rules

- Keep code, docs, and GitHub evidence public-safe: no secrets, device codes,
  private Drive paths, local env values/URLs, or internal admin endpoints.
- Use one logical change per Conventional Commit and one related thread per PR.
  Work on a `codex/<topic>` or `claude/<topic>` branch; never push to `main`.
- Runtime changes need focused tests and release notes. Read [README.md](README.md)
  for runtime context and [define-tests](.agents/skills/define-tests/SKILL.md)
  to select the required checks for the changed surface. Fix check failures;
  never bypass hooks.

## Task routing

Read the skill for the operation you are performing; do not load the whole catalog.

| Operation | Skill |
| --- | --- |
| Codex GitHub identity/writeback | [codex](.agents/skills/codex/SKILL.md) |
| Commit | [commit](.agents/skills/commit/SKILL.md) |
| Select verification | [define-tests](.agents/skills/define-tests/SKILL.md) |
| Open/update a PR | [open-pr](.agents/skills/open-pr/SKILL.md) |
| Review a PR | [review](.agents/skills/review/SKILL.md) |
| Cut/check a release | [release](.agents/skills/release/SKILL.md) |
| Edit skills | [sharpen-skill](.agents/skills/sharpen-skill/SKILL.md) |
| Edit guidance | [update-guidance](.agents/skills/update-guidance/SKILL.md) |

## Shared skill contract

Canonical skills live in `.agents/skills`; `.claude/skills` contains symlinks to
those skills. `CLAUDE.md` imports this file so both agents use the same guidance.

When nested at `platform_repos/runtimes/hermes`, the parent skill root is
`../../../.agents/skills` relative to this repo root. For a standalone clone,
`TINYLOOP_PARENT_REPO` can name the Tinyloop checkout. For the selected operation,
read its same-named parent skill if mounted, then apply the local skill's repo,
verification, and release overrides. `update-guidance` uses the parent's
`sharpen-skill` (there is no parent `update-guidance`). With no parent checkout,
use the local skills; do not assume monorepo commands exist here.
