#!/usr/bin/env bash
# vault-prerequisites.sh -- Install required packages for the OSAC Vault instance.
# Run as root (or with sudo) on a RHEL/CentOS/Fedora host.
set -euo pipefail

if (( EUID != 0 )); then
    echo "ERROR: This script must be run as root (or via sudo)." >&2
    exit 1
fi

###############################################################################
# Detect package manager
###############################################################################
if command -v dnf &>/dev/null; then
    PKG_MGR="dnf"
elif command -v yum &>/dev/null; then
    PKG_MGR="yum"
else
    echo "ERROR: Neither dnf nor yum found." >&2
    exit 1
fi

###############################################################################
# Install system packages
###############################################################################
if [[ "${PKG_MGR}" == "dnf" ]]; then
    CONFIG_MGR_PKG="dnf-plugins-core"
else
    CONFIG_MGR_PKG="yum-utils"
fi

echo "==> Installing podman, jq, and ${CONFIG_MGR_PKG} ..."
${PKG_MGR} install -y podman jq "${CONFIG_MGR_PKG}"

###############################################################################
# Add HashiCorp repo and install Vault CLI
###############################################################################
HASHICORP_REPO="/etc/yum.repos.d/hashicorp.repo"
if [[ -f "${HASHICORP_REPO}" ]]; then
    echo "==> HashiCorp repo already configured."
else
    echo "==> Adding HashiCorp RPM repository ..."
    if [[ "${PKG_MGR}" == "dnf" ]]; then
        dnf config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
    else
        yum-config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
    fi
fi

echo "==> Installing Vault CLI ..."
${PKG_MGR} install -y vault

###############################################################################
# Verify
###############################################################################
echo ""
echo "=== Installed versions ==="
echo "  podman : $(podman --version)"
echo "  vault  : $(vault --version)"
echo "  jq     : $(jq --version)"
echo ""
echo "Prerequisites satisfied. Run vault-setup.sh next."
