#!/usr/bin/env python3
"""Tests for the real adb_gui module (device parsing and track-devices)."""

import os
import stat
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adb_gui import ADBWrapper, parse_device_list  # noqa: E402


class TestParseDeviceList(unittest.TestCase):
    """parse_device_list must accept device lines and reject adb chatter."""

    def test_devices_l_output(self):
        output = (
            "List of devices attached\n"
            "abc12345       device usb:123 product:device model:Pixel_5 device:redfin\n"
            "def67890       device usb:456 product:device model:Galaxy_S21 device:samsung\n"
        )
        devices = parse_device_list(output)

        self.assertEqual([d['id'] for d in devices], ['abc12345', 'def67890'])
        self.assertEqual(devices[0]['model'], 'Pixel_5')
        self.assertEqual(devices[1]['model'], 'Galaxy_S21')

    def test_header_only(self):
        self.assertEqual(parse_device_list("List of devices attached\n"), [])

    def test_unauthorized_and_offline(self):
        output = (
            "List of devices attached\n"
            "abc12345       unauthorized\n"
            "def67890       offline\n"
        )
        devices = parse_device_list(output)

        self.assertEqual([d['status'] for d in devices], ['unauthorized', 'offline'])

    def test_no_permissions_status(self):
        output = (
            "List of devices attached\n"
            "abc12345\tno permissions (missing udev rules; see "
            "[http://developer.android.com/tools/device.html])\n"
        )
        devices = parse_device_list(output)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]['status'], 'no permissions')

    def test_adb_server_chatter_is_not_a_device(self):
        # `adb devices` output is merged with stderr, so version-mismatch
        # warnings and daemon banners show up in the same text.
        output = (
            "List of devices attached\n"
            "abc12345       device model:Pixel_5\n"
            "adb server version (32) doesn't match this client (41); killing...\n"
            "* daemon started successfully *\n"
        )
        devices = parse_device_list(output)

        self.assertEqual([d['id'] for d in devices], ['abc12345'])

    def test_error_lines_are_rejected(self):
        output = "error: device offline\n"
        self.assertEqual(parse_device_list(output), [])


class TestTrackDevices(unittest.TestCase):
    """track_devices consumes the adb hex4-length framed stream."""

    def _adb_running(self, script_body):
        fd, path = tempfile.mkstemp(prefix='fake_adb_')
        os.close(fd)
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh\n' + script_body + '\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.addCleanup(os.unlink, path)
        adb = ADBWrapper()
        adb.adb_path = path
        return adb

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_framed_updates_are_reported(self):
        # two frames: 4-byte payload, then an empty one
        adb = self._adb_running("printf '0004test0000'")
        changes = []

        framed = adb.track_devices(lambda: changes.append(1))

        self.assertTrue(framed)
        self.assertEqual(len(changes), 2)

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_unframed_output_reports_unavailable(self):
        adb = self._adb_running("printf 'List of devices attached\\n'")
        changes = []

        framed = adb.track_devices(lambda: changes.append(1))

        self.assertFalse(framed)
        self.assertEqual(changes, [])

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_missing_binary_is_reported_unavailable(self):
        adb = ADBWrapper()
        adb.adb_path = '/nonexistent/adb-binary'

        self.assertFalse(adb.track_devices(lambda: None))

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_stop_tracking_kills_a_running_stream(self):
        adb = self._adb_running("exec sleep 60")
        result = []
        thread = threading.Thread(
            target=lambda: result.append(adb.track_devices(lambda: None)))
        thread.start()

        deadline = time.time() + 10
        while time.time() < deadline and adb._track_proc is None:
            time.sleep(0.05)
        self.assertIsNotNone(adb._track_proc, 'stream never started')

        adb.stop_tracking()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [False])
        self.assertIsNone(adb._track_proc)

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_stop_tracking_refuses_a_new_stream(self):
        """After shutdown a racing stream must die, not leak unregistered."""
        adb = self._adb_running("exec sleep 60")
        adb.stop_tracking()
        result = []
        thread = threading.Thread(
            target=lambda: result.append(adb.track_devices(lambda: None)))
        thread.start()
        thread.join(timeout=10)

        self.assertFalse(thread.is_alive(), 'child was never stopped')
        self.assertEqual(result, [False])
        self.assertIsNone(adb._track_proc)


