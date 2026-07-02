"""Import legacy Tinyhat runtime secrets into Hermes env files.

This command is for OpenClaw -> Hermes migrations. The old Tinyhat runtime
secret vault was platform-readable by design; this command pulls that same map
through the authenticated Computer API, writes it into Hermes' normal env files,
reloads terminal passthrough helpers, and restarts the Hermes gateway when
needed. Results include only names and counts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hermes_runtime.commands import apply_config
from hermes_runtime.commands.configure_telegram import _env_file_candidates
from hermes_runtime.platform_paths import context_computer_api_path


SCHEMA = "tinyhat_hermes_import_legacy_tinyhat_secrets_v1"


async def run(ctx: Any, command: dict[str, Any]) -> dict[str, Any]:
    spec = command.get("spec") if isinstance(command.get("spec"), dict) else {}
    api_path = context_computer_api_path(ctx, "runtime-secrets")
    payload = await ctx.platform.get_json(api_path)
    secrets = apply_config._clean_secret_map(payload)
    secret_names = sorted(secrets)

    env_files = [
        apply_config._write_runtime_secret_env_file(env_path, secrets)
        for env_path in _env_file_candidates()
    ]
    removed_keys = sorted(
        {
            str(key)
            for item in env_files
            for key in item.get("removed_keys", [])
        }
    )
    env_paths = [Path(str(item["path"])) for item in env_files]
    env_reload = apply_config.load_env_files_into_process(
        env_paths,
        keys=secret_names,
    )
    terminal_env_passthrough = apply_config.sync_terminal_env_passthrough(
        secret_names,
        remove_names=removed_keys,
    )

    restart_required = bool(secret_names or removed_keys)
    if restart_required:
        hermes_bin = apply_config.find_hermes_binary()
        if hermes_bin is None:
            raise RuntimeError("Hermes CLI was not found; cannot restart Hermes gateway.")
        gateway = await apply_config._run_gateway(hermes_bin)
        if not gateway.get("healthy"):
            raise RuntimeError("Hermes gateway did not report a healthy status.")
    else:
        gateway = {
            "restarted": False,
            "restart_required": False,
            "reason": "no_legacy_runtime_secrets",
        }

    return {
        "schema": SCHEMA,
        "reason": spec.get("reason") or "admin_import_legacy_tinyhat_secrets",
        "configured": True,
        "secret_count": len(secret_names),
        "secret_names": secret_names,
        "removed_secret_names": removed_keys,
        "env_files": env_files,
        "env_reload": env_reload,
        "terminal_env_passthrough": terminal_env_passthrough,
        "gateway": gateway,
        "restart_requested": restart_required,
        "systemd_restart_requested": False,
        "diagnostic": f"imported {len(secret_names)} legacy Tinyhat secret(s)",
        "values_masked": True,
    }
