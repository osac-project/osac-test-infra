# Secure relay access to central-host services

The central monitoring host (`osac-ci-1`) has a public IP, and its SSH port
sees continuous internet-wide brute-force traffic (thousands of attempts a
day, common for any host with SSH on the open internet). Rather than expose
services on that public IP directly, they're also reachable through a
**relay machine** — an internal, VPN-reachable, always-on host that holds
one or more persistent, tightly-restricted SSH tunnels back to the central
host, one per service (or group of related services). Anyone who can reach
the relay (e.g. anyone on the internal VPN) can reach the forwarded
services; nobody else can, without also opening those ports on the central
host's own public-facing firewall.

Originally built for Grafana/Prometheus/Alertmanager; the same pattern now
also covers Vault, as a separate tunnel with its own restricted identity
(see "A service sensitive enough to warrant its own identity" below).

This does not replace the central host's own security posture (that's
covered by the broader hardening effort — SSH key-only auth, sudo scoping,
etc.) — it's an additional access path that doesn't require any of those
ports to be reachable from the public internet at all.

## Architecture

```
   internal VPN                                     public internet
  (trusted users)                                   (everyone else)
        |                                                   |
        v                                                   v
  +----------------+                              +--------------------+
  |  relay machine |--- SSH tunnel #1 (grafana- -->|   central host     |
  |  (always-on,   |    tunnel key, restricted     |   (osac-ci-1)      |
  |  VPN-reachable)|    to 3000/9091/9093)          |                   |
  |                |                                |  Grafana   :3000  |
  |  :3000  :9091  |<-- forwards to 127.0.0.1 ------|  Prometheus:9091  |
  |  :9093         |    on the central host         |  Alertmgr  :9093  |
  |                |                                |                   |
  |                |--- SSH tunnel #2 (vault-  ---->|                   |
  |                |    tunnel key, restricted      |                   |
  |  :8210         |    to 8210 ONLY)               |  Vault     :8210  |
  |                |<-- forwards to 127.0.0.1 ------|  (TLS listener,   |
  +----------------+                                |   see below)      |
                                                     +--------------------+
```

Vault's `:8210` is a dedicated **TLS** listener, separate from the plain-HTTP
`:8200` listener every CI workflow's AppRole login already depends on (see
"Vault: a second, TLS-only listener for relay access" below) — `:8200` is
never tunneled and stays loopback-only on the central host.

Two separate SSH tunnels (separate keypairs, separate restricted central-host
users), not one tunnel carrying both — see "A service sensitive enough to
warrant its own identity" below for why Vault doesn't just share the
Grafana/Prometheus/Alertmanager tunnel.

The relay **initiates** the connection outbound to the central host — the
central host never needs to reach the relay, and no inbound firewall change
is required on the relay's side beyond opening the forwarded ports to
whichever network the trusted users are on (its VPN-facing interface).

## One-time setup

