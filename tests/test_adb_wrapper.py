#!/usr/bin/env python3
"""
Tests for the real ADBWrapper in adb_gui.

This file used to contain a private copy of ADBWrapper "so it could be
tested without tkinter", which meant every test here passed against a
second implementation while the shipped one could be broken. It now
imports the real class; `import adb_gui` only needs tkinter to be
present, not a display.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add the parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adb_gui import ADBWrapper, parse_device_list  # noqa: E402


class TestADBWrapper(unittest.TestCase):
    """Test cases for the shipped ADBWrapper class"""

    def setUp(self):
        """Set up test fixtures"""
        self.adb = ADBWrapper()
        self.assertIsInstance(self.adb, ADBWrapper)

    @patch('subprocess.run')
    def test_get_devices_with_devices(self, mock_run):
        """Test get_devices with connected devices"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """List of devices attached
abc12345       device usb:123 product:device model:Pixel_5 device:redfin
def67890       device usb:456 product:device model:Galaxy_S21 device:samsung
"""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        devices = self.adb.get_devices()

        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['id'], 'abc12345')
        self.assertEqual(devices[0]['status'], 'device')
        self.assertEqual(devices[0]['model'], 'Pixel_5')
        self.assertEqual(devices[1]['id'], 'def67890')
        self.assertEqual(devices[1]['model'], 'Galaxy_S21')

    @patch('subprocess.run')
    def test_get_devices_no_devices(self, mock_run):
        """Test get_devices with no connected devices"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "List of devices attached\n"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        devices = self.adb.get_devices()

        self.assertEqual(len(devices), 0)

    @patch('subprocess.run')
    def test_get_devices_unauthorized(self, mock_run):
        """Test get_devices with unauthorized device"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """List of devices attached
abc12345       unauthorized
"""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        devices = self.adb.get_devices()

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['status'], 'unauthorized')

    @patch('subprocess.run')
    def test_list_packages(self, mock_run):
        """Test list_packages"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = """package:com.example.app1
package:com.example.app2
package:com.test.app3
"""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        packages = self.adb.list_packages('device123')

        self.assertEqual(len(packages), 3)
        self.assertIn('com.example.app1', packages)
        self.assertIn('com.example.app2', packages)
        self.assertIn('com.test.app3', packages)
        # The call itself must select the device and the -3 filter.
        sent = mock_run.call_args[0][0]
        self.assertEqual(sent[1:3], ['-s', 'device123'])
        self.assertIn('-3', sent)

    @patch('subprocess.run')
    def test_run_command_success(self, mock_run):
        """Test run_command success"""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "success"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        output, success = self.adb.run_command('version')

        self.assertTrue(success)
        self.assertEqual(output, "success")

    @patch('subprocess.run')
    def test_run_command_failure(self, mock_run):
        """Test run_command failure"""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error message"
        mock_run.return_value = mock_result

        output, success = self.adb.run_command('invalid_command')

        self.assertFalse(success)
        self.assertIn("error message", output)

    @patch('subprocess.run')
    def test_run_command_timeout(self, mock_run):
        """Test run_command timeout"""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='adb', timeout=30)

        output, success = self.adb.run_command('long_running_command')

        self.assertFalse(success)
        self.assertIn("timed out", output)

    @patch('subprocess.run')
    def test_run_command_not_found(self, mock_run):
        """Test run_command when ADB is not found"""
        mock_run.side_effect = FileNotFoundError()

        output, success = self.adb.run_command('version')

        self.assertFalse(success)
        self.assertIn("not found", output.lower())

    @patch('subprocess.run')
    def test_run_command_survives_undecodable_output(self, mock_run):
        """A device answering in non-UTF-8 must not break the command."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = None
        mock_result.stderr = None
        # Simulate what subprocess does with errors='replace'.
        mock_run.return_value = mock_result
        mock_run.side_effect = None

        def run(cmd, **kwargs):
            self.assertEqual(kwargs.get('errors'), 'replace')
            result = MagicMock()
            result.returncode = 0
            result.stdout = 'ok\ufffddef'
            result.stderr = ''
            return result

        mock_run.side_effect = run

        output, success = self.adb.run_command('shell', 'ls')

        self.assertTrue(success)
        self.assertIn('\ufffd', output)


class TestPackageParsing(unittest.TestCase):
    """Package name parsing lives in one place: ADBWrapper._package_names."""

    def test_parse_package_names(self):
        output = """package:com.android.settings
package:com.google.android.apps.maps
package:org.example.myapp
"""
        self.assertEqual(
            ADBWrapper._package_names(output),
            ['com.android.settings', 'com.google.android.apps.maps',
             'org.example.myapp'])

    def test_non_package_lines_are_ignored(self):
        output = "Error: failure\npackage:com.x\n"
        self.assertEqual(ADBWrapper._package_names(output), ['com.x'])


class TestDeviceParsing(unittest.TestCase):
    """Device list parsing lives in parse_device_list."""

    def test_parse_devices_output(self):
        output = """List of devices attached
emulator-5554          device product:sdk_gphone_x86 model:sdk_gphone_x86 device:generic_x86 transport_id:1
192.168.1.100:5555     device product:q2q model:SM_F916B device:q2q transport_id:2
"""
        devices = parse_device_list(output)

        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['id'], 'emulator-5554')
        self.assertEqual(devices[0]['model'], 'sdk_gphone_x86')
        self.assertEqual(devices[1]['id'], '192.168.1.100:5555')
        self.assertEqual(devices[1]['model'], 'SM_F916B')


if __name__ == '__main__':
    unittest.main()
