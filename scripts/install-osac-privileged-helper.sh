#!/usr/bin/env bash
# Run once as root on a runner host. CI uses pkexec for this fixed helper and
# does not need sudo access.
set -euo pipefail

[[ "${EUID}" -eq 0 && $# -eq 1 && "$1" =~ ^[a-z_][a-z0-9_-]*$ ]] || {
  echo "Usage (as root): install-osac-privileged-helper.sh CALLER_USER" >&2
  exit 2
}
CALLER_USER="$1"
id "${CALLER_USER}" >/dev/null
command -v pkexec >/dev/null || { echo "pkexec must be installed on this host." >&2; exit 1; }
command -v pkaction >/dev/null || { echo "pkaction must be installed on this host." >&2; exit 1; }
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

for helper in osac-privileged osac-assisted-cluster-flavor osac-assisted-cluster-tool \
              cluster-tool-assisted-compact-boot assisted-compact-watchdog.py \
              osac-compact-dns osac-cluster-kubeconfig; do
  install -o root -g root -m 0755 "${SCRIPT_DIR}/${helper}" "/usr/local/sbin/${helper}"
done
printf '%s\n' "${CALLER_USER}" > /etc/osac-assisted-caller
chown root:root /etc/osac-assisted-caller
chmod 0644 /etc/osac-assisted-caller

POLICY_FILE=/usr/share/polkit-1/actions/org.osac.assisted-flavor.policy
cat > "${POLICY_FILE}" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1.0/policyconfig.dtd">
<policyconfig>
  <action id="org.osac.assisted-flavor">
    <description>Run the OSAC Assisted flavor host helper</description>
    <message>Authorization is required to run the OSAC Assisted flavor host helper</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>no</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/sbin/osac-privileged</annotate>
  </action>
</policyconfig>
EOF
chown root:root "${POLICY_FILE}"
chmod 0644 "${POLICY_FILE}"

RULE_FILE=/etc/polkit-1/rules.d/49-osac-assisted-flavor.rules
cat > "${RULE_FILE}" <<EOF
polkit.addRule(function(action, subject) {
    if (action.id == "org.osac.assisted-flavor" && subject.user == "${CALLER_USER}") {
        return polkit.Result.YES;
    }
});
EOF
chown root:root "${RULE_FILE}"
chmod 0644 "${RULE_FILE}"
policy_ready=false
for ((attempt = 1; attempt <= 5; attempt++)); do
  if pkaction --action-id org.osac.assisted-flavor >/dev/null 2>&1; then
    policy_ready=true
    break
  fi
  sleep 1
done
[[ "${policy_ready}" == "true" ]] || {
  echo "Polkit did not register the OSAC action; the previous sudo policy was preserved." >&2
  exit 1
}

# Revoke only the sudo allowlist installed by the earlier Assisted helper.
OLD_SUDOERS=/etc/sudoers.d/osac-assisted-cluster-flavor
if [[ -f "${OLD_SUDOERS}" &&
      "$(head -n 1 "${OLD_SUDOERS}")" == "# Installed by scripts/machine-init.sh when OSAC_ASSISTED_FLAVOR_HELPER_ENABLED=true." ]]; then
  rm -f "${OLD_SUDOERS}"
fi
echo "Installed the OSAC helper and polkit rule for ${CALLER_USER}."
