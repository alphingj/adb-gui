#!/usr/bin/env python3
"""
ADB GUI - A graphical user interface for Android Debug Bridge (ADB)
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import base64
import atexit
import io
import queue
import subprocess
import threading
import os
import posixpath
import re
import shlex
import shutil
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime

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


class ADBGUI(tk.Tk):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        
        self.title("ADB GUI - Android Debug Bridge Interface")
        self.geometry("1200x700")
        
        self.adb = ADBWrapper()
        self._device_text = "Ready"
        self._busy_label = None
        self._closing = False
        self._last_device_id = None
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
        
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(main_container, textvariable=self.status_var, relief='sunken', anchor='w')
        status_bar.pack(fill='x', pady=(10, 0))
        
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