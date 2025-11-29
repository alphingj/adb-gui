#!/usr/bin/env python3
"""
ADB GUI - A graphical user interface for Android Debug Bridge (ADB)
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import subprocess
import threading
import os
import re
from datetime import datetime


class ADBWrapper:
    """Wrapper class for ADB commands"""
    
    def __init__(self):
        self.adb_path = self._find_adb()
    
    def _find_adb(self):
        """Find ADB executable in PATH or common locations"""
        # Check if adb is in PATH
        try:
            result = subprocess.run(['which', 'adb'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception:
            pass
        
        # Check common locations
        common_paths = [
            '/usr/bin/adb',
            '/usr/local/bin/adb',
            os.path.expanduser('~/Android/Sdk/platform-tools/adb'),
            os.path.expanduser('~/Library/Android/sdk/platform-tools/adb'),
            os.path.expandvars('C:\\Users\\%USERNAME%\\AppData\\Local\\Android\\Sdk\\platform-tools\\adb.exe'),
        ]
        
        for path in common_paths:
            if os.path.exists(path):
                return path
        
        return 'adb'  # Fallback to default
    
    def run_command(self, *args, timeout=30):
        """Run an ADB command and return the output"""
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
        lines = output.strip().split('\n')[1:]  # Skip header
        for line in lines:
            if line.strip():
                parts = line.split()
                if len(parts) >= 2:
                    device_id = parts[0]
                    status = parts[1]
                    # Extract model name if available
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
    
    def get_device_info(self, device_id=None):
        """Get detailed device information"""
        prefix = ['-s', device_id] if device_id else []
        
        info = {}
        
        # Get various properties
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
        
        # Get battery info
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
    
    def install_apk(self, apk_path, device_id=None):
        """Install an APK file"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'install', '-r', apk_path, timeout=120)
    
    def uninstall_package(self, package_name, device_id=None):
        """Uninstall a package"""
        prefix = ['-s', device_id] if device_id else []
        return self.run_command(*prefix, 'uninstall', package_name)
    
    def list_files(self, path, device_id=None):
        """List files in a directory on the device"""
        prefix = ['-s', device_id] if device_id else []
        output, success = self.run_command(*prefix, 'shell', 'ls', '-la', path)
        if not success:
            return []
        
        files = []
        for line in output.strip().split('\n'):
            if line and not line.startswith('total'):
                parts = line.split()
                if len(parts) >= 8:
                    # Parse ls -la output
                    permissions = parts[0]
                    size = parts[4] if len(parts) > 4 else ''
                    name = ' '.join(parts[7:]) if len(parts) > 7 else ''
                    if name and name not in ['.', '..']:
                        is_dir = permissions.startswith('d')
                        files.append({
                            'name': name,
                            'is_dir': is_dir,
                            'size': size,
                            'permissions': permissions
                        })
        return files
    
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
        # Take screenshot on device
        output, success = self.run_command(*prefix, 'shell', 'screencap', '-p', '/sdcard/screenshot.png')
        if not success:
            return output, False
        # Pull to local
        output, success = self.run_command(*prefix, 'pull', '/sdcard/screenshot.png', save_path)
        # Clean up
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


