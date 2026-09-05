---
name: sharpen-skill
description: Edit or add development skills in the public Tinyhat Hermes runtime repo while keeping them aligned with Tinyloop parent skill patterns.
---

# sharpen-skill - Hermes runtime repo adapter

Apply the [shared skill contract](../../../AGENTS.md#shared-skill-contract).
Then keep runtime repo skills small and adapter-shaped.

## Rules

- Canonical skills live in `.agents/skills/<name>/SKILL.md`.
- `.claude/skills/<name>` must be a symlink to `../../.agents/skills/<name>`.
- Prefer adapter skills that cite the parent Tinyloop skill and list only Hermes-specific overrides.
- Do not paste large parent skill bodies into this public repo.
- Keep private Tinyloop docs, local paths, device codes, and secrets out of skill text.

## Validate

```bash
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
git diff --check
```
