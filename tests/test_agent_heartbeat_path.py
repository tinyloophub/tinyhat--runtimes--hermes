"""Preinstalled GCE Computers opt into the Redis-first heartbeat contract."""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from hermes_runtime.platform_paths import context_computer_api_path


class AgentHeartbeatPathTest(unittest.TestCase):
    def test_preinstalled_gce_only_changes_heartbeat(self):
        with patch.dict(os.environ, {"TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "1"}):
            ctx = SimpleNamespace(platform_auth="gcloud")
            self.assertEqual(context_computer_api_path(ctx, "heartbeat"), "/hapi/v2/computers/me/heartbeat")
            self.assertEqual(context_computer_api_path(ctx, "runtime-command/result"), "/hapi/v1/computers/me/runtime-command/result")
            local = SimpleNamespace(platform_auth="local_dev")
            self.assertEqual(context_computer_api_path(local, "heartbeat"), "/hapi/v1/computers/local-dev/heartbeat")

    def test_existing_gce_keeps_legacy_endpoint(self):
        with patch.dict(os.environ, {"TINYHAT_AGENT_SYSTEMS_PREINSTALLED": "0"}):
            self.assertEqual(context_computer_api_path(SimpleNamespace(platform_auth="gcloud"), "heartbeat"), "/hapi/v1/computers/me/heartbeat")