class TestDeviceListRefresh(unittest.TestCase):
    """The device panel must not re-notify panels on an unchanged list."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

    def test_unchanged_device_notifies_once(self):
        from adb_gui import ADBGUI, ADBWrapper

        fake = [{'id': 'fake123', 'status': 'device', 'model': 'Pixel'}]
        originals = (ADBWrapper.get_devices, ADBWrapper.run_command)
        ADBWrapper.get_devices = lambda self: fake
        # Keep every other adb call instant so the queue drains quickly.
        ADBWrapper.run_command = lambda self, *args, **kwargs: ("", False)
        self.addCleanup(setattr, ADBWrapper, 'get_devices', originals[0])
        self.addCleanup(setattr, ADBWrapper, 'run_command', originals[1])

        app = ADBGUI()
        self.addCleanup(app.destroy)
        self.addCleanup(self._shutdown, app)

        seen = []
        original = app.device_panel.on_device_change

        def record(device_id):
            seen.append(device_id)
            original(device_id)

        # Installed before the event loop runs, so it catches the first
        # refresh submitted during construction.
        app.device_panel.on_device_change = record

        def pump(ticks=25):
            for _ in range(ticks):
                app.update()
                time.sleep(0.02)

        deadline = time.time() + 5
        while app.device_panel.get_current_device() != 'fake123':
            if time.time() > deadline:
                self.fail('device was never selected')
            pump(1)
        self.assertEqual(seen, ['fake123'])
        seen.clear()

        # A refresh that changes nothing must not notify the panels again.
        for _ in range(3):
            app.device_panel.refresh_devices()
            pump()
        self.assertEqual(seen, [])

        # Shell output written before further refreshes must survive them.
        app.shell_panel.append_output('keepme\n')
        app.device_panel.refresh_devices()
        pump()
        self.assertIn('keepme', app.shell_panel.output_text.get('1.0', 'end'))

    @staticmethod
    def _shutdown(app):
        """Stop tracking and cancel timers before the window goes away."""
        app._closing = True
        app.adb.stop_tracking()
        for ident in app.after_info():
            app.after_cancel(ident)


class TestTaskRunner(unittest.TestCase):
    """Jobs run on the worker; results come back on the Tk thread."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

    def _make_app(self):
        from adb_gui import ADBGUI

        app = ADBGUI()
        self.addCleanup(app.destroy)
        self.addCleanup(self._shutdown, app)
        return app

    @staticmethod
    def _shutdown(app):
        """Stop tracking and cancel timers before the window goes away."""
        app._closing = True
        app.adb.stop_tracking()
        for ident in app.after_info():
            app.after_cancel(ident)

    def test_result_is_delivered_on_the_main_thread(self):
        import threading

        app = self._make_app()
        seen = []
        app.runner.submit(lambda: 'value',
                          lambda result: seen.append(
                              (result, threading.current_thread().name)))

        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            app.update()
            time.sleep(0.01)

        self.assertEqual(seen, [('value', 'MainThread')])

    def test_polling_fallback_schedules_itself(self):
        app = self._make_app()

        pending_before = set(app.after_info())
        app._poll_devices()
        pending_after = set(app.after_info())

        # It queued a device refresh and armed the next poll.
        self.assertIn('Listing devices', app.runner._labels)
        self.assertTrue(pending_after - pending_before)
        app._closing = True


class TestIconHelpers(unittest.TestCase):
    """Icon decoding must never touch Tk and must reject adb's text answers."""

    @staticmethod
    def _png(width, height, color=(255, 0, 0, 255)):
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGBA', (width, height), color).save(buffer, format='PNG')
        return buffer.getvalue()

    def test_text_answer_is_not_an_image(self):
        from adb_gui import decode_icon

        self.assertIsNone(decode_icon(b'Unknown command: dump-icon'))
        self.assertIsNone(decode_icon(b''))
        self.assertIsNone(decode_icon(None))

    def test_pillow_downscales_to_the_requested_size(self):
        from adb_gui import HAS_PIL, PNG_SIGNATURE, decode_icon

        if not HAS_PIL:
            self.skipTest('Pillow not installed')

        import io
        from PIL import Image

        decoded = decode_icon(self._png(400, 400), max_size=64)

        self.assertTrue(decoded.startswith(PNG_SIGNATURE))
        self.assertLessEqual(max(Image.open(io.BytesIO(decoded)).size), 64)

    def test_without_pillow_the_bytes_are_passed_through(self):
        import adb_gui
        from adb_gui import decode_icon

        original = adb_gui.HAS_PIL
        adb_gui.HAS_PIL = False
        self.addCleanup(setattr, adb_gui, 'HAS_PIL', original)

        self.assertEqual(decode_icon(self._png(400, 400)), self._png(400, 400))

    def test_oversized_photo_is_shrunk_by_tk(self):
        import tkinter as tk
        from adb_gui import make_photo

        try:
            root = tk.Tk()
        except Exception:
            self.skipTest('no display available')
        self.addCleanup(root.destroy)

        photo = make_photo(self._png(400, 400), max_size=64)

        self.assertLessEqual(max(photo.width(), photo.height()), 64)


class TestGetAppIcon(unittest.TestCase):
    """get_app_icon must pass the package name, not the device id, to adb."""

    @staticmethod
    def _png():
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGBA', (8, 8)).save(buffer, format='PNG')
        return buffer.getvalue()

    @staticmethod
    def _recording_adb(response):
        adb = ADBWrapper()
        calls = []

        def run_command(*args, **kwargs):
            calls.append((args, kwargs))
            return response

        adb.run_command = run_command
        return adb, calls

    def test_package_and_device_reach_adb_in_order(self):
        adb, calls = self._recording_adb((self._png(), True))

        data, error = adb.get_app_icon('com.example.app', 'serial-1')

        self.assertIsNone(error)
        self.assertTrue(data.startswith(b'\x89PNG'))
        self.assertEqual(
            calls[0][0],
            ('-s', 'serial-1', 'shell', 'cmd', 'package', 'dump-icon',
             'com.example.app', '--png')
        )

    def test_text_error_from_device_is_reported(self):
        adb, _ = self._recording_adb((b'Unknown command: dump-icon', True))

        data, error = adb.get_app_icon('com.example.app', 'serial-1')

        self.assertIsNone(data)
        self.assertEqual(error, 'Unknown command: dump-icon')

    def test_failed_command_is_reported(self):
        adb, _ = self._recording_adb((b'', False))

        self.assertEqual(adb.get_app_icon('com.example.app'), (None, 'Could not retrieve icon'))

    def test_text_error_with_a_failure_exit_is_still_explained(self):
        adb, _ = self._recording_adb((b'Unknown command: dump-icon', False))

        self.assertEqual(adb.get_app_icon('com.example.app', 'serial-1'),
                         (None, 'Unknown command: dump-icon'))


