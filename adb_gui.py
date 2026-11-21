#!/usr/bin/env python3
"""
ADB GUI - A graphical user interface for Android Debug Bridge (ADB)
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import base64
import atexit
import io
import json
import os
import posixpath
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

DEFAULT_ICON_SIZE = 128
PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
PLAY_STORE_URL = ('https://play.google.com/store/apps/details'
                  '?id={package}&hl=en&gl=US')
USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/120 Safari/537.36')

# States adb reports in the second column of `adb devices` output.
DEVICE_STATUSES = {
    'device', 'offline', 'unauthorized', 'recovery', 'sideload',
    'rescue', 'bootloader', 'host', 'no permissions', 'unknown',
}


class AppConfig:
    """Tiny JSON settings store persisted under the user config dir.

    Remembers window geometry, last-selected device, dark-mode preference,
    and default push/pull directories so the app feels like a real product
    instead of forgetting everything on restart.
    """

    DEFAULT = {
        'window_geometry': '1200x700',
        'dark_mode': False,
        'last_device': '',
        'default_pull_dir': str(Path.home()),
        'default_push_dir': str(Path.home()),
    }

    def __init__(self):
        self.path = Path.home() / '.config' / 'adb-gui' / 'settings.json'
        self._data = dict(self.DEFAULT)
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                with self.path.open('r', encoding='utf-8') as fh:
                    self._data.update(json.load(fh))
        except Exception:
            # Corrupt or unreadable config is not fatal; start fresh.
            self._data = dict(self.DEFAULT)

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('w', encoding='utf-8') as fh:
                json.dump(self._data, fh, indent=2)
        except Exception:
            pass  # Best effort; config failure must never crash the app.

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


CONFIG = AppConfig()


def parse_device_list(output):
    """Parse `adb devices -l` output into a list of device dicts.

    Only lines whose second token is a known adb device state are kept, so
    adb's own chatter (server version warnings, daemon startup banners) can
    never be mistaken for a connected device.
    """
    devices = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue

        device_id, status = parts[0], parts[1]
        if status == 'no' and len(parts) > 2 and parts[2] == 'permissions':
            status = 'no permissions'  # "no permissions (missing udev rules)"
        elif status not in DEVICE_STATUSES:
            continue
        # A serial never ends in ':' — that rejects "error: device offline".
        if device_id.endswith(':'):
            continue

        model = ''
        for part in parts[2:]:
            if part.startswith('model:'):
                model = part[len('model:'):]
                break

        devices.append({
            'id': device_id,
            'status': status,
            'model': model
        })
    return devices


def decode_icon(image_bytes, max_size=DEFAULT_ICON_SIZE):
    """Turn icon bytes into something displayable. Never touches Tk.

    Returns PNG bytes no larger than `max_size`, or None when the data is
    not an image this machine can decode — adb likes to answer with plain
    text such as "Unknown command: dump-icon".
    """
    if not image_bytes:
        return None
    if HAS_PIL:
        try:
            image = Image.open(io.BytesIO(image_bytes)).convert('RGBA')
            image.thumbnail((max_size, max_size), Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format='PNG')
            return buffer.getvalue()
        except Exception:
            return None
    if image_bytes.startswith(PNG_SIGNATURE):
        return image_bytes
    return None


def make_photo(png_bytes, max_size=DEFAULT_ICON_SIZE):
    """Wrap PNG bytes in a tk.PhotoImage. Tk objects belong on the Tk thread."""
    photo = tk.PhotoImage(data=base64.b64encode(png_bytes).decode('ascii'))
    largest = max(photo.width(), photo.height())
    if largest > max_size:
        # Integer shrink, used when Pillow was not there to downscale for us.
        photo = photo.subsample(-(-largest // max_size))
    return photo


def parse_play_icon_url(html):
    """Return the og:image URL of a Play Store listing page, if it has one."""
    match = (re.search(r'<meta[^>]*property="og:image"[^>]*content="([^"]+)"', html)
             or re.search(r'<meta[^>]*content="([^"]+)"[^>]*property="og:image"', html))
    return match.group(1) if match else None


def fetch_play_icon(package_name, timeout=15):
    """Download an app icon from its Play Store listing.

    Fallback for devices whose `cmd package` cannot dump icons. Returns
    (bytes, None) or (None, reason) — only one app is looked up, and only
    when the user selects it.
    """
    headers = {'User-Agent': USER_AGENT, 'Accept-Language': 'en-US,en;q=0.9'}

    def get(url, limit):
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout
        ) as response:
            return response.read(limit)

    try:
        html = get(PLAY_STORE_URL.format(package=package_name),
                   4_000_000).decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, "not published on the Play Store"
        return None, f"Play Store answered HTTP {error.code}"
    except (urllib.error.URLError, OSError):
        return None, "no network connection"

    icon_url = parse_play_icon_url(html)
    if not icon_url:
        return None, "no icon listed on the Play Store"

    try:
        return get(icon_url, 5_000_000), None
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        return None, "could not download the Play Store icon"


class ADBWrapper:
    """Wrapper class for ADB commands"""
    
    def __init__(self):
        self.adb_path = self._find_adb()
        self._track_proc = None
        self._track_lock = threading.Lock()
        self._tracking_stopped = False
    
    def _find_adb(self):
        """Find ADB executable in PATH or common locations"""
        adb_in_path = shutil.which('adb')
        if adb_in_path:
            return adb_in_path
        
        common_paths = []
        
        if sys.platform == 'win32':
            common_paths = [
                r'C:\Users\%USERNAME%\AppData\Local\Android\Sdk\platform-tools\adb.exe',
                r'C:\android-sdk\platform-tools\adb.exe',
            ]
        else:
            common_paths = [
                '/usr/bin/adb',
                '/usr/local/bin/adb',
                '/opt/android-sdk/platform-tools/adb',
                os.path.expanduser('~/Android/Sdk/platform-tools/adb'),
                os.path.expanduser('~/Library/Android/sdk/platform-tools/adb'),
            ]
        
        for path in common_paths:
            expanded = os.path.expandvars(path) if sys.platform == 'win32' else path
            if os.path.exists(expanded):
                return expanded
        
        return 'adb'
    
    def run_command(self, *args, timeout=30, binary=False):
        """Run an ADB command and return the output
        
        Args:
            *args: Command arguments
            timeout: Timeout in seconds
            binary: If True, return bytes instead of string
            
        Returns:
            tuple: (output, success)
        """
        cmd = [self.adb_path] + list(args)
        try:
            if binary:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout)
                return result.stdout + result.stderr, result.returncode == 0
            else:
                # errors='replace': a single non-UTF-8 file name on the device
                # must not abort the whole listing.
                result = subprocess.run(cmd, capture_output=True, text=True,
                                        errors='replace', timeout=timeout)
                return result.stdout + result.stderr, result.returncode == 0
        except subprocess.TimeoutExpired:
            if binary:
                return b"Command timed out", False
            return "Command timed out", False
        except FileNotFoundError:
            if binary:
                return b"ADB not found", False
            return "ADB not found. Please install Android SDK Platform Tools.", False
        except Exception as e:
            if binary:
                return str(e).encode(), False
            return str(e), False
    
    def get_devices(self):
        """Get list of connected devices"""
        output, success = self.run_command('devices', '-l')
        if not success:
            return []
        return parse_device_list(output)

    def track_devices(self, on_change):
        """Block while streaming device-list change notifications.

        Runs `adb track-devices -l` and consumes the hex4-length framed
        stream the adb server emits whenever a device is added, removed or
        changes state, calling `on_change` for every frame. Returns when the
        stream ends; the return value reports whether the stream was ever
        framed correctly (False means track-devices is unavailable).

        `on_change` runs on the calling thread, so the caller is responsible
        for marshalling back onto the Tk thread.
        """
        try:
            proc = subprocess.Popen(
                [self.adb_path, 'track-devices', '-l'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
        except OSError:
            return False

        framed = False
        with self._track_lock:
            if self._tracking_stopped:
                # stop_tracking ran while Popen was in flight; without this
                # the child would never be registered and never killed.
                register = False
            else:
                self._track_proc = proc
                register = True
        if not register:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
            return False
        try:
            while True:
                header = proc.stdout.read(4)
                if len(header) < 4:
                    return framed
                try:
                    length = int(header, 16)
                except ValueError:
                    return framed
                framed = True
                remaining = length
                while remaining:
                    chunk = proc.stdout.read(remaining)
                    if not chunk:
                        return framed
                    remaining -= len(chunk)
                on_change()
        finally:
            with self._track_lock:
                if self._track_proc is proc:
                    self._track_proc = None
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

    def stop_tracking(self):
        """Stop tracking for good: kill the stream and refuse to start another.

        Called on shutdown, so a stream racing with the kill is stopped too
        rather than leaking an unregistered `adb track-devices` child.
        """
        with self._track_lock:
            self._tracking_stopped = True
            proc, self._track_proc = self._track_proc, None
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
    
    def get_device_info(self, device_id=None):
        """Get detailed device information"""
        prefix = ['-s', device_id] if device_id else []
        
        info = {}
        
        props = [
            ('Model', 'ro.product.model'),
            ('Brand', 'ro.product.brand'),
            ('Manufacturer', 'ro.product.manufacturer'),
            ('Android Version', 'ro.build.version.release'),
            ('SDK Version', 'ro.build.version.sdk'),
            ('Build Number', 'ro.build.display.id'),
            ('Device', 'ro.product.device'),
            ('Hardware', 'ro.hardware'),
            ('Serial Number', 'ro.serialno'),
        ]
        
        for name, prop in props:
            output, success = self.run_command(*prefix, 'shell', 'getprop', prop)
            if success:
                info[name] = output.strip()
        
        output, success = self.run_command(*prefix, 'shell', 'dumpsys', 'battery')
        if success:
            for line in output.split('\n'):
                if 'level:' in line:
                    info['Battery Level'] = line.split(':')[1].strip() + '%'
                elif 'status:' in line:
                    status_code = line.split(':')[1].strip()
                    statuses = {'1': 'Unknown', '2': 'Charging', '3': 'Discharging', '4': 'Not charging', '5': 'Full'}
                    info['Battery Status'] = statuses.get(status_code, status_code)
        
        return info
    
    @staticmethod
    def _package_names(output):
        """Package names from any `pm list packages` output."""
        return sorted(
            line[len('package:'):] for line in output.strip().splitlines()
            if line.startswith('package:')
        )
    
    def list_packages(self, device_id=None, third_party_only=True):
        """List installed packages"""
        prefix = ['-s', device_id] if device_id else []
        args = prefix + ['shell', 'pm', 'list', 'packages']
        if third_party_only:
            args.append('-3')
        
        output, success = self.run_command(*args)
        if not success:
            return []
        return self._package_names(output)
    
    def list_frozen_packages(self, device_id=None):
        """Packages that are frozen (disabled) instead of uninstalled."""
        prefix = ['-s', device_id] if device_id else []
        output, success = self.run_command(
            *prefix, 'shell', 'pm', 'list', 'packages', '-d')
        if not success:
            return []
        return self._package_names(output)
    
    def freeze_package(self, package_name, device_id=None):
        """Freeze an app in place: disabled, still installed, data kept.

        `pm disable-user` (not `uninstall`) is what makes this reversible;
        `-k` must not be combined with `--user`, Android rejects it.
        """
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'pm', 'disable-user',
                                '--user', '0', package_name, timeout=60)
    
    def unfreeze_package(self, package_name, device_id=None):
        """Bring a frozen app back to its normal state."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'pm', 'enable',
                                '--user', '0', package_name, timeout=60)
    
    def install_apk(self, apk_path, device_id=None):
        """Install an APK file"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'install', '-r', apk_path, timeout=120)
    
    def uninstall_package(self, package_name, device_id=None):
        """Uninstall a package"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'uninstall', package_name)
    
    def list_files(self, path, device_id=None):
        """List files in a directory on the device.

        Returns (files, error): error is None when the listing succeeded.
        An empty directory is not an error; a failed `ls` (permission
        denied, missing directory) comes back as a message so the caller
        can tell the two apart instead of showing a blank listing.

        The path is quoted because `adb shell` joins its arguments with
        spaces, so an unquoted path containing spaces lists nothing.
        """
        prefix = ['-s', device_id] if device_id else []
        # A trailing slash makes `ls` dereference a symlinked directory
        # (`/sdcard` is one on modern Android): without it the listing is a
        # single row describing the link instead of the directory's contents.
        listing_path = path if path.endswith('/') else path + '/'
        output, success = self.run_command(
            *prefix, 'shell', 'ls -la ' + shlex.quote(listing_path))
        if not success:
            lines = [line for line in output.splitlines() if line.strip()]
            return [], lines[-1] if lines else f'Cannot list {path}'
        
        files = []
        for line in output.splitlines():
            line = line.rstrip('\r')
            if not line:
                continue
            head = line.split(None, 1)
            if not head:
                continue
            if '?' in head[0]:
                # toybox masks every field it may not stat as `?`, and the
                # date and time collapse into one column, so the name
                # follows six fields here instead of seven. Parsing those
                # rows the normal way shifts the columns and two masked
                # rows can end up with the same name (crashing the tree).
                fields = line.split(None, 6)
                if len(fields) < 7:
                    continue
                permissions, name, size = fields[0], fields[6], fields[4]
            else:
                # maxsplit with None as the separator splits on runs of
                # whitespace: `ls` pads the size column with several spaces,
                # and those must count as one separator. The remainder after
                # the seventh field is the name, spaces inside it included.
                fields = line.split(None, 7)
                if len(fields) < 8:
                    continue
                permissions, name, size = fields[0], fields[7], fields[4]
            # Rows are 10-character permission strings; `total 4` and any
            # stray error text are not, so they cannot parse as entries.
            # A fully masked first character still counts: better a row
            # with `?` metadata than no row at all.
            if len(permissions) != 10 or permissions[0] not in 'dcbpls-?':
                continue
            # Symlinks print as `name -> target`; split that off, but only
            # for symlinks, so a regular file called `a -> b` is kept whole.
            if permissions[0] == 'l' and ' -> ' in name:
                name = name.split(' -> ', 1)[0]
            if name in ('.', '..'):
                continue
            files.append({
                'name': name,
                'is_dir': permissions.startswith('d'),
                # A symlink may well point at a directory (`/sdcard` does);
                # `ls` cannot tell us, so mark it and let a double-click
                # try the listing — a file target then reports "Not a
                # directory" instead of doing nothing.
                'is_link': permissions.startswith('l'),
                'size': size,
                'permissions': permissions
            })
        return files, None
    
    def pull_file(self, remote_path, local_path, device_id=None):
        """Pull a file from device"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'pull', remote_path, local_path, timeout=300)
    
    def push_file(self, local_path, remote_path, device_id=None):
        """Push a file to device"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'push', local_path, remote_path, timeout=300)

    def mkdir(self, path, device_id=None):
        """Create a directory on the device: `mkdir -p <path>`."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'mkdir', '-p', path)

    def delete(self, path, device_id=None, recursive=False):
        """Delete a file or directory on the device.

        Uses `rm -r` for directories so non-empty folders can be removed.
        """
        prefix = ['-s', device_id] if device_id else []
        cmd = ['shell', 'rm']
        if recursive:
            cmd.append('-r')
        cmd.append(shlex.quote(path))
        return self.run_command(*prefix, *cmd)

    def rename(self, src, dst, device_id=None):
        """Rename / move a file or directory on the device: `mv <src> <dst>`."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'mv',
                                shlex.quote(src), shlex.quote(dst))

    def find_files(self, path, pattern, device_id=None):
        """Find files on the device: `find <path> -name <pattern>`.

        Returns (output, success). output is the raw find result.
        """
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(
            *prefix, 'shell', 'find', path, '-name', shlex.quote(pattern))

    def take_screenshot(self, save_path, device_id=None):
        """Take a screenshot"""
        prefix = ['-s', device_id] if device_id else []
        output, success = self.run_command(*prefix, 'shell', 'screencap', '-p', '/sdcard/screenshot.png')
        if not success:
            return output, False
        output, success = self.run_command(*prefix, 'pull', '/sdcard/screenshot.png', save_path)
        self.run_command(*prefix, 'shell', 'rm', '/sdcard/screenshot.png')
        return output, success
    
    def run_shell_command(self, command, device_id=None):
        """Run a shell command on device"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', command, timeout=60)
    
    def reboot(self, mode='', device_id=None):
        """Reboot the device"""
        prefix = ['-s', device_id] if device_id else []
        args = prefix + ['reboot']
        if mode:
            args.append(mode)
        return self.run_command(*args)
    
    def get_state(self, device_id=None):
        """Get device state"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'get-state')
    
    def get_serial(self, device_id=None):
        """Get device serial number"""
        prefix = ['-s', device_id] if device_id else []
        output, success = self.run_command(*prefix, 'get-serialno')
        return output.strip() if success else None
    
    def kill_adb(self):
        """Kill the ADB server"""
        return self.run_command('kill-server')
    
    def start_adb_server(self):
        """Start the ADB server"""
        return self.run_command('start-server')
    
    def install_multiple_apks(self, apk_paths, device_id=None):
        """Install multiple APK files"""
        prefix = ['-s', device_id] if device_id else []
        all_success = True
        results = []
        for apk_path in apk_paths:
            output, success = self.run_command(*prefix, 'install', '-r', apk_path, timeout=120)
            results.append((apk_path, output, success))
            if not success:
                all_success = False
        return output, all_success
    
    def clear_app_data(self, package_name, device_id=None):
        """Clear app data"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'pm', 'clear', package_name)
    
    def clear_app_cache(self, package_name, device_id=None):
        """Clear app cache"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'pm', 'clear', '--cache-only', package_name)
    
    def get_app_info(self, package_name, device_id=None):
        """Get app information"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'dumpsys', 'package', package_name)

    def get_exported_activities(self, package_name, device_id=None):
        """List exported (launchable) activities for a package.

        Parses `pm dump <pkg>` and returns activity names that carry
        `android:exported="true"` and an intent filter with MAIN/LAUNCHER,
        plus any other exported activities the package declares.
        """
        prefix = ['-s', device_id] if device_id else []
        out, ok = self.run_command(*prefix, 'shell', 'pm', 'dump', package_name)
        if not ok:
            return [], out
        activities = []
        current = None
        exported = False
        has_main_launcher = False
        for line in out.splitlines():
            stripped = line.strip()
            # Activity declarations look like "  Activity x: .FooActivity"
            m = re.match(r'Activity\s+\S+:\s+(\S+)', stripped)
            if m:
                if current and (exported or has_main_launcher):
                    activities.append(current)
                current = m.group(1)
                exported = False
                has_main_launcher = False
                continue
            if current is None:
                continue
            low = stripped.lower()
            if 'exported="true"' in low or "exported='true'" in low:
                exported = True
            if 'action android:name="android.intent.action.MAIN"' in low:
                has_main_launcher = True
            if 'category android:name="android.intent.category.LAUNCHER"' in low:
                has_main_launcher = True
        if current and (exported or has_main_launcher):
            activities.append(current)
        return activities, ""

    def launch_activity(self, package_name, activity, device_id=None):
        """Launch a specific activity: `am start -n <pkg>/<activity>`."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'am', 'start',
                                '-n', f'{package_name}/{activity}')

    def send_intent(self, action, data, package, device_id=None):
        """Send an intent: `am start -a <action> -d <data> <pkg>`."""
        prefix = ['-s', device_id] if device_id else []
        cmd = ['shell', 'am', 'start', '-a', action]
        if data:
            cmd += ['-d', data]
        if package:
            cmd += [package]
        return self.run_command(*prefix, *cmd)

    def send_broadcast(self, action, device_id=None):
        """Send a broadcast: `am broadcast -a <action>`."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'am', 'broadcast', '-a', action)

    def get_app_ops(self, package_name, device_id=None):
        """Get runtime permission/op state for a package."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'dumpsys', 'appops',
                                package_name)

    def force_stop_app(self, package_name, device_id=None):
        """Force stop an app"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'am', 'force-stop', package_name)
    
    def sync_files(self, device_id=None):
        """Sync files to/from device"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'sync', timeout=60)
    
    def forward_port(self, host_port, device_port, device_id=None):
        """Forward port from host to device"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'forward', f'tcp:{host_port}', f'tcp:{device_port}')
    
    def reverse_port(self, device_port, host_port, device_id=None):
        """Reverse port from device to host"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'reverse', f'tcp:{device_port}', f'tcp:{host_port}')

    def set_stay_on(self, device_id=None, enabled=True):
        """Keep the screen on while charging (or restore normal sleep)."""
        value = 'true' if enabled else 'false'
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'svc', 'power', 'stayon', value)

    def set_wifi(self, device_id=None, enabled=True):
        """Turn Wi-Fi on or off."""
        value = 'enable' if enabled else 'disable'
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'svc', 'wifi', value)

    def set_bluetooth(self, device_id=None, enabled=True):
        """Turn Bluetooth on or off."""
        value = 'enable' if enabled else 'disable'
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'svc', 'bluetooth', value)

    def set_dnd(self, device_id=None, enabled=True):
        """Toggle Do Not Disturb (Zen mode 0=off, 1=no interruptions)."""
        value = '1' if enabled else '0'
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'shell', 'settings', 'put',
                                'secure', 'zen_mode', value)

    def set_ui_mode(self, device_id=None, mode='normal'):
        """Set UI mode: normal, night, car, or desk."""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'cmd', 'uimode', mode)

    def extract_apk(self, package_name, local_path, device_id=None):
        """Pull an installed APK from the device to a local file.

        Resolves the package's install path via `pm path`, then pulls it.
        """
        prefix = ['-s', device_id] if device_id else []

        def run():
            out, ok = self.run_command(*prefix, 'shell', 'pm', 'path',
                                       package_name)
            if not ok:
                return out, False
            # pm path prints "package:/data/app/.../base.apk"
            remote = None
            for line in out.splitlines():
                if line.startswith('package:'):
                    remote = line.split(':', 1)[1].strip()
                    break
            if not remote:
                return f"Could not resolve path for {package_name}", False
            return self.pull_file(remote, local_path, device_id)

        return run()

    def run_on_all_devices(self, command, *args):
        """Run one adb shell command on every connected device.

        Returns a dict of {serial: (output, success)}.
        """
        devices = self.get_devices()
        results = {}
        for dev in devices:
            serial = dev.get('serial') or dev.get('id')
            if not serial:
                continue
            out, ok = self.run_command('-s', serial, 'shell', command,
                                       *args)
            results[serial] = (out, ok)
        return results

    def get_app_icon(self, package_name, device_id=None):
        """Get an app icon as PNG bytes

        Returns:
            tuple: (bytes or None, error message or None)
        """
        prefix = ['-s', device_id] if device_id else []
        
        output, success = self.run_command(
            *prefix, 'shell', 'cmd', 'package', 'dump-icon', package_name,
            '--png', binary=True
        )
        if not output:
            return None, "Could not retrieve icon"
        if not output.startswith(PNG_SIGNATURE):
            # adb answered with text, e.g. "Unknown command: dump-icon".
            first_line = output.decode('utf-8', 'replace').strip().split('\n')[0]
            return None, first_line or "Device returned no icon"
        if not success:
            return None, "Could not retrieve icon"
        return output, None


class TaskRunner:
    """Runs blocking work off the Tk thread and hands results back to it.

    The Tk thread only ever queues jobs (`submit`) and drains completed
    callbacks (`_drain` on a timer); worker threads only ever queue results
    (`post`). Neither side touches the other's queue directly, so no Tk call
    is ever made from a background thread and the UI never blocks on adb.
    """

    POLL_MS = 30

    def __init__(self, root, on_status=None):
        self._root = root
        self._on_status = on_status
        self._jobs = queue.Queue()
        self._ready = queue.Queue()
        self._labels = []
        self._drain_id = None
        threading.Thread(target=self._work, name='adb-worker', daemon=True).start()
        self._drain()

    def close(self):
        """Stop delivering results. Call before the root is destroyed."""
        if self._drain_id is None:
            return
        try:
            self._root.after_cancel(self._drain_id)
        except tk.TclError:
            pass
        self._drain_id = None

    def post(self, fn, *args):
        """Schedule fn(*args) on the Tk thread. Safe from any thread."""
        self._ready.put((fn, args))

    def submit(self, fn, on_done=None, label="", widgets=()):
        """Queue fn() on the worker; on_done(result) runs on the Tk thread.

        `label` is shown in the status bar while the job is pending and
        `widgets` are disabled until it finishes.
        """
        for widget in widgets:
            widget.config(state='disabled')
        self._labels.append(label)
        self._jobs.put((fn, on_done, label, widgets))
        self._notify_status()

    def _work(self):
        while True:
            fn, on_done, label, widgets = self._jobs.get()
            try:
                result, error = fn(), None
            except Exception as exc:
                result, error = None, exc
            self.post(self._finish, on_done, result, error, label, widgets)

    def _finish(self, on_done, result, error, label, widgets):
        if label in self._labels:
            self._labels.remove(label)
        for widget in widgets:
            try:
                widget.config(state='normal')
            except tk.TclError:
                pass
        if error is not None:
            messagebox.showerror(
                "Error",
                f"{label or 'Background task'} failed:\n{error}"
            )
        elif on_done is not None:
            on_done(result)
        self._notify_status()

    def _notify_status(self):
        if self._on_status:
            self._on_status(self._labels[-1] if self._labels else None)

    def _drain(self):
        # Reschedule before running callbacks: a callback may open a modal
        # dialog, and that nested event loop must still deliver results.
        try:
            self._drain_id = (self._root.after(self.POLL_MS, self._drain)
                              if self._root.winfo_exists() else None)
        except tk.TclError:
            self._drain_id = None
            return
        while True:
            try:
                fn, args = self._ready.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception:
                traceback.print_exc()


class DevicePanel(ttk.LabelFrame):
    """Panel for device selection and basic info"""
    
    def __init__(self, parent, adb, runner, on_device_change=None):
        super().__init__(parent, text="Device", padding=10)
        self.adb = adb
        self.runner = runner
        self.on_device_change = on_device_change
        self.current_device = None
        self.devices = {}
        self._selected_state = None
        
        ttk.Label(self, text="Select Device:").grid(row=0, column=0, sticky='w')
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(self, textvariable=self.device_var, width=40, state='readonly')
        self.device_combo.grid(row=0, column=1, padx=5, sticky='ew')
        self.device_combo.bind('<<ComboboxSelected>>', self._on_device_selected)
        
        ttk.Button(self, text="↻ Refresh", command=self.refresh_devices).grid(row=0, column=2, padx=5)
        
        self.status_label = ttk.Label(self, text="No device connected", foreground='gray')
        self.status_label.grid(row=1, column=0, columnspan=3, sticky='w', pady=(5, 0))
        
        self.columnconfigure(1, weight=1)
    
    def refresh_devices(self):
        """Refresh the device list without blocking the Tk thread."""
        self.runner.submit(self.adb.get_devices, self._apply_devices,
                           label="Listing devices")

    def _apply_devices(self, devices):
        self.devices = {f"{d['id']} ({d['model']})" if d['model'] else d['id']: d for d in devices}
        
        device_names = list(self.devices.keys())
        self.device_combo['values'] = device_names
        
        if device_names:
            if self.device_var.get() not in device_names:
                self.device_combo.current(0)
            self._on_device_selected(None)
        else:
            self.device_var.set('')
            self.current_device = None
            self.status_label.config(text="No device connected", foreground='gray')
            self._selected_state = None
            # Panels ignore this when they already have no device, so this
            # only ever resets state left over from a device we just lost.
            if self.on_device_change:
                self.on_device_change(None)
    
    def _on_device_selected(self, event):
        """Handle device selection.

        Only reports a change to the panels when the selected device *or*
        its state actually differs from the last one reported, so periodic
        refreshes can never reset panels the user is working in.
        """
        selected = self.device_var.get()
        if selected not in self.devices:
            return

        device = self.devices[selected]
        status = device['status']
        color = 'green' if status == 'device' else 'orange'
        self.status_label.config(text=f"Status: {status}", foreground=color)

        state = (device['id'], status)
        if state == self._selected_state:
            return
        self._selected_state = state
        self.current_device = device['id']
        if self.on_device_change:
            self.on_device_change(self.current_device)
    
    def get_current_device(self):
        """Get the currently selected device ID"""
        return self.current_device


class DeviceInfoPanel(ttk.LabelFrame):
    """Panel for displaying device information"""
    
    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Device Information", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        
        self.info_text = scrolledtext.ScrolledText(self, height=12, width=50, state='disabled')
        self.info_text.pack(fill='both', expand=True)
        
        ttk.Button(self, text="Refresh Info", command=self.refresh_info).pack(pady=(5, 0))
        self._write("No device selected")
    
    def set_device(self, device_id):
        """Set the current device"""
        if device_id == self.device_id:
            return
        self.device_id = device_id
        self.refresh_info()
    
    def refresh_info(self):
        """Refresh device information on the worker thread."""
        if not self.device_id:
            self._write("No device selected")
            return
        
        self._write("Loading device information...\n")
        device_id = self.device_id
        self.runner.submit(
            lambda: self.adb.get_device_info(device_id),
            lambda info: self._show_info(info, device_id),
            label="Reading device info"
        )
    
    def _show_info(self, info, device_id):
        # The user may have switched devices while we were reading.
        if device_id != self.device_id:
            return
        if not info:
            self._write("No information available")
            return
        self._write("".join(f"{key}: {value}\n" for key, value in info.items()))
    
    def _write(self, text):
        self.info_text.config(state='normal')
        self.info_text.delete('1.0', tk.END)
        self.info_text.insert(tk.END, text)
        self.info_text.config(state='disabled')


