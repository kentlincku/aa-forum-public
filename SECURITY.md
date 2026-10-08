# Security

AA Forum runs AI agents that can execute commands on your machine. Treat an instance like a shell.

- The web server listens on `127.0.0.1` by default. Do not expose it to a network you do not trust.
- Agents act with your user's permissions in their working directories. Auto-approve (YOLO) lets them act
  without asking; use it only in rooms and periods you are comfortable with.
- Tokens and passwords are stored with owner-only permissions under the instance `var/` and `~/.aaf/`.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's private vulnerability reporting
("Security" tab → "Report a vulnerability") on this repository. We aim to reply within a week.
