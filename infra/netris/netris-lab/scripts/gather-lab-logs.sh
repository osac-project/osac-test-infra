#!/usr/bin/env bash
# Collect host-side Netris lab diagnostics. Never collect kubeconfigs, Pulumi
# state, or secret files; the artifact action redacts and scans these outputs.
set -u

LAB_DIR="${1:?usage: gather-lab-logs.sh LAB_DIR}"
KUBECONFIG_PATH=/etc/rancher/k3s/k3s.yaml
mkdir -p \
  "${LAB_DIR}/host" \
  "${LAB_DIR}/networking" \
  "${LAB_DIR}/k3s/pods" \
  "${LAB_DIR}/k3s/installer" \
  "${LAB_DIR}/libvirt/qemu" \
  "${LAB_DIR}/systemd" \
  "${LAB_DIR}/journal" \
  "${LAB_DIR}/dns"

capture() {
  local label="$1" file="$2"
  shift 2
  {
    printf 'collector=%s\ncollected_utc=%s\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
    "$@"
    local rc=$?
    printf '\nexit_code=%s\n' "$rc"
  } >"${file}" 2>&1
  return 0
}

capture_retry() {
  local label="$1" file="$2" attempts="$3" delay="$4"
  shift 4
  {
    printf 'collector=%s\ncollected_utc=%s\n' "$label" "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)"
    local attempt rc
    for ((attempt = 1; attempt <= attempts; attempt++)); do
      printf '\nattempt=%s/%s\n' "$attempt" "$attempts"
      "$@"
      rc=$?
      printf 'exit_code=%s\n' "$rc"
      [[ $rc -eq 0 ]] && break
      [[ $attempt -eq $attempts ]] || sleep "$delay"
    done
  } >"${file}" 2>&1
  return 0
}

retry_command() {
  local attempts="$1" delay="$2" attempt rc output
  shift 2
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    output=$("$@" 2>&1)
    rc=$?
    if [[ $rc -eq 0 || $attempt -eq $attempts ]]; then
      printf '%s\n' "$output"
      return "$rc"
    fi
    sleep "$delay"
  done
}

capture "run metadata" "${LAB_DIR}/host/run-metadata.txt" bash -c '
  echo "hostname=$(hostname -f 2>/dev/null || hostname)"
  echo "kernel=$(uname -r)"
  echo "boot_id=$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
  uptime
  echo
  cat /etc/os-release
'
capture "host resources" "${LAB_DIR}/host/resources.txt" bash -c 'free -h; echo; df -hT; echo; df -ih; echo; lscpu'
capture "block devices" "${LAB_DIR}/host/block-devices.txt" lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINT
capture "installed tool versions" "${LAB_DIR}/host/tool-versions.txt" bash -c '
  for tool in curl kubectl virsh qemu-img; do
    echo "=== ${tool} ==="
    command -v "${tool}" >/dev/null 2>&1 && "${tool}" --version 2>&1 | head -n 3 || echo "not installed"
  done
  echo "=== k3s ==="
  k3s --version 2>&1 || true
'

capture "network interfaces" "${LAB_DIR}/networking/interfaces.txt" ip -details -brief address show
capture "network routes and rules" "${LAB_DIR}/networking/routes.txt" bash -c 'ip -4 route show table all; echo; ip -6 route show table all; echo; ip rule show'
capture "neighbor table" "${LAB_DIR}/networking/neighbors.txt" ip neigh show
capture "firewall state" "${LAB_DIR}/networking/firewall.txt" bash -c 'firewall-cmd --state 2>&1; echo; firewall-cmd --get-active-zones 2>&1; echo; nft list ruleset 2>&1 || iptables-save 2>&1'
capture "resolver configuration" "${LAB_DIR}/dns/resolv-conf.txt" cat /etc/resolv.conf
capture_retry "DNS resolution for K3s installer" "${LAB_DIR}/dns/get-k3s-hosts.txt" 5 2 getent ahosts get.k3s.io

capture "failed systemd units" "${LAB_DIR}/systemd/failed-units.txt" systemctl --failed --no-pager
capture "systemd unit inventory" "${LAB_DIR}/systemd/lab-units.txt" bash -c "systemctl list-units --all --no-pager 'k3s*' 'libvirt*' 'virtqemud*' 'dnsmasq*' 'openvpn*' 'socat-forwarder@*' 'NetworkManager*' 'firewalld*'"
capture "host warning and error journal" "${LAB_DIR}/journal/warnings-errors.txt" journalctl -b --no-pager -p warning -n 2000
capture "kernel warning and error journal" "${LAB_DIR}/journal/kernel-warnings-errors.txt" journalctl -k -b --no-pager -p warning -n 2000
capture "SELinux denials" "${LAB_DIR}/journal/selinux-denials.txt" bash -c 'ausearch -m AVC -ts boot 2>&1 || journalctl -b --no-pager | grep -i "avc:.*denied" | tail -n 1000 || true'

units=(k3s libvirtd virtqemud dnsmasq openvpn-client@client firewalld NetworkManager)
for unit in "${units[@]}"; do
  safe_unit="${unit//[^A-Za-z0-9_.@-]/_}"
  capture "systemd status ${unit}" "${LAB_DIR}/systemd/${safe_unit}-status.txt" systemctl status "${unit}" --no-pager --full
  capture "journal ${unit}" "${LAB_DIR}/journal/${safe_unit}.txt" journalctl -b -u "${unit}" --no-pager -n 1500
