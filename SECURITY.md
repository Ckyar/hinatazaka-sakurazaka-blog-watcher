# Security

- Never commit `.env`, Discord bot tokens, GitHub tokens, SQLite state, logs, or downloaded images.
- If a Discord token is exposed, reset it immediately in the Discord Developer Portal and update `.env` on every host.
- Keep `.env` readable only by the service user (`chmod 600 .env`).
- Use GitHub browser/device authentication, Git Credential Manager, or SSH keys. Do not paste credentials into issues, commits, or chat.