class DevicePanel(ttk.LabelFrame):
    """Panel for device selection and basic info"""
    
    def __init__(self, parent, adb, on_device_change=None):
        super().__init__(parent, text="Device", padding=10)
        self.adb = adb
        self.on_device_change = on_device_change
        self.current_device = None
        
        # Device dropdown
        ttk.Label(self, text="Select Device:").grid(row=0, column=0, sticky='w')
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(self, textvariable=self.device_var, width=40, state='readonly')
        self.device_combo.grid(row=0, column=1, padx=5, sticky='ew')
        self.device_combo.bind('<<ComboboxSelected>>', self._on_device_selected)
        
        # Refresh button
        ttk.Button(self, text="↻ Refresh", command=self.refresh_devices).grid(row=0, column=2, padx=5)
        
        # Status
        self.status_label = ttk.Label(self, text="No device connected", foreground='gray')
        self.status_label.grid(row=1, column=0, columnspan=3, sticky='w', pady=(5, 0))
        
        self.columnconfigure(1, weight=1)
        
        # Initial refresh
        self.refresh_devices()
    
    def refresh_devices(self):
        """Refresh the device list"""
        devices = self.adb.get_devices()
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
            if self.on_device_change:
                self.on_device_change(None)
    
    def _on_device_selected(self, event):
        """Handle device selection"""
        selected = self.device_var.get()
        if selected in self.devices:
            device = self.devices[selected]
            self.current_device = device['id']
            status = device['status']
            color = 'green' if status == 'device' else 'orange'
            self.status_label.config(text=f"Status: {status}", foreground=color)
            if self.on_device_change:
                self.on_device_change(self.current_device)
    
    def get_current_device(self):
        """Get the currently selected device ID"""
        return self.current_device


class DeviceInfoPanel(ttk.LabelFrame):
    """Panel for displaying device information"""
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="Device Information", padding=10)
        self.adb = adb
        self.device_id = None
        
        self.info_text = scrolledtext.ScrolledText(self, height=12, width=50, state='disabled')
        self.info_text.pack(fill='both', expand=True)
        
        ttk.Button(self, text="Refresh Info", command=self.refresh_info).pack(pady=(5, 0))
    
    def set_device(self, device_id):
        """Set the current device"""
        self.device_id = device_id
        self.refresh_info()
    
    def refresh_info(self):
        """Refresh device information"""
        self.info_text.config(state='normal')
        self.info_text.delete('1.0', tk.END)
        
        if not self.device_id:
            self.info_text.insert(tk.END, "No device selected")
        else:
            self.info_text.insert(tk.END, "Loading device information...\n")
            self.update()
            
            info = self.adb.get_device_info(self.device_id)
            self.info_text.delete('1.0', tk.END)
            
            for key, value in info.items():
                self.info_text.insert(tk.END, f"{key}: {value}\n")
        
        self.info_text.config(state='disabled')


class AppManagerPanel(ttk.LabelFrame):
    """Panel for app management"""
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="App Manager", padding=10)
        self.adb = adb
        self.device_id = None
        
        # Controls frame
        controls = ttk.Frame(self)
        controls.pack(fill='x', pady=(0, 5))
        
        self.third_party_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(controls, text="Third-party apps only", variable=self.third_party_var).pack(side='left')
        ttk.Button(controls, text="Refresh", command=self.refresh_apps).pack(side='left', padx=5)
        ttk.Button(controls, text="Install APK", command=self.install_apk).pack(side='left', padx=5)
        ttk.Button(controls, text="Uninstall Selected", command=self.uninstall_selected).pack(side='left', padx=5)
        
        # App list
        list_frame = ttk.Frame(self)
        list_frame.pack(fill='both', expand=True)
        
        self.app_listbox = tk.Listbox(list_frame, selectmode='extended')
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=self.app_listbox.yview)
        self.app_listbox.config(yscrollcommand=scrollbar.set)
        
        self.app_listbox.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
    
    def set_device(self, device_id):
        """Set the current device"""
        self.device_id = device_id
        self.refresh_apps()
    
    def refresh_apps(self):
        """Refresh the app list"""
        self.app_listbox.delete(0, tk.END)
        
        if not self.device_id:
            return
        
        packages = self.adb.list_packages(self.device_id, self.third_party_var.get())
        for pkg in packages:
            self.app_listbox.insert(tk.END, pkg)
    
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
            output, success = self.adb.install_apk(apk_path, self.device_id)
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
        
        selected = self.app_listbox.curselection()
        if not selected:
            messagebox.showwarning("Warning", "No app selected")
            return
        
        packages = [self.app_listbox.get(i) for i in selected]
        if messagebox.askyesno("Confirm", f"Uninstall {len(packages)} app(s)?"):
            for pkg in packages:
                output, success = self.adb.uninstall_package(pkg, self.device_id)
                if not success:
                    messagebox.showerror("Error", f"Failed to uninstall {pkg}:\n{output}")
            self.refresh_apps()


