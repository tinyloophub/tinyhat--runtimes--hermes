---
name: commit
description: Commit changes in the public Tinyhat Hermes runtime repo. Use parent Tinyloop atomicity guidance, then run Hermes-specific checks before committing.
---

# commit - Hermes runtime repo adapter

Parent alignment: when this standalone repo is nested under Tinyloop, first read the same-named skill from the parent skill root described in `AGENTS.md`, then apply this repo's override.
Apply the runtime-specific checks below instead of the monorepo `./scripts/pre-commit.sh` gate.

## Steps

1. Run `git status --short` and group the diff into one logical change.
   Split unrelated docs, runtime behavior, CI, and release changes into separate commits.
2. Run baseline checks:

   ```bash
   git diff --check
   python3 scripts/check_dev_skills.py
   python3 scripts/check_repo_basics.py
   ```

3. For future runtime code, bootstrap, or dev image changes, add and run focused tests in the same PR before committing.
4. Commit with a Conventional Commit subject such as:

   ```bash
   git commit -m "chore(runtime): add Hermes repo basics"
   ```

## Notes

- Keep generated/runtime repo behavior public-safe; never commit tenant secrets, private URLs, device codes, or local env values.
- Use the Codex or Claude bot identity when the maintainer machine has one configured.
