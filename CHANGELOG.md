# Changelog

## Unreleased

- Hardened long-running GUI stability: installed global Python and thread exception hooks (`sys.excepthook` / `threading.excepthook`), guarded Qt timer/paint/resize/native callbacks, bound single-shot timers to `QObject` owners, and added local `diagnostics/` logging (`Runtime_*.jsonl`, `Crash_*.txt`, and 60s resource heartbeats) with a bounded in-memory fallback when diagnostic files cannot be written.
- Decoupled receive logging from UI rendering: the connection worker now decodes and writes active `Session_*.txt` logs directly on the receive path while the UI drains bounded display chunks every 30 ms, and a background maintenance loop handles idle-time log rotation safely.
- Improved SSH/Telnet thread-safety by guarding event-loop publishing/teardown and coalescing rapid resize updates onto the worker loop.
- Added opt-in Serial auto-reconnect after an open port drops (disabled by default under Serial advanced settings; preserves active session logs across reconnects).
- Added automated offscreen stability tests covering the reconnect default, simulated Serial reconnect, idle log rotation, write-failure isolation, and Qt timer/diagnostics fallback.
- Added seven repeatable connection tests. Serial covers `loop://` echo, reconnect, and manual recovery after a simulated port-open failure. SSH covers password authentication, echo, first-use trust refusal, a failed trust save, reconnect with a saved host key, and rejection after key rotation. Telnet covers the cleartext confirmation, echo, and initial and updated window size negotiation (NAWS), including an escaped IAC byte.
- Added a GitHub Actions job for Windows Server 2025 x64 and CPython 3.14.10. The job uses commit-pinned actions and read-only repository permissions. It checks the existing dependency lock and reports test names and failures in the job log. Added local test instructions to the build guide.
- Installed the 15 hash-locked wheels in a fresh Windows 11 x64 virtual environment and passed `pip check`. All seven tests passed locally, including a run from a separate working directory. Suppressing a synthetic Serial echo produced a nonzero exit and identified the missing echo and connection status. The workflow passed static validation with `actionlint`.
- Validation used offscreen Qt, temporary settings, loopback, synthetic credentials, and ephemeral SSH host keys. Physical COM ports and drivers, external servers, and interactive display and DPI are outside the suite. Hosted results are available in the Windows tests workflow once it runs.

## v0.1.0

- Initial public source release under GPL-3.0-only.