class AppManagerPanel(ttk.LabelFrame):
    """Panel for app management with icon display"""
    
    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="App Manager", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self.icon_cache = {}
        self.all_packages = []
        self.frozen_packages = set()
        # Only the newest icon request may touch the panel; older ones that
        # are already queued are dropped by the worker before they run.
        self._icon_token = 0
        self._icon_lock = threading.Lock()
        self._icon_wanted = None
        self._icon_in_flight = None
        
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(0, 5))
        
        self.third_party_var = tk.BooleanVar(value=True)
        # The checkbox must apply itself; before, toggling it did nothing
        # until the user also pressed Refresh.
        ttk.Checkbutton(controls, text="Third-party apps only",
                        variable=self.third_party_var,
                        command=self.refresh_apps).pack(side='left')
        
        self.online_icons_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Icons from Play Store",
                        variable=self.online_icons_var).pack(side='left', padx=(10, 0))
        
        search_frame = ttk.Frame(controls)
        search_frame.pack(side='left', padx=10)
        ttk.Label(search_frame, text="Search:").pack(side='left')
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=20)
        self.search_entry.pack(side='left', padx=5)
        self.search_var.trace_add('write', self._on_search_change)
        
        ttk.Button(controls, text="Refresh", command=self.refresh_apps).pack(side='left', padx=5)
        self.install_btn = ttk.Button(controls, text="Install APK", command=self.install_apk)
        self.install_btn.pack(side='left', padx=5)
        self.uninstall_btn = ttk.Button(controls, text="Uninstall Selected", command=self.uninstall_selected)
        self.uninstall_btn.pack(side='left', padx=5)
        # Reversible counterpart to Uninstall: disables the app in place
        # instead of deleting it, and flips to "Enable" for disabled apps.
        # The same actions are on the D/E keys and the right-click menu.
        self.freeze_btn = ttk.Button(controls, text="Disable", command=self.freeze_selected)
        self.freeze_btn.pack(side='left', padx=5)
        self.clear_data_btn = ttk.Button(controls, text="Clear Data", command=self.clear_data_selected)
        self.clear_data_btn.pack(side='left', padx=5)
        
        main_frame = ttk.Frame(self)
        main_frame.pack(fill='both', expand=True)
        
        list_frame = ttk.Frame(main_frame)
        list_frame.pack(side='left', fill='both', expand=True, padx=(0, 10))
        
        columns = ('name', 'state')
        # extended: Ctrl/Shift click and drag select several apps, which the
        # D/E keys and the context menu then act on together.
        self.app_tree = ttk.Treeview(list_frame, columns=columns, show='headings',
                                     selectmode='extended')
        self.app_tree.heading('name', text='App Name')
        self.app_tree.heading('state', text='State')
        self.app_tree.column('name', width=260)
        self.app_tree.column('state', width=90, anchor='center')
        self.app_tree.tag_configure('disabled', foreground='gray')
        self.app_tree.pack(fill='both', expand=True)
        
        self.app_tree.bind('<<TreeviewSelect>>', self._on_selection_change)
        # D disables the selection, E re-enables it: keeping an app without
        # uninstalling it should not need a trip to the toolbar.
        for key in ('d', 'D'):
            self.app_tree.bind(f'<Key-{key}>', self._on_disable_key)
        for key in ('e', 'E'):
            self.app_tree.bind(f'<Key-{key}>', self._on_enable_key)
        self.app_tree.bind('<Button-3>', self._on_app_context)
        if sys.platform == 'darwin':
            # Aqua has no Button-3; Ctrl-click reports Button-2 there.
            self.app_tree.bind('<Button-2>', self._on_app_context)
        
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.app_tree.yview)
        self.app_tree.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        
        icon_frame = ttk.Frame(main_frame, width=128, height=128)
        icon_frame.pack(side='right', fill='y', padx=(10, 0))
        icon_frame.pack_propagate(False)
        
        self.icon_label = ttk.Label(icon_frame, text="No icon", anchor='center')
        self.icon_label.pack(fill='both', expand=True)
        
        self.detail_label = ttk.Label(self, text="Select an app to see details", relief='sunken', anchor='w')
        self.detail_label.pack(fill='x', pady=(5, 0))
        
        # Right-click menu: the toolbar actions plus their key shortcuts,
        # shown where people look for them.
        self.app_menu = tk.Menu(self, tearoff=0)
        self.app_menu.add_command(label='Disable', accelerator='D',
                                  command=self.disable_selected)
        self.app_menu.add_command(label='Enable', accelerator='E',
                                  command=self.enable_selected)
        self.app_menu.add_separator()
        self.app_menu.add_command(label='Clear Data', command=self.clear_data_selected)
        self.app_menu.add_command(label='Clear Cache', command=self.clear_cache_selected)
        self.app_menu.add_command(label='Force Stop', command=self.force_stop_selected)
        self.app_menu.add_separator()
        self.app_menu.add_command(label='App Info', command=self.app_info_selected)
        self.app_menu.add_command(label='Uninstall', command=self.uninstall_selected)
    
    def set_device(self, device_id):
        """Set the current device"""
        if device_id == self.device_id:
            return
        self.device_id = device_id
        self.icon_cache.clear()
        self.all_packages = []
        self.frozen_packages = set()
        with self._icon_lock:
            self._icon_wanted = None
        self._icon_token += 1
        self._icon_in_flight = None
        self.icon_label.config(image='', text="No icon")
        self.detail_label.config(text="Select an app to see details")
        self.search_var.set('')
        self._filter_apps()
        self.refresh_apps()
    
    def refresh_apps(self):
        """Refresh the app list on the worker thread."""
        if not self.device_id:
            self.all_packages = []
            self._filter_apps()
            return
        
        device_id = self.device_id
        third_party = self.third_party_var.get()
        self.runner.submit(
            lambda: (self.adb.list_packages(device_id, third_party),
                     set(self.adb.list_frozen_packages(device_id))),
            lambda result: self._show_packages(result, device_id),
            label="Listing apps"
        )
    
    def _show_packages(self, result, device_id):
        # Ignore a result that belongs to a device we have already left.
        if device_id != self.device_id:
            return
        packages, frozen = result
        self.all_packages = packages
        self.frozen_packages = frozen
        self._filter_apps()
    
    def _filter_apps(self):
        """Filter apps based on search term"""
        search_term = self.search_var.get().lower()
        selected = [self.app_tree.item(i, 'values')[0]
                    for i in self.app_tree.selection()]
        for item in self.app_tree.get_children():
            self.app_tree.delete(item)
        
        packages = [p for p in self.all_packages if search_term in p.lower()]
        for pkg in packages:
            disabled = pkg in self.frozen_packages
            self.app_tree.insert('', 'end', iid=pkg, values=(
                pkg, 'Disabled' if disabled else ''),
                tags=('disabled',) if disabled else ())
        
        # Keep whatever is still visible selected, so a refresh, a search
        # keystroke or a freeze does not throw the selection away.
        visible = set(packages)
        restored = [p for p in selected if p in visible]
        if restored:
            self.app_tree.selection_set(restored)
        self._update_freeze_button()
    
    def _on_search_change(self, *args):
        """Handle search text change"""
        self._filter_apps()
    
    def _detail_text(self, pkg, suffix=''):
        """Detail line for a package, including its disabled state."""
        state = ' (disabled)' if pkg in self.frozen_packages else ''
        return f"Package: {pkg}{state}{suffix}"
    
    def _update_freeze_button(self):
        """Label the button with the state it would move the selection to."""
        selection = self.app_tree.selection()
        packages = [self.app_tree.item(i, 'values')[0] for i in selection]
        if any(pkg in self.frozen_packages for pkg in packages):
            self.freeze_btn.config(text='Enable')
        else:
            self.freeze_btn.config(text='Disable')
    
    def _on_selection_change(self, event=None):
        """Handle selection change in app tree"""
        selection = self.app_tree.selection()
        if not selection:
            self._icon_token += 1
            self._icon_in_flight = None
            self.icon_label.config(image='', text="No icon")
            self.detail_label.config(text="Select an app to see details")
            self._update_freeze_button()
            return
        
        pkg = self.app_tree.item(selection[0], 'values')[0]
        self.detail_label.config(text=self._detail_text(pkg))
        self._update_freeze_button()
        
        if pkg in self.icon_cache:
            self._icon_token += 1
            self._icon_in_flight = None
            self.icon_label.config(image=self.icon_cache[pkg], text="")
            return
        
        if self._icon_in_flight == pkg:
            # Rebuilding the tree re-fires this event: keep waiting for the
            # request already running rather than fetching the icon twice.
            self.icon_label.config(image='', text="Loading...")
            return
        
        self._icon_token += 1
        token = self._icon_token
        self._icon_in_flight = pkg
        self.icon_label.config(image='', text="Loading...")
        with self._icon_lock:
            self._icon_wanted = pkg
        # Tk vars are read here, on the Tk thread, never by the worker.
        online = self.online_icons_var.get()
        self.runner.submit(
            lambda: self._fetch_icon(pkg, online),
            lambda result: self._show_icon(pkg, token, result),
            label="Loading icon"
        )
    
    def _fetch_icon(self, pkg, online):
        """Worker thread: skip superseded requests, then decode the icon."""
        with self._icon_lock:
            if self._icon_wanted != pkg:
                return None, None
        data, error = self.adb.get_app_icon(pkg, self.device_id)
        if error and online:
            with self._icon_lock:
                if self._icon_wanted != pkg:
                    return None, None
            data, error = fetch_play_icon(pkg)
        if error:
            return None, error
        decoded = decode_icon(data)
        return (decoded, None) if decoded else (None, "unsupported image format")
    
    def _show_icon(self, pkg, token, result):
        # A result that is no longer the newest one must not be displayed.
        if token != self._icon_token:
            return
        self._icon_in_flight = None
        data, error = result
        if not data:
            self.icon_label.config(image='', text="No icon")
            reason = error or "device returned no image"
            self.detail_label.config(
                text=self._detail_text(pkg, f" — icon unavailable: {reason}"))
            return
        try:
            photo = make_photo(data)
        except tk.TclError:
            self.icon_label.config(image='', text="No icon")
            return
        self.icon_cache[pkg] = photo
        self.icon_label.config(image=photo, text="")
    
    def install_apk(self):
        """Install an APK file"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        apk_path = filedialog.askopenfilename(
            title="Select APK file",
            filetypes=[("APK files", "*.apk"), ("All files", "*.*")]
        )
        
        if apk_path:
            device_id = self.device_id
            self.runner.submit(
                lambda: self.adb.install_apk(apk_path, device_id),
                self._after_install,
                label="Installing APK",
                widgets=[self.install_btn]
            )
    
    def _after_install(self, result):
        output, success = result
        if success:
            messagebox.showinfo("Success", "APK installed successfully")
            self.refresh_apps()
        else:
            messagebox.showerror("Error", f"Installation failed:\n{output}")
    
    def uninstall_selected(self):
        """Uninstall selected apps"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        selected = self.app_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        
        packages = [self.app_tree.item(i, 'values')[0] for i in selected]
        if not messagebox.askyesno("Confirm", f"Uninstall {len(packages)} app(s)?"):
            return
        
        device_id = self.device_id
        
        def run():
            failures = []
            for pkg in packages:
                output, success = self.adb.uninstall_package(pkg, device_id)
                if not success:
                    failures.append(f"Failed to uninstall {pkg}:\n{output}")
            return failures
        
        self.runner.submit(run, self._after_uninstall, label="Uninstalling apps",
                           widgets=[self.uninstall_btn])
    
    def _after_uninstall(self, failures):
        if failures:
            messagebox.showerror("Error", "\n\n".join(failures))
        self.refresh_apps()
    
    def freeze_selected(self):
        """Toolbar button: move the selection to the state the label promises."""
        self.set_disabled(self.freeze_btn.cget('text') == 'Disable')
    
    def disable_selected(self):
        """Keyboard/menu: disable the selection, keeping app and data."""
        self.set_disabled(True)
    
    def enable_selected(self):
        """Keyboard/menu: re-enable the selection."""
        self.set_disabled(False)
    
    def set_disabled(self, disable):
        """Disable or enable the selected apps without touching their data.

        `pm disable-user` rather than `uninstall`, so the package, its data
        and its version survive — and the same action reverses it, which is
        why this path asks for no confirmation (Uninstall and Clear Data
        still do).
        """
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        selected = self.app_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        
        packages = [self.app_tree.item(i, 'values')[0] for i in selected]
        device_id = self.device_id
        action = self.adb.freeze_package if disable else self.adb.unfreeze_package
        verb = 'disable' if disable else 'enable'
        
        def run():
            applied, failures = [], []
            for pkg in packages:
                output, success = action(pkg, device_id)
                if success:
                    applied.append(pkg)
                else:
                    failures.append(f"Failed to {verb} {pkg}:\n{output}")
            return applied, failures
        
        self.runner.submit(
            run,
            lambda result: self._after_freeze(result, disable),
            label="Disabling apps" if disable else "Enabling apps",
            widgets=[self.freeze_btn])
    
    def _on_state_key(self, event, action):
        """Shared D/E handling: modifiers belong to other commands, and a
        key press with nothing selected must not open a dialog."""
        if event.state & (0x4 | 0x8 | 0x40):   # Ctrl, Alt, Super
            return None
        if self.app_tree.selection():
            action()
        return 'break'
    
    def _on_disable_key(self, event):
        return self._on_state_key(event, self.disable_selected)
    
    def _on_enable_key(self, event):
        return self._on_state_key(event, self.enable_selected)
    
    def _on_app_context(self, event):
        """Right-click: select the row under the cursor, then show the menu."""
        row = self.app_tree.identify_row(event.y)
        if not row:
            return                      # empty area: nothing to act on
        if row not in self.app_tree.selection():
            self.app_tree.selection_set(row)
        self._post_app_menu(event.x_root, event.y_root)
    
    def _post_app_menu(self, x, y):
        try:
            self.app_menu.tk_popup(x, y)
        finally:
            self.app_menu.grab_release()
    
    def _after_freeze(self, result, froze):
        applied, failures = result
        if froze:
            self.frozen_packages.update(applied)
        else:
            self.frozen_packages.difference_update(applied)
        self._filter_apps()
        self._on_selection_change()
        if failures:
            messagebox.showerror("Error", "\n\n".join(failures))
    
    def clear_data_selected(self):
        """Clear data of selected apps"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        selected = self.app_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        
        packages = [self.app_tree.item(i, 'values')[0] for i in selected]
        if not messagebox.askyesno("Confirm", f"Clear data for {len(packages)} app(s)?"):
            return
        
        device_id = self.device_id
        
        def run():
            failures = []
            for pkg in packages:
                output, success = self.adb.clear_app_data(pkg, device_id)
                if not success:
                    failures.append(f"Failed to clear data for {pkg}:\n{output}")
            return failures
        
        self.runner.submit(run, self._after_clear_data, label="Clearing app data",
                           widgets=[self.clear_data_btn])
    
    def _after_clear_data(self, failures):
        if failures:
            messagebox.showerror("Error", "\n\n".join(failures))
        else:
            messagebox.showinfo("Success", "Data cleared")

    # ---- newly exposed dormant wrapper actions ---------------------------

    def clear_cache_selected(self):
        """Clear the cache of the selected apps."""
        self._clear_action_selected(
            'clear_app_cache', 'Clear Cache', 'clearing cache for',
            'Cache cleared', 'Failed to clear cache for {}')

    def force_stop_selected(self):
        """Force stop the selected apps."""
        self._clear_action_selected(
            'force_stop_app', 'Force Stop', 'force stopping',
            'Apps stopped', 'Failed to force stop {}')

    def app_info_selected(self):
        """Show package details for the selected app."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        selected = self.app_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        pkg = self.app_tree.item(selected[0], 'values')[0]
        device_id = self.device_id

        def run():
            return self.adb.get_app_info(pkg, device_id)

        def show(result):
            output, success = result
            if success:
                self._show_app_info(pkg, output)
            else:
                messagebox.showerror("Error", f"Could not read app info:\n{output}")

        self.runner.submit(run, show, label=f"Reading info for {pkg}")

    def _clear_action_selected(self, method, label, verb, success_msg, fail_msg):
        """Shared runner for per-app actions that take one package each."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        selected = self.app_tree.selection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        packages = [self.app_tree.item(i, 'values')[0] for i in selected]
        device_id = self.device_id
        action = getattr(self.adb, method)

        def run():
            failures = []
            for pkg in packages:
                output, success = action(pkg, device_id)
                if not success:
                    failures.append(fail_msg.format(pkg) + f":\n{output}")
            return failures

        def done(failures):
            if failures:
                messagebox.showerror("Error", "\n\n".join(failures))
            else:
                messagebox.showinfo("Success", f"{success_msg} ({len(packages)} app(s))")
                self.refresh_apps()

        self.runner.submit(run, done, label=f"{verb} {len(packages)} app(s)")

    def _show_app_info(self, pkg, info_text):
        """Display package details in a read-only dialog."""
        dlg = tk.Toplevel(self)
        dlg.title(f"App Info — {pkg}")
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        txt = scrolledtext.ScrolledText(dlg, width=70, height=25)
        txt.pack(fill='both', expand=True, padx=10, pady=10)
        txt.insert('1.0', info_text)
        txt.config(state='disabled')
        ttk.Button(dlg, text="Close", command=dlg.destroy).pack(pady=(0, 10))



class FileManagerPanel(ttk.LabelFrame):
    """Panel for file management"""
    
    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="File Manager", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self.current_path = '/sdcard'
        
        path_frame = ttk.Frame(self)
        path_frame.pack(fill='x', pady=(0, 5))
        
        ttk.Label(path_frame, text="Path:").pack(side='left')
        self.path_var = tk.StringVar(value=self.current_path)
        self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.path_entry.bind('<Return>', lambda e: self.navigate_to(self.path_var.get()))
        
        ttk.Button(path_frame, text="Go", command=lambda: self.navigate_to(self.path_var.get())).pack(side='left')
        ttk.Button(path_frame, text="↑ Up", command=self.go_up).pack(side='left', padx=5)
        
        # Listing failures (permission denied, bad path) are shown here
        # instead of looking like an empty directory.
        self.error_label = ttk.Label(self, text='', foreground='#b00020')
        self.error_label.pack(fill='x', pady=(0, 5))
        
        list_frame = ttk.Frame(self)
        list_frame.pack(fill='both', expand=True)
        
        columns = ('name', 'size', 'permissions')
        self.file_tree = ttk.Treeview(list_frame, columns=columns, show='headings')
        self.file_tree.heading('name', text='Name')
        self.file_tree.heading('size', text='Size')
        self.file_tree.heading('permissions', text='Permissions')
        self.file_tree.column('name', width=200)
        self.file_tree.column('size', width=80)
        self.file_tree.column('permissions', width=100)
        
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.file_tree.yview)
        self.file_tree.config(yscrollcommand=scrollbar.set)
        
        self.file_tree.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        
        self.file_tree.bind('<Double-1>', self.on_double_click)
        
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill='x', pady=(5, 0))
        
        self.refresh_btn = ttk.Button(btn_frame, text="Refresh", command=self.refresh_files)
        self.refresh_btn.pack(side='left', padx=2)
        self.mkdir_btn = ttk.Button(btn_frame, text="New Folder", command=self.mkdir_dialog)
        self.mkdir_btn.pack(side='left', padx=2)
        self.rename_btn = ttk.Button(btn_frame, text="Rename", command=self.rename_dialog)
        self.rename_btn.pack(side='left', padx=2)
        self.delete_btn = ttk.Button(btn_frame, text="Delete", command=self.delete_dialog)
        self.delete_btn.pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Search", command=self.search_dialog).pack(side='left', padx=2)
        self.pull_btn = ttk.Button(btn_frame, text="Pull File", command=self.pull_file)
        self.pull_btn.pack(side='left', padx=2)
        self.push_btn = ttk.Button(btn_frame, text="Push File", command=self.push_file)
        self.push_btn.pack(side='left', padx=2)
    
    def set_device(self, device_id):
        """Set the current device"""
        if device_id == self.device_id:
            return
        self.device_id = device_id
        self.current_path = '/sdcard'
        self.path_var.set(self.current_path)
        self.refresh_files()
    
    def refresh_files(self):
        """Refresh the file list on the worker thread."""
        if not self.device_id:
            self._populate([], None)
            return
        
        path = self.current_path
        device_id = self.device_id
        self.runner.submit(
            lambda: self.adb.list_files(path, device_id),
            lambda result: self._show_files(result, path, device_id),
            label=f"Listing {path}"
        )
    
    def _show_files(self, result, path, device_id):
        # Ignore results for a directory or device we have already left.
        if path != self.current_path or device_id != self.device_id:
            return
        files, error = result
        self._populate(files, error)
    
    def _populate(self, files, error=None):
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        inserted = set()
        for f in files:
            icon = '📁 ' if f['is_dir'] else '📄 '
            # The iid is the raw name: the displayed value carries an icon
            # prefix that must never be parsed back out (lstrip would eat
            # leading spaces and icon characters from real file names).
            tags = ('dir' if f['is_dir']
                    else 'link' if f.get('is_link') else 'file',)
            if f['name'] in inserted:
                # A faithful parse never yields duplicates (ls names are
                # unique), but a Tk "item already exists" error would kill
                # the whole callback, so drop the row instead of crashing.
                continue
            inserted.add(f['name'])
            self.file_tree.insert('', tk.END, iid=f['name'], values=(
                icon + f['name'],
                f['size'],
                f['permissions']
            ), tags=tags)
        self.error_label.config(text=error or '')
    
    def navigate_to(self, path):
        """Navigate to a specific path, normalised before it is used."""
        path = self._normalise_path(path)
        if not path:
            return
        self.current_path = path
        self.path_var.set(path)
        self.refresh_files()
    
    def _normalise_path(self, path):
        """Turn user input into an absolute device path, or '' if unusable.

        Empty/whitespace input and `..` segments used to leave
        `current_path` in a state where `go_up` could never recover.
        """
        path = (path or '').strip()
        if not path:
            return self.current_path if self.current_path else ''
        if not path.startswith('/'):
            path = posixpath.join(self.current_path, path)
        normalised = posixpath.normpath(path)
        return normalised if normalised != '.' else ''
    
    def go_up(self):
        """Go to parent directory"""
        parent = posixpath.dirname(self.current_path.rstrip('/'))
        if parent:
            self.navigate_to(parent)
        elif self.current_path != '/':
            # '/' has no parent; anything else (an empty or relative path
            # handed down from older builds) recovers to the root instead
            # of leaving the panel stuck with nowhere to go.
            self.navigate_to('/')
    
    def on_double_click(self, event):
        """Handle double-click on item"""
        selection = self.file_tree.selection()
        if not selection:
            return
        # The iid is the raw name; the displayed value has an icon prefix
        # that must not be parsed back out.
        name = selection[0]
        tags = self.file_tree.item(selection[0], 'tags')
        if 'dir' in tags or 'link' in tags:
            self.navigate_to(posixpath.join(self.current_path, name))
    
    def pull_file(self):
        """Pull selected file from device"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "No file selected")
            return
        
        name = selection[0]
        remote_path = posixpath.join(self.current_path, name)
        
        local_path = filedialog.asksaveasfilename(
            title="Save file as",
            initialfile=name
        )
        
        if local_path:
            device_id = self.device_id
            self.runner.submit(
                lambda: self.adb.pull_file(remote_path, local_path, device_id),
                lambda result: self._after_pull(result, local_path),
                label="Pulling file",
                widgets=[self.pull_btn]
            )
    
    def _after_pull(self, result, local_path):
        output, success = result
        if success:
            messagebox.showinfo("Success", f"File saved to {local_path}")
        else:
            messagebox.showerror("Error", f"Pull failed:\n{output}")
    
    def push_file(self):
        """Push a file to device"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        local_path = filedialog.askopenfilename(title="Select file to push")
        
        if local_path:
            filename = os.path.basename(local_path)
            remote_path = posixpath.join(self.current_path, filename)
            device_id = self.device_id
            
            def run():
                return self.adb.push_file(local_path, remote_path, device_id), remote_path
            
            self.runner.submit(
                run,
                self._after_push,
                label="Pushing file",
                widgets=[self.push_btn]
            )
    
    def _after_push(self, result):
        (output, success), remote_path = result
        if success:
            messagebox.showinfo("Success", f"File pushed to {remote_path}")
            self.refresh_files()
        else:
            messagebox.showerror("Error", f"Push failed:\n{output}")

    # ---- new file operations: mkdir, rename, delete, find --------------

    def mkdir_dialog(self):
        """Prompt for a new folder name and create it on the device."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        name = self._prompt("New folder name", "Enter folder name:")
        if not name:
            return
        remote_path = posixpath.join(self.current_path, name)
        device_id = self.device_id

        def run():
            return self.adb.mkdir(remote_path, device_id)

        def done(result):
            output, success = result
            if success:
                self.refresh_files()
            else:
                messagebox.showerror("Error", f"Could not create folder:\n{output}")

        self.runner.submit(run, done, label="Creating folder")

    def rename_dialog(self):
        """Prompt for a new name and rename the selected item."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "No item selected")
            return
        old_name = selection[0]
        old_path = posixpath.join(self.current_path, old_name)
        new_name = self._prompt("Rename", f"New name for '{old_name}':", initial=old_name)
        if not new_name or new_name == old_name:
            return
        new_path = posixpath.join(self.current_path, new_name)
        device_id = self.device_id

        def run():
            return self.adb.rename(old_path, new_path, device_id)

        def done(result):
            output, success = result
            if success:
                self.refresh_files()
            else:
                messagebox.showerror("Error", f"Rename failed:\n{output}")

        self.runner.submit(run, done, label="Renaming item")

    def delete_dialog(self):
        """Prompt for confirmation and delete the selected item(s)."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "No item selected")
            return
        items = list(selection)
        tags = [self.file_tree.item(i, 'tags') for i in items]
        any_dir = any('dir' in t for t in tags)
        count = len(items)
        if not messagebox.askyesno(
                "Confirm delete",
                f"Delete {count} item(s)?\n" + "\n".join(items) +
                (("\nThis includes folders and their contents.") if any_dir else "")):
            return
        device_id = self.device_id

        def run():
            failures = []
            for name in items:
                remote_path = posixpath.join(self.current_path, name)
                is_dir = 'dir' in self.file_tree.item(name, 'tags')
                output, success = self.adb.delete(remote_path, device_id, recursive=is_dir)
                if not success:
                    failures.append(f"{name}:\n{output}")
            return failures

        def done(failures):
            if failures:
                messagebox.showerror("Error", "\n\n".join(failures))
            self.refresh_files()

        self.runner.submit(run, done, label=f"Deleting {count} item(s)")

    def search_dialog(self):
        """Prompt for a filename pattern and search the current directory."""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        pattern = self._prompt("Search", "Filename pattern (e.g. *.log):", initial='*')
        if not pattern:
            return
        device_id = self.device_id
        path = self.current_path

        def run():
            return self.adb.find_files(path, pattern, device_id)

        def done(result):
            output, success = result
            self._populate_search_results(pattern, output, success)

        self.runner.submit(run, done, label="Searching files")

    def _populate_search_results(self, pattern, output, success):
        """Show find results in the tree (read-only search view)."""
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        if not success:
            self.error_label.config(text=f"Search error: {output}")
            return
        self.error_label.config(text=f"Results for '{pattern}' in {self.current_path}:")
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            # show just the name portion
            name = posixpath.basename(line)
            is_dir = False  # find output doesn't tell us type easily here
            self.file_tree.insert('', tk.END, iid=name, values=(
                '📄 ' + name, '', ''
            ))
        # Refresh button still works to go back to normal listing

    def _prompt(self, title, prompt, initial=''):
        """Show a simple text input dialog and return the entered string."""
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self.winfo_toplevel())
        dlg.grab_set()
        dlg.resizable(False, False)
        result = {}

        ttk.Label(dlg, text=prompt).pack(padx=20, pady=(15, 5))
        var = tk.StringVar(value=initial)
        entry = ttk.Entry(dlg, textvariable=var, width=40)
        entry.pack(padx=20, pady=5)
        entry.focus_set()
        entry.select_range(0, tk.END)

        btn_frame = ttk.Frame(dlg)
        btn_frame.pack(pady=(10, 15))

        def ok():
            result['value'] = var.get()
            dlg.destroy()

        def cancel():
            result['value'] = None
            dlg.destroy()

        ttk.Button(btn_frame, text="OK", command=ok).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="Cancel", command=cancel).pack(side='left', padx=5)
        entry.bind('<Return>', lambda e: ok())
        entry.bind('<Escape>', lambda e: cancel())
        dlg.wait_window()
        return result.get('value')


