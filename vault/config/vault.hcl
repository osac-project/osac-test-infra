# Vault server configuration for OSAC CI runner
# Deployed by vault-setup.sh to ~/.vault-server/config/vault.hcl

storage "file" {
  path = "/vault/data"
}

listener "tcp" {
  # Bind to all interfaces inside the container so Podman's port-forward
  # works.  Host-side exposure is restricted to 127.0.0.1 by the Quadlet
  # PublishPort directive.
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

# Vault 2.0.2 removed IPC_LOCK from the official container image;
# disabling mlock avoids the need for the capability in rootless Podman.
disable_mlock = true

ui = true
