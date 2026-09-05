---
name: commit
description: Commit changes in the public Tinyhat Hermes runtime repo. Use parent Tinyloop atomicity guidance, then run Hermes-specific checks before committing.
---

# commit - Hermes runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).
Apply the runtime-specific checks below instead of the monorepo `./scripts/pre-commit.sh` gate.

## Steps

1. Run `git status --short` and group the diff into one logical change.
   Split unrelated docs, runtime behavior, CI, and release changes into separate commits.
2. Run the applicable checks from [define-tests](../define-tests/SKILL.md).
   These replace the monorepo pre-commit gate. Fix failures; never bypass hooks.
3. Stage only this logical change and review the staged diff.
4. Commit with a Conventional Commit subject such as:

   ```bash
   git commit -m "chore(runtime): add Hermes repo basics"
   ```

## Notes

- Keep generated/runtime repo behavior public-safe; never commit tenant secrets, private URLs, device codes, or local env values.
- Use the Codex or Claude bot identity when the maintainer machine has one configured.
