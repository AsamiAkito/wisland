import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wisland.knowledge.tunnel import SshTunnel, tunnel_command


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        key = Path(self.temp.name) / 'test-key'
        key.write_text('not a real key', encoding='ascii')
        self.config = {'ssh_enabled': True, 'ssh_host': 'example.test', 'ssh_user': 'root',
                       'ssh_key': str(key), 'ssh_local_port': 18767}

    def tearDown(self):
        self.temp.cleanup()

    def test_command_uses_verified_key_authentication_only(self):
        with patch('shutil.which', return_value='ssh.exe'):
            command = tunnel_command(self.config)
        self.assertIn('BatchMode=yes', command)
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('127.0.0.1:18767:127.0.0.1:8766', command)
        self.assertNotIn('-pw', command)

    def test_hidden_process_is_started_once_and_closed_on_stop(self):
        process = Mock()
        process.poll.return_value = None
        manager = SshTunnel()
        with patch('shutil.which', return_value='ssh.exe'), patch('wisland.knowledge.tunnel.port_open', return_value=False), patch('subprocess.Popen', return_value=process) as start:
            manager.ensure(self.config)
            manager.ensure(self.config)
            self.assertEqual(start.call_count, 1)
            self.assertEqual(start.call_args.kwargs['creationflags'], 0x08000000 if os.name == 'nt' else 0)
        manager.stop()
        process.terminate.assert_called_once()
        self.assertIsNone(manager.process)

    def test_authentication_failure_does_not_repeat_login(self):
        process = Mock()
        process.poll.return_value = None
        process.stderr.read.return_value = b'Permission denied (publickey)'
        manager = SshTunnel()
        with patch('shutil.which', return_value='ssh.exe'), patch('wisland.knowledge.tunnel.port_open', return_value=False), patch('subprocess.Popen', return_value=process) as start:
            manager.ensure(self.config)
            process.poll.return_value = 255
            self.assertIn('认证失败', manager.ensure(self.config))
            manager.ensure(self.config)
            self.assertEqual(start.call_count, 1)

    def test_existing_connection_is_not_killed_and_is_taken_over_after_close(self):
        manager = SshTunnel()
        process = Mock()
        process.poll.return_value = None
        with patch('shutil.which', return_value='ssh.exe'), patch('wisland.knowledge.tunnel.port_open', side_effect=[True, False]), patch('subprocess.Popen', return_value=process) as start:
            self.assertIn('已有本机连接', manager.ensure(self.config))
            start.assert_not_called()
            manager.ensure(self.config)
            start.assert_called_once()
        manager.stop()

    def test_network_failure_waits_before_reconnecting(self):
        first, second = Mock(), Mock()
        first.poll.return_value = None
        first.stderr.read.return_value = b'Connection reset by peer'
        second.poll.return_value = None
        manager = SshTunnel()
        with patch('shutil.which', return_value='ssh.exe'), patch('wisland.knowledge.tunnel.port_open', return_value=False), patch('subprocess.Popen', side_effect=[first, second]) as start:
            with patch('time.monotonic', return_value=0):
                manager.ensure(self.config)
                first.poll.return_value = 1
                manager.ensure(self.config)
            with patch('time.monotonic', return_value=1):
                self.assertIn('等待重连', manager.ensure(self.config))
                self.assertEqual(start.call_count, 1)
            with patch('time.monotonic', return_value=100):
                manager.ensure(self.config)
                self.assertEqual(start.call_count, 2)
        manager.stop()