Two scripts, one per side, mirroring the existing `--add-tunnel` pattern
`monitoring-setup.sh` uses for pulling metrics from remote runners — except
this tunnel runs in the opposite direction and for a different purpose
(pushing central's own services out to a relay, not pulling metrics in).

### 1. On the relay machine

```bash
sudo ./monitoring/scripts/setup-tunnel-relay.sh <label> <central-host> <central-tunnel-user> <port> [<port> ...]

# Example: relay to the monitoring central host's Grafana/Prometheus/Alertmanager
sudo ./monitoring/scripts/setup-tunnel-relay.sh osac-ci1 <central-host-ip-or-hostname> grafana-tunnel 3000 9091 9093

# Example: a second, independent tunnel for Vault (its own identity -- see
# "A service sensitive enough to warrant its own identity" below)
sudo ./monitoring/scripts/setup-tunnel-relay.sh osac-ci1-vault <central-host-ip-or-hostname> vault-tunnel 8210
```

This creates a dedicated, unprivileged, shell-less local user
(`<label>-tunnel`) and a systemd service holding a persistent,
auto-reconnecting SSH tunnel. It prints the new key's public half and the
exact command to run on the central host next — the tunnel will keep
retrying and failing to connect until that step happens, which is expected.

### 2. On the central host

```bash
sudo ./monitoring/scripts/authorize-tunnel-relay.sh <central-tunnel-user> "<public-key-from-step-1>" <port> [<port> ...]
```

This creates a dedicated, unprivileged, shell-less system user
(`<central-tunnel-user>`, e.g. `grafana-tunnel`) and installs the relay's key
into its `authorized_keys` with
`permitopen="127.0.0.1:<port>",... restrict,port-forwarding` — critically,
this is an SSH-protocol-level restriction enforced before any shell or sudo
is even reachable. **This key can only forward a connection to the specific
`127.0.0.1:<port>`s given here; it cannot open a shell, run a command, reach
any other port, or do anything else**, regardless of what OS-level
permissions that account might otherwise have. Verified directly (not just
assumed) when this was set up: an attempt to get a shell with the restricted
key is refused outright, forwarding to an authorized port works normally,
and forwarding to an unauthorized port on the same host is refused with
"administratively prohibited" by sshd itself.

Both scripts are idempotent — re-running either with the same arguments
detects the existing user/keypair/service and leaves it alone.

## Adding another port, or another relay

- **Another port for an existing tunnel identity**: re-run
  `setup-tunnel-relay.sh` with the full port list (existing plus new) — it
  rewrites the systemd unit with the complete forward list, then
  `systemctl restart <label>-tunnel.service` to pick it up (a plain re-run
  alone does *not* restart an already-active service, since
  `systemctl enable --now` is a no-op on something already running). You
  must **also** re-run `authorize-tunnel-relay.sh` on the central host with
  the same full port list — `permitopen` is derived from the ports given at
  authorize time, so a port added only on the relay side stays refused by
  sshd until the central side's authorization is updated too.
- **A service sensitive enough to warrant its own identity** (e.g. Vault
  alongside the Grafana/Prometheus/Alertmanager tunnel): use a distinct
  `<label>` on the relay (its own keypair, its own systemd service) *and* a
  distinct `<central-tunnel-user>` on the central host, rather than adding
  the port to an existing identity. `permitopen` means each key can already
  only reach the ports it was explicitly authorized for, but a fully
  separate keypair means a leaked/compromised key for one service (say,
  read-only dashboards) can't be reused to reach a much more sensitive one
  (say, the secret store backing every credential in this setup) — the
  isolation holds even if `permitopen` were somehow misconfigured on one
  side, since there's no shared key to exploit in the first place. Verified
  directly when the Vault tunnel was added: the Grafana-tunnel key's forward
  attempt to Vault's port was refused ("administratively prohibited"), and
  the Vault-tunnel key's forward attempt to Grafana's port failed the same
  way.
- **A second, independent relay** (a different physical/VM relay machine):
  same as above — distinct `<label>` and `<central-tunnel-user>` — so the
  two relays get fully separate identities and neither can be used to
  impersonate or interfere with the other.

## Verifying

From the relay machine, after setup:

```bash
curl -sk https://127.0.0.1:3000/api/health      # Grafana
curl -s  http://127.0.0.1:9091/-/healthy         # Prometheus
curl -s  http://127.0.0.1:9093/-/healthy         # Alertmanager
curl -sk https://127.0.0.1:8210/v1/sys/health    # Vault (TLS listener -- see below)
```

Vault's web UI (`ui = true` in `vault.hcl`) is reachable the same way, from a
browser on the relay's network: `https://<relay-host>:8210/ui/`. Logging in
still requires a real Vault token or AppRole credentials — the tunnel only
narrows *network* reachability, it doesn't bypass Vault's own auth.

## Vault: a second, TLS-only listener for relay access

Vault's `vault.hcl` has two `listener "tcp"` blocks, not one:

- `:8200`, `tls_disable = 1` — the original listener. Every internal
  consumer (every GitHub Actions workflow's AppRole login, every manual
  `vault kv` call in this repo's docs) uses this via
  `VAULT_ADDR=http://127.0.0.1:8200`. **Never tunneled, never changed** when
  the relay was added — there was no reason to touch something dozens of
  workflows already depend on just to add one more access path.
- `:8210`, TLS, self-signed cert whose CN/SAN is the **relay's** hostname
  (not the central host's) — this is the one the relay tunnel forwards.
  Because plain HTTP over a corporate VPN can be blocked or silently
  upgraded by browsers/network policy, this listener exists specifically so
  relay access can be HTTPS without touching the listener everything else
  depends on.

Both listeners run in the same container (`Volume=%h/.vault-server/certs:/vault/certs:Z`
in `vault.container`, referenced by `tls_cert_file`/`tls_key_file` in
`vault.hcl`). **Gotcha hit while setting this up**: the Vault container runs
as a non-root user internally, and `:Z` only sets SELinux context, not POSIX
permissions — a `600`-mode key file owned by the host's root user caused
`error loading TLS cert: ... permission denied` and the container failed to
start. Fixed by `chmod 644` on the key (safe here since `~/.vault-server`
itself is `700`, root-only — the real access boundary is the directory, not
this one file's mode). If you add a third listener/service like this,
check the actual UID the container image runs as before assuming `600` will
work, since it clearly doesn't always.

Adding this port followed the exact same identity-isolation pattern as the
tunnel itself: a **separate** Vault-only tunnel identity, not folding `:8210`
into the existing Grafana/Prometheus/Alertmanager tunnel.

## Trusting the relay's self-signed certificates

Every TLS-terminated service reachable through a relay (Grafana on `:3000`,
Vault on `:8210`) uses a self-signed cert issued for the **relay's**
hostname specifically — not the central host's — so there's no certificate
name-mismatch warning, only the expected "self-signed, not trusted by a
public CA" one. To remove that too, each teammate runs this **on their own
machine** — no SSH access to any server, no git clone, nothing beyond
network access to the relay itself (which they already have via VPN to use
Grafana/Vault in the first place):

```bash
curl -sL https://raw.githubusercontent.com/osac-project/osac-test-infra/main/monitoring/scripts/trust-relay-certs.sh \
  | bash -s -- <relay-host> [port ...]
# defaults to ports 3000 (Grafana) and 8210 (Vault) if none are given
```

`raw.githubusercontent.com` serves this over a normal, publicly-trusted
certificate — no bootstrapping-trust problem piping it into `bash`. The
script itself only ever fetches from `<relay-host>` (never runs anything
else remotely), and prints what it fetched before installing it, per below.

It prints each cert's SHA-256 fingerprint before installing it — **confirm
it matches the reference fingerprint below** before trusting it, since this
script has no other way to prove you're actually talking to the real relay
and not something else on the network answering to the same name. This
table lives in git specifically so it's a separate channel from the live
TLS connection itself — checking it here still catches an on-path attacker
between your machine and the relay, which is the actual thing this
verification step defends against.

| Service | Port | SHA-256 fingerprint |
|---|---|---|
| Grafana | 3000 | `9E:50:62:83:CC:4C:31:71:29:5B:64:D3:37:4D:00:3D:8B:00:1E:C1:3F:C6:A0:3E:02:11:4B:4C:16:70:0B:06` |
| Vault | 8210 | `CA:0C:80:9E:18:00:A3:27:79:53:99:D9:17:18:AB:D5:6E:9D:D4:22:06:46:8C:05:2C:42:41:31:06:61:F6:13` |

Regenerated to add `osac-ci.redhat.com` as a SAN (a real CNAME now points at the relay's
existing hostname — see below), which is why both fingerprints changed from the
values in earlier commits even though the relay itself didn't move.

**Whoever regenerates either cert must update this table in the same
change** — a stale fingerprint here would either block legitimate trust
(mismatch against a rotated-but-fine cert) or, worse, train people to
ignore mismatches. Get the current fingerprint of a live cert with:
`openssl s_client -connect <relay-host>:<port> -servername <relay-host> </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256`.

Supports Fedora/RHEL, Debian/Ubuntu, and macOS system trust stores.

Firefox keeps its own certificate store, separate from the system one, and
isn't covered by the script: `about:preferences#privacy` → "View
Certificates" → "Authorities" → "Import..." → select the fetched `.crt`
file → check "Trust this CA to identify websites".

If a real internal CA becomes available for this hostname later (e.g. via
Red Hat IT, if `infra-edge.lab.eng.rdu2.redhat.com` is covered by one),
prefer that over this script — it would mean certs are trusted automatically
on every corporate-managed machine, with no per-teammate action at all.
Checked this relay directly when the question came up: no FreeIPA/AD
enrollment, no active certmonger, no internal CA already in its trust
bundle -- so that's not available today, at least not through this host.

**When adding a new relay-facing TLS service**: generate its self-signed
cert for the *relay's* hostname (not the service's own host), e.g.:

```bash
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
  -keyout service.key -out service.crt \
  -subj "/CN=<relay-hostname>" \
  -addext "subjectAltName=DNS:<relay-hostname>"
```

Doing this at cert-creation time avoids the mismatch warning outright,
rather than needing to reissue and redeploy the cert (with a service
restart) later, as had to be done for Grafana's pre-existing
central-host-only cert when the relay's TLS-trust story was tightened up.

## Grafana OAuth login only works from one canonical host at a time

Prometheus and Alertmanager don't need authentication, so relaying them is
purely additive. **Grafana is different** — its GitHub OAuth login breaks the
moment you access it from an address other than whichever one `GF_SERVER_ROOT_URL`
and the GitHub OAuth app's callback URL currently agree on. This isn't
cosmetic like the TLS warning above; login fails outright with "Missing
saved oauth state", because the state cookie set on the address you started
from doesn't follow you to the callback redirect (which always targets
whatever `root_url` says, regardless of which address you actually used).

GitHub OAuth Apps only support a single registered callback URL, so only
**one** address can ever be canonical for login. As of this setup, that's
the relay's address, not the central host's own — meaning direct
public-IP-based Grafana *login* no longer works (the dashboards/API would
still be reachable, just not a fresh GitHub sign-in). This was a deliberate
trade made when the relay became the primary access path; see
`monitoring/README.md`'s Grafana setup section for the exact mechanics.

**To switch the canonical host later** (e.g. adding a second relay and
wanting *it* to be canonical instead):
1. Update the GitHub OAuth app's "Authorization callback URL" to the new
   address **first**.
2. Only then update `GF_SERVER_ROOT_URL` — both the live
   `~/.monitoring-server/.env.grafana` on the central host *and*
   `secret/osac/monitoring/grafana-oauth`'s `root_url` field in Vault (so
   the next automated deploy doesn't silently revert it back).
3. Restart `grafana.service`.

Doing this in the other order breaks login from *every* address until both
sides match again — GitHub rejects the request before Grafana's own
state-cookie logic is even reached.

## Troubleshooting

**Tunnel service won't come up / keeps restarting**: check
`journalctl -u <label>-tunnel.service` on the relay. Most likely cause is
the central host hasn't run `authorize-tunnel-relay.sh` yet for this
relay's key, or the two hosts' keys have drifted out of sync (e.g. the
relay's key was regenerated but the central host still has the old one
authorized — re-run `authorize-tunnel-relay.sh` with the current key to fix).

**Changed the port list but the relay still isn't reachable on the new
port**: `setup-tunnel-relay.sh` doesn't restart an already-running service
(see above) — `systemctl restart <label>-tunnel.service` after re-running it.
Also check the relay's own firewall allows the new port on whichever zone
covers its VPN-facing interface.

**Confirming the restriction actually holds** (worth re-checking after any
change to sshd config on the central host):

```bash
# Should be refused:
ssh -i <relay-keyfile> <central-tunnel-user>@<central-host> whoami

# Should work (assuming something's listening on 127.0.0.1:<port> there):
ssh -N -i <relay-keyfile> -L <local-port>:127.0.0.1:<port> <central-tunnel-user>@<central-host> &
curl http://127.0.0.1:<local-port>/...
```
