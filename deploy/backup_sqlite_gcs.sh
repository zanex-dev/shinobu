#!/usr/bin/env bash
# Shinobu SQLite + Soul Online Hot-Backup to Google Cloud Storage (GCS)
# Pure white-hat script — configure via /etc/shinobu/backup.env or environment variables.
#
# Required env:
#   GCS_BUCKET            Target bucket name without gs:// (e.g. my-shinobu-backups)
# Optional env:
#   SHINOBU_HOME          Runtime home directory (default: /var/lib/shinobu/home)
#   BACKUP_STAGE_DIR      Local temporary staging directory (default: /var/lib/shinobu/backups)
#   BACKUP_KEEP_LOCAL     Number of recent local archives to retain (default: 7)
#   GCS_PREFIX            Object prefix inside bucket (default: shinobu-cloud)
#
# Example cron (daily at 04:15 UTC):
#   15 4 * * * shinobu GCS_BUCKET=<YOUR_GCS_BUCKET> /opt/shinobu/deploy/backup_sqlite_gcs.sh

set -euo pipefail

SHINOBU_HOME="${SHINOBU_HOME:-/var/lib/shinobu/home}"
BACKUP_STAGE_DIR="${BACKUP_STAGE_DIR:-/var/lib/shinobu/backups}"
BACKUP_KEEP_LOCAL="${BACKUP_KEEP_LOCAL:-7}"
GCS_PREFIX="${GCS_PREFIX:-shinobu-cloud}"
GCS_BUCKET="${GCS_BUCKET:-}"

if [[ -z "${GCS_BUCKET}" ]]; then
  echo "[backup] ERROR: GCS_BUCKET is not set (example: export GCS_BUCKET=my-shinobu-backups)." >&2
  exit 2
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "[backup] ERROR: sqlite3 CLI is required for consistent WAL online snapshots." >&2
  exit 127
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "[backup] ERROR: gcloud CLI is required to upload snapshots to GCS." >&2
  exit 127
fi

DB_PATH="${SHINOBU_HOME}/memory/shinobu.db"
SOUL_DIR="${SHINOBU_HOME}/soul"

if [[ ! -f "${DB_PATH}" ]]; then
  echo "[backup] ERROR: SQLite database not found at ${DB_PATH}" >&2
  exit 1
fi

mkdir -p "${BACKUP_STAGE_DIR}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORKDIR="$(mktemp -d "${BACKUP_STAGE_DIR}/.stage_${TIMESTAMP}_XXXXXX")"
ARCHIVE_NAME="shinobu_backup_${TIMESTAMP}.tar.gz"
ARCHIVE_PATH="${BACKUP_STAGE_DIR}/${ARCHIVE_NAME}"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

echo "[backup] Taking online SQLite snapshot from ${DB_PATH} ..."
sqlite3 "${DB_PATH}" ".timeout 10000" ".backup '${WORKDIR}/shinobu.db'"

# Integrity check on snapshot before uploading
INTEGRITY="$(sqlite3 "${WORKDIR}/shinobu.db" "PRAGMA quick_check;")"
if [[ "${INTEGRITY}" != "ok" ]]; then
  echo "[backup] ERROR: SQLite snapshot integrity check failed: ${INTEGRITY}" >&2
  exit 1
fi

if [[ -d "${SOUL_DIR}" ]]; then
  cp -a "${SOUL_DIR}" "${WORKDIR}/soul"
fi

tar -C "${WORKDIR}" -czf "${ARCHIVE_PATH}" .
chmod 600 "${ARCHIVE_PATH}"

DEST_URI="gs://${GCS_BUCKET}/${GCS_PREFIX}/${ARCHIVE_NAME}"
echo "[backup] Uploading ${ARCHIVE_NAME} -> ${DEST_URI} ..."
gcloud storage cp "${ARCHIVE_PATH}" "${DEST_URI}" --quiet

# Prune old local archives to keep VM disk lean
mapfile -t LOCAL_ARCHIVES < <(ls -1t "${BACKUP_STAGE_DIR}"/shinobu_backup_*.tar.gz 2>/dev/null || true)
if (( ${#LOCAL_ARCHIVES[@]} > BACKUP_KEEP_LOCAL )); then
  for old_file in "${LOCAL_ARCHIVES[@]:BACKUP_KEEP_LOCAL}"; do
    rm -f "${old_file}"
  done
fi

echo "[backup] Completed successfully: ${DEST_URI}"
