"""Install Hermes Agent using the official installer when it is missing.

What it does:
    Checks whether the ``hermes`` CLI is already installed. If it is present,
    the command returns the current Hermes status and does not reinstall. If it
    is missing, the command installs the small Debian prerequisites when it can
    do so safely as root, then runs the official Hermes Agent installer:

        curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

    By default Tinyhat passes ``--skip-browser`` to keep the first managed
    Computer setup minimal. Set ``TINYHAT_HERMES_INSTALL_ARGS`` on the machine
    to override those installer arguments.

When to use it:
    Hat admin queues this automatically during Computer creation after the
    Tinyhat runtime has started heartbeating. You can also run it manually if a
    machine was created before Hermes was installed.

Example input:
    {"kind": "install_hermes", "spec": {}}

Example output:
    {
      "installed_before": false,
      "installed_now": true,
      "changed": true,
      "status": {"ok": true, "version": "Hermes Agent 0.1.0"}
    }

Side effects:
    May install Debian packages ``ca-certificates``, ``curl``, ``git``, and
    ``xz-utils`` when running as root on Debian/Ubuntu. Runs the public Hermes
    installer if Hermes is missing. Does not configure Tinyhat platform state.
"""

from __future__ import annotations

from typing import Any

from hermes_runtime.hermes_cli import (
    find_hermes_binary,
    hermes_install_script,
    maybe_install_debian_prerequisites,
    probe_hermes_status,
    run_shell,
)


async def run(_ctx: Any, _command: dict[str, Any]) -> dict[str, Any]:
    installed_before = find_hermes_binary() is not None
    prerequisites: dict[str, Any] | None = None
    install_result: dict[str, Any] | None = None

    if not installed_before:
        prerequisites = await maybe_install_debian_prerequisites()
        install_result = await run_shell(
            hermes_install_script(),
            timeout_seconds=900,
            env={"CI": "1"},
        )
        if not install_result.get("ok"):
            raise RuntimeError(
                "Hermes installer failed with returncode="
                f"{install_result.get('returncode')}"
            )

    status = await probe_hermes_status()
    if not status.get("installed"):
        raise RuntimeError("Hermes installer completed, but hermes CLI was not found.")
    if not status.get("ok"):
        raise RuntimeError("Hermes CLI is installed, but status checks failed.")

    return {
        "schema": "tinyhat_hermes_install_v1",
        "installed_before": installed_before,
        "installed_now": True,
        "changed": not installed_before,
        "install_url": "https://hermes-agent.nousresearch.com/install.sh",
        "install_args_source": "TINYHAT_HERMES_INSTALL_ARGS",
        "prerequisites": prerequisites,
        "install": install_result,
        "status": status,
    }
