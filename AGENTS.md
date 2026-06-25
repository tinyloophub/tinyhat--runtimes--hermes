# AGENTS.md - Tinyhat Hermes runtime

This public repo is the standalone runtime slot for Tinyhat-managed Hermes
Computers.

It starts intentionally small. The repo currently owns the Tinyhat-facing
runtime contract, development guidance, release shape, and future home for the
Hermes Computer bootstrap/supervision code. It does **not** vendor upstream
Hermes Agent or copy OpenClaw runtime behavior.

## Hermes Boundary - Official Interfaces Only

This runtime should install, start, configure, and inspect Hermes Agent through
documented interfaces: the official installer, supported CLI commands, public
configuration files, and documented runtime behavior.

Do not reach into private Hermes implementation details, cache layouts,
database files, or undocumented on-disk state. If Tinyhat needs behavior Hermes
does not expose, request or build a public interface upstream instead of
depending on internals.

## Current Scope

- Keep the repo public-safe and reviewable.
- Define code ownership and branch-protection-friendly development rules.
- Keep dev skills aligned with the Tinyloop parent workflows when the repo is
  checked out under `platform_repos/runtimes/hermes`.
- Add Hermes-specific runtime code only in focused PRs with matching tests and
  release notes.

## Dev Skills

Canonical repo-local development skills live under [`.agents/skills`](.agents/skills).
Claude-facing adapters under [`.claude/skills`](.claude/skills) are symlinks back to that canonical directory.

When this repo is checked out under the Tinyloop monorepo at
`platform_repos/runtimes/hermes`, skills that name a parent Tinyloop skill
should read the parent file first, then apply this repo's override. From the
repo root, the default parent path is `../../../.agents/skills`; from inside an
adapter `SKILL.md`, use the parent skill root described here or set
`TINYLOOP_PARENT_REPO` when working from a standalone clone.

## Contribution Rules

- Keep this repo public-safe: no private Drive paths, tenant secrets,
  local-only URLs, device codes, or internal admin endpoints.
- Use one logical change per commit and Conventional Commit subjects.
- Never push directly to `main`; open a PR from a branch such as
  `codex/<topic>` or `claude/<topic>`.
- Guidance/dev-skill changes should run:
  - `git diff --check`
  - `python3 scripts/check_dev_skills.py`
  - `python3 scripts/check_repo_basics.py`
- Runtime behavior changes must add focused tests before they land.

## Skill Index

| Operation | Skill |
| --- | --- |
| Codex GitHub identity/writeback | [codex](.agents/skills/codex/SKILL.md) |
| Commit | [commit](.agents/skills/commit/SKILL.md) |
| Pick tests | [define-tests](.agents/skills/define-tests/SKILL.md) |
| Open a PR | [open-pr](.agents/skills/open-pr/SKILL.md) |
| Review a PR | [review](.agents/skills/review/SKILL.md) |
| Cut/check a release | [release](.agents/skills/release/SKILL.md) |
| Edit skills | [sharpen-skill](.agents/skills/sharpen-skill/SKILL.md) |
| Edit guidance | [update-guidance](.agents/skills/update-guidance/SKILL.md) |