class FileManagerPanel(ttk.LabelFrame):
    """Panel for file management"""
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="File Manager", padding=10)
        self.adb = adb
        self.device_id = None
        self.current_path = '/sdcard'
        
        # Path bar
        path_frame = ttk.Frame(self)
        path_frame.pack(fill='x', pady=(0, 5))
        
        ttk.Label(path_frame, text="Path:").pack(side='left')
        self.path_var = tk.StringVar(value=self.current_path)
        self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.pack(side='left', fill='x', expand=True, padx=5)
        self.path_entry.bind('<Return>', lambda e: self.navigate_to(self.path_var.get()))
        
        ttk.Button(path_frame, text="Go", command=lambda: self.navigate_to(self.path_var.get())).pack(side='left')
        ttk.Button(path_frame, text="↑ Up", command=self.go_up).pack(side='left', padx=5)
        
        # File list
        list_frame = ttk.Frame(self)
        list_frame.pack(fill='both', expand=True)
        
        # Create treeview with columns
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
        
        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill='x', pady=(5, 0))
        
        ttk.Button(btn_frame, text="Refresh", command=self.refresh_files).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Pull File", command=self.pull_file).pack(side='left', padx=2)
        ttk.Button(btn_frame, text="Push File", command=self.push_file).pack(side='left', padx=2)
    
    def set_device(self, device_id):
        """Set the current device"""
        self.device_id = device_id
        self.current_path = '/sdcard'
        self.path_var.set(self.current_path)
        self.refresh_files()
    
    def refresh_files(self):
        """Refresh the file list"""
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        
        if not self.device_id:
            return
        
        files = self.adb.list_files(self.current_path, self.device_id)
        for f in files:
            icon = '📁 ' if f['is_dir'] else '📄 '
            self.file_tree.insert('', tk.END, values=(
                icon + f['name'],
                f['size'],
                f['permissions']
            ), tags=('dir' if f['is_dir'] else 'file',))
    
    def navigate_to(self, path):
        """Navigate to a specific path"""
        self.current_path = path
        self.path_var.set(path)
        self.refresh_files()
    
    def go_up(self):
        """Go to parent directory"""
        parent = os.path.dirname(self.current_path.rstrip('/'))
        if parent:
            self.navigate_to(parent)
    
    def on_double_click(self, event):
        """Handle double-click on item"""
        selection = self.file_tree.selection()
        if selection:
            item = self.file_tree.item(selection[0])
            tags = item['tags']
            name = item['values'][0].lstrip('📁📄 ')
            
            if 'dir' in tags:
                new_path = os.path.join(self.current_path, name)
                self.navigate_to(new_path)
    
    def pull_file(self):
        """Pull selected file from device"""
        if not self.device_id:
            messagebox.showerror("Error", "No device selected")
            return
        
        selection = self.file_tree.selection()
        if not selection:
            messagebox.showwarning("Warning", "No file selected")
            return
        
        item = self.file_tree.item(selection[0])
        name = item['values'][0].lstrip('📁📄 ')
        remote_path = os.path.join(self.current_path, name)
        
        local_path = filedialog.asksaveasfilename(
            title="Save file as",
            initialfile=name
        )
        
        if local_path:
            output, success = self.adb.pull_file(remote_path, local_path, self.device_id)
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
            remote_path = os.path.join(self.current_path, filename)
            
            output, success = self.adb.push_file(local_path, remote_path, self.device_id)
            if success:
                messagebox.showinfo("Success", f"File pushed to {remote_path}")
                self.refresh_files()
            else:
                messagebox.showerror("Error", f"Push failed:\n{output}")


