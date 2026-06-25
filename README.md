# Tinyhat Hermes runtime

This repository is the public Tinyhat runtime for Hermes Computers. Its job is
small on purpose: keep a platform heartbeat alive, run a tiny whitelist of
platform commands, and stage runtime updates so they activate only on restart.

Canonical handle:

```text
tinyhat/runtimes/hermes
```

GitHub repository:

```text
tinyloophub/tinyhat--runtimes--hermes
```

The platform creates and heals this repository from the monorepo recovery seed
so dev and production use the same public runtime repo name.

## Runtime responsibilities

The runtime is intentionally not the agent framework. Hermes runs beside it.
This process only does the platform-visible work needed to manage a Computer:

- send a heartbeat to the Tinyhat platform;
- receive at most one command per heartbeat;
- run only the commands documented below;
- report command results back to the platform;
- stage updates and activate them on runtime restart.

For local development this runs in Docker with `--restart unless-stopped`. On a
production Linux Computer the same runtime should run under a process manager
such as systemd with restart enabled and a high enough priority that Hermes can
use the machine's resources without starving the heartbeat process.

## Command whitelist

Every platform command is implemented as a file under
`hermes_runtime/commands/`. If a command is not listed here, the runtime rejects
it.

| Command | File | Why it exists | Side effects |
| --- | --- | --- | --- |
| `ping` | `hermes_runtime/commands/ping.py` | Basic liveness check from Hat admin. | None. Returns `pong`. |
| `whoami` | `hermes_runtime/commands/whoami.py` | Asks the platform to attest which Computer this runtime token belongs to. | None. Calls `/hapi/v1/computers/local-dev/whoami` in local dev. |
| `check_update` | `hermes_runtime/commands/check_update.py` | Checks the configured runtime update target on demand without waiting for the daily schedule. | Calls GitHub release refs, writes `updates/last_check.json`, reports the result to the platform update-check API, does not stage or activate code. |
| `stage_update` | `hermes_runtime/commands/stage_update.py` | Downloads or prepares a target runtime version without changing the running process. In the local foundation it writes a staged version marker. | Writes `staged/VERSION` under runtime state. |
| `activate_update` | `hermes_runtime/commands/activate_update.py` | Requests activation of an already staged update. | Writes `ACTIVATE_ON_RESTART` and exits after reporting success so the process manager restarts the runtime. |

## Daily update checks

The runtime does not check GitHub for updates on every heartbeat. Each heartbeat
only performs a fast local due-time check against small config files. When the
configured time has arrived and the check has not already run for that local
date, the runtime starts the network check as an async background task so the
heartbeat can continue.

Default schedule:

```text
02:35 America/Los_Angeles
```

Override the schedule by writing these files under the runtime state directory:

| File | Example | Meaning |
| --- | --- | --- |
| `config/update_check_time` | `02:35` | Local 24-hour time for the daily check. |
| `config/update_check_timezone` | `America/Los_Angeles` | IANA timezone for interpreting the check time. |
| `config/update_check_channel` | `lts` | `lts`, `latest`, or `custom`. |
| `config/update_check_ref` | `v0.20.0-dev.20260625T125559Z.pr2-smoke` | Optional exact ref. Required for custom checks. |

The latest check result is stored at `updates/last_check.json` for local
debugging and is reported through the platform update-check result API. It is
not embedded into heartbeat metrics. Use the admin `check_update` command when
you want to run the same check immediately from Hat admin.

## Update channels

Versions should be immutable Git tags shaped like `vX.Y.Z`.

- `latest`: the `channels/latest` branch points at the final release currently
  promoted for fast-moving Computers.
- `lts`: the `channels/lts` branch points at the conservative final release
  used by default Computer creation.
- `custom`: an explicit tag or commit selected by an operator.

For development, secondary test releases use prerelease tags shaped
`vX.Y.Z-dev.YYYYMMDDTHHMMSSZ[.suffix]`, for example
`v0.20.0-dev.20260625T173000Z.smoke`. They should be published as GitHub
pre-releases with the Latest marker off. Dev releases may point at a PR branch
commit, so you can test from GitHub without waiting for the PR to merge.

The helper below prints a tag in that shape and can optionally create an
annotated local tag:

```bash
python3 scripts/make_dev_release_tag.py --base v0.20.0 --suffix smoke
python3 scripts/make_dev_release_tag.py --base v0.20.0 --suffix smoke --apply
```

To publish the current branch as a testable GitHub prerelease, run:

```bash
python3 scripts/publish_dev_release.py --base v0.20.0 --suffix smoke --publish
```

The script prints `release_ref=<tag>` and an exact installer command. Paste that
tag into Hat admin's Custom/dev release field when creating or updating a local
Hermes Computer.

The runtime does not decide which tag is safe. It asks the platform for a target
version/channel, stages that target, and only activates it after restart. This
keeps a running agent from changing underneath active work.

See [VERSIONING.md](VERSIONING.md) for the full dev -> candidate -> final ->
latest/LTS lifecycle.

## Install path

Tinyhat installs this runtime the same way Hermes Agent documents its own
installer: with a public script that can be read before running it.

```bash
curl -fsSL https://raw.githubusercontent.com/tinyloophub/tinyhat--runtimes--hermes/channels/lts/install.sh \
  | bash -s -- --ref channels/lts
```

Use `channels/latest` for the latest promoted final, or an exact immutable tag
such as `v0.20.0`.

## Upstream Hermes install path

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

This foundation does not install Hermes Agent yet and does not assign a
Computer to an agent. It proves the management loop first: heartbeat,
attestation, command dispatch, and restart-activated updates.

## Development

Run the repository basics checks before opening a pull request:

```bash
git diff --check
python -m compileall -q scripts
python -m compileall -q hermes_runtime
python -m unittest discover -s tests -v
python3 scripts/check_dev_skills.py
python3 scripts/check_repo_basics.py
```
