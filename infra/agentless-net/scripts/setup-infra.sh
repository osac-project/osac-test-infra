#!/usr/bin/env bash
#
# Install prerequisites and cache the SNO snapshot flavor.
# Idempotent — safe to re-run.
#
set -euo pipefail

MGMT_IMAGE="${MGMT_IMAGE:-quay.io/osac-project/cluster-flavors:sno-4-22}"
FLAVOR_NAME="sno-4-22"

info() { echo "==> $*"; }

# ---------- Docker (needed by containerlab) ----------

if ! command -v docker &>/dev/null; then
    info "Installing Docker..."
    dnf install -y dnf-plugins-core
    dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    dnf install -y docker-ce docker-ce-cli containerd.io
    systemctl enable --now docker
else
    info "Docker already installed"
fi

# ---------- Containerlab ----------

if ! command -v containerlab &>/dev/null; then
    info "Installing containerlab..."
    bash -c "$(curl -sL https://get.containerlab.dev)"
else
    info "Containerlab already installed"
fi

# ---------- KVM / libvirt ----------

if ! command -v virsh &>/dev/null; then
    info "Installing libvirt/KVM..."
    dnf install -y qemu-kvm libvirt virt-install
    systemctl enable --now libvirtd
else
    info "libvirt already installed"
fi

# ---------- Ansible ----------

if ! command -v ansible-playbook &>/dev/null; then
    info "Installing Ansible..."
    dnf install -y ansible-core
fi

# ---------- cluster-tool ----------

if ! command -v cluster-tool &>/dev/null; then
    info "Installing cluster-tool..."
    # cluster-tool is a stdlib-only Python script (shebang #!/usr/bin/env
    # python3) distributed as a git repo, not a release binary -- install it
    # the same way scripts/machine-init.sh does. The previous
    # releases/latest/download URL pointed at a nonexistent repo, so curl
    # silently wrote the "Not Found" error page to the binary.
    CLUSTER_TOOL_DIR="/opt/cluster-tool"
    if [ ! -d "${CLUSTER_TOOL_DIR}/.git" ]; then
        git clone https://github.com/osac-project/cluster-tool.git "${CLUSTER_TOOL_DIR}"
    fi
    install -m 0755 "${CLUSTER_TOOL_DIR}/cluster-tool" /usr/local/bin/cluster-tool
fi

if ! cluster-tool servers 2>/dev/null | grep -q "local"; then
    info "Setting up cluster-tool local server..."
    cluster-tool connect local --host local --data-path /var/lib/cluster-tool
    # Full path: sudo's secure_path does not include /usr/local/bin, so
    # `sudo cluster-tool` fails with "command not found" even though the bare
    # calls above resolve fine (same reason bmaas uses an absolute path).
    sudo /usr/local/bin/cluster-tool setup client
else
    info "cluster-tool already configured"
fi

# ---------- Other tools ----------

for tool in sshpass envsubst jq; do
    if ! command -v "$tool" &>/dev/null; then
        info "Installing ${tool}..."
        dnf install -y "$tool" || pip3 install "$tool" 2>/dev/null || true
    fi
done

# ---------- Docker iptables workaround ----------
#
# Docker sets the iptables FORWARD chain policy to DROP.
# Libvirt creates NAT rules in nftables, but iptables and nftables are
# evaluated independently — Docker's DROP overrides libvirt's ACCEPT,
# leaving VMs with no internet access.

if iptables -S FORWARD 2>/dev/null | grep -q "\-P FORWARD DROP"; then
    if ! iptables -C FORWARD -s 192.168.0.0/16 -j ACCEPT 2>/dev/null; then
        info "Adding iptables FORWARD rules for libvirt VMs..."
        iptables -I FORWARD -s 192.168.0.0/16 -j ACCEPT
        iptables -I FORWARD -d 192.168.0.0/16 -j ACCEPT
        iptables -I FORWARD -s 10.0.0.0/8 -j ACCEPT
        iptables -I FORWARD -d 10.0.0.0/8 -j ACCEPT
    fi
fi

# ---------- Pull snapshot flavor ----------

if cluster-tool flavors 2>/dev/null | grep -q "$FLAVOR_NAME"; then
    info "Snapshot flavor '${FLAVOR_NAME}' already cached"
else
    info "Pulling snapshot flavor '${FLAVOR_NAME}'..."
    cluster-tool pull "$MGMT_IMAGE"
fi

info "setup-infra complete."