class TestIconDisplay(unittest.TestCase):
    """Selecting a row loads its icon on the Tk thread and drops stale work."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

        # A job that raises pops a modal dialog; in a test that would hang.
        import types
        import adb_gui

        self.dialogs = []
        original = adb_gui.messagebox
        adb_gui.messagebox = types.SimpleNamespace(
            showerror=lambda *a, **k: self.dialogs.append(a),
            showinfo=lambda *a, **k: self.dialogs.append(a),
            showwarning=lambda *a, **k: self.dialogs.append(a),
            askyesno=lambda *a, **k: True,
        )
        self.addCleanup(setattr, adb_gui, 'messagebox', original)

    @staticmethod
    def _png(color):
        import io
        from PIL import Image

        buffer = io.BytesIO()
        Image.new('RGBA', (64, 64), color).save(buffer, format='PNG')
        return buffer.getvalue()

    def _panel_with_packages(self, packages, icons):
        """Build a standalone AppManagerPanel — no device tracking, no races."""
        import tkinter as tk
        from adb_gui import ADBWrapper, AppManagerPanel, TaskRunner

        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)

        adb = ADBWrapper()
        adb.get_app_icon = lambda pkg, device_id=None: icons.get(
            pkg, (None, 'no icon for this package'))

        panel = AppManagerPanel(root, adb, TaskRunner(root))
        self.addCleanup(panel.runner.close)
        panel.device_id = 'fake-device'
        panel.all_packages = list(packages)
        panel._filter_apps()
        return root, panel

    @staticmethod
    def _shown_image(panel):
        image = panel.icon_label.cget('image')
        if isinstance(image, (tuple, list)):
            image = image[0] if image else ''
        return image or ''

    @staticmethod
    def _select(panel, pkg):
        item = panel.app_tree.get_children()[0]
        for candidate in panel.app_tree.get_children():
            if panel.app_tree.item(candidate, 'values')[0] == pkg:
                item = candidate
        panel.app_tree.selection_set(item)
        panel._on_selection_change()

    @staticmethod
    def _pump_until(root, predicate, timeout=5):
        deadline = time.time() + timeout
        while not predicate():
            if time.time() > deadline:
                return False
            root.update()
            time.sleep(0.01)
        return True

    def test_selected_app_shows_and_caches_its_icon(self):
        green = self._png((0, 255, 0, 255))
        root, panel = self._panel_with_packages(
            ['com.example.green'], {'com.example.green': (green, None)})

        self._select(panel, 'com.example.green')

        self.assertTrue(self._pump_until(
            root, lambda: 'com.example.green' in panel.icon_cache))
        photo = panel.icon_cache['com.example.green']
        self.assertEqual(self._shown_image(panel), str(photo))
        self.assertEqual(panel.icon_label.cget('text'), '')
        self.assertEqual(self.dialogs, [])

    def test_a_newer_selection_discards_the_stale_icon(self):
        blue = self._png((0, 0, 255, 255))
        red = self._png((255, 0, 0, 255))
        root, panel = self._panel_with_packages(
            ['com.example.red', 'com.example.blue'],
            {'com.example.red': (red, None), 'com.example.blue': (blue, None)})

        self._select(panel, 'com.example.red')
        self._select(panel, 'com.example.blue')

        self.assertTrue(self._pump_until(
            root, lambda: 'com.example.blue' in panel.icon_cache))
        photo = panel.icon_cache['com.example.blue']
        self.assertEqual(self._shown_image(panel), str(photo))
        self.assertNotIn('com.example.red', panel.icon_cache)
        self.assertEqual(self.dialogs, [])

    def test_a_device_without_icons_says_why(self):
        root, panel = self._panel_with_packages(['com.example.plain'], {})
        panel.online_icons_var.set(False)

        self._select(panel, 'com.example.plain')

        self.assertTrue(self._pump_until(
            root, lambda: 'icon unavailable' in panel.detail_label.cget('text')))
        self.assertIn('no icon for this package', panel.detail_label.cget('text'))
        self.assertEqual(panel.icon_label.cget('text'), 'No icon')
        self.assertEqual(self.dialogs, [])

    def test_play_store_fallback_fills_in_when_adb_has_no_icon(self):
        from unittest import mock

        cyan = self._png((0, 200, 255, 255))
        root, panel = self._panel_with_packages(['com.example.web'], {})

        with mock.patch('adb_gui.fetch_play_icon', return_value=(cyan, None)) as lookup:
            self._select(panel, 'com.example.web')
            self.assertTrue(self._pump_until(
                root, lambda: 'com.example.web' in panel.icon_cache))

        lookup.assert_called_once_with('com.example.web')
        photo = panel.icon_cache['com.example.web']
        self.assertEqual(self._shown_image(panel), str(photo))
        self.assertEqual(self.dialogs, [])

    def test_the_online_lookup_can_be_switched_off(self):
        from unittest import mock

        root, panel = self._panel_with_packages(['com.example.web'], {})
        panel.online_icons_var.set(False)

        with mock.patch('adb_gui.fetch_play_icon') as lookup:
            self._select(panel, 'com.example.web')
            self.assertTrue(self._pump_until(
                root, lambda: 'icon unavailable' in panel.detail_label.cget('text')))

        lookup.assert_not_called()
        self.assertIn('no icon for this package', panel.detail_label.cget('text'))
        self.assertEqual(self.dialogs, [])


class TestSearchField(unittest.TestCase):
    """The search box filters the list through a Tcl 9 safe trace."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

    def test_building_the_panel_does_not_warn_about_trace(self):
        import warnings
        from adb_gui import ADBGUI

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            app = ADBGUI()
        self.addCleanup(self._shutdown, app)

        trace_warnings = [str(w.message) for w in caught if 'trace' in str(w.message).lower()]
        self.assertEqual(trace_warnings, [])

    def test_typing_filters_the_app_list(self):
        from adb_gui import ADBGUI

        app = ADBGUI()
        self.addCleanup(self._shutdown, app)
        panel = app.apps_panel
        panel.device_id = 'fake-device'
        panel.all_packages = ['com.example.alpha', 'com.example.beta']
        panel._filter_apps()
        self.assertEqual(len(panel.app_tree.get_children()), 2)

        panel.search_var.set('beta')
        self.assertEqual(
            [panel.app_tree.item(i, 'values')[0] for i in panel.app_tree.get_children()],
            ['com.example.beta']
        )

        panel.search_var.set('')
        self.assertEqual(len(panel.app_tree.get_children()), 2)

    @staticmethod
    def _shutdown(app):
        app._closing = True
        app.adb.stop_tracking()
        for ident in app.after_info():
            app.after_cancel(ident)