class ShellPanel(ttk.LabelFrame):
    """Panel for shell command execution"""
    
    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Shell", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self.command_history = []
        self.history_index = -1
        
        self.output_text = scrolledtext.ScrolledText(self, height=15, wrap=tk.WORD)
        self.output_text.pack(fill='both', expand=True)
        self.output_text.config(state='disabled')
        
        input_frame = ttk.Frame(self)
        input_frame.pack(fill='x', pady=(5, 0))
        
        ttk.Label(input_frame, text="$").pack(side='left')
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(input_frame, textvariable=self.cmd_var)
        self.cmd_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.cmd_entry.bind('<Return>', self.execute_command)
        self.cmd_entry.bind('<Up>', self.history_up)
        self.cmd_entry.bind('<Down>', self.history_down)
        
        ttk.Button(input_frame, text="Execute", command=self.execute_command).pack(side='left')
        ttk.Button(input_frame, text="Clear", command=self.clear_output).pack(side='left', padx=5)
    
    def set_device(self, device_id):
        """Set the current device"""
        if device_id == self.device_id:
            return
        self.device_id = device_id
        self.clear_output()
        if device_id:
            self.append_output(f"Connected to device: {device_id}\n", 'info')
    
    def append_output(self, text, tag='normal'):
        """Append text to output"""
        self.output_text.config(state='normal')
        self.output_text.insert(tk.END, text)
        self.output_text.see(tk.END)
        self.output_text.config(state='disabled')
    
    def clear_output(self):
        """Clear the output area"""
        self.output_text.config(state='normal')
        self.output_text.delete('1.0', tk.END)
        self.output_text.config(state='disabled')
    
    def execute_command(self, event=None):
        """Execute the entered command"""
        if not self.device_id:
            self.append_output("Error: No device selected\n")
            return
        
        command = self.cmd_var.get().strip()
        if not command:
            return
        
        self.command_history.append(command)
        self.history_index = len(self.command_history)
        
        self.append_output(f"$ {command}\n")
        self.cmd_var.set('')
        
        # Queue instead of spawning a thread per Enter, so a burst of
        # commands cannot create an unbounded number of adb children.
        self.runner.submit(
            lambda: self.adb.run_shell_command(command, self.device_id)[0],
            lambda output: self.append_output(output + "\n"),
            label="Running command"
        )
    
    def history_up(self, event):
        """Navigate command history up"""
        if self.command_history and self.history_index > 0:
            self.history_index -= 1
            self.cmd_var.set(self.command_history[self.history_index])
    
    def history_down(self, event):
        """Navigate command history down"""
        if self.history_index < len(self.command_history) - 1:
            self.history_index += 1
            self.cmd_var.set(self.command_history[self.history_index])
        else:
            self.history_index = len(self.command_history)
            self.cmd_var.set('')


class ToolsPanel(ttk.LabelFrame):
    """Panel for various tools"""
    
    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Tools", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        
        self.screenshot_btn = ttk.Button(
            self, text="📷 Take Screenshot", command=self.take_screenshot)
        self.screenshot_btn.pack(fill='x', pady=2)
        
        ttk.Separator(self, orient='horizontal').pack(fill='x', pady=5)
        ttk.Label(self, text="Reboot Options:").pack(anchor='w')
        
        reboot_frame = ttk.Frame(self)
        reboot_frame.pack(fill='x')
        
        self.reboot_btns = []
        for text, mode in (("🔄 Reboot", ''),
                           ("⚙️ Recovery", 'recovery'),
                           ("🔧 Bootloader", 'bootloader')):
            button = ttk.Button(reboot_frame, text=text,
                                command=lambda m=mode: self.reboot(m))
            button.pack(side='left', padx=2)
            self.reboot_btns.append(button)
    
    def set_device(self, device_id):
        """Set the current device"""
        self.device_id = device_id
    
    def take_screenshot(self):
        """Take a screenshot"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = filedialog.asksaveasfilename(
            title="Save screenshot as",
            initialfile=f"screenshot_{timestamp}.png",
            defaultextension=".png",
            filetypes=[("PNG files", "*.png")]
        )
        
        if save_path:
            device_id = self.device_id
            self.runner.submit(
                lambda: self.adb.take_screenshot(save_path, device_id),
                lambda result: self._after_screenshot(result, save_path),
                label="Taking screenshot",
                widgets=[self.screenshot_btn]
            )
    
    def _after_screenshot(self, result, save_path):
        output, success = result
        if success:
            messagebox.showinfo("Success", f"Screenshot saved to {save_path}")
        else:
            messagebox.showerror("Error", f"Screenshot failed:\n{output}")
    
    def reboot(self, mode):
        """Reboot the device"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        mode_name = mode if mode else "normal"
        if not messagebox.askyesno("Confirm", f"Reboot device to {mode_name} mode?"):
            return
        
        device_id = self.device_id
        self.runner.submit(
            lambda: self.adb.reboot(mode, device_id),
            self._after_reboot,
            label=f"Rebooting to {mode_name}",
            widgets=self.reboot_btns
        )
    
    def _after_reboot(self, result):
        output, success = result
        if not success:
            messagebox.showerror("Error", f"Reboot failed:\n{output}")


