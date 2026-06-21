#!/usr/bin/env bash
# machine-init.sh -- Initialize a bare-metal machine for OSAC e2e CI.
#
# This script prepares the machine to serve as a self-hosted GitHub Actions
# runner that runs OSAC e2e tests using cluster-tool for on-demand OpenShift
# cluster provisioning and HashiCorp Vault for secret management.
#
# Usage:
#   sudo ./machine-init.sh [OPTIONS] [STEP ...]
#
# Steps (run all if none specified):
#   packages       Install system packages (libvirt, qemu-kvm, podman, etc.)
#   services       Enable system services (libvirtd, haproxy, podman.socket)
#   oc             Install OpenShift CLI
#   osac           Install osac CLI (fulfillment-service)
#   cluster-tool   Install and configure cluster-tool for local CI mode
#   vault          Install Vault CLI
#   verify         Show installed versions and storage info
#
# Options:
#   --data-path PATH   Where cluster-tool stores disk images and overlays
#                      (auto-detects largest partition if omitted)
#   -h, --help         Show this help
#
# Examples:
#   sudo ./machine-init.sh                          # Run all steps
#   sudo ./machine-init.sh cluster-tool             # Only install/configure cluster-tool
#   sudo ./machine-init.sh packages services        # Only packages + services
#   sudo ./machine-init.sh --data-path /data/ct     # All steps, explicit storage path
set -euo pipefail

###############################################################################
# Constants
###############################################################################
CLUSTER_TOOL_DIR="/opt/cluster-tool"
CLUSTER_TOOL_BIN="/usr/local/bin/cluster-tool"

###############################################################################
# Parse arguments
###############################################################################
DATA_PATH=""
STEPS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --data-path)
            DATA_PATH="$2"
            shift 2
            ;;
        --data-path=*)
            DATA_PATH="${1#*=}"
            shift
            ;;
        -h|--help)
            sed -n '2,/^[^#]/{ /^#/s/^# \?//p }' "$0"
            exit 0
            ;;
        packages|services|oc|osac|cluster-tool|vault|verify)
            STEPS+=("$1")
            shift
            ;;
        *)
            echo "Unknown argument: $1" >&2
            echo "Run with --help for usage." >&2
            exit 1
            ;;
    esac
done