class _Response:
    """Stand-in for the object urllib.request.urlopen returns."""

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit=-1):
        return self.payload[:limit] if limit >= 0 else self.payload


class TestPlayIconLookup(unittest.TestCase):
    """Play Store fallback: parse the listing page, download the og:image."""

    PAGE = ('<html><head><meta property="og:image" '
            'content="https://icons.example/app.png"></head></html>')
    PAGE_REVERSED = ('<html><head><meta content="https://icons.example/app.png" '
                     'property="og:image"></head></html>')

    @staticmethod
    def _fake_urlopen(payload=b'', error=None):
        from unittest import mock

        def opener(request, timeout=None):
            if error is not None:
                raise error
            return _Response(payload)

        return mock.patch('adb_gui.urllib.request.urlopen', opener)

    def test_og_image_is_found_in_either_attribute_order(self):
        from adb_gui import parse_play_icon_url

        self.assertEqual(parse_play_icon_url(self.PAGE),
                         'https://icons.example/app.png')
        self.assertEqual(parse_play_icon_url(self.PAGE_REVERSED),
                         'https://icons.example/app.png')

    def test_a_page_without_og_image_yields_nothing(self):
        from adb_gui import parse_play_icon_url

        self.assertIsNone(parse_play_icon_url('<html><body>consent</body></html>'))

    def test_a_missing_listing_reports_that_the_app_is_not_published(self):
        import urllib.error
        from adb_gui import fetch_play_icon

        error = urllib.error.HTTPError('https://play.google.com/', 404,
                                       'Not Found', {}, None)
        with self._fake_urlopen(error=error):
            self.assertEqual(fetch_play_icon('com.example.absent'),
                             (None, 'not published on the Play Store'))

    def test_no_network_is_reported_not_raised(self):
        import urllib.error
        from adb_gui import fetch_play_icon

        with self._fake_urlopen(error=urllib.error.URLError('connection refused')):
            self.assertEqual(fetch_play_icon('com.example.app'),
                             (None, 'no network connection'))

    def test_a_listing_page_is_followed_to_its_icon(self):
        from unittest import mock
        from adb_gui import fetch_play_icon

        responses = [self.PAGE.encode(), b'PNG-bytes']

        def opener(request, timeout=None):
            return _Response(responses.pop(0))

        with mock.patch('adb_gui.urllib.request.urlopen', opener):
            self.assertEqual(fetch_play_icon('com.example.app'),
                             (b'PNG-bytes', None))
        self.assertEqual(responses, [])


