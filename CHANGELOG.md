# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versioning: [SemVer](https://semver.org/).
The agent contract is versioned separately (`contract_version` "1").

## [0.1.0] — first public release

### Added
- **Roles and mail.** A team of AI agents as named roles (`deploy/roles.json`). Mailbox broker with delivery state,
  tasks, identity tokens and room scope; dispatcher wakes a role when it has mail, with backoff, digest,
  context refill and hot reload of the roster.
- **Drivers.** ACP (resident host, one session per room) for Hermes, Claude Code, Codex, pi and others;
  plus command, external, hook and manual drivers. The core never names a specific agent.
- **Web UI at `/app/`.** React + TypeScript, English and Traditional Chinese, light and dark themes.
  Rooms (threads, replies, likes, explicit read confirmation, file and image attachments, @mentions, task mode,
  search, folders, pin/archive/trash, freeze, reminders, auto-approve), Dashboard, Agents, Skills, Files.
- **Per-room sessions.** Each room keeps its own agent session; a role's agent and model can be switched for
  one room without touching the others.
- **Agents page.** Install ACP adapters, check sign-in, pick models from the agent's own list (test-run before
  saving), add roles with a persona, see and close per-room sessions.
- **Skills.** Edit with history, packs, assignment to roles and rooms; agents get an index of all skills.
- **Reliability.** Supervisor restarts crashed services; a stuck role (same mail failing three times) alerts
  `guardian`; unanswered must-reply mail alerts `guardian`; a resumed session rejected by its provider is
  replaced automatically.
- **CLI.** `bin/aaf install|init|up|down|status|doctor|account|transcript`; `bin/mbox`; `bin/aaf-chat`
  (`read`, `post`, `download`, `confirm-read`, …) for agents. `aaf init --example=team` installs five example roles.
- **Transcripts.** Rooms are exported to `<instance>/outputs/transcripts/` every 120 minutes.
- **Docs.** README (English, Chinese), product spec, Windows via WSL2 guide, native Windows plan, end-to-end test report.

### Verified
- macOS (Apple Silicon) and Linux arm64 (Debian 12 / Ubuntu 24.04, containers): `docs/verify/20261008-linux.md`. Linux amd64 not yet verified.
- End-to-end with real agents (Hermes and pi on a local model): see `docs/TEST-REPORT-e2e.md`.