# Default: run all steps
if [[ ${#STEPS[@]} -eq 0 ]]; then
    STEPS=(packages services oc osac cluster-tool vault verify)
fi

###############################################################################
# Preflight checks
###############################################################################
if (( EUID != 0 )); then
    echo "ERROR: This script must be run as root." >&2
    exit 1
fi

should_run() {
    local step="$1"
    for s in "${STEPS[@]}"; do
        [[ "$s" == "$step" ]] && return 0
    done
    return 1
}

echo "=========================================="
echo " OSAC CI Machine Initialization"
echo " Steps: ${STEPS[*]}"
echo "=========================================="
echo ""

###############################################################################
# Step: packages
###############################################################################
install_packages() {
    echo "==> Installing system packages..."

    if ! command -v dnf &>/dev/null; then
        echo "ERROR: dnf not found. This script requires RHEL/CentOS/Fedora." >&2
        exit 1
    fi

    dnf install -y \
        libvirt \
        qemu-kvm \
        podman \
        podman-docker \
        pigz \
        haproxy \
        skopeo \
        zstd \
        jq \
        git \
        dnf-plugins-core \
        dnsmasq \
        python3

    echo "    Done."
}

###############################################################################
# Step: services
###############################################################################
enable_services() {
    echo "==> Enabling system services..."

    # libvirtd
    systemctl enable --now libvirtd

    # HAProxy -- only write base config if not already configured by cluster-tool
    if ! grep -q 'default_backend api-' /etc/haproxy/haproxy.cfg 2>/dev/null; then
        echo "    Writing HAProxy base config..."
        cp /etc/haproxy/haproxy.cfg /etc/haproxy/haproxy.cfg.bak 2>/dev/null || true
        cat > /etc/haproxy/haproxy.cfg <<'HAPROXY_EOF'
global
    log stdout local0
    maxconn 4096

defaults
    mode tcp
    log global
    timeout connect 10s
    timeout client 300s
    timeout server 300s

frontend api-frontend
    bind *:6443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req_ssl_hello_type 1 }
    default_backend api-default

frontend ingress-https-frontend
    bind *:443
    tcp-request inspect-delay 5s
    tcp-request content accept if { req_ssl_hello_type 1 }
    default_backend ingress-https-default

frontend ingress-http-frontend
    bind *:80
    default_backend ingress-http-default

backend api-default
    server placeholder 127.0.0.1:16443 check

backend ingress-https-default
    server placeholder 127.0.0.1:1443 check

backend ingress-http-default
    server placeholder 127.0.0.1:1080 check
HAPROXY_EOF
    fi

    setsebool -P haproxy_connect_any 1 2>/dev/null || true

    # Open firewall ports if firewalld is active
    if systemctl is-active firewalld &>/dev/null; then
        echo "    Opening firewall ports (6443, 443, 80)..."
        firewall-cmd --permanent --add-port=6443/tcp --add-port=443/tcp --add-port=80/tcp
        firewall-cmd --reload
    fi

    systemctl enable --now haproxy
    systemctl restart haproxy

    # Podman socket for GitHub Actions (docker compatibility)
    systemctl enable --now podman.socket

    echo "    Done."
}

###############################################################################
# Step: oc
###############################################################################
install_oc() {
    echo "==> Installing OpenShift CLI..."

    if command -v oc &>/dev/null; then
        echo "    oc already installed: $(oc version --client 2>/dev/null | head -1)"
    else
        OC_URL="https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable/openshift-client-linux.tar.gz"
        echo "    Downloading from ${OC_URL}..."
        TMP_OC=$(mktemp -d)
        curl -sL "${OC_URL}" | tar xz -C "${TMP_OC}"
        install -m 0755 "${TMP_OC}/oc" /usr/local/bin/oc
        install -m 0755 "${TMP_OC}/kubectl" /usr/local/bin/kubectl 2>/dev/null || true
        rm -rf "${TMP_OC}"
        echo "    oc installed: $(oc version --client 2>/dev/null | head -1)"
    fi
}

###############################################################################
# Step: osac
###############################################################################
install_osac() {
    echo "==> Installing osac CLI..."

    local version="${OSAC_VERSION:-}"

    if command -v osac &>/dev/null && [[ -z "${version}" ]]; then
        echo "    osac already installed: $(osac version 2>/dev/null || echo 'unknown version')"
        return 0
    fi

    # Use env var if set, otherwise fetch latest release tag from GitHub
    if [[ -z "${version}" ]]; then
        echo "    Detecting latest release..."
        version=$(curl -sfL -o /dev/null -w '%{url_effective}' \
            "https://github.com/osac-project/fulfillment-service/releases/latest" \
            | grep -oP 'v\K[0-9]+\.[0-9]+\.[0-9]+$') \
            || { echo "ERROR: failed to detect latest osac version" >&2; exit 1; }
    fi

    local url="https://github.com/osac-project/fulfillment-service/releases/download/v${version}/osac_Linux_x86_64"
    echo "    Downloading osac v${version} from ${url}..."
    curl -fL -o /usr/local/bin/osac "${url}" \
        || { echo "ERROR: failed to download osac v${version}" >&2; exit 1; }
    chmod +x /usr/local/bin/osac
    echo "    osac v${version} installed at /usr/local/bin/osac"
}

###############################################################################
# Step: cluster-tool
###############################################################################
setup_cluster_tool() {
    echo "==> Installing cluster-tool..."

    # Ensure git is available (needed for clone)
    if ! command -v git &>/dev/null; then
        dnf install -y git
    fi

    # Install the binary
    if [[ -f "${CLUSTER_TOOL_BIN}" ]]; then
        echo "    cluster-tool already installed at ${CLUSTER_TOOL_BIN}"
        echo "    To update: git -C ${CLUSTER_TOOL_DIR} pull && cp ${CLUSTER_TOOL_DIR}/cluster-tool ${CLUSTER_TOOL_BIN}"
    else
        if [[ -d "${CLUSTER_TOOL_DIR}" ]]; then
            git -C "${CLUSTER_TOOL_DIR}" pull --ff-only
        else
            git clone https://github.com/omer-vishlitzky/cluster-tool.git "${CLUSTER_TOOL_DIR}"
        fi
        install -m 0755 "${CLUSTER_TOOL_DIR}/cluster-tool" "${CLUSTER_TOOL_BIN}"
        echo "    cluster-tool installed at ${CLUSTER_TOOL_BIN}"
    fi

    echo "==> Configuring cluster-tool for local CI mode..."

    CT_CONFIG_DIR="${HOME}/.config/cluster-tool"
    mkdir -p "${CT_CONFIG_DIR}"

    # Determine data path
    if [[ -z "${DATA_PATH}" ]] && [[ -f "${CT_CONFIG_DIR}/config" ]]; then
        EXISTING_DATA=$(grep '^CLUSTER_TOOL_DATA=' "${CT_CONFIG_DIR}/config" | cut -d= -f2)
        if [[ -n "${EXISTING_DATA}" ]]; then
            DATA_PATH="${EXISTING_DATA}"
            echo "    Using existing data path: ${DATA_PATH}"
        fi
    fi

    if [[ -z "${DATA_PATH}" ]]; then
        # Auto-detect: largest partition
        LARGEST=$(df --output=avail,target -x tmpfs -x devtmpfs | tail -n +2 | sort -rn | head -1 | awk '{print $2}')
        DATA_PATH="${LARGEST}/cluster-tool"
        echo "    Auto-detected data path: ${DATA_PATH}"
    fi

    # Write config (only if not already present)
    if [[ ! -f "${CT_CONFIG_DIR}/config" ]]; then
        echo "CLUSTER_TOOL_DATA=${DATA_PATH}" > "${CT_CONFIG_DIR}/config"
    fi

    # Create data directories
    mkdir -p "${DATA_PATH}/flavors" "${DATA_PATH}/overlays" "${DATA_PATH}/tmp" "${DATA_PATH}/containers/storage"

    # Generate SSH keypair for cluster-tool (used for VM access)
    if [[ ! -f "${CT_CONFIG_DIR}/cluster-tool.key" ]]; then
        ssh-keygen -t ed25519 -f "${CT_CONFIG_DIR}/cluster-tool.key" -N '' -q
        echo "    SSH keypair generated."
    else
        echo "    SSH keypair already exists."
    fi

    # Configure podman parallel downloads (faster OCI image pulls)
    CONTAINERS_CONF="${HOME}/.config/containers/containers.conf"
    mkdir -p "$(dirname "${CONTAINERS_CONF}")"
    if [[ ! -f "${CONTAINERS_CONF}" ]]; then
        cat > "${CONTAINERS_CONF}" <<'EOF'
[engine]
image_parallel_copies = 20
EOF
        echo "    Podman parallel downloads configured."
    fi

    # Register as local server
    SERVERS_FILE="${CT_CONFIG_DIR}/servers.json"
    if [[ ! -f "${SERVERS_FILE}" ]]; then
        cat > "${SERVERS_FILE}" <<'EOF'
{"servers": {"local": {"host": "local"}}, "default": "local"}
EOF
        echo "    Registered as local server."
    else
        echo "    Server registry already exists."
    fi

    # DNS setup for local mode: cluster-tool creates dnsmasq entries
    # in /etc/NetworkManager/dnsmasq.d/ for cluster DNS resolution
    mkdir -p /etc/NetworkManager/dnsmasq.d
    if [[ ! -f /etc/NetworkManager/conf.d/cluster-tool-dns.conf ]]; then
        mkdir -p /etc/NetworkManager/conf.d
        cat > /etc/NetworkManager/conf.d/cluster-tool-dns.conf <<'EOF'
[main]
dns=dnsmasq
EOF
        # Kill stale dnsmasq processes before restart
        pkill -x dnsmasq 2>/dev/null || true
        sleep 1
        systemctl restart NetworkManager

        # Verify resolv.conf points to localhost
        for _ in $(seq 1 30); do
            if grep -q '127.0.0.1' /etc/resolv.conf 2>/dev/null; then
                echo "    DNS configured (resolv.conf -> 127.0.0.1)"
                break
            fi
            sleep 0.5
        done
    fi

    echo "    Done."
}

###############################################################################
# Step: vault
###############################################################################
install_vault() {
    echo "==> Installing Vault CLI..."

    if command -v vault &>/dev/null; then
        echo "    vault already installed: $(vault --version)"
    else
        HASHICORP_REPO="/etc/yum.repos.d/hashicorp.repo"
        if [[ ! -f "${HASHICORP_REPO}" ]]; then
            dnf config-manager --add-repo https://rpm.releases.hashicorp.com/RHEL/hashicorp.repo
        fi
        dnf install -y vault
        echo "    vault installed: $(vault --version)"
    fi
}

###############################################################################
# Step: verify
###############################################################################
run_verify() {
    local failures=0

    echo ""
    echo "=========================================="
    echo " Verification"
    echo "=========================================="

    _check() {
        local name="$1"
        local cmd="$2"
        local result
        if result=$(eval "$cmd" 2>&1); then
            result="${result%%$'\n'*}"
            printf "  %-20s %s\n" "${name}:" "${result}"
        else
            result="${result%%$'\n'*}"
            result="${result:-NOT FOUND}"
            printf "  %-20s FAILED — %s\n" "${name}:" "${result}"
            (( failures++ )) || true
        fi
    }

    _check "virsh"          "virsh version"
    _check "podman"         "podman --version"
    _check "oc"             "/usr/local/bin/oc version --client"
    _check "haproxy"        "haproxy -v"
    _check "pigz"           "pigz --version"
    _check "skopeo"         "skopeo --version"
    _check "osac"           "osac version"
    _check "vault"          "vault --version"
    _check "jq"             "jq --version"

    if [[ -f "${CLUSTER_TOOL_BIN}" ]]; then
        printf "  %-20s %s\n" "cluster-tool:" "installed at ${CLUSTER_TOOL_BIN}"
    else
        printf "  %-20s FAILED — NOT FOUND\n" "cluster-tool:"
        (( failures++ )) || true
    fi

    if [[ -n "${DATA_PATH}" ]]; then
        echo ""
        AVAIL=$(df -h "${DATA_PATH}" 2>/dev/null | tail -1 | awk '{print $4}')
        echo "  Storage available:  ${AVAIL} at ${DATA_PATH}"
    fi

    if (( failures > 0 )); then
        echo ""
        echo "  ${failures} check(s) failed."
        return 1
    fi
}

###############################################################################
# Run selected steps
###############################################################################
should_run packages      && install_packages
should_run services      && enable_services
should_run oc            && install_oc
should_run osac          && install_osac
should_run cluster-tool  && setup_cluster_tool
should_run vault         && install_vault
should_run verify        && run_verify

echo ""
echo "=========================================="
echo " Done!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "  1. Set up Vault:     vault/scripts/vault-setup.sh"
echo "  2. Install runners:  scripts/runners/action-runners-setup.sh <TOKEN> [NUM_RUNNERS]"
echo "  3. Pull a flavor:    cluster-tool pull quay.io/rh-ee-ovishlit/cluster-flavors:vmaas-kustomize"
echo "  4. Test a boot:      cluster-tool boot --flavor vmaas-kustomize --name test-1"
echo "  5. Verify cluster:   KUBECONFIG=~/.kube/test-1.kubeconfig oc get nodes"
echo "  6. Clean up test:    cluster-tool destroy test-1"