done

capture "K3s container list" "${LAB_DIR}/k3s/containers.txt" bash -c 'k3s crictl ps -a 2>&1 || crictl ps -a 2>&1 || true'
capture_retry "K3s node status" "${LAB_DIR}/k3s/nodes.txt" 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get nodes -o wide
capture_retry "K3s all pods" "${LAB_DIR}/k3s/pods-all.txt" 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get pods -A -o wide
capture_retry "K3s recent events" "${LAB_DIR}/k3s/events.txt" 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get events -A --sort-by=.metadata.creationTimestamp
capture_retry "K3s deployments, jobs, and services" "${LAB_DIR}/k3s/workloads.txt" 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get deployments,statefulsets,daemonsets,jobs,services -A -o wide
capture_retry "Netris Helm resources" "${LAB_DIR}/k3s/helm-resources.txt" 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get helmcharts,helmchartconfigs -A -o wide

for namespace in netris-controller kube-system cert-manager; do
  pods=$(retry_command 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get pods -n "${namespace}" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || true)
  for pod in ${pods}; do
    pod_file="${pod//[^A-Za-z0-9_.-]/_}"
    containers=$(retry_command 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get pod "${pod}" -n "${namespace}" -o jsonpath='{.spec.containers[*].name}' 2>/dev/null || true)
    for container in ${containers}; do
      container_file="${container//[^A-Za-z0-9_.-]/_}"
      capture_retry "${namespace}/${pod}/${container} current log" "${LAB_DIR}/k3s/pods/${namespace}-${pod_file}-${container_file}.log" 3 2 \
        env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s logs "${pod}" -n "${namespace}" -c "${container}" --timestamps --tail=3000
      capture_retry "${namespace}/${pod}/${container} previous log" "${LAB_DIR}/k3s/pods/${namespace}-${pod_file}-${container_file}-previous.log" 2 1 \
        env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s logs "${pod}" -n "${namespace}" -c "${container}" --previous --timestamps --tail=3000
    done
    init_containers=$(retry_command 4 2 env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s get pod "${pod}" -n "${namespace}" -o jsonpath='{.spec.initContainers[*].name}' 2>/dev/null || true)
    for container in ${init_containers}; do
      container_file="${container//[^A-Za-z0-9_.-]/_}"
      capture_retry "${namespace}/${pod}/${container} init log" "${LAB_DIR}/k3s/pods/${namespace}-${pod_file}-init-${container_file}.log" 3 2 \
        env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s logs "${pod}" -n "${namespace}" -c "${container}" --timestamps --tail=3000
      capture_retry "${namespace}/${pod}/${container} previous init log" "${LAB_DIR}/k3s/pods/${namespace}-${pod_file}-init-${container_file}-previous.log" 2 1 \
        env KUBECONFIG="${KUBECONFIG_PATH}" kubectl --request-timeout=15s logs "${pod}" -n "${namespace}" -c "${container}" --previous --timestamps --tail=3000
    done
  done
done

if [[ -d /tmp/netris-k3s-installer ]]; then
  find /tmp/netris-k3s-installer -maxdepth 1 -type f -print0 | while IFS= read -r -d '' file; do
    cp -- "$file" "${LAB_DIR}/k3s/installer/$(basename "$file")" 2>/dev/null || true
  done
fi
if [[ -f /tmp/install-k3s.sh ]]; then
  sha256sum /tmp/install-k3s.sh >"${LAB_DIR}/k3s/installer/install-script-sha256.txt" 2>&1 || true
fi

capture "libvirt domains" "${LAB_DIR}/libvirt/domains.txt" bash -c '
  virsh list --all --title
  echo
  for vm in $(virsh list --all --name 2>/dev/null); do
    [ -n "$vm" ] || continue
    echo "=== $vm ==="
    virsh dominfo "$vm" 2>&1
    virsh domstate "$vm" --reason 2>&1
    virsh domifaddr "$vm" --source lease 2>&1 || true
  done
'
capture "libvirt networks and storage pools" "${LAB_DIR}/libvirt/networks-pools.txt" bash -c 'virsh net-list --all; echo; virsh pool-list --all; echo; virsh nodeinfo'
capture "libvirt XML" "${LAB_DIR}/libvirt/domain-xml.txt" bash -c '
  for vm in $(virsh list --all --name 2>/dev/null); do
    [ -n "$vm" ] || continue
    echo "=== $vm ==="
    virsh dumpxml "$vm" 2>&1
    echo
  done
'
if [[ -d /var/log/libvirt/qemu ]]; then
  find /var/log/libvirt/qemu -maxdepth 1 -type f -name '*.log' -print0 | while IFS= read -r -d '' file; do
    dest="${LAB_DIR}/libvirt/qemu/$(basename "$file")"
    tail -n 4000 "$file" >"${dest}" 2>&1 || true
  done
fi

printf 'Netris host diagnostics completed at %s UTC\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || true)" \
  >"${LAB_DIR}/collection-status.txt"
