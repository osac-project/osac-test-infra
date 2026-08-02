#!/usr/bin/env bash
# vault-backup.sh -- Backup Vault's encrypted data directory.
# Called by vault-backup.service (daily via vault-backup.timer).
set -euo pipefail

VAULT_HOME="${HOME}/.vault-server"
BACKUP_DIR="${VAULT_HOME}/backups"
DATA_DIR="${VAULT_HOME}/data"
RETENTION_DAYS=30

mkdir -p "${BACKUP_DIR}"

###############################################################################
# Create backup
###############################################################################
timestamp=$(date +%Y%m%d-%H%M%S)
backup_file="${BACKUP_DIR}/vault-data-${timestamp}.tar.gz"

echo "[vault-backup] Backing up ${DATA_DIR} ..."
tar -czf "${backup_file}" -C "${VAULT_HOME}" data
chmod 0600 "${backup_file}"
echo "[vault-backup] Backup created: ${backup_file}"

###############################################################################
# Prune old backups
###############################################################################
echo "[vault-backup] Pruning backups older than ${RETENTION_DAYS} days ..."
pruned=$(find "${BACKUP_DIR}" -name "vault-data-*.tar.gz" -mtime "+${RETENTION_DAYS}" -delete -print | wc -l)
echo "[vault-backup] Pruned ${pruned} old backup(s)."
