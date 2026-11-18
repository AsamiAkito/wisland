"""Verify portable SSH jobs without contacting a real server or model."""

import json
import shlex
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wisland.knowledge.organizer_cloud import call_agent


class OrganizerConnectionTests(unittest.TestCase):
    def test_custom_directory_and_service_are_passed_as_single_shell_arguments(self):
        config = {
            "ssh_host": "example.test",
            "ssh_user": "worker",
            "ssh_key": "test-key",
            "ssh_project_dir": "/srv/reader's notes; literal",
            "ssh_service": "custom-worker",
        }
        completed = SimpleNamespace(returncode=0, stdout=b'{"ok": true}')
        with patch("wisland.knowledge.organizer_cloud.subprocess.run", return_value=completed) as run:
            self.assertEqual(call_agent(config, {"mode": "topics"}), {"ok": True})
        command = run.call_args.args[0]
        remote = shlex.split(command[-1])
        self.assertEqual(remote[:10], [
            "docker", "compose", "--project-directory", config["ssh_project_dir"],
            "-f", config["ssh_project_dir"] + "/compose.yaml", "exec", "-T", "custom-worker", "python",
        ])
        self.assertEqual(command[-2], "worker@example.test")
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), {"mode": "topics"})

    def test_existing_deployments_keep_the_original_directory_and_service(self):
        completed = SimpleNamespace(returncode=0, stdout=b'{}')
        with patch("wisland.knowledge.organizer_cloud.subprocess.run", return_value=completed) as run:
            call_agent({"ssh_host": "example.test"}, {})
        remote = shlex.split(run.call_args.args[0][-1])
        self.assertEqual(remote[3], "/opt/wisland-knowledge-server")
        self.assertEqual(remote[8], "knowledge")

    def test_invalid_deployment_options_fail_before_ssh_starts(self):
        for values in ({"ssh_project_dir": "relative/path"}, {"ssh_project_dir": "/srv/\nnotes"},
                       {"ssh_service": "--privileged"}, {"ssh_service": "worker; command"}):
            with self.subTest(values=values), patch("wisland.knowledge.organizer_cloud.subprocess.run") as run:
                with self.assertRaises(ValueError):
                    call_agent(values, {})
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
