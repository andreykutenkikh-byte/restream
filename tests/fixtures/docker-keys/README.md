# Docker public signing keys

These are public verification keys, not application credentials. Retrieved over verified HTTPS
on 2026-10-07 from Docker's official endpoints:

- `ubuntu.asc`: https://download.docker.com/linux/ubuntu/gpg
  — primary fingerprint `9DC858229FC7DD38854AE2D88D81803C0EBFCD88`.
- `rhel.asc`: https://download.docker.com/linux/rhel/gpg
  — primary fingerprint `060A61C51B558A7F742B77AAC52FEB6B621E9F35`.

The generated installer gate is exercised against the matching key and the wrong-family key.
Tests do not download keys or weaken the fixed fingerprint check.