class LogcatPanel(ttk.LabelFrame):
    """Panel for viewing logcat"""
    
    # threadtime (the Android default): 01-15 12:34:56.789  1234  1234 W Tag : msg
    # brief:                            W/Tag( 1234): msg
    LEVEL_PATTERN = re.compile(
        r'^(?:\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\d+\s+\d+\s+)?'
        r'([VDIWEF])(?:/|\s)')
    MAX_LINES = 5000
    FILTER_DELAY_MS = 150
    TRIM_DELAY_MS = 250

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Logcat", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self.is_running = False
        self.process = None
        self.lines = []
        self._filter_job = None
        
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(0, 5))
        
        self.start_btn = ttk.Button(controls, text="▶ Start", command=self.start_logcat)
        self.start_btn.pack(side='left', padx=2)
        
        self.stop_btn = ttk.Button(controls, text="⏹ Stop", command=self.stop_logcat, state='disabled')
        self.stop_btn.pack(side='left', padx=2)
        
        ttk.Button(controls, text="Clear", command=self.clear_log).pack(side='left', padx=2)
        
        ttk.Label(controls, text="Filter:").pack(side='left', padx=(10, 2))
        self.filter_var = tk.StringVar()
        self.filter_entry = ttk.Entry(controls, textvariable=self.filter_var, width=20)
        self.filter_entry.pack(side='left')
        
        self.log_text = scrolledtext.ScrolledText(self, height=15, wrap=tk.WORD)
        self.log_text.pack(fill='both', expand=True)
        self.log_text.config(state='disabled')
        
        self.log_text.tag_config('V', foreground='gray')
        self.log_text.tag_config('D', foreground='blue')
        self.log_text.tag_config('I', foreground='green')
        self.log_text.tag_config('W', foreground='orange')
        self.log_text.tag_config('E', foreground='red')
        self.log_text.tag_config('F', foreground='red', background='yellow')
        self.filter_var.trace_add('write', self._on_filter_change)
    
    def set_device(self, device_id):
        """Set the current device"""
        if device_id == self.device_id:
            return
        self.stop_logcat()
        self.device_id = device_id
        self.clear_log()
    
    def close(self):
        """Make sure no adb logcat child outlives the window."""
        self.is_running = False
        if self._filter_job:
            self.after_cancel(self._filter_job)
            self._filter_job = None
        process, self.process = self.process, None
        if process:
            self._reap(process)
    
    @staticmethod
    def _reap(process):
        """Terminate a logcat child and wait for it to actually exit."""
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass
    
    def start_logcat(self):
        """Start logcat capture"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        if self.is_running:
            return
        
        self.is_running = True
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        
        device_id = self.device_id
        
        def read_logcat():
            cmd = [self.adb.adb_path, '-s', device_id, 'logcat']
            process = None
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                self.process = process
                # Stop (or a device change) may have hit us while starting.
                if not self.is_running:
                    process.terminate()
                
                while self.is_running and process.poll() is None:
                    line = process.stdout.readline()
                    if line:
                        self.runner.post(self.append_log, line)
            except Exception as e:
                self.runner.post(self.append_log, f"Error: {e}\n")
            finally:
                if process is not None:
                    self._reap(process)
                if self.process is process:
                    self.process = None
                self.runner.post(self._on_logcat_stopped)
        
        threading.Thread(target=read_logcat, daemon=True).start()
    
    def stop_logcat(self):
        """Stop logcat capture"""
        self.is_running = False
        process, self.process = self.process, None
        if process:
            try:
                process.terminate()
            except Exception:
                pass
    
    def _on_logcat_stopped(self):
        """Handle logcat stopped"""
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
    
    def _on_filter_change(self, *args):
        """Re-render the buffer shortly after typing stops."""
        if self._filter_job:
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(self.FILTER_DELAY_MS, self._rerender)
    
    def _schedule_trim_render(self):
        """Coalesce overflow re-renders.

        A busy logcat trims on nearly every line; re-rendering the whole
        buffer each time costs more than the flood itself and freezes the
        UI, so at most one render is ever pending.
        """
        if self._filter_job is None:
            self._filter_job = self.after(self.TRIM_DELAY_MS, self._rerender)
    
    def _filter_text(self):
        return self.filter_var.get().strip().lower()
    
    def _matches(self, text):
        needle = self._filter_text()
        return not needle or needle in text.lower()
    
    def append_log(self, text):
        """Store a logcat line and show it when it passes the filter."""
        self.lines.append(text)
        if len(self.lines) > self.MAX_LINES:
            del self.lines[:-self.MAX_LINES]
            self._schedule_trim_render()
        elif self._matches(text):
            self._insert(text)
    
    def _rerender(self, *args):
        """Redraw the whole buffer under the current filter."""
        self._filter_job = None
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', tk.END)
        for line in self.lines:
            if self._matches(line):
                self._insert_locked(line)
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
    
    def _insert(self, text):
        self.log_text.config(state='normal')
        self._insert_locked(text)
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
    
    def _insert_locked(self, text):
        match = self.LEVEL_PATTERN.match(text)
        self.log_text.insert(tk.END, text, match.group(1) if match else None)
    
    def clear_log(self):
        """Clear the log"""
        self.lines = []
        if self._filter_job:
            self.after_cancel(self._filter_job)
            self._filter_job = None
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state='disabled')


class PortForwardPanel(ttk.LabelFrame):
    """TCP port forwarding between host and device.

    Uses `adb forward` (host -> device) and `adb reverse` (device -> host),
    which work on every non-rooted device. Handy for accessing a dev server
    running on the phone from the PC browser, or exposing a PC port to the
    device.
    """

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Port Forwarding", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None

        # Inputs
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(0, 5))

        ttk.Label(controls, text="Host port:").pack(side='left')
        self.host_port_var = tk.StringVar(value='8080')
        ttk.Entry(controls, textvariable=self.host_port_var, width=8).pack(side='left', padx=(2, 8))

        ttk.Label(controls, text="Device port:").pack(side='left')
        self.device_port_var = tk.StringVar(value='8080')
        ttk.Entry(controls, textvariable=self.device_port_var, width=8).pack(side='left', padx=(2, 8))

        ttk.Button(controls, text="Forward (host→device)",
                   command=self.forward).pack(side='left', padx=(8, 2))
        ttk.Button(controls, text="Reverse (device→host)",
                   command=self.reverse).pack(side='left')
        ttk.Button(controls, text="List active",
                   command=self.list_forwards).pack(side='left', padx=(8, 0))
        ttk.Button(controls, text="Clear all",
                   command=self.clear_all).pack(side='left', padx=(2, 0))

        self.listbox = tk.Listbox(self, height=8)
        self.listbox.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, ttk.Entry)):
                        w.config(state=state)
        self.listbox.config(state=state)
        if device_id:
            self.list_forwards()

    def _ports(self):
        try:
            return int(self.host_port_var.get()), int(self.device_port_var.get())
        except ValueError:
            messagebox.showerror("Error", "Ports must be numbers")
            return None

    def forward(self):
        """adb forward tcp:host tcp:device"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        ports = self._ports()
        if not ports:
            return
        host_port, device_port = ports

        def run():
            return self.adb.forward_port(host_port, device_port, self.device_id)

        def done(result):
            output, success = result
            if success:
                messagebox.showinfo("Forwarded",
                                    f"Host:{host_port} → Device:{device_port}")
                self.list_forwards()
            else:
                messagebox.showerror("Error", f"Forward failed:\n{output}")

        self.runner.submit(run, done, label="Forwarding port")

    def reverse(self):
        """adb reverse tcp:device tcp:host"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        ports = self._ports()
        if not ports:
            return
        host_port, device_port = ports

        def run():
            return self.adb.reverse_port(device_port, host_port, self.device_id)

        def done(result):
            output, success = result
            if success:
                messagebox.showinfo("Reversed",
                                    f"Device:{device_port} → Host:{host_port}")
                self.list_forwards()
            else:
                messagebox.showerror("Error", f"Reverse failed:\n{output}")

        self.runner.submit(run, done, label="Reversing port")

    def list_forwards(self):
        if not self.device_id:
            return

        def run():
            return self.adb.run_command('-s', self.device_id, 'forward', '--list')

        def done(result):
            output, success = result
            self.listbox.delete(0, tk.END)
            if success:
                for line in output.splitlines():
                    if line.strip():
                        self.listbox.insert(tk.END, line)
            else:
                self.listbox.insert(tk.END, f"(error: {output})")

        self.runner.submit(run, done, label="Listing forwards")

    def clear_all(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        if not messagebox.askyesno("Confirm", "Remove all port forwards?"):
            return

        def run():
            return self.adb.run_command('-s', self.device_id, 'forward', '--remove-all')

        def done(result):
            output, success = result
            if success:
                self.list_forwards()
            else:
                messagebox.showerror("Error", f"Clear failed:\n{output}")

        self.runner.submit(run, done, label="Clearing forwards")


class HardwareMonitorPanel(ttk.LabelFrame):
    """Read-only hardware stats: CPU, battery, GPU frames, thermals.

    Works on non-rooted devices — nothing here requires elevated privileges.
    Refreshes on demand; auto-refresh can be toggled.
    """

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Hardware Monitor", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self._auto_job = None
        self._auto = False

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        self.refresh_btn = ttk.Button(top, text="Refresh", command=self.refresh)
        self.refresh_btn.pack(side='left')

        self.auto_var = tk.BooleanVar(value=False)
        self.auto_chk = ttk.Checkbutton(top, text="Auto-refresh (2s)",
                                        variable=self.auto_var,
                                        command=self._toggle_auto)
        self.auto_chk.pack(side='left', padx=(10, 0))

        self.root_var = tk.BooleanVar(value=False)
        self.root_chk = ttk.Checkbutton(top, text="Root required for freq",
                                        variable=self.root_var, state='disabled')
        self.root_chk.pack(side='left', padx=(10, 0))

        self.text = scrolledtext.ScrolledText(self, height=16, wrap=tk.WORD)
        self.text.pack(fill='both', expand=True)
        self.text.config(state='disabled')

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        self.refresh_btn.config(state=state)
        self.auto_chk.config(state=state)
        if not device_id:
            self._toggle_auto(False)
            self.auto_var.set(False)

    def _toggle_auto(self, *_):
        self._auto = bool(self.auto_var.get())
        if self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None
        if self._auto and self.device_id:
            self._schedule_auto()

    def _schedule_auto(self):
        self._auto_job = self.after(2000, self._auto_tick)

    def _auto_tick(self):
        if self._auto and self.device_id and not self._closing():
            self.refresh()
            self._schedule_auto()

    def _closing(self):
        return getattr(self.winfo_toplevel(), '_closing', False)

    def refresh(self):
        if not self.device_id:
            return

        def run():
            parts = []
            # CPU load
            out, ok = self.adb.run_command('-s', self.device_id, 'shell', 'dumpsys', 'cpuinfo')
            parts.append("=== CPU ===")
            parts.append(out if ok else f"(error: {out})")
            # Battery
            out, ok = self.adb.run_command('-s', self.device_id, 'shell', 'dumpsys', 'battery')
            parts.append("\n=== BATTERY ===")
            parts.append(out if ok else f"(error: {out})")
            # GPU surfaceflinger (frame stats)
            out, ok = self.adb.run_command('-s', self.device_id, 'shell', 'dumpsys', 'SurfaceFlinger', '--latency', '5')
            parts.append("\n=== GPU (last 5 frames latency ms) ===")
            parts.append(out if ok else f"(error: {out})")
            # Thermals (best effort)
            out, ok = self.adb.run_command('-s', self.device_id, 'shell', 'dumpsys', 'thermalservice')
            parts.append("\n=== THERMAL ===")
            parts.append(out if ok else f"(error: {out})")
            return "\n".join(parts)

        def done(text):
            self.text.config(state='normal')
            self.text.delete('1.0', tk.END)
            self.text.insert('1.0', text)
            self.text.config(state='disabled')
            self.text.see('1.0')

        self.runner.submit(run, done, label="Reading hardware stats")


class RemoteControlPanel(ttk.LabelFrame):
    """Device-wide toggles that need no root: Stay Awake, Wi-Fi, Bluetooth."""

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Device Controls", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None

        self.stay_var = tk.BooleanVar(value=False)
        self.wifi_var = tk.BooleanVar(value=False)
        self.bt_var = tk.BooleanVar(value=False)
        self.dnd_var = tk.BooleanVar(value=False)
        self.ui_mode_var = tk.StringVar(value='normal')

        row = ttk.Frame(self)
        row.pack(fill='x', pady=(0, 5))

        self.stay_btn = ttk.Checkbutton(row, text="Stay Awake (screen on)",
                                        variable=self.stay_var,
                                        command=self._toggle_stay)
        self.stay_btn.pack(side='left', padx=(0, 12))

        self.wifi_btn = ttk.Checkbutton(row, text="Wi-Fi",
                                        variable=self.wifi_var,
                                        command=self._toggle_wifi)
        self.wifi_btn.pack(side='left', padx=(0, 12))

        self.bt_btn = ttk.Checkbutton(row, text="Bluetooth",
                                      variable=self.bt_var,
                                      command=self._toggle_bt)
        self.bt_btn.pack(side='left', padx=(0, 12))

        self.dnd_btn = ttk.Checkbutton(row, text="Do Not Disturb",
                                       variable=self.dnd_var,
                                       command=self._toggle_dnd)
        self.dnd_btn.pack(side='left', padx=(0, 12))

        ttk.Label(row, text="UI mode:").pack(side='left')
        self.ui_mode_combo = ttk.Combobox(row, textvariable=self.ui_mode_var,
                                          values=('normal', 'night', 'car',
                                                  'desk'),
                                          width=10, state='readonly')
        self.ui_mode_combo.pack(side='left', padx=(2, 0))
        self.ui_mode_combo.bind('<<ComboboxSelected>>',
                                lambda _e: self._toggle_ui_mode())

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for w in (self.stay_btn, self.wifi_btn, self.bt_btn,
                  self.dnd_btn, self.ui_mode_combo):
            w.config(state=state)
        if device_id:
            self._sync_states()

    def _sync_states(self):
        # Read current state from the device so the checks reflect reality.
        def run():
            results = {}
            for name, cmd in (('stay', ['svc', 'power', 'stayon']),
                              ('wifi', ['svc', 'wifi']),
                              ('bt', ['svc', 'bluetooth']),
                              ('dnd', ['settings', 'get', 'secure',
                                       'zen_mode']),
                              ('uimode', ['cmd', 'uimode'])):
                out, ok = self.adb.run_command('-s', self.device_id, 'shell',
                                               *cmd)
                results[name] = (out, ok)
            return results

        def done(results):
            # stayon: "stayon true" / "stayon false"
            out, ok = results.get('stay', ('', False))
            self.stay_var.set(ok and 'true' in out.lower())
            # wifi: "wifi enabled" / "wifi disabled"
            out, ok = results.get('wifi', ('', False))
            self.wifi_var.set(ok and 'enabled' in out.lower())
            # bt: "bluetooth enabled" / "bluetooth disabled"
            out, ok = results.get('bt', ('', False))
            self.bt_var.set(ok and 'enabled' in out.lower())
            # dnd: zen_mode value "0" or "1"
            out, ok = results.get('dnd', ('', False))
            self.dnd_var.set(ok and out.strip() == '1')
            # uimode: parse "System UI mode: normal" / "night"
            out, ok = results.get('uimode', ('', False))
            mode = 'normal'
            if ok:
                for line in out.splitlines():
                    if 'night' in line.lower():
                        mode = 'night'
                        break
                    if 'car' in line.lower():
                        mode = 'car'
                        break
                    if 'desk' in line.lower():
                        mode = 'desk'
                        break
            self.ui_mode_var.set(mode)

        self.runner.submit(run, done, label="Syncing device state")

    def _toggle_stay(self):
        self._toggle('stay', self.stay_var.get(), self.adb.set_stay_on)

    def _toggle_wifi(self):
        self._toggle('wifi', self.wifi_var.get(), self.adb.set_wifi)

    def _toggle_bt(self):
        self._toggle('bt', self.bt_var.get(), self.adb.set_bluetooth)

    def _toggle_dnd(self):
        self._toggle('dnd', self.dnd_var.get(), self.adb.set_dnd)

    def _toggle_ui_mode(self):
        if not self.device_id:
            return
        mode = self.ui_mode_var.get()

        def run():
            return self.adb.set_ui_mode(self.device_id, mode)

        def done(result):
            output, success = result
            if not success:
                messagebox.showerror("Error", f"UI mode failed:\n{output}")

        self.runner.submit(run, done, label="Setting UI mode")

    def _toggle(self, name, enabled, fn):
        if not self.device_id:
            return

        def run():
            return fn(self.device_id, enabled)

        def done(result):
            output, success = result
            if not success:
                # Revert the checkmark on failure
                if name == 'stay':
                    self.stay_var.set(not enabled)
                elif name == 'wifi':
                    self.wifi_var.set(not enabled)
                elif name == 'bt':
                    self.bt_var.set(not enabled)
                elif name == 'dnd':
                    self.dnd_var.set(not enabled)
                messagebox.showerror("Error", f"{name} toggle failed:\n{output}")

        self.runner.submit(run, done, label=f"Toggling {name}")


class ApkExtractorPanel(ttk.LabelFrame):
    """Extract installed APKs from the device to the PC.

    Resolves the install path via `pm path`, then pulls the file.
    Works on non-rooted devices for user-installed apps.
    """

    def __init__(self, parent, adb, runner, app=None):
        super().__init__(parent, text="APK Extractor", padding=10)
        self.adb = adb
        self.runner = runner
        self.app = app
        self.device_id = None

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Package:").pack(side='left')
        self.pkg_var = tk.StringVar()
        self.pkg_entry = ttk.Entry(top, textvariable=self.pkg_var, width=30)
        self.pkg_entry.pack(side='left', padx=(2, 8))
        ttk.Button(top, text="Browse apps",
                   command=self._fill_from_selection).pack(side='left',
                                                           padx=(2, 8))
        ttk.Button(top, text="Extract APK",
                   command=self.extract).pack(side='left')

        self.listbox = tk.Listbox(self, height=8)
        self.listbox.pack(fill='both', expand=True, pady=(5, 0))

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        self.pkg_entry.config(state=state)
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, ttk.Entry)):
                        w.config(state=state)
        self.listbox.config(state=state)
        if device_id:
            self._refresh_list()

    def _fill_from_selection(self):
        """Copy the currently selected app package from the Apps tab."""
        # Best effort: look for an AppManagerPanel ancestor.
        apps = self._find_app_panel()
        if apps:
            sel = apps.app_tree.selection()
            if sel:
                pkg = apps.app_tree.item(sel[0], 'values')[0]
                self.pkg_var.set(pkg)

    def _find_app_panel(self):
        if self.app and hasattr(self.app, 'apps_panel'):
            return self.app.apps_panel
        parent = self.master
        while parent:
            if isinstance(parent, AppManagerPanel):
                return parent
            parent = parent.master
        return None

    def _refresh_list(self):
        if not self.device_id:
            return

        def run():
            return self.adb.list_packages(self.device_id,
                                          third_party_only=True)

        def done(result):
            output, success = result
            self.listbox.delete(0, tk.END)
            if success:
                for line in output.splitlines():
                    pkg = line.replace('package:', '').strip()
                    if pkg:
                        self.listbox.insert(tk.END, pkg)

        self.runner.submit(run, done, label="Listing packages")

    def extract(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        pkg = self.pkg_var.get().strip()
        if not pkg:
            messagebox.showwarning("Warning", "Enter a package name")
            return

        default_name = f"{pkg}.apk"
        local_path = filedialog.asksaveasfilename(
            defaultextension='.apk',
            initialfile=default_name,
            title=f"Save {pkg} APK as",
            filetypes=[("APK files", "*.apk"), ("All files", "*.*")])
        if not local_path:
            return

        def run():
            return self.adb.extract_apk(pkg, local_path, self.device_id)

        def done(result):
            output, success = result
            if success:
                messagebox.showinfo("Extracted",
                                    f"APK saved to:\n{local_path}")
            else:
                messagebox.showerror("Error", f"Extraction failed:\n{output}")

        self.runner.submit(run, done, label=f"Extracting {pkg}")


class MultiDevicePanel(ttk.LabelFrame):
    """Send one shell command to every connected device at once.

    Useful for fleet QA: install the same APK on 5 devices, reboot all,
    or run a diagnostic command everywhere.
    """

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Multi-Device", padding=10)
        self.adb = adb
        self.runner = runner

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Command:").pack(side='left')
        self.cmd_var = tk.StringVar()
        self.cmd_entry = ttk.Entry(top, textvariable=self.cmd_var, width=40)
        self.cmd_entry.pack(side='left', padx=(2, 8), fill='x', expand=True)
        ttk.Button(top, text="Run on all devices",
                   command=self.run_all).pack(side='left')

        self.tree = ttk.Treeview(self, columns=('serial', 'output'),
                                 show='headings', height=8)
        self.tree.heading('serial', text='Device')
        self.tree.heading('output', text='Result')
        self.tree.column('serial', width=120)
        self.tree.column('output', width=400)
        self.tree.pack(fill='both', expand=True)

        scroll = ttk.Scrollbar(self, orient='vertical',
                               command=self.tree.yview)
        self.tree.configure(yscroll=scroll.set)
        scroll.pack(side='right', fill='y')

    def run_all(self):
        cmd = self.cmd_var.get().strip()
        if not cmd:
            messagebox.showwarning("Warning", "Enter a command")
            return

        # Split into tokens, but keep it simple: shell command + args.
        parts = shlex.split(cmd)
        shell_cmd = parts[0]
        shell_args = parts[1:]

        for item in self.tree.get_children():
            self.tree.delete(item)

        def run():
            return self.adb.run_on_all_devices(shell_cmd, *shell_args)

        def done(results):
            for serial, (output, ok) in results.items():
                status = 'OK' if ok else 'FAIL'
                snippet = output.strip().replace('\n', ' | ')[:200]
                display = f"[{status}] {snippet}"
                self.tree.insert('', tk.END, values=(serial, display))

        self.runner.submit(run, done, label=f"Running on {len(results) if results else 0} devices")


class ActivityLauncherPanel(ttk.LabelFrame):
    """Launch exported activities of a selected app.

    Parses `pm dump <pkg>` to find activities with
    `android:exported="true"` or MAIN/LAUNCHER intent filters, then lets the
    user launch any one with `am start -n`. No root required.
    """

    def __init__(self, parent, adb, runner, app=None):
        super().__init__(parent, text="Activity Launcher", padding=10)
        self.adb = adb
        self.runner = runner
        self.app = app
        self.device_id = None

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Package:").pack(side='left')
        self.pkg_var = tk.StringVar()
        self.pkg_entry = ttk.Entry(top, textvariable=self.pkg_var, width=30)
        self.pkg_entry.pack(side='left', padx=(2, 8))
        ttk.Button(top, text="Scan activities",
                   command=self.scan).pack(side='left')
        ttk.Button(top, text="Launch selected",
                   command=self.launch).pack(side='left', padx=(8, 0))

        self.listbox = tk.Listbox(self, height=10)
        self.listbox.pack(fill='both', expand=True, pady=(5, 0))

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        self.pkg_entry.config(state=state)
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, ttk.Entry)):
                        w.config(state=state)
        self.listbox.config(state=state)

    def _fill_from_app_panel(self):
        """Copy the currently selected app package from the Apps tab."""
        apps = self._find_app_panel()
        if apps:
            sel = apps.app_tree.selection()
            if sel:
                pkg = apps.app_tree.item(sel[0], 'values')[0]
                self.pkg_var.set(pkg)
                return True
        return False

    def _find_app_panel(self):
        if self.app and hasattr(self.app, 'apps_panel'):
            return self.app.apps_panel
        parent = self.master
        while parent:
            if isinstance(parent, AppManagerPanel):
                return parent
            parent = parent.master
        return None

    def scan(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        pkg = self.pkg_var.get().strip()
        if not pkg:
            if not self._fill_from_app_panel():
                messagebox.showwarning("Warning", "Enter a package name")
            else:
                pkg = self.pkg_var.get().strip()
        if not pkg:
            return

        def run():
            return self.adb.get_exported_activities(pkg, self.device_id)

        def done(result):
            activities, error = result
            self.listbox.delete(0, tk.END)
            if not activities:
                if error:
                    self.listbox.insert(tk.END, f"(error: {error})")
                else:
                    self.listbox.insert(tk.END, "(no exported activities found)")
            else:
                for act in activities:
                    self.listbox.insert(tk.END, act)

        self.runner.submit(run, done, label=f"Scanning activities for {pkg}")

    def launch(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning("Warning", "No activity selected")
            return
        activity = self.listbox.get(sel[0])
        if activity.startswith('('):
            messagebox.showwarning("Warning", "Cannot launch this entry")
            return
        pkg = self.pkg_var.get().strip() or self._pkg_from_activity(activity)

        def run():
            return self.adb.launch_activity(pkg, activity, self.device_id)

        def done(result):
            output, success = result
            if success:
                messagebox.showinfo("Launched", f"Started {activity}")
            else:
                messagebox.showerror("Error", f"Launch failed:\n{output}")

        self.runner.submit(run, done, label=f"Launching {activity}")

    def _pkg_from_activity(self, activity):
        """Best effort: strip leading dot and prepend package from entry."""
        if activity.startswith('.'):
            return self.pkg_var.get().strip() + activity
        return activity


class IntentSenderPanel(ttk.LabelFrame):
    """Send custom intents and broadcasts to test receivers, services,
    and deep links without writing a test app."""

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Intent Sender", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None

        form = ttk.Frame(self)
        form.pack(fill='x', pady=(0, 8))

        ttk.Label(form, text="Action:").grid(row=0, column=0, sticky='w', pady=2)
        self.action_var = tk.StringVar(value='android.intent.action.VIEW')
        ttk.Entry(form, textvariable=self.action_var, width=40).grid(
            row=0, column=1, sticky='ew', pady=2, padx=(5, 0))

        ttk.Label(form, text="Data (URI):").grid(row=1, column=0, sticky='w', pady=2)
        self.data_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.data_var, width=40).grid(
            row=1, column=1, sticky='ew', pady=2, padx=(5, 0))

        ttk.Label(form, text="Package:").grid(row=2, column=0, sticky='w', pady=2)
        self.pkg_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.pkg_var, width=40).grid(
            row=2, column=1, sticky='ew', pady=2, padx=(5, 0))

        form.columnconfigure(1, weight=1)

        btns = ttk.Frame(self)
        btns.pack(fill='x', pady=(0, 8))
        ttk.Button(btns, text="Start Activity", command=self.start_activity).pack(side='left', padx=(0, 5))
        ttk.Button(btns, text="Send Broadcast", command=self.send_broadcast).pack(side='left')

        self.output = scrolledtext.ScrolledText(self, height=8, wrap=tk.WORD, state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, (ttk.Frame, tk.Frame)):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Entry, ttk.Button)):
                        w.config(state=state)

    def _log(self, text):
        self.output.config(state='normal')
        self.output.insert(tk.END, text + '\n')
        self.output.see(tk.END)
        self.output.config(state='disabled')

    def start_activity(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        action = self.action_var.get().strip()
        data = self.data_var.get().strip()
        pkg = self.pkg_var.get().strip()
        if not action:
            messagebox.showwarning("Warning", "Enter an action")
            return

        def run():
            return self.adb.send_intent(action, data, pkg, self.device_id)

        def done(result):
            output, success = result
            self._log(f"> am start -a {action}" + (f" -d {data}" if data else "") + (f" {pkg}" if pkg else ""))
            self._log(output.strip() or '(no output)')
            if not success:
                messagebox.showerror("Error", f"Intent failed:\n{output}")

        self.runner.submit(run, done, label="Sending intent")

    def send_broadcast(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        action = self.action_var.get().strip()
        if not action:
            messagebox.showwarning("Warning", "Enter an action")
            return

        def run():
            return self.adb.send_broadcast(action, self.device_id)

        def done(result):
            output, success = result
            self._log(f"> am broadcast -a {action}")
            self._log(output.strip() or '(no output)')
            if not success:
                messagebox.showerror("Error", f"Broadcast failed:\n{output}")

        self.runner.submit(run, done, label="Sending broadcast")


class AppOpsViewerPanel(ttk.LabelFrame):
    """View runtime permission/op state for a package (`dumpsys appops`).

    Read-only: shows which ops (location, camera, microphone, etc.) are
    allowed, rejected, or prompted for the selected app."""

    def __init__(self, parent, adb, runner, app=None):
        super().__init__(parent, text="App Ops Viewer", padding=10)
        self.adb = adb
        self.runner = runner
        self.app = app
        self.device_id = None

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Package:").pack(side='left')
        self.pkg_var = tk.StringVar()
        self.pkg_entry = ttk.Entry(top, textvariable=self.pkg_var, width=30)
        self.pkg_entry.pack(side='left', padx=(2, 8))
        ttk.Button(top, text="Refresh", command=self.refresh).pack(side='left')
        ttk.Button(top, text="From selection", command=self._fill_from_app_panel).pack(side='left', padx=(8, 0))

        self.output = scrolledtext.ScrolledText(self, height=16, wrap=tk.WORD, state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        self.pkg_entry.config(state=state)
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, ttk.Entry)):
                        w.config(state=state)

    def _fill_from_app_panel(self):
        apps = self._find_app_panel()
        if apps:
            sel = apps.app_tree.selection()
            if sel:
                pkg = apps.app_tree.item(sel[0], 'values')[0]
                self.pkg_var.set(pkg)
                return True
        return False

    def _find_app_panel(self):
        if self.app and hasattr(self.app, 'apps_panel'):
            return self.app.apps_panel
        parent = self.master
        while parent:
            if isinstance(parent, AppManagerPanel):
                return parent
            parent = parent.master
        return None

    def refresh(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        pkg = self.pkg_var.get().strip()
        if not pkg:
            messagebox.showwarning("Warning", "Enter a package name")
            return

        def run():
            return self.adb.get_app_ops(pkg, self.device_id)

        def done(result):
            output, success = result
            self.output.config(state='normal')
            self.output.delete('1.0', tk.END)
            self.output.insert('1.0', output if success else f"(error: {output})")
            self.output.config(state='disabled')

        self.runner.submit(run, done, label=f"Reading app ops for {pkg}")


class PackageInspectorPanel(ttk.LabelFrame):
    """Inspect a package's full manifest via `dumpsys package`.

    Shows declared permissions, activities, services, receivers, and
    whether the app is debuggable — useful for QA and red-team recon."""

    def __init__(self, parent, adb, runner, app=None):
        super().__init__(parent, text="Package Inspector", padding=10)
        self.adb = adb
        self.runner = runner
        self.app = app
        self.device_id = None

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Package:").pack(side='left')
        self.pkg_var = tk.StringVar()
        self.pkg_entry = ttk.Entry(top, textvariable=self.pkg_var, width=30)
        self.pkg_entry.pack(side='left', padx=(2, 8))
        ttk.Button(top, text="Inspect", command=self.inspect).pack(side='left')
        ttk.Button(top, text="From selection", command=self._fill_from_app_panel).pack(side='left', padx=(8, 0))

        self.output = scrolledtext.ScrolledText(self, height=16, wrap=tk.WORD, state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        self.pkg_entry.config(state=state)
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, ttk.Entry)):
                        w.config(state=state)

    def _fill_from_app_panel(self):
        apps = self._find_app_panel()
        if apps:
            sel = apps.app_tree.selection()
            if sel:
                pkg = apps.app_tree.item(sel[0], 'values')[0]
                self.pkg_var.set(pkg)
                return True
        return False

    def _find_app_panel(self):
        if self.app and hasattr(self.app, 'apps_panel'):
            return self.app.apps_panel
        parent = self.master
        while parent:
            if isinstance(parent, AppManagerPanel):
                return parent
            parent = parent.master
        return None

    def inspect(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        pkg = self.pkg_var.get().strip()
        if not pkg:
            messagebox.showwarning("Warning", "Enter a package name")
            return

        def run():
            return self.adb.get_app_info(pkg, self.device_id)

        def done(result):
            output, success = result
            self.output.config(state='normal')
            self.output.delete('1.0', tk.END)
            self.output.insert('1.0', output if success else f"(error: {output})")
            self.output.config(state='disabled')

        self.runner.submit(run, done, label=f"Inspecting {pkg}")


class MacroStepDialog(tk.Toplevel):
    """Dialog to add/edit one macro step (tap, swipe, type, keyevent, sleep)."""

    STEP_TYPES = ('Tap', 'Swipe', 'Type text', 'Key event', 'Sleep')

    def __init__(self, parent, step=None):
        super().__init__(parent)
        self.title("Macro Step")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)
        self.result = None

        ttk.Label(self, text="Step type:").grid(row=0, column=0, sticky='w',
                                                 padx=10, pady=(10, 0))
        self.type_var = tk.StringVar(value=step['type'] if step else 'Tap')
        type_combo = ttk.Combobox(self, textvariable=self.type_var,
                                  values=self.STEP_TYPES, state='readonly',
                                  width=18)
        type_combo.grid(row=0, column=1, sticky='ew', padx=10, pady=(10, 0))
        type_combo.bind('<<ComboboxSelected>>', lambda _e: self._layout_fields())

        self.fields_frame = ttk.Frame(self)
        self.fields_frame.grid(row=1, column=0, columnspan=2, sticky='ew',
                               padx=10, pady=10)
        self.fields_frame.columnconfigure(1, weight=1)

        self._widgets = {}
        self.step = step or {'type': 'Tap'}
        self._layout_fields()

        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky='e', padx=10,
                       pady=(0, 12))
        ttk.Button(btn_frame, text="OK", command=self._ok).pack(side='left',
                                                                 padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(
            side='left', padx=5)

        self.columnconfigure(1, weight=1)
        self.wait_window(self)

    def _layout_fields(self):
        for w in self.fields_frame.winfo_children():
            w.destroy()
        self._widgets = {}
        step_type = self.type_var.get()

        def add_row(label, key, default=''):
            row = len(self._widgets)
            ttk.Label(self.fields_frame, text=label).grid(row=row, column=0,
                                                          sticky='w', pady=2)
            var = tk.StringVar(value=self.step.get(key, default))
            entry = ttk.Entry(self.fields_frame, textvariable=var, width=30)
            entry.grid(row=row, column=1, sticky='ew', pady=2, padx=(5, 0))
            self._widgets[key] = var

        if step_type == 'Tap':
            add_row("X:", 'x', '0')
            add_row("Y:", 'y', '0')
        elif step_type == 'Swipe':
            add_row("X1:", 'x1', '0')
            add_row("Y1:", 'y1', '0')
            add_row("X2:", 'x2', '0')
            add_row("Y2:", 'y2', '0')
            add_row("Duration (ms):", 'duration', '300')
        elif step_type == 'Type text':
            add_row("Text:", 'text', '')
        elif step_type == 'Key event':
            add_row("Keycode/name:", 'keycode', 'KEYCODE_HOME')
        elif step_type == 'Sleep':
            add_row("Milliseconds:", 'ms', '500')

    def _ok(self):
        step_type = self.type_var.get()
        step = {'type': step_type}
        for key, var in self._widgets.items():
            step[key] = var.get().strip()
        self.result = step
        self.destroy()


class MacroPanel(ttk.LabelFrame):
    """Record, edit, save, and replay input macros (no root required).

    Steps are plain `adb shell input` commands: tap, swipe, text, keyevent,
    and sleep. Build a macro step by step, then play it back on the device.
    """

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Macro Recorder", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self.steps = []
        self._playing = False
        self._play_job = None

        # Steps list
        list_frame = ttk.Frame(self)
        list_frame.pack(fill='both', expand=True, pady=(0, 5))

        self.listbox = tk.Listbox(list_frame, height=8)
        self.listbox.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(list_frame, orient='vertical',
                               command=self.listbox.yview)
        self.listbox.configure(yscroll=scroll.set)
        scroll.pack(side='right', fill='y')

        # Step editor
        edit_frame = ttk.Frame(self)
        edit_frame.pack(fill='x', pady=(0, 5))
        ttk.Button(edit_frame, text="Add Step",
                   command=self.add_step).pack(side='left', padx=2)
        ttk.Button(edit_frame, text="Edit Step",
                   command=self.edit_step).pack(side='left', padx=2)
        ttk.Button(edit_frame, text="Remove",
                   command=self.remove_step).pack(side='left', padx=2)
        ttk.Button(edit_frame, text="Move Up",
                   command=lambda: self._move_step(-1)).pack(side='left',
                                                              padx=2)
        ttk.Button(edit_frame, text="Move Down",
                   command=lambda: self._move_step(1)).pack(side='left',
                                                             padx=2)

        # Playback controls
        play_frame = ttk.Frame(self)
        play_frame.pack(fill='x', pady=(0, 5))
        self.play_btn = ttk.Button(play_frame, text="Play Macro",
                                   command=self.play_macro)
        self.play_btn.pack(side='left', padx=2)
        self.stop_btn = ttk.Button(play_frame, text="Stop",
                                   command=self.stop_macro, state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        # File ops
        file_frame = ttk.Frame(self)
        file_frame.pack(fill='x')
        ttk.Button(file_frame, text="Save Macro",
                   command=self.save_macro).pack(side='left', padx=2)
        ttk.Button(file_frame, text="Load Macro",
                   command=self.load_macro).pack(side='left', padx=2)
        ttk.Button(file_frame, text="Clear",
                   command=self.clear_macro).pack(side='left', padx=2)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Button, tk.Listbox)):
                        w.config(state=state)
        self.listbox.config(state=state)

    def _step_text(self, step):
        t = step['type']
        if t == 'Tap':
            return f"TAP ({step.get('x','0')},{step.get('y','0')})"
        if t == 'Swipe':
            return (f"SWIPE ({step.get('x1','0')},{step.get('y1','0')})"
                    f" -> ({step.get('x2','0')},{step.get('y2','0')})"
                    f" {step.get('duration','300')}ms")
        if t == 'Type text':
            return f"TYPE {step.get('text','')}"
        if t == 'Key event':
            return f"KEY {step.get('keycode','KEYCODE_HOME')}"
        if t == 'Sleep':
            return f"SLEEP {step.get('ms','500')}ms"
        return f"UNKNOWN {t}"

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        for step in self.steps:
            self.listbox.insert(tk.END, self._step_text(step))

    def add_step(self):
        dlg = MacroStepDialog(self)
        if dlg.result:
            self.steps.append(dlg.result)
            self.refresh_list()

    def edit_step(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showwarning("Warning", "No step selected")
            return
        idx = sel[0]
        dlg = MacroStepDialog(self, self.steps[idx])
        if dlg.result:
            self.steps[idx] = dlg.result
            self.refresh_list()

    def remove_step(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        del self.steps[idx]
        self.refresh_list()

    def _move_step(self, delta):
        sel = self.listbox.curselection()
        if not sel:
            return
        idx = sel[0]
        new = idx + delta
        if 0 <= new < len(self.steps):
            self.steps[idx], self.steps[new] = self.steps[new], self.steps[idx]
            self.refresh_list()
            self.listbox.selection_set(new)

    def clear_macro(self):
        self.steps = []
        self.refresh_list()

    def save_macro(self):
        if not self.steps:
            messagebox.showwarning("Warning", "No steps to save")
            return
        path = filedialog.asksaveasfilename(defaultextension='.json',
                                            filetypes=[("JSON", "*.json")],
                                            title="Save macro")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                json.dump(self.steps, fh, indent=2)
            messagebox.showinfo("Saved", f"Macro saved to {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Save failed:\n{e}")

    def load_macro(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")],
                                          title="Load macro")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as fh:
                self.steps = json.load(fh)
            self.refresh_list()
            messagebox.showinfo("Loaded", f"Macro loaded from {path}")
        except Exception as e:
            messagebox.showerror("Error", f"Load failed:\n{e}")

    def play_macro(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        if not self.steps:
            messagebox.showwarning("Warning", "Macro is empty")
            return
        if self._playing:
            return
        self._playing = True
        self.play_btn.config(state='disabled')
        self.stop_btn.config(state='normal')

        def run():
            device_id = self.device_id
            for i, step in enumerate(self.steps):
                if not self._playing:
                    break
                ok = self._exec_step(device_id, step, i)
                if not ok:
                    return f"Step {i + 1} failed: {step['type']}"
                ms = step.get('ms') or 0
                # Sleep between steps (except the last)
                if i < len(self.steps) - 1 and ms:
                    import time as _time
                    _time.sleep(ms / 1000.0)
            return None

        def done(result):
            self._playing = False
            self.play_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            if result:
                messagebox.showerror("Error", str(result))
            else:
                messagebox.showinfo("Done", "Macro finished")

        self.runner.submit(run, done, label="Playing macro")

    def stop_macro(self):
        self._playing = False

    def _exec_step(self, device_id, step, index):
        t = step['type']
        try:
            if t == 'Tap':
                x = int(step.get('x', '0'))
                y = int(step.get('y', '0'))
                out, ok = self.adb.run_shell_command(
                    f'input tap {x} {y}', device_id)
            elif t == 'Swipe':
                x1 = int(step.get('x1', '0'))
                y1 = int(step.get('y1', '0'))
                x2 = int(step.get('x2', '0'))
                y2 = int(step.get('y2', '0'))
                dur = int(step.get('duration', '300'))
                out, ok = self.adb.run_shell_command(
                    f'input swipe {x1} {y1} {x2} {y2} {dur}', device_id)
            elif t == 'Type text':
                text = step.get('text', '')
                # Escape spaces for adb shell input text
                safe = text.replace(' ', '%s')
                out, ok = self.adb.run_shell_command(
                    f'input text {safe}', device_id)
            elif t == 'Key event':
                key = step.get('keycode', 'KEYCODE_HOME')
                out, ok = self.adb.run_shell_command(
                    f'input keyevent {key}', device_id)
            elif t == 'Sleep':
                # Sleep handled by caller between steps
                return True
            else:
                return False
            return ok
        except Exception:
            return False


class SchedulerPanel(ttk.LabelFrame):
    """Run an ADB shell command on a repeating interval.

    Useful for polling a property, repeating a keyevent, or any
    command that should run every N seconds until stopped.
    """

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Scheduler", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self._running = False
        self._job = None
        self._count = 0

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Command:").pack(side='left')
        self.cmd_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.cmd_var, width=30).pack(
            side='left', padx=(2, 8), fill='x', expand=True)

        ttk.Label(top, text="Every (s):").pack(side='left')
        self.interval_var = tk.StringVar(value='5')
        ttk.Entry(top, textvariable=self.interval_var, width=6).pack(
            side='left', padx=(2, 8))

        self.start_btn = ttk.Button(top, text="Start", command=self.start)
        self.start_btn.pack(side='left', padx=2)
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop,
                                   state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        self.count_var = tk.StringVar(value="Runs: 0")
        ttk.Label(top, textvariable=self.count_var).pack(side='left',
                                                         padx=(10, 0))

        self.output = scrolledtext.ScrolledText(self, height=10, wrap=tk.WORD,
                                                state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Entry, ttk.Button)):
                        w.config(state=state)
        self.output.config(state=state)
        if not device_id:
            self.stop()

    def _log(self, text):
        self.output.config(state='normal')
        self.output.insert(tk.END, text + '\n')
        self.output.see(tk.END)
        self.output.config(state='disabled')

    def start(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        cmd = self.cmd_var.get().strip()
        if not cmd:
            messagebox.showwarning("Warning", "Enter a command")
            return
        try:
            interval = float(self.interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Interval must be a positive number")
            return

        self._running = True
        self._count = 0
        self.count_var.set("Runs: 0")
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.output.config(state='normal')
        self.output.delete('1.0', tk.END)
        self.output.config(state='disabled')
        self._tick()

    def stop(self):
        self._running = False
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

    def _tick(self):
        if not self._running:
            return

        def run():
            return self.adb.run_shell_command(self.cmd_var.get().strip(),
                                              self.device_id)

        def done(result):
            output, success = result
            self._count += 1
            self.count_var.set(f"Runs: {self._count}")
            prefix = "OK" if success else "FAIL"
            snippet = output.strip().replace('\n', ' | ')[:120]
            self._log(f"[{prefix}] {snippet}")
            if self._running:
                self._job = self.after(int(float(self.interval_var.get()) * 1000),
                                       self._tick)

        self.runner.submit(run, done, label="Scheduled command")


class TimelapsePanel(ttk.LabelFrame):
    """Auto-screenshot timelapse: capture the screen every N seconds to a
    folder on the PC. Works on non-rooted devices."""

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Screenshot Timelapse", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None
        self._running = False
        self._job = None
        self._count = 0

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))

        ttk.Label(top, text="Folder:").pack(side='left')
        self.folder_var = tk.StringVar(value=str(Path.home()))
        ttk.Entry(top, textvariable=self.folder_var, width=30).pack(
            side='left', padx=(2, 5), fill='x', expand=True)
        ttk.Button(top, text="Browse", command=self.browse_folder).pack(
            side='left', padx=(2, 8))

        ttk.Label(top, text="Every (s):").pack(side='left')
        self.interval_var = tk.StringVar(value='10')
        ttk.Entry(top, textvariable=self.interval_var, width=6).pack(
            side='left', padx=(2, 8))

        self.start_btn = ttk.Button(top, text="Start", command=self.start)
        self.start_btn.pack(side='left', padx=2)
        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop,
                                   state='disabled')
        self.stop_btn.pack(side='left', padx=2)

        self.count_var = tk.StringVar(value="Captures: 0")
        ttk.Label(top, textvariable=self.count_var).pack(side='left',
                                                         padx=(10, 0))

        self.output = scrolledtext.ScrolledText(self, height=8, wrap=tk.WORD,
                                                state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, (ttk.Entry, ttk.Button)):
                        w.config(state=state)
        self.output.config(state=state)
        if not device_id:
            self.stop()

    def browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder_var.get())
        if folder:
            self.folder_var.set(folder)

    def _log(self, text):
        self.output.config(state='normal')
        self.output.insert(tk.END, text + '\n')
        self.output.see(tk.END)
        self.output.config(state='disabled')

    def start(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Error", "Choose a valid folder")
            return
        try:
            interval = float(self.interval_var.get())
            if interval <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Interval must be a positive number")
            return

        self._running = True
        self._count = 0
        self.count_var.set("Captures: 0")
        self.start_btn.config(state='disabled')
        self.stop_btn.config(state='normal')
        self.output.config(state='normal')
        self.output.delete('1.0', tk.END)
        self.output.config(state='disabled')
        self._tick()

    def stop(self):
        self._running = False
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')

    def _tick(self):
        if not self._running:
            return

        folder = self.folder_var.get().strip()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        save_path = os.path.join(folder, f"screenshot_{timestamp}.png")

        def run():
            return self.adb.take_screenshot(save_path, self.device_id)

        def done(result):
            output, success = result
            self._count += 1
            self.count_var.set(f"Captures: {self._count}")
            if success:
                self._log(f"Saved {save_path}")
            else:
                self._log(f"Failed: {output}")
            if self._running:
                self._job = self.after(
                    int(float(self.interval_var.get()) * 1000), self._tick)

        self.runner.submit(run, done, label="Capturing screenshot")


class ExportReportPanel(ttk.LabelFrame):
    """Export a full device report: info, battery, CPU, packages, and recent
    logcat to a text file for bug reports or documentation."""

    def __init__(self, parent, adb, runner):
        super().__init__(parent, text="Export Device Report", padding=10)
        self.adb = adb
        self.runner = runner
        self.device_id = None

        top = ttk.Frame(self)
        top.pack(fill='x', pady=(0, 5))
        ttk.Button(top, text="Export Report", command=self.export).pack(
            side='left')

        self.output = scrolledtext.ScrolledText(self, height=12, wrap=tk.WORD,
                                                state='disabled')
        self.output.pack(fill='both', expand=True)

        self.set_device(None)

    def set_device(self, device_id):
        self.device_id = device_id
        state = 'normal' if device_id else 'disabled'
        for child in self.winfo_children():
            if isinstance(child, ttk.Frame):
                for w in child.winfo_children():
                    if isinstance(w, ttk.Button):
                        w.config(state=state)

    def _log(self, text):
        self.output.config(state='normal')
        self.output.insert(tk.END, text + '\n')
        self.output.see(tk.END)
        self.output.config(state='disabled')

    def export(self):
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.txt',
            filetypes=[("Text", "*.txt"), ("All files", "*.*")],
            title="Save device report")
        if not path:
            return

        def run():
            parts = []
            parts.append("=== ADB GUI Device Report ===")
            parts.append(f"Generated: {datetime.now().isoformat()}")
            parts.append(f"Device: {self.device_id}")
            parts.append("")

            # Device info
            parts.append("=== DEVICE INFO ===")
            out, ok = self.adb.run_command('-s', self.device_id, 'shell',
                                           'getprop')
            parts.append(out if ok else f"(error: {out})")
            parts.append("")

            # Battery
            parts.append("=== BATTERY ===")
            out, ok = self.adb.run_command('-s', self.device_id, 'shell',
                                           'dumpsys', 'battery')
            parts.append(out if ok else f"(error: {out})")
            parts.append("")

            # CPU
            parts.append("=== CPU ===")
            out, ok = self.adb.run_command('-s', self.device_id, 'shell',
                                           'dumpsys', 'cpuinfo')
            parts.append(out if ok else f"(error: {out})")
            parts.append("")

            # Packages (count + list)
            parts.append("=== PACKAGES ===")
            out, ok = self.adb.list_packages(self.device_id,
                                             third_party_only=True)
            if ok:
                pkgs = [l.replace('package:', '').strip()
                        for l in out.splitlines() if l.strip()]
                parts.append(f"Third-party apps: {len(pkgs)}")
                parts.append(out)
            else:
                parts.append(f"(error: {out})")
            parts.append("")

            # Recent logcat (last 100 lines)
            parts.append("=== LOGCAT (last 100 lines) ===")
            out, ok = self.adb.run_command('-s', self.device_id, 'shell',
                                           'logcat', '-d', '-t', '100')
            parts.append(out if ok else f"(error: {out})")

            return "\n".join(parts)

        def done(report):
            try:
                with open(path, 'w', encoding='utf-8') as fh:
                    fh.write(report)
                messagebox.showinfo("Exported",
                                    f"Device report saved to:\n{path}")
                self.output.config(state='normal')
                self.output.delete('1.0', tk.END)
                self.output.insert('1.0', report[:2000] +
                                   ("\n... (truncated in view)"
                                    if len(report) > 2000 else ""))
                self.output.config(state='disabled')
            except Exception as e:
                messagebox.showerror("Error", f"Save failed:\n{e}")

        self.runner.submit(run, done, label="Exporting device report")


class ADBGUI(tk.Tk):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        
        self.title("ADB GUI - Android Debug Bridge Interface")
        self.geometry(CONFIG.get('window_geometry', '1200x700'))
        
        self.adb = ADBWrapper()
        self.settings = CONFIG
        self._device_text = "Ready"
        self._busy_label = None
        self._closing = False
        self._last_device_id = None
        self._dark_mode = self.settings.get('dark_mode', False)
        self.runner = TaskRunner(self, on_status=self._on_busy_status)
        
        main_container = ttk.Frame(self, padding=10)
        main_container.pack(fill='both', expand=True)
        
        self.device_panel = DevicePanel(main_container, self.adb, self.runner,
                                        self.on_device_change)
        self.device_panel.pack(fill='x', pady=(0, 10))
        
        self.notebook = ttk.Notebook(main_container)
        self.notebook.pack(fill='both', expand=True)
        
        self.create_info_tab()
        self.create_apps_tab()
        self.create_files_tab()
        self.create_shell_tab()
        self.create_logcat_tab()
        self.create_tools_tab()
        self.create_port_forward_tab()
        self.create_hardware_monitor_tab()
        self.create_device_controls_tab()
        self.create_apk_extractor_tab()
        self.create_multi_device_tab()
        self.create_activity_launcher_tab()
        self.create_intent_sender_tab()
        self.create_app_ops_tab()
        self.create_package_inspector_tab()
        self.create_macro_tab()
        self.create_scheduler_tab()
        self.create_timelapse_tab()
        self.create_export_report_tab()
        
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Frame(main_container)
        status_bar.pack(fill='x', pady=(10, 0))
        ttk.Label(status_bar, textvariable=self.status_var, relief='sunken',
                  anchor='w').pack(side='left', fill='x', expand=True)
        self.dark_mode_var = tk.BooleanVar(value=self._dark_mode)
        ttk.Checkbutton(status_bar, text="Dark mode",
                        variable=self.dark_mode_var,
                        command=self.toggle_dark_mode).pack(side='right')
        self._apply_theme()
        
        self.device_panel.refresh_devices()
        self._start_device_tracking()
        # Without this the title-bar close is Tk's default, which never runs
        # the teardown below and leaves an `adb logcat` child running.
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        # Interpreter exit (Ctrl-C, an exception before mainloop, a script
        # that forgets to destroy) must not leave adb children behind either.
        atexit.register(self.destroy)
    
    def destroy(self):
        """Shut background work down before the window goes away."""
        self._closing = True
        # Remember window geometry for next launch.
        try:
            self.settings.set('window_geometry', self.geometry())
            self.settings.save()
        except Exception:
            pass
        if getattr(self, '_window_gone', False):
            return
        if not getattr(self, '_torn_down', False):
            self._torn_down = True
            if hasattr(self, 'logcat_panel'):
                # Kills the logcat child and cancels this panel's own timers.
                # Never cancel timers from the root instead: after_cancel
                # deletes the Tcl command globally while the panel still
                # lists it, and the panel's own destroy then fails.
                self.logcat_panel.close()
            if hasattr(self, 'hardware_panel'):
                self.hardware_panel._toggle_auto(False)
            if hasattr(self, 'scheduler_panel'):
                self.scheduler_panel.stop()
            if hasattr(self, 'timelapse_panel'):
                self.timelapse_panel.stop()
            if hasattr(self, 'macro_panel'):
                self.macro_panel.stop_macro()
            if hasattr(self, 'adb'):
                self.adb.stop_tracking()
            if hasattr(self, 'runner'):
                self.runner.close()
        super().destroy()
        self._window_gone = True
    
    def create_info_tab(self):
        """Create device info tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Device Info")
        
        self.info_panel = DeviceInfoPanel(frame, self.adb, self.runner)
        self.info_panel.pack(fill='both', expand=True)
    
    def create_apps_tab(self):
        """Create app manager tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Apps")
        
        self.apps_panel = AppManagerPanel(frame, self.adb, self.runner)
        self.apps_panel.pack(fill='both', expand=True)
    
    def create_files_tab(self):
        """Create file manager tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Files")
        
        self.files_panel = FileManagerPanel(frame, self.adb, self.runner)
        self.files_panel.pack(fill='both', expand=True)
    
    def create_shell_tab(self):
        """Create shell tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Shell")
        
        self.shell_panel = ShellPanel(frame, self.adb, self.runner)
        self.shell_panel.pack(fill='both', expand=True)
    
    def create_logcat_tab(self):
        """Create logcat tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Logcat")
        
        self.logcat_panel = LogcatPanel(frame, self.adb, self.runner)
        self.logcat_panel.pack(fill='both', expand=True)
    
    def create_tools_tab(self):
        """Create tools tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Tools")
        
        self.tools_panel = ToolsPanel(frame, self.adb, self.runner)
        self.tools_panel.pack(fill='both', expand=True)

    def create_port_forward_tab(self):
        """Create port forwarding tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Port Forwarding")
        
        self.port_forward_panel = PortForwardPanel(frame, self.adb, self.runner)
        self.port_forward_panel.pack(fill='both', expand=True)

    def create_hardware_monitor_tab(self):
        """Create hardware monitor tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Hardware Monitor")
        
        self.hardware_panel = HardwareMonitorPanel(frame, self.adb, self.runner)
        self.hardware_panel.pack(fill='both', expand=True)

    def create_device_controls_tab(self):
        """Create device controls tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Device Controls")
        
        self.controls_panel = RemoteControlPanel(frame, self.adb, self.runner)
        self.controls_panel.pack(fill='both', expand=True)

    def create_apk_extractor_tab(self):
        """Create APK extractor tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="APK Extractor")
        
        self.apk_panel = ApkExtractorPanel(frame, self.adb, self.runner, app=self)
        self.apk_panel.pack(fill='both', expand=True)

    def create_multi_device_tab(self):
        """Create multi-device tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Multi-Device")
        
        self.multi_panel = MultiDevicePanel(frame, self.adb, self.runner)
        self.multi_panel.pack(fill='both', expand=True)

    def create_activity_launcher_tab(self):
        """Create activity launcher tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Activity Launcher")
        
        self.activity_panel = ActivityLauncherPanel(frame, self.adb, self.runner, app=self)
        self.activity_panel.pack(fill='both', expand=True)

    def create_intent_sender_tab(self):
        """Create intent sender tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Intent Sender")
        
        self.intent_panel = IntentSenderPanel(frame, self.adb, self.runner)
        self.intent_panel.pack(fill='both', expand=True)

    def create_app_ops_tab(self):
        """Create app ops viewer tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="App Ops")
        
        self.appops_panel = AppOpsViewerPanel(frame, self.adb, self.runner, app=self)
        self.appops_panel.pack(fill='both', expand=True)

    def create_package_inspector_tab(self):
        """Create package inspector tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Package Inspector")
        
        self.pkg_inspector_panel = PackageInspectorPanel(frame, self.adb, self.runner, app=self)
        self.pkg_inspector_panel.pack(fill='both', expand=True)

    def create_macro_tab(self):
        """Create macro recorder tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Macro Recorder")
        
        self.macro_panel = MacroPanel(frame, self.adb, self.runner)
        self.macro_panel.pack(fill='both', expand=True)

    def create_scheduler_tab(self):
        """Create scheduler tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Scheduler")
        
        self.scheduler_panel = SchedulerPanel(frame, self.adb, self.runner)
        self.scheduler_panel.pack(fill='both', expand=True)

    def create_timelapse_tab(self):
        """Create screenshot timelapse tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Screenshot Timelapse")
        
        self.timelapse_panel = TimelapsePanel(frame, self.adb, self.runner)
        self.timelapse_panel.pack(fill='both', expand=True)

    def create_export_report_tab(self):
        """Create export report tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Export Report")
        
        self.export_panel = ExportReportPanel(frame, self.adb, self.runner)
        self.export_panel.pack(fill='both', expand=True)

    def on_device_change(self, device_id):
        """Handle a device selection or device state change.

        A new device resets every panel. The same device reporting a new
        state (e.g. unauthorized -> device) only reloads data, so shell
        output, a running logcat and the current directory survive.
        """
        if device_id == self._last_device_id:
            self.info_panel.refresh_info()
            self.apps_panel.refresh_apps()
            self.files_panel.refresh_files()
        else:
            self._last_device_id = device_id
            self.info_panel.set_device(device_id)
            self.apps_panel.set_device(device_id)
            self.files_panel.set_device(device_id)
            self.shell_panel.set_device(device_id)
            self.logcat_panel.set_device(device_id)
            self.tools_panel.set_device(device_id)
            self.port_forward_panel.set_device(device_id)
            self.hardware_panel.set_device(device_id)
            self.controls_panel.set_device(device_id)
            self.apk_panel.set_device(device_id)
            self.activity_panel.set_device(device_id)
            self.intent_panel.set_device(device_id)
            self.appops_panel.set_device(device_id)
            self.pkg_inspector_panel.set_device(device_id)
            self.macro_panel.set_device(device_id)
            self.scheduler_panel.set_device(device_id)
            self.timelapse_panel.set_device(device_id)
            self.export_panel.set_device(device_id)
            self.multi_panel.tree.delete(*self.multi_panel.tree.get_children())
        
        if device_id:
            self._device_text = f"Connected to: {device_id}"
        else:
            self._device_text = "No device connected"
        self._update_status()
    
    def _on_busy_status(self, label):
        """Receive the label of the job the runner is working on (or None)."""
        self._busy_label = label
        self._update_status()
    
    def _update_status(self):
        if not hasattr(self, 'status_var') or self._closing:
            return
        text = self._device_text
        if self._busy_label:
            text = f"{text} — {self._busy_label}…"
        self.status_var.set(text)

    def _apply_theme(self):
        """Apply light or dark ttk theme based on self._dark_mode."""
        style = ttk.Style(self)
        if self._dark_mode:
            # Clam theme is the most customizable; override key colors.
            style.theme_use('clam')
            style.configure('.', background='#2b2b2b', foreground='#ffffff',
                            fieldbackground='#3c3c3c')
            style.configure('TFrame', background='#2b2b2b')
            style.configure('TLabel', background='#2b2b2b',
                            foreground='#ffffff')
            style.configure('TEntry', fieldbackground='#3c3c3c',
                            foreground='#ffffff')
            style.configure('TCombobox', fieldbackground='#3c3c3c',
                            foreground='#ffffff')
            style.configure('TCheckbutton', background='#2b2b2b',
                            foreground='#ffffff')
            style.configure('TRadiobutton', background='#2b2b2b',
                            foreground='#ffffff')
            style.configure('TButton', background='#3c3c3c',
                            foreground='#ffffff')
            style.map('TButton', background=[('active', '#4a4a4a')])
            style.configure('TNotebook', background='#2b2b2b')
            style.configure('TNotebook.Tab', background='#3c3c3c',
                            foreground='#ffffff')
            style.map('TNotebook.Tab', background=[('selected', '#4a4a4a')])
            style.configure('Horizontal.TProgressbar',
                            background='#4a4a4a')
            self.configure(bg='#2b2b2b')
        else:
            style.theme_use('default')
            self.configure(bg='')

    def toggle_dark_mode(self):
        """Toggle dark mode on/off and persist the choice."""
        self._dark_mode = bool(self.dark_mode_var.get())
        self.settings.set('dark_mode', self._dark_mode)
        self.settings.save()
        self._apply_theme()
    
    def _start_device_tracking(self):
        """Follow `adb track-devices` so the list updates the instant it changes."""
        def watch():
            delay = 2.0
            misses = 0
            while not self._closing:
                framed = self.adb.track_devices(self._on_devices_changed)
                if self._closing:
                    return
                if framed:
                    misses, delay = 0, 2.0
                else:
                    misses += 1
                    if misses >= 3:
                        self.runner.post(self._poll_devices)
                        return
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        
        threading.Thread(target=watch, name='device-tracker', daemon=True).start()
    
    def _on_devices_changed(self):
        """Called from the tracker thread; marshal onto the Tk thread."""
        self.runner.post(self.device_panel.refresh_devices)
    
    def _poll_devices(self):
        """Fallback used only when `adb track-devices` is unavailable."""
        if self._closing:
            return
        self.device_panel.refresh_devices()
        if not self._closing:
            self.after(5000, self._poll_devices)


def main():
    """Main entry point"""
    app = ADBGUI()
    app.mainloop()


if __name__ == "__main__":
    main()