#!/bin/bash

# REQUIRES yq v4

# Detect active Pulumi stack
ACTIVE_STACK=$(pulumi stack ls | grep '*' | awk '{print $1}' | tr -d '*')

# Path to the Pulumi configuration file
PULUMI_CONFIG_PATH="Pulumi.${ACTIVE_STACK}.yaml"

# Extract SSH user from Pulumi config, handling commented or uncommented lines
SSH_USER=$(grep -E '^\s*#?\s*netris-air:hypers_ssh_user:' "$PULUMI_CONFIG_PATH" | sed 's/.*:\s*\(.*\)/\1/' | head -n 1)

# Fallback to 'ubuntu' if SSH_USER is empty or not found
SSH_USER=${SSH_USER:-ubuntu}

# Check if VM name is provided
if [ -z "$1" ]; then
    echo "Usage: $0 <vm-name>"
    exit 1
fi

VM_NAME="$1"

# Step 1: Check if Pulumi config file exists
if [ ! -f "$PULUMI_CONFIG_PATH" ]; then
    echo "Error: Pulumi configuration file $PULUMI_CONFIG_PATH not found."
    exit 1
fi

# Step 2: Extract hypervisor IPs using yq v4 syntax, into an array
readarray -t HYPERVISORS < <(yq e '.config["netris-air:hypers_list"][]' "$PULUMI_CONFIG_PATH" 2>/dev/null)

# Debug: Print extracted IPs
printf 'Extracted hypervisor IPs: %s\n' "${HYPERVISORS[@]}"

if [ ${#HYPERVISORS[@]} -eq 0 ]; then
    echo "Error: No hypervisors found in the configuration file $PULUMI_CONFIG_PATH."
    echo "Debug: Run 'yq e .config[\"netris-air:hypers_list\"][] Pulumi.main.yaml' to verify parsing."
    exit 1
fi

# Function to rebuild VM on a hypervisor
rebuild_vm_on_hypervisor() {
    local hypervisor_ip="$1"

    echo "Checking VM on hypervisor: $hypervisor_ip"

    # Check if the VM exists on the hypervisor
    if ssh "${SSH_USER}@${hypervisor_ip}" "sudo virsh dominfo ${VM_NAME} &>/dev/null"; then
        echo "VM $VM_NAME found on hypervisor $hypervisor_ip. Proceeding with rebuild..."

        # Run the rebuild commands remotely
        ssh "${SSH_USER}@${hypervisor_ip}" << EOF
VOL_NAME=\$(sudo virsh domblklist "$VM_NAME" --details | grep disk | awk '{print \$4}')
VOLUME_PATH=\$(sudo virsh vol-path --pool default "\$VOL_NAME")

sudo virsh destroy "$VM_NAME"
sleep 5

while [ "\$(sudo virsh domstate "$VM_NAME")" != "shut off" ]; do
    echo "Waiting for VM to shut down..."
    sleep 3
done

BACKING_VOL=\$(sudo qemu-img info --backing-chain "\$VOLUME_PATH" | grep "^backing file:" | awk -F': ' '{print \$2}' | head -n 1)

sudo virsh vol-delete --pool default "\$VOL_NAME"
sudo virsh vol-create-as --pool default "\$VOL_NAME" 200G --format qcow2 --backing-vol "\$BACKING_VOL" --backing-vol-format qcow2
sudo virsh start "$VM_NAME"
EOF
        if [ $? -eq 0 ]; then
            echo "VM $VM_NAME rebuilt successfully on hypervisor $hypervisor_ip."
            return 0
        else
            echo "Error: Failed to rebuild VM $VM_NAME on hypervisor $hypervisor_ip."
            return 1
        fi
    else
        echo "VM $VM_NAME not found on hypervisor $hypervisor_ip."
        return 1
    fi
}

# Step 3: Iterate over hypervisors and attempt rebuild
success=false
for hypervisor_ip in "${HYPERVISORS[@]}"; do
    # Skip empty elements
    [ -z "$hypervisor_ip" ] && continue
    rebuild_vm_on_hypervisor "$hypervisor_ip"
    if [ $? -eq 0 ]; then
        success=true
        break
    fi
done

if [ "$success" = false ]; then
    echo "VM $VM_NAME was not found on any of the specified hypervisors."
    exit 1
fi
