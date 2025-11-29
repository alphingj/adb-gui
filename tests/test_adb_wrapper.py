#!/usr/bin/env python3
"""
Unit tests for ADB GUI - Testing the ADBWrapper class
These tests mock the subprocess calls to test the wrapper functionality
"""

import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ADBWrapper:
    """Copy of ADBWrapper class for testing without tkinter dependency"""
    
    def __init__(self):
        self.adb_path = self._find_adb()
    
    def _find_adb(self):
        """Find ADB executable in PATH or common locations"""
        import subprocess
        try:
            result = subprocess.run(['which', 'adb'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        return 'adb'
    
    def run_command(self, *args, timeout=30):
        """Run an ADB command and return the output"""
        import subprocess
        cmd = [self.adb_path] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return result.stdout + result.stderr, result.returncode == 0
        except subprocess.TimeoutExpired:
            return "Command timed out", False
        except FileNotFoundError:
            return "ADB not found. Please install Android SDK Platform Tools.", False
        except Exception as e:
            return str(e), False
    
    def get_devices(self):
        """Get list of connected devices"""
        output, success = self.run_command('devices', '-l')
        if not success:
            return []
        
        devices = []
        lines = output.strip().split('\n')[1:]
        for line in lines:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    device_id = parts[0]
                    status = parts[1]
                    model = ""
                    for part in parts:
                        if part.startswith('model:'):
                            model = part.replace('model:', '')
                            break
                    devices.append({
                        'id': device_id,
                        'status': status,
                        'model': model
                    })
        return devices
    
    def list_packages(self, device_id=None, third_party_only=True):
        """List installed packages"""
        prefix = ['-s', device_id] if device_id else []
        args = prefix + ['shell', 'pm', 'list', 'packages']
        if third_party_only:
            args.append('-3')
        
        output, success = self.run_command(*args)
        if not success:
            return []
        
        packages = []
        for line in output.strip().split('\n'):
            if line.startswith('package:'):
                packages.append(line.replace('package:', ''))
        return sorted(packages)


class TestADBWrapper(unittest.TestCase):
    """Test cases for ADBWrapper class"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.adb = ADBWrapper()
    
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


class TestPackageParsing(unittest.TestCase):
    """Test package name parsing"""
    
    def test_parse_package_names(self):
        """Test parsing package names from pm list packages output"""
        output = """package:com.android.settings
package:com.google.android.apps.maps
package:org.example.myapp
"""
        packages = []
        for line in output.strip().split('\n'):
            if line.startswith('package:'):
                packages.append(line.replace('package:', ''))
        
        self.assertEqual(len(packages), 3)
        self.assertEqual(packages[0], 'com.android.settings')
        self.assertEqual(packages[1], 'com.google.android.apps.maps')
        self.assertEqual(packages[2], 'org.example.myapp')


class TestDeviceParsing(unittest.TestCase):
    """Test device parsing"""
    
    def test_parse_devices_output(self):
        """Test parsing devices output"""
        output = """List of devices attached
emulator-5554          device product:sdk_gphone_x86 model:sdk_gphone_x86 device:generic_x86 transport_id:1
192.168.1.100:5555     device product:q2q model:SM_F916B device:q2q transport_id:2
"""
        lines = output.strip().split('\n')[1:]
        devices = []
        
        for line in lines:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    device_id = parts[0]
                    status = parts[1]
                    model = ""
                    for part in parts:
                        if part.startswith('model:'):
                            model = part.replace('model:', '')
                            break
                    devices.append({
                        'id': device_id,
                        'status': status,
                        'model': model
                    })
        
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]['id'], 'emulator-5554')
        self.assertEqual(devices[0]['model'], 'sdk_gphone_x86')
        self.assertEqual(devices[1]['id'], '192.168.1.100:5555')
        self.assertEqual(devices[1]['model'], 'SM_F916B')


if __name__ == '__main__':
    unittest.main()
