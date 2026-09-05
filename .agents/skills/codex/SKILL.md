---
name: codex
description: Codex conventions for the public Tinyhat Hermes runtime repo. Use for GitHub writeback, PR comments/reviews, issue comments, and identity restoration.
---

# codex - Hermes runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).
Apply the overrides below for `tinyloophub/tinyhat--runtimes--hermes`.

## Rules

- Codex-authored GitHub comments and reviews use the configured Codex bot identity when one is available.
- Restore `gh` to the maintainer account after the write and verify with `gh auth status`.
- Read back the posted author and final signature; include the actual model/effort
  line immediately before the signature when the parent workflow requires it.
- End every Codex-authored GitHub comment/review body with:

```text
— posted by Codex
```

- Use the target repo explicitly in commands:

```bash
gh pr view <n> --repo tinyloophub/tinyhat--runtimes--hermes
gh issue view <n> --repo tinyloophub/tinyhat--runtimes--hermes
```

## Public-Repo Boundary

Do not copy private Tinyloop monorepo details, Drive paths, secrets, local env values, device codes, or internal URLs into this public repo, PR bodies, issues, or comments.