class TestLogcatPanel(unittest.TestCase):
    """Level colours, the live filter, buffer trimming and child teardown."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

        from adb_gui import LogcatPanel, TaskRunner

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.runner = TaskRunner(self.root)
        self.addCleanup(self.runner.close)
        self.panel = LogcatPanel(self.root, ADBWrapper(), self.runner)
        self.addCleanup(self._cancel_filter)

    def _cancel_filter(self):
        if self.panel._filter_job:
            self.panel.after_cancel(self.panel._filter_job)
            self.panel._filter_job = None

    def _text(self):
        import tkinter as tk
        return self.panel.log_text.get('1.0', tk.END)

    @staticmethod
    def _process(calls, fail_on_terminate=False):
        class FakeProcess:
            def terminate(self):
                calls.append('terminate')
                if fail_on_terminate:
                    raise OSError('already dead')

            def kill(self):
                calls.append('kill')

            def wait(self, timeout=None):
                calls.append('wait')
                return 0

        return FakeProcess()

    def test_threadtime_lines_are_tagged(self):
        """The Android default format produced no colour at all."""
        line = '01-15 12:34:56.789  1234  1234 W ActivityManager: slow\n'
        self.panel.append_log(line)
        self.assertIn('W', self.panel.log_text.tag_names('1.0'))

    def test_brief_lines_are_tagged(self):
        self.panel.append_log('E/AndroidRuntime( 1234): boom\n')
        self.assertIn('E', self.panel.log_text.tag_names('1.0'))

    def test_unmatched_lines_get_no_tag(self):
        self.panel.append_log('     wrapped continuation\n')
        self.assertEqual(self.panel.log_text.tag_names('1.0'), ())

    def test_filter_keeps_matching_lines_only(self):
        self.panel.filter_var.set('boom')
        self._cancel_filter()
        self.panel.append_log('01-15 12:34:56.789  1234  1234 E Crash: boom\n')
        self.panel.append_log('01-15 12:34:56.790  1234  1234 I Other: quiet\n')
        content = self._text()
        self.assertIn('boom', content)
        self.assertNotIn('quiet', content)

    def test_filter_is_case_insensitive(self):
        self.panel.filter_var.set('CRASH')
        self._cancel_filter()
        self.panel.append_log('crash in layer\n')
        self.assertIn('crash in layer', self._text())

    def test_changing_the_filter_redraws_the_buffer(self):
        self.panel.append_log('one\n')
        self.panel.append_log('two\n')

        self.panel.filter_var.set('two')
        job = self.panel._filter_job
        self.assertIsNotNone(job, 'filter changes must schedule a re-render')
        self.panel.after_cancel(job)
        self.panel._filter_job = None
        self.panel._rerender()

        content = self._text()
        self.assertNotIn('one', content)
        self.assertIn('two', content)

    def test_the_buffer_is_capped(self):
        self.panel.MAX_LINES = 5
        for i in range(10):
            self.panel.append_log(f'line {i}\n')

        self.assertEqual(len(self.panel.lines), 5)
        # Overflow coalesces into one pending re-render instead of one per line.
        self.assertIsNotNone(self.panel._filter_job)
        self.panel._rerender()

        content = self._text()
        self.assertNotIn('line 0\n', content)
        self.assertIn('line 9', content)
        self.assertIn('line 5', content)

    def test_stop_logcat_terminates_and_forgets_the_child(self):
        calls = []
        self.panel.process = self._process(calls)
        self.panel.is_running = True

        self.panel.stop_logcat()

        self.assertFalse(self.panel.is_running)
        self.assertIsNone(self.panel.process)
        self.assertIn('terminate', calls)

    def test_close_kills_the_logcat_child(self):
        calls = []
        self.panel.process = self._process(calls)
        self.panel.is_running = True

        self.panel.close()

        self.assertFalse(self.panel.is_running)
        self.assertIsNone(self.panel.process)
        self.assertEqual(calls, ['terminate', 'wait'])

    def test_reap_falls_back_to_kill(self):
        from adb_gui import LogcatPanel

        calls = []
        LogcatPanel._reap(self._process(calls, fail_on_terminate=True))
        self.assertEqual(calls, ['terminate', 'kill', 'wait'])

    def test_close_cancels_a_pending_render(self):
        """Timers are cancelled by their owner, never by the root."""
        self.panel.filter_var.set('pending')
        self.assertIsNotNone(self.panel._filter_job)

        self.panel.close()

        self.assertIsNone(self.panel._filter_job)

    def test_clear_log_drops_the_buffer(self):
        self.panel.append_log('line\n')
        self.panel.clear_log()
        self.assertEqual(self.panel.lines, [])
        self.assertNotIn('line', self._text())


class TestShutdown(unittest.TestCase):
    """Closing the window must tear background work down first."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

    def test_title_bar_close_runs_the_teardown(self):
        import tkinter as tk
        from adb_gui import ADBGUI

        app = ADBGUI()
        # A timer owned by a child widget: cancelling it from the root would
        # delete the Tcl command while the panel still lists it, and the
        # window would survive the close.
        app.logcat_panel.filter_var.set('pending')
        self.assertIsNotNone(app.logcat_panel._filter_job)

        # Invoke exactly what a click on the title-bar close button runs.
        command = app.tk.call('wm', 'protocol', app._w, 'WM_DELETE_WINDOW')
        app.tk.call(command)

        self.assertTrue(app._torn_down)
        self.assertTrue(app._closing)
        self.assertIsNone(app.runner._drain_id)
        with self.assertRaises(tk.TclError):
            app.winfo_exists()

    def test_destroy_is_safe_to_call_twice(self):
        from adb_gui import ADBGUI

        app = ADBGUI()
        app.destroy()
        app.destroy()
        self.assertTrue(app._torn_down)


class TestFreezeHelpers(unittest.TestCase):
    """Freezing goes through `pm disable-user`, never through uninstall."""

    def _fake_adb(self, script_body):
        fd, path = tempfile.mkstemp(prefix='fake_adb_')
        os.close(fd)
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh\n' + script_body + '\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.addCleanup(os.unlink, path)
        adb = ADBWrapper()
        adb.adb_path = path
        return adb

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_freeze_disables_the_package(self):
        adb = self._fake_adb("printf '%s\\n' \"$@\"")

        output, success = adb.freeze_package('com.example.app', 'serial-1')

        self.assertTrue(success)
        self.assertEqual(output.splitlines(),
                         ['-s', 'serial-1', 'shell', 'pm', 'disable-user',
                          '--user', '0', 'com.example.app'])
        self.assertNotIn('uninstall', output)

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_unfreeze_re_enables_the_package(self):
        adb = self._fake_adb("printf '%s\\n' \"$@\"")

        output, success = adb.unfreeze_package('com.example.app', 'serial-1')

        self.assertTrue(success)
        self.assertEqual(output.splitlines(),
                         ['-s', 'serial-1', 'shell', 'pm', 'enable',
                          '--user', '0', 'com.example.app'])

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_frozen_packages_come_from_pm_list_d(self):
        adb = self._fake_adb(
            "printf 'package:com.zeta.app\\npackage:com.alpha.app\\n'")

        self.assertEqual(adb.list_frozen_packages('serial-1'),
                         ['com.alpha.app', 'com.zeta.app'])

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_a_failed_listing_yields_no_frozen_packages(self):
        adb = self._fake_adb("printf 'boom\\n'; exit 1")

        self.assertEqual(adb.list_frozen_packages('serial-1'), [])


