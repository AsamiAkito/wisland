import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from wisland.services import usage


@unittest.skipUnless(os.name == "nt", "Windows CLI discovery and process handling")
class CodexProcessTests(unittest.TestCase):
    def test_desktop_package_when_cli_is_missing_from_path(self):
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "app/resources/codex.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            result = subprocess.CompletedProcess([], 0, stdout=folder + "\n")
            with patch.object(usage.shutil, "which", return_value=None), patch.object(
                    usage.subprocess, "run", return_value=result) as lookup:
                self.assertEqual(Path(usage._codex_executable()), binary)
                self.assertIn("Get-AppxPackage -Name OpenAI.Codex", lookup.call_args.args[0][-1])

    def test_timeout_releases_stdout_inherited_by_cmd_child(self):
        with tempfile.TemporaryDirectory() as folder:
            shim = Path(folder) / "codex.cmd"
            shim.write_text(f'@echo off\n"{sys.executable}" -c "import time;time.sleep(5)"\n')
            timer = threading.Timer
            with patch.object(usage.shutil, "which", return_value=str(shim)), patch.object(
                    usage.threading, "Timer", side_effect=lambda _, fn: timer(0.5, fn)):
                start = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "超时"):
                    usage.codex_limits()
                self.assertLess(time.monotonic() - start, 3)
