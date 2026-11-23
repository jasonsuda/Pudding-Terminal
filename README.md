# Pudding Terminal

Version v0.1.0 is available as a source-only release under [GPL-3.0-only](LICENSE).

Pudding Terminal combines serial consoles, SSH sessions, and Telnet in a Windows desktop app. It provides VT-style rendering, ANSI colors, scrollback, configurable keyboard and mouse behavior, light and dark themes, timestamps, and optional receive logging.

## Install and start

Use Windows 11 x64 and CPython 3.14.10 x64. Download or clone the source and extract it to a folder writable by your account. In PowerShell, change the two example paths below to the actual source and Python locations.

```powershell
$project = 'C:\Work\Pudding-Terminal'
$basePython = 'C:\Path\To\Python31410\python.exe'
$venv = Join-Path $project '.venv'
& $basePython -m venv $venv
$python = Join-Path $venv 'Scripts\python.exe'
& $python -m pip --isolated --disable-pip-version-check --no-cache-dir install --only-binary ':all:' --require-hashes -r (Join-Path $project 'requirements-win-x64.lock')
& $python -m pip --isolated --disable-pip-version-check check
& $python -B (Join-Path $project 'Pudding Terminal.py')
```

See [BUILD.md](BUILD.md) for the dependency pins and offline installation.

## Connect to a device or lab host

- **Serial.** Choose the COM port and match the device settings. A virtual pair might use COM4 at 9600 baud with 8 data bits, no parity, 1 stop bit, and no flow control. If a port is in use, close the program using it and try again.
- **SSH.** Enter an authorized endpoint such as `terminal.example.test`. Verify the displayed host-key fingerprint through a separate trusted channel before accepting it. If a saved key changes, verify the rotation with the host operator before updating stored trust.
- **Telnet.** Login information and session contents travel in cleartext. Use an authorized isolated network. The confirmation dialog defaults to Cancel.

The terminal opens disconnected. Start receive logging manually after signing in. Remote output can contain sensitive data, including echoed input. The timestamp switch affects new screen and log lines.

The application stores preferences in `config/pudding.ini` next to the source and uses `device_logs/` in the same folder by default. Protect those files and omit them from shared archives and issue reports. If you report a layout or connection problem, use synthetic examples and include the Windows version and Python version.

[Third-party notices](THIRD_PARTY_NOTICES.md)

## Run the connection tests

Use the same Windows x64 virtual environment as the app. Run the [test suite](BUILD.md#run-the-connection-tests) to check Serial, SSH, and Telnet with synthetic data and loopback peers. See the [changelog](CHANGELOG.md) for coverage and validation limits.
