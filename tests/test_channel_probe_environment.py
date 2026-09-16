"""Provider probes must not inherit channel or platform credentials."""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from hermes_runtime.channel_agent import native
from hermes_runtime.hermes_cli import run_process


class ProbeEnvironmentTests(unittest.TestCase):
    def test_replacement_environment_does_not_merge_removed_credentials_back_in(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "test-only", "TINYHAT_TEST_SECRET": "test-only", "OPENAI_API_KEY": "test-only"}):
            result = asyncio.run(run_process(
                [sys.executable, "-c", 'import os,json; print(json.dumps([name in os.environ for name in ["TELEGRAM_BOT_TOKEN", "TINYHAT_TEST_SECRET", "OPENAI_API_KEY"]]))'],
                timeout_seconds=5, env=native.child_env(), replace_env=True,
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(json.loads(result["stdout"]), [False, False, False])

    def test_version_and_authentication_probes_use_replacement_environment(self):
        with patch.object(native.shutil, "which", return_value="/usr/bin/codex"), patch.object(native, "run_process", AsyncMock(return_value={"ok": True})) as run:
            asyncio.run(native.probe("codex"))
        self.assertEqual(run.await_count, 2)
        self.assertTrue(all(call.kwargs["replace_env"] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
