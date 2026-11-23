# Changelog

## Unreleased

- Added seven repeatable connection tests. Serial covers `loop://` echo, reconnect, and manual recovery after a simulated port-open failure. SSH covers password authentication, echo, first-use trust refusal, a failed trust save, reconnect with a saved host key, and rejection after key rotation. Telnet covers the cleartext confirmation, echo, and initial and updated window size negotiation (NAWS), including an escaped IAC byte.
- Added a GitHub Actions job for Windows Server 2025 x64 and CPython 3.14.10. The job uses commit-pinned actions and read-only repository permissions. It checks the existing dependency lock and reports test names and failures in the job log. Added local test instructions to the build guide.
- Installed the 15 hash-locked wheels in a fresh Windows 11 x64 virtual environment and passed `pip check`. All seven tests passed locally, including a run from a separate working directory. Suppressing a synthetic Serial echo produced a nonzero exit and identified the missing echo and connection status. The workflow passed static validation with `actionlint`.
- Validation used offscreen Qt, temporary settings, loopback, synthetic credentials, and ephemeral SSH host keys. Physical COM ports and drivers, external servers, and interactive display and DPI are outside the suite. Hosted results are available in the Windows tests workflow once it runs.

## v0.1.0

- Initial public source release under GPL-3.0-only.