class ShellPanel(ttk.LabelFrame):
    """Panel for shell command execution"""
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="Shell", padding=10)
        self.adb = adb
        self.device_id = None
        self.command_history = []
        self.history_index = -1
        
        # Output area
        self.output_text = scrolledtext.ScrolledText(self, height=15, wrap=tk.WORD)
        self.output_text.pack(fill='both', expand=True)
        self.output_text.config(state='disabled')
        
        # Command input
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
        
        # Add to history
        self.command_history.append(command)
        self.history_index = len(self.command_history)
        
        self.append_output(f"$ {command}\n")
        self.cmd_var.set('')
        
        # Run command in thread to avoid blocking UI
        def run():
            result, success = self.adb.run_shell_command(command, self.device_id)
            # Capture result in closure to avoid race condition
            self.after(0, lambda result=result: self.append_output(result + "\n"))
        
        threading.Thread(target=run, daemon=True).start()
    
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
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="Tools", padding=10)
        self.adb = adb
        self.device_id = None
        
        # Screenshot button
        ttk.Button(self, text="📷 Take Screenshot", command=self.take_screenshot).pack(fill='x', pady=2)
        
        # Reboot options
        ttk.Separator(self, orient='horizontal').pack(fill='x', pady=5)
        ttk.Label(self, text="Reboot Options:").pack(anchor='w')
        
        reboot_frame = ttk.Frame(self)
        reboot_frame.pack(fill='x')
        
        ttk.Button(reboot_frame, text="🔄 Reboot", command=lambda: self.reboot('')).pack(side='left', padx=2)
        ttk.Button(reboot_frame, text="⚙️ Recovery", command=lambda: self.reboot('recovery')).pack(side='left', padx=2)
        ttk.Button(reboot_frame, text="🔧 Bootloader", command=lambda: self.reboot('bootloader')).pack(side='left', padx=2)
    
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
            output, success = self.adb.take_screenshot(save_path, self.device_id)
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
        if messagebox.askyesno("Confirm", f"Reboot device to {mode_name} mode?"):
            output, success = self.adb.reboot(mode, self.device_id)
            if not success:
                messagebox.showerror("Error", f"Reboot failed:\n{output}")


class LogcatPanel(ttk.LabelFrame):
    """Panel for viewing logcat"""
    
    def __init__(self, parent, adb):
        super().__init__(parent, text="Logcat", padding=10)
        self.adb = adb
        self.device_id = None
        self.is_running = False
        self.process = None
        
        # Controls
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
        
        # Log output
        self.log_text = scrolledtext.ScrolledText(self, height=15, wrap=tk.WORD)
        self.log_text.pack(fill='both', expand=True)
        self.log_text.config(state='disabled')
        
        # Configure tags for different log levels
        self.log_text.tag_config('V', foreground='gray')
        self.log_text.tag_config('D', foreground='blue')
        self.log_text.tag_config('I', foreground='green')
        self.log_text.tag_config('W', foreground='orange')
        self.log_text.tag_config('E', foreground='red')
        self.log_text.tag_config('F', foreground='red', background='yellow')
    
    def set_device(self, device_id):
        """Set the current device"""
        self.stop_logcat()
        self.device_id = device_id
        self.clear_log()
    
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
        
        def read_logcat():
            cmd = [self.adb.adb_path, '-s', self.device_id, 'logcat']
            filter_text = self.filter_var.get()
            if filter_text:
                cmd.extend(['-s', filter_text])
            
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                
                while self.is_running and self.process.poll() is None:
                    line = self.process.stdout.readline()
                    if line:
                        # Capture line value explicitly to avoid closure issues
                        self.after(0, lambda l=line: self.append_log(l))
            except Exception as e:
                # Capture error message explicitly
                error_msg = f"Error: {e}\n"
                self.after(0, lambda msg=error_msg: self.append_log(msg))
            finally:
                self.after(0, self._on_logcat_stopped)
        
        threading.Thread(target=read_logcat, daemon=True).start()
    
    def stop_logcat(self):
        """Stop logcat capture"""
        self.is_running = False
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
            self.process = None
    
    def _on_logcat_stopped(self):
        """Handle logcat stopped"""
        self.start_btn.config(state='normal')
        self.stop_btn.config(state='disabled')
    
    def append_log(self, text):
        """Append text to log with color coding"""
        self.log_text.config(state='normal')
        
        # Determine log level for coloring
        tag = None
        if len(text) > 0:
            # Standard logcat format starts with letter indicating level
            match = re.match(r'^([VDIWEF])/', text)
            if match:
                tag = match.group(1)
        
        self.log_text.insert(tk.END, text, tag)
        self.log_text.see(tk.END)
        self.log_text.config(state='disabled')
    
    def clear_log(self):
        """Clear the log"""
        self.log_text.config(state='normal')
        self.log_text.delete('1.0', tk.END)
        self.log_text.config(state='disabled')


