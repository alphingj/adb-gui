# ADB GUI

A comprehensive graphical user interface for Android Debug Bridge (ADB) tools.

![Python](https://img.shields.io/badge/Python-3.6+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## Features

- **Device Management**: Automatically detect and connect to Android devices
- **Device Information**: View detailed device specifications (model, Android version, battery status, etc.)
- **App Manager**: Install, uninstall, and list applications on your device
- **File Manager**: Browse, push, and pull files between device and computer
- **Shell Terminal**: Execute shell commands directly on the device
- **Logcat Viewer**: Real-time log viewing with color-coded log levels
- **Tools**: Take screenshots, reboot to different modes (normal, recovery, bootloader)

## Screenshots

The application provides a tabbed interface with the following panels:

- **Device Info Tab**: Shows device model, manufacturer, Android version, battery status
- **Apps Tab**: List and manage installed applications
- **Files Tab**: Browse device file system, transfer files
- **Shell Tab**: Interactive shell command execution
- **Logcat Tab**: Real-time log viewer with filtering
- **Tools Tab**: Screenshot and reboot utilities

## Requirements

### System Requirements

- Python 3.6 or higher
- tkinter (usually included with Python)
- Android SDK Platform Tools (for the `adb` command)

### Installing Android SDK Platform Tools

Download from the official Android developer site:
https://developer.android.com/studio/releases/platform-tools

Or install via package manager:

**Linux (Ubuntu/Debian):**
```bash
sudo apt install android-sdk-platform-tools
```

**macOS (Homebrew):**
```bash
brew install android-platform-tools
```

**Windows:**
Download and extract the platform-tools, then add to system PATH.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/alphingj/adb-gui.git
cd adb-gui
```

2. Ensure Python 3.6+ is installed:
```bash
python3 --version
```

3. Run the application:
```bash
python3 adb_gui.py
```

## Usage

1. **Connect your Android device** via USB
2. **Enable USB debugging** on your device:
   - Go to Settings > Developer Options > USB Debugging
   - (If Developer Options is not visible, go to Settings > About Phone > tap Build Number 7 times)
3. **Run the application**: `python3 adb_gui.py`
4. **Select your device** from the dropdown at the top
5. **Use the tabs** to access different features

### Device Info Tab
View comprehensive device information including:
- Device model and manufacturer
- Android version and SDK level
- Battery level and charging status
- Hardware information

### Apps Tab
- View installed applications (filter by third-party or all)
- Install APK files from your computer
- Uninstall selected applications

### Files Tab
- Navigate the device file system
- Double-click folders to enter them
- Pull files from device to computer
- Push files from computer to device

### Shell Tab
- Execute any shell command on the device
- Command history navigation (up/down arrows)
- Clear output with the Clear button

### Logcat Tab
- Real-time log viewing
- Color-coded by log level (Verbose, Debug, Info, Warning, Error, Fatal)
- Start/Stop controls
- Filter logs by tag

### Tools Tab
- Take screenshots (saved as PNG)
- Reboot device (normal, recovery, or bootloader mode)

## Troubleshooting

### "ADB not found" error
Ensure Android SDK Platform Tools is installed and `adb` is in your system PATH.

### Device not detected
1. Ensure USB debugging is enabled
2. Try a different USB cable or port
3. Check if device is authorized (look for authorization dialog on device)
4. Run `adb devices` in terminal to verify ADB can see the device

### Permission denied on Linux
Add your user to the plugdev group:
```bash
sudo usermod -aG plugdev $USER
```

Then create udev rules for Android devices:
```bash
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="*", MODE="0666", GROUP="plugdev"' | sudo tee /etc/udev/rules.d/51-android.rules
sudo udevadm control --reload-rules
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is open source and available under the MIT License.

## Acknowledgments

- Android Debug Bridge (ADB) - Google
- Python tkinter - Python Software Foundation
# adb-gui
