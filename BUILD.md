# Windows source setup

Pudding Terminal runs from `Pudding Terminal.py` on Windows 11 x64. The dependency lock targets CPython 3.14.10 x64 and contains hashes for the 15 Windows runtime wheels used by this project. Each dependency is installed separately by pip. The application source has no compilation or packaging step.

## Install the runtime

Create a fresh virtual environment with CPython 3.14.10 x64 as shown in [README.md](README.md). Use [requirements-win-x64.lock](requirements-win-x64.lock) with `--require-hashes --only-binary :all:`, then run `pip check`. [requirements.txt](requirements.txt) lists the same versions in a readable format.

For an offline installation, download the locked wheels on a compatible Windows x64 Python environment into an explicitly chosen directory. On the target host, pass `--no-index --find-links <absolute-wheel-directory>` alongside the lock and hash-checking options. Store those wheels outside the source repository.

Using the venv interpreter and the absolute path to the source file, check that the entry point compiles without executing it.

```powershell
$entry = Join-Path $project 'Pudding Terminal.py'
& $python -B -I -c 'import sys; from pathlib import Path; p = Path(sys.argv[1]); compile(p.read_bytes(), p.name, "exec")' $entry
& $python -B $entry
```

The first launch requires no connection settings. Use a writable source folder. Preferences and default receive logs are stored beside the entry point even when PowerShell is started from a different working directory. Initial configuration, generated logs, wheel archives and virtual environments are local to each installation. This source distribution ships no compiled application artifacts.

## Run the connection tests

With the locked dependencies installed and `$project` and `$python` set as shown in the README, run the suite from any PowerShell working directory.

```powershell
& $python -X utf8 -B -m unittest discover -s (Join-Path $project 'tests') -v
```

The suite runs Qt offscreen. It uses pyserial's `loop://`, local SSH and Telnet servers on port `0` so Windows assigns free ports, and a temporary preferences directory per test. SSH host keys are generated in memory and the login uses synthetic credentials. The tests require no physical COM port, external host, or extra package.

Verbose output names each test. A connection timeout reports the waiting step and the application's status and dialogs. The [Windows workflow](.github/workflows/windows-tests.yml) runs the suite after checking the locked dependencies.
