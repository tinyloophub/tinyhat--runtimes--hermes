---
name: open-pr
description: Open a PR for the public Tinyhat Hermes runtime repo. Use parent Tinyloop PR discipline, then apply Hermes repo scope and test-report requirements.
---

# open-pr - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, first read the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Apply this repo's target, checks, and release boundary below.

## Scope Check

- One related thread per PR.
- Keep Hermes runtime behavior separate from monorepo provisioning changes and separate from upstream Hermes Agent changes.
- If a PR depends on a Tinyloop monorepo or upstream Hermes PR, link it and mark the PR draft until the dependency is ready.
- Release PRs and promotion requests are maintainer-reviewed only. Do not start
  cross-agent review for them, and do not open channel-promotion PRs; use
  `scripts/promote_release_channel.py` as `farid-tinyloop` after a final release
  is published.

## Commands

```bash
git status --short
git log --oneline origin/main..HEAD
git diff --check
python -m compileall -q scripts
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
```

Add runtime checks from `define-tests` for any touched runtime surface.
For future install/config/launch behavior, unit-test-only evidence is not enough:
include the Linux/container proof or explicitly call out why it was not required.

## PR Creation

Create PRs against:

```text
tinyloophub/tinyhat--runtimes--hermes
```

Use the configured Codex bot identity for Codex-authored PRs when available, then restore `gh` to the maintainer account.

The PR body should include:

- What changed and why.
- Hermes runtime vs upstream Hermes Agent boundary notes when install/config behavior changes.
- Exact verification commands and results.
- Terminal/command evidence as fenced, sanitized text or a committed Markdown evidence file. Do not convert terminal output into screenshots.
- Screenshots or recordings only for changed user-visible, admin, Telegram, or other real visual surfaces.
- Dependency links to Tinyloop monorepo or upstream Hermes Agent PRs when relevant.
