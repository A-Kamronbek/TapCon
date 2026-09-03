#!/usr/bin/env bash
#
# TapCon nightly backup.
#
#   /opt/tapcon/deploy/backup.sh
#
# Installed as a cron job on the VPS:
#
#   15 2 * * *  /opt/tapcon/deploy/backup.sh >> /var/log/tapcon-backup.log 2>&1
#
# WHAT A BACKUP OF THE DATABASE ALONE IS WORTH: nothing, for the part that
# matters most. Every seller's provider credentials are encrypted with
# FIELD_ENCRYPTION_KEY, which lives in .env and NOT in the database. Restore
# a dump onto a machine with a different key and every seller's Payme, Click,
# Uzum, Paynet and Octo keys are permanently unreadable — the site comes back
# up, the ledger is intact, and not one payment can be taken until every
# seller re-enters their credentials by hand.
#
# So this backs up .env too, and the restore drill in deploy/RESTORE.md is
# what proves the pair actually works. A backup nobody has restored is a
# hypothesis.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tapcon}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tapcon}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(date +%Y-%m-%d-%H%M)"

# Read DATABASE_URL out of .env without sourcing the whole file.
DATABASE_URL="$(grep -E '^DATABASE_URL=' "${APP_DIR}/.env" | cut -d= -f2- || true)"
if [ -z "${DATABASE_URL}" ]; then
  echo "FATAL: no DATABASE_URL in ${APP_DIR}/.env" >&2
  exit 1
fi

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"

DUMP="${BACKUP_DIR}/tapcon-${STAMP}.dump"
SECRETS="${BACKUP_DIR}/tapcon-env-${STAMP}.enc"

# --- the database ----------------------------------------------------------
# Custom format: compressed, and pg_restore can read it selectively.
pg_dump --format=custom --no-owner --no-privileges \
        --dbname="${DATABASE_URL}" --file="${DUMP}"

# A dump that cannot be read is not a backup. pg_restore --list parses the
# archive's table of contents and fails on a truncated or corrupt file, which
# is the failure a half-finished overnight dump actually produces.
pg_restore --list "${DUMP}" > /dev/null
chmod 600 "${DUMP}"

# --- the key that makes the dump usable ------------------------------------
# .env holds FIELD_ENCRYPTION_KEY, SECRET_KEY and the Eskiz login, so it is
# never written to disk unprotected. BACKUP_PASSPHRASE lives outside this
# machine — in your password manager, not in .env, or a single stolen server
# would give up both halves at once.
if [ -n "${BACKUP_PASSPHRASE:-}" ]; then
  openssl enc -aes-256-cbc -pbkdf2 -salt \
    -in "${APP_DIR}/.env" -out "${SECRETS}" -pass env:BACKUP_PASSPHRASE
  chmod 600 "${SECRETS}"
else
  echo "WARNING: BACKUP_PASSPHRASE is not set — .env was NOT backed up." >&2
  echo "         A database restore without it cannot decrypt any" >&2
  echo "         seller's provider credentials." >&2
fi

# --- offsite ---------------------------------------------------------------
# A backup on the same disk as the database survives a mistake, not a fire, a
# ransomware run, or the provider deleting the VM. Set BACKUP_REMOTE to an
# rclone remote (e.g. "b2:tapcon-backups") to send it somewhere else.
if [ -n "${BACKUP_REMOTE:-}" ]; then
  rclone copy "${DUMP}" "${BACKUP_REMOTE}/" --quiet
  [ -f "${SECRETS}" ] && rclone copy "${SECRETS}" "${BACKUP_REMOTE}/" --quiet
  rclone delete "${BACKUP_REMOTE}/" --min-age "${KEEP_DAYS}d" --quiet
else
  echo "WARNING: BACKUP_REMOTE is not set — this backup is only on this" >&2
  echo "         server, which is the machine most likely to be lost." >&2
fi

find "${BACKUP_DIR}" -name 'tapcon-*' -mtime "+${KEEP_DAYS}" -delete

echo "$(date -Is) ok  ${DUMP} ($(du -h "${DUMP}" | cut -f1))"
