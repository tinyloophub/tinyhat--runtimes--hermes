---
name: review
description: Review PRs in the public Tinyhat Hermes runtime repo, using parent Tinyloop review quality rules with Hermes-specific risk checks.
---

# review - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, first read the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Apply the runtime-specific risk checklist below.

## Runtime Checklist

- Release PRs and promotion requests are maintainer-reviewed only. If asked to
  review one as an agent, report that it is reserved for the maintainer instead
  of approving it.
- Boot/install scripts remain public-safe and do not embed secrets, private URLs, device codes, or local-only paths.
- Hermes integration uses documented installer, CLI, config, and runtime interfaces.
- The repo does not vendor or fork upstream Hermes Agent unless a PR explicitly scopes that decision.
- Dev Docker or Linux smoke changes still prove the runtime under the intended user.
- Version/CHANGELOG changes match the behavior actually shipped.

## Evidence

Prefer concrete commands:

```bash
git diff --check
python -m compileall -q scripts
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
```

Review command output as text evidence: fenced logs, summaries, or committed
Markdown evidence files. Do not ask authors to screenshot terminal output.
Reserve screenshots or recordings for changed admin, Telegram, browser, or
other user-visible surfaces.

Post GitHub reviews under the Codex bot when acting as Codex, and end with `— posted by Codex`.