class ADBGUI(tk.Tk):
    """Main application window"""
    
    def __init__(self):
        super().__init__()
        
        self.title("ADB GUI - Android Debug Bridge Interface")
        self.geometry("1000x700")
        
        # Initialize ADB wrapper
        self.adb = ADBWrapper()
        
        # Create main container
        main_container = ttk.Frame(self, padding=10)
        main_container.pack(fill='both', expand=True)
        
        # Device panel at top
        self.device_panel = DevicePanel(main_container, self.adb, self.on_device_change)
        self.device_panel.pack(fill='x', pady=(0, 10))
        
        # Create notebook for tabs
        self.notebook = ttk.Notebook(main_container)
        self.notebook.pack(fill='both', expand=True)
        
        # Create tabs
        self.create_info_tab()
        self.create_apps_tab()
        self.create_files_tab()
        self.create_shell_tab()
        self.create_logcat_tab()
        self.create_tools_tab()
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(main_container, textvariable=self.status_var, relief='sunken', anchor='w')
        status_bar.pack(fill='x', pady=(10, 0))
        
        # Set up periodic device refresh
        self.after(5000, self.periodic_refresh)
    
    def create_info_tab(self):
        """Create device info tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Device Info")
        
        self.info_panel = DeviceInfoPanel(frame, self.adb)
        self.info_panel.pack(fill='both', expand=True)
    
    def create_apps_tab(self):
        """Create app manager tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Apps")
        
        self.apps_panel = AppManagerPanel(frame, self.adb)
        self.apps_panel.pack(fill='both', expand=True)
    
    def create_files_tab(self):
        """Create file manager tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Files")
        
        self.files_panel = FileManagerPanel(frame, self.adb)
        self.files_panel.pack(fill='both', expand=True)
    
    def create_shell_tab(self):
        """Create shell tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Shell")
        
        self.shell_panel = ShellPanel(frame, self.adb)
        self.shell_panel.pack(fill='both', expand=True)
    
    def create_logcat_tab(self):
        """Create logcat tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Logcat")
        
        self.logcat_panel = LogcatPanel(frame, self.adb)
        self.logcat_panel.pack(fill='both', expand=True)
    
    def create_tools_tab(self):
        """Create tools tab"""
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="Tools")
        
        self.tools_panel = ToolsPanel(frame, self.adb)
        self.tools_panel.pack(fill='both', expand=True)
    
    def on_device_change(self, device_id):
        """Handle device selection change"""
        # Update all panels with the new device
        self.info_panel.set_device(device_id)
        self.apps_panel.set_device(device_id)
        self.files_panel.set_device(device_id)
        self.shell_panel.set_device(device_id)
        self.logcat_panel.set_device(device_id)
        self.tools_panel.set_device(device_id)
        
        if device_id:
            self.status_var.set(f"Connected to: {device_id}")
        else:
            self.status_var.set("No device connected")
    
    def periodic_refresh(self):
        """Periodically refresh device list"""
        self.device_panel.refresh_devices()
        self.after(5000, self.periodic_refresh)


def main():
    """Main entry point"""
    app = ADBGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
