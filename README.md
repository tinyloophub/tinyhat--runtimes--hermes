# Tinyhat Hermes runtime

This repository is the public Tinyhat platform runtime slot for Hermes
Computers.

Canonical handle:

```text
tinyhat/runtimes/hermes
```

GitHub repository:

```text
tinyloophub/tinyhat--runtimes--hermes
```

The repo is intentionally minimal while Hermes Computer provisioning is being
introduced. The platform creates and heals this repository from the monorepo
recovery seed so dev and production use the same public runtime repo name.

## Intended install path

Hermes Agent's Linux command-line installer is:

```bash
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

The installer expects Git plus `curl` and `xz-utils` on Linux. It handles the
Hermes clone, Python 3.11 via uv, Node.js 22, ripgrep, ffmpeg, a virtual
environment, and the global `hermes` command setup.

For a dedicated unprivileged service account, install Chromium system
libraries separately when browser automation is needed:

```bash
sudo npx playwright install-deps chromium
```

Then run the regular Hermes installer as the service user. A headless runtime
that does not need browser automation can pass `--skip-browser` to the
installer.

## Current scope

This seed only establishes the public runtime repository. The VM bootstrap,
systemd units, platform heartbeat/binding loop, and Hermes-specific Computer
assignment flow will land in later steps.

## Development

Run the repository basics checks before opening a pull request:

```bash
git diff --check
python -m compileall -q scripts
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
```
