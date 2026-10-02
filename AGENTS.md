# project-hub

Use Python 3.11. For a fresh Linux/cloud checkout, run `bash scripts/cloud-setup.sh`,
then `bash scripts/check.sh`. Tests use temporary fixtures; production credentials
are not required. Full OCR integration also needs Tesseract (`kor`, `eng`) and
Poppler; tests report skips if these are unavailable. Never claim skipped OCR passed.

This repository is public. Never commit .env, credentials.json, token.json, private
keys, service-account JSON, collected files, logs, SQLite databases, or backups.
Do not copy Pi secrets into a cloud environment. Stage explicit reviewed paths.

Production is on Raspberry Pi at /home/khb/project-hub. The local PC is not needed
to run its bot or web library. Cloud tasks cannot directly reach the Pi LAN address.
Do not assume a commit or a push deployed the change. Use the phone deployment
guide in deploy/raspberry-pi/PHONE.md; deployment requires the user's explicit
choice of commit. Never put an administrative deployment endpoint on the public
/library route or weaken code-server's existing Authelia/OTP authentication.

Keep Discord collection, source files, and AI organization separate. Preserve
source citations, Discord channel permissions, AI quotas, HTML sandbox isolation,
and database compatibility. Coordinate with any other active work before editing
or deploying shared files. Database migrations and dependency changes require a
specific deployment plan; the phone deploy tool only replaces application code.