class TestDisablePanel(unittest.TestCase):
    """Disabling is a reversible in-place action with visible state."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

        import types
        import adb_gui

        self.dialogs = []
        original = adb_gui.messagebox
        adb_gui.messagebox = types.SimpleNamespace(
            showerror=lambda *a, **k: self.dialogs.append(a),
            showinfo=lambda *a, **k: self.dialogs.append(a),
            showwarning=lambda *a, **k: self.dialogs.append(a),
            askyesno=lambda *a, **k: True,
        )
        self.addCleanup(setattr, adb_gui, 'messagebox', original)

        from adb_gui import AppManagerPanel, TaskRunner

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.runner = TaskRunner(self.root)
        self.addCleanup(self.runner.close)

        self.calls = []
        self.outcome = True
        adb = types.SimpleNamespace(
            freeze_package=self._freeze,
            unfreeze_package=self._unfreeze,
            get_app_icon=lambda pkg, device_id=None: (None, 'no icon'),
        )
        self.panel = AppManagerPanel(self.root, adb, self.runner)
        self.panel.online_icons_var.set(False)   # tests never hit the network
        self.panel.device_id = 'fake-device'
        self.panel.all_packages = ['com.example.live', 'com.example.frozen']
        self.panel.frozen_packages = {'com.example.frozen'}
        self.panel._filter_apps()

    def _freeze(self, pkg, device_id=None):
        self.calls.append(('freeze', pkg, device_id))
        return 'done', self.outcome

    def _unfreeze(self, pkg, device_id=None):
        self.calls.append(('unfreeze', pkg, device_id))
        return 'done', self.outcome

    def _pump(self, seconds=3.0):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def _row(self, pkg):
        return self.panel.app_tree.item(pkg)

    def _select(self, *pkgs):
        self.panel.app_tree.selection_set(list(pkgs))
        self.panel._on_selection_change()

    def _state(self, pkg):
        return self._row(pkg)['values'][1]

    def test_frozen_apps_are_marked_in_the_list(self):
        self.assertEqual(self._state('com.example.frozen'), 'Disabled')
        self.assertEqual(self._state('com.example.live'), '')
        self.assertIn('disabled', self._row('com.example.frozen')['tags'])
        self.assertNotIn('disabled', self._row('com.example.live')['tags'])

    def test_the_button_states_the_action_for_the_selection(self):
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Disable')
        self._select('com.example.live')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Disable')
        self._select('com.example.frozen')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Enable')

    def test_freezing_marks_the_app_and_keeps_the_selection(self):
        self._select('com.example.live')

        self.panel.freeze_selected()
        self._pump()

        self.assertEqual(self.calls,
                         [('freeze', 'com.example.live', 'fake-device')])
        self.assertIn('com.example.live', self.panel.frozen_packages)
        self.assertEqual(self._state('com.example.live'), 'Disabled')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Enable')
        self.assertEqual(list(self.panel.app_tree.selection()),
                         ['com.example.live'])
        self.assertEqual(self.dialogs, [])

    def test_unfreezing_takes_the_mark_away(self):
        self._select('com.example.frozen')

        self.panel.freeze_selected()
        self._pump()

        self.assertEqual(self.calls,
                         [('unfreeze', 'com.example.frozen', 'fake-device')])
        self.assertNotIn('com.example.frozen', self.panel.frozen_packages)
        self.assertEqual(self._state('com.example.frozen'), '')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Disable')

    def test_a_failed_freeze_is_not_recorded(self):
        self.outcome = False
        self._select('com.example.live')

        self.panel.freeze_selected()
        self._pump()

        self.assertNotIn('com.example.live', self.panel.frozen_packages)
        self.assertEqual(self._state('com.example.live'), '')
        self.assertTrue(self.dialogs, 'the failure must be reported')

    def test_freeze_needs_a_selection(self):
        self.panel.freeze_selected()

        self.assertEqual(self.calls, [])
        self.assertTrue(self.dialogs)

    # --- keyboard and context menu ------------------------------------
    def _key(self, state=0, keysym='d'):
        import types
        return types.SimpleNamespace(state=state, keysym=keysym)

    def test_the_shortcuts_are_bound_to_the_tree(self):
        for sequence in ('<Key-d>', '<Key-D>', '<Key-e>', '<Key-E>',
                         '<Button-3>'):
            self.assertTrue(self.panel.app_tree.bind(sequence),
                            f'{sequence} is not bound')

    def test_d_disables_the_whole_selection(self):
        self._select('com.example.live', 'com.example.frozen')

        self.panel._on_disable_key(self._key())
        self._pump()

        self.assertEqual(self.calls,
                         [('freeze', 'com.example.live', 'fake-device'),
                          ('freeze', 'com.example.frozen', 'fake-device')])
        self.assertEqual(self._state('com.example.live'), 'Disabled')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Enable')
        self.assertEqual(list(self.panel.app_tree.selection()),
                         ['com.example.live', 'com.example.frozen'])

    def test_e_enables_the_selection(self):
        self._select('com.example.frozen')

        self.panel._on_enable_key(self._key(keysym='E'))
        self._pump()

        self.assertEqual(self.calls,
                         [('unfreeze', 'com.example.frozen', 'fake-device')])
        self.assertEqual(self._state('com.example.frozen'), '')
        self.assertEqual(self.panel.freeze_btn.cget('text'), 'Disable')

    def test_modified_keys_belong_to_someone_else(self):
        self._select('com.example.live')

        for state in (0x4, 0x8, 0x40):        # Ctrl, Alt, Super
            self.panel._on_disable_key(self._key(state=state))
            self.panel._on_enable_key(self._key(state=state))
        self._pump()

        self.assertEqual(self.calls, [])
        self.assertNotIn('com.example.live', self.panel.frozen_packages)

    def test_a_key_press_with_nothing_selected_is_silent(self):
        self.panel._on_disable_key(self._key())

        self.assertEqual(self.calls, [])
        self.assertEqual(self.dialogs, [])

    def test_the_menu_lists_the_actions_with_their_keys(self):
        menu = self.panel.app_menu
        self.assertEqual(menu.entrycget(0, 'label'), 'Disable')
        self.assertEqual(menu.entrycget(0, 'accelerator'), 'D')
        self.assertEqual(menu.entrycget(1, 'label'), 'Enable')
        self.assertEqual(menu.entrycget(1, 'accelerator'), 'E')

    def test_the_menu_disables_the_selection_when_invoked(self):
        self._select('com.example.live')

        self.panel.app_menu.invoke(0)
        self._pump()

        self.assertEqual(self.calls,
                         [('freeze', 'com.example.live', 'fake-device')])
        self.assertEqual(self._state('com.example.live'), 'Disabled')

    def test_right_click_takes_over_the_row_under_the_cursor(self):
        import types
        self.panel.app_tree.identify_row = lambda y: 'com.example.live'
        posted = []
        self.panel._post_app_menu = lambda x, y: posted.append((x, y))
        self._select('com.example.frozen')

        self.panel._on_app_context(
            types.SimpleNamespace(y=10, x_root=30, y_root=40))

        self.assertEqual(list(self.panel.app_tree.selection()),
                         ['com.example.live'])
        self.assertEqual(posted, [(30, 40)])

    def test_right_click_on_empty_space_shows_no_menu(self):
        import types
        self.panel.app_tree.identify_row = lambda y: ''
        posted = []
        self.panel._post_app_menu = lambda x, y: posted.append((x, y))

        self.panel._on_app_context(
            types.SimpleNamespace(y=10, x_root=30, y_root=40))

        self.assertEqual(posted, [])


class TestFileListing(unittest.TestCase):
    """`ls` parsing: quoting, symlinks, failures and odd bytes."""

    def _adb(self, output, success=True):
        adb = ADBWrapper()
        self.calls = []

        def fake_run(*args, **kwargs):
            self.calls.append(args)
            return output, success

        adb.run_command = fake_run
        return adb

    def test_a_path_with_spaces_is_quoted_for_the_device_shell(self):
        adb = self._adb('total 0\n')

        files, error = adb.list_files('/sdcard/My Dir', 'serial-1')

        self.assertIsNone(error)
        self.assertEqual(files, [])
        # One shell string, quoted: `adb shell` joins argv with spaces, so
        # an unquoted path would be split by the device shell.
        self.assertEqual(self.calls,
                         [('-s', 'serial-1', 'shell',
                           "ls -la '/sdcard/My Dir/'")])

    def test_the_listing_path_ends_with_a_slash(self):
        """Trailing slash: `ls` then dereferences a symlinked directory
        such as /sdcard instead of describing the link itself."""
        adb = self._adb('total 0\n')

        adb.list_files('/sdcard', 'serial-1')

        self.assertEqual(self.calls,
                         [('-s', 'serial-1', 'shell', 'ls -la /sdcard/')])

    def test_a_symlink_target_is_not_part_of_the_name(self):
        adb = self._adb(
            'total 8\n'
            'lrwxrwxrwx 1 root root 21 2024-01-01 00:00 link -> /system/bin\n'
            'drwxrwx--x 2 root root 4096 2024-01-01 00:00 DCIM\n')

        files, error = adb.list_files('/sdcard')

        self.assertIsNone(error)
        self.assertEqual([f['name'] for f in files], ['link', 'DCIM'])
        self.assertFalse(files[0]['is_dir'])
        self.assertTrue(files[1]['is_dir'])

    def test_a_regular_file_may_contain_arrow_text(self):
        adb = self._adb(
            '-rw-r--r-- 1 root root 5 2024-01-01 00:00 a -> b\n')

        files, _ = adb.list_files('/sdcard')

        self.assertEqual(files[0]['name'], 'a -> b')

    def test_spaces_inside_a_name_survive_parsing(self):
        adb = self._adb(
            '-rw-r--r-- 1 root root 5 2024-01-01 00:00 my  photo.png\n')

        files, _ = adb.list_files('/sdcard')

        self.assertEqual(files[0]['name'], 'my  photo.png')

    def test_padded_columns_still_parse(self):
        """Real `ls -la` pads the size column with runs of spaces; those
        runs are one separator, and the name keeps its own spaces."""
        adb = self._adb(
            'total 8\n'
            '-rw-rw---- 1 root everybody    2 2026-09-30 22:45 '
            'hello world.txt\n'
            'drwxrwx--x 2 root everybody 4096 2026-09-30 22:45 sub dir\n')

        files, error = adb.list_files('/sdcard')

        self.assertIsNone(error)
        self.assertEqual([f['name'] for f in files],
                         ['hello world.txt', 'sub dir'])
        self.assertEqual(files[0]['size'], '2')

    def test_masked_fields_do_not_hide_or_corrupt_rows(self):
        """toybox masks every field it may not stat as `?`, and the date
        and time collapse into one column: the name follows six fields,
        not seven, and a masked symlink prints as `name -> ?`."""
        adb = self._adb(
            'total 88\n'
            'l?????????   ? ?      ?             ?                ? '
            'cache -> ?\n'
            'l?????????   ? ?      ?             ?                ? '
            'init -> ?\n'
            'd?????????   ? ?      ?             ?                ? '
            'data_mirror\n'
            '-?????????   ? ?      ?             ?                ? '
            'verity_key\n'
            'drwxr-xr-x  2 root   root       4096 2009-01-01 05:30 acct\n')

        files, error = adb.list_files('/')

        self.assertIsNone(error)
        names = [f['name'] for f in files]
        # The old parse shifted both masked symlink rows to `-> ?`: two
        # entries, one name, and the tree insert then raised TclError.
        self.assertEqual(names,
                         ['cache', 'init', 'data_mirror', 'verity_key',
                          'acct'])
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(files[0]['is_link'])
        self.assertTrue(files[2]['is_dir'])
        self.assertEqual(files[2]['size'], '?')
        self.assertEqual(files[4]['size'], '4096')

    def test_a_fully_masked_row_still_appears(self):
        adb = self._adb(
            '??????????   ? ?      ?             ?                ? '
            'mystery\n')

        files, _ = adb.list_files('/')

        self.assertEqual(files[0]['name'], 'mystery')
        self.assertEqual(files[0]['permissions'], '??????????')

    def test_permission_denied_is_reported_not_swallowed(self):
        adb = self._adb('ls: /data: Permission denied\n', success=False)

        files, error = adb.list_files('/data')

        self.assertEqual(files, [])
        self.assertEqual(error, 'ls: /data: Permission denied')

    def test_an_empty_directory_is_not_an_error(self):
        adb = self._adb('total 0\n')

        files, error = adb.list_files('/sdcard/empty')

        self.assertEqual(files, [])
        self.assertIsNone(error)

    @unittest.skipIf(sys.platform == 'win32', 'shell script helper')
    def test_non_utf8_names_do_not_abort_the_listing(self):
        fd, path = tempfile.mkstemp(prefix='fake_adb_')
        os.close(fd)
        with open(path, 'w') as handle:
            handle.write('#!/bin/sh\nprintf \'ok\\377def\\n\'\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        self.addCleanup(os.unlink, path)
        adb = ADBWrapper()
        adb.adb_path = path

        output, success = adb.run_command('devices')

        self.assertTrue(success)
        self.assertIn('ok', output)
        self.assertIn('def', output)


class TestFileManagerPanel(unittest.TestCase):
    """Navigation and identity in the file browser."""

    def setUp(self):
        import tkinter as tk
        try:
            tk.Tk().destroy()
        except Exception:
            self.skipTest('no display available')

        import types
        import adb_gui

        self.dialogs = []
        original_messagebox = adb_gui.messagebox
        adb_gui.messagebox = types.SimpleNamespace(
            showerror=lambda *a, **k: self.dialogs.append(a),
            showinfo=lambda *a, **k: self.dialogs.append(a),
            showwarning=lambda *a, **k: self.dialogs.append(a),
            askyesno=lambda *a, **k: True,
        )
        self.addCleanup(setattr, adb_gui, 'messagebox', original_messagebox)

        original_filedialog = adb_gui.filedialog
        adb_gui.filedialog = types.SimpleNamespace(
            asksaveasfilename=lambda **k: '/tmp/pulled.bin',
            askopenfilename=lambda **k: '',
        )
        self.addCleanup(setattr, adb_gui, 'filedialog', original_filedialog)

        from adb_gui import FileManagerPanel, TaskRunner

        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.runner = TaskRunner(self.root)
        self.addCleanup(self.runner.close)

        self.files = ([], None)
        self.pulled = []
        self.adb = types.SimpleNamespace(
            list_files=lambda path, device_id=None: self.files,
            pull_file=lambda *args: self.pulled.append(args) or ('done', True),
        )
        self.panel = FileManagerPanel(self.root, self.adb, self.runner)
        self.panel.device_id = 'fake-device'

    def _pump(self, seconds=2.0):
        end = time.time() + seconds
        while time.time() < end:
            self.root.update()
            time.sleep(0.02)

    def _show(self, entries, error=None):
        self.files = (entries, error)
        self.panel.refresh_files()
        self._pump()

    def _entry(self, name, is_dir=False):
        return {'name': name, 'is_dir': is_dir, 'size': '0',
                'permissions': 'd' if is_dir else '-'}

    def test_empty_input_leaves_the_path_alone(self):
        self.panel.navigate_to('')
        self._pump()

        self.assertEqual(self.panel.current_path, '/sdcard')
        self.assertEqual(self.panel.path_var.get(), '/sdcard')

        self.panel.navigate_to('   ')
        self._pump()

        self.assertEqual(self.panel.current_path, '/sdcard')

    def test_relative_and_parent_input_is_normalised(self):
        self.panel.navigate_to('relative')
        self._pump()
        self.assertEqual(self.panel.current_path, '/sdcard/relative')

        self.panel.navigate_to('/sdcard/../etc')
        self._pump()
        self.assertEqual(self.panel.current_path, '/etc')

    def test_go_up_never_leaves_the_panel_stuck(self):
        self.panel.current_path = ''

        self.panel.go_up()

        self.assertEqual(self.panel.current_path, '/')

        self.panel.go_up()
        self.assertEqual(self.panel.current_path, '/')

    def test_double_click_uses_the_raw_name(self):
        self._show([self._entry(' plain', is_dir=True),
                    self._entry('📁 icon dir', is_dir=True)])
        self.panel.file_tree.selection_set(' plain')
        self.panel.on_double_click(None)
        self._pump()
        self.assertEqual(self.panel.current_path, '/sdcard/ plain')

        self.panel.navigate_to('/sdcard')
        self._pump()
        self.panel.file_tree.selection_set('📁 icon dir')
        self.panel.on_double_click(None)
        self._pump()
        self.assertEqual(self.panel.current_path, '/sdcard/📁 icon dir')

    def test_pull_builds_the_remote_path_from_the_raw_name(self):
        self._show([self._entry('my file.txt')])

        self.panel.file_tree.selection_set('my file.txt')
        self.panel.pull_file()
        self._pump()

        self.assertEqual(self.pulled,
                         [('/sdcard/my file.txt', '/tmp/pulled.bin',
                           'fake-device')])

    def test_a_listing_failure_is_shown_and_cleared(self):
        self._show([], error='ls: /data: Permission denied')
        self.assertEqual(self.panel.error_label.cget('text'),
                         'ls: /data: Permission denied')

        self._show([self._entry('ok.txt')])
        self.assertEqual(self.panel.error_label.cget('text'), '')

    def test_double_click_enters_a_symlink(self):
        entry = self._entry('sdcard')
        entry['is_link'] = True
        self._show([entry])

        self.panel.file_tree.selection_set('sdcard')
        self.panel.on_double_click(None)
        self._pump()

        self.assertEqual(self.panel.current_path, '/sdcard/sdcard')

    def test_duplicate_rows_never_crash_the_tree(self):
        """A collapsed parse would hand the tree two rows with one iid;
        TclError must not kill the whole populate callback."""
        entry = self._entry('dup.txt')

        self.panel._populate([entry, dict(entry)])

        self.assertEqual(self.panel.file_tree.get_children(), ('dup.txt',))


if __name__ == '__main__':
    unittest.main()
