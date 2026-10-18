# Netris VMaaS CUDN EVPN Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Netris backend prepare an OpenShift cluster and fabric for VMaaS using the `netris` + `cudn_evpn` NetworkClass, with documented prerequisites and a runnable VMaaS E2E entry point.

**Architecture:** Keep the existing `cudn-evpn-netris-test` OSAC profile as the opt-in registration layer. Add an idempotent Ansible role for OpenShift's FRR-K8s, route advertisement, NMState, VTEP, and worker BGP prerequisites; extend the Netris topology with a dedicated OCP underlay peer and routes; wire the flow into the backend's `vmaas` suite contract and run the moved suite from the OSAC mono-repo. A fail-closed platform check prevents the current SNO VM snapshot from being advertised as a supported OCP 4.22 EVPN target.

**Tech Stack:** GNU Make, Ansible, Netris Terraform provider and controller API, OpenShift 4.22 networking APIs, OVN-Kubernetes CUDN EVPN, FRR-K8s, NMState, OSAC E2E pytest suite.

**Spec:** `../enhancement-proposals/enhancements/OSAC-4291-cudn-evpn-k8s-manager-phase-1-networking/design.md`; installation details in the adjacent `clarifications.md`; the current OSAC installer profile is `../osac/osac-installer/values/cudn-evpn-netris-test/`.

## Global Constraints

- The Netris lab currently pins `ocp_version: "4.22"` in `infra/netris/inventory/group_vars/all.yml`.
- The OCP 4.22 BGP EVPN primary CUDN flow is supported on a bare-metal platform; a cluster reporting `platformStatus.type: None` or a virtualized SNO snapshot does not pass the supported-platform gate. See [OCP 4.22 BGP EVPN documentation](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/advanced_networking/bgp-evpn-for-user-defined-networks).
- Enable host routing and global IP forwarding, CNO FRR routing capability, and OVN-Kubernetes route advertisements before creating EVPN CUDNs. See [OCP route advertisement documentation](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/advanced_networking/route-advertisements).
- Use CNO's FRR-K8s provider (`additionalRoutingCapabilities.providers: [FRR]`); do not describe or install an unrelated FRR operator as a substitute.
- `cudn-evpn-netris-test` already registers `netris` + `cudn_evpn` and enables VMaaS. Infrastructure work must prepare the cluster and fabric and must not pre-create tenant VirtualNetworks or Subnets that the E2E fixtures own.
- E2E tests now live in the OSAC mono-repo at `/opt/osac/tests/e2e/vmaas/`. Do not add, modify, or delete pytest tests in this repository.
- Preserve CaaS and BMaaS defaults. CUDN EVPN-specific fabric and cluster configuration must be selected explicitly by `SUITE=vmaas` or the opt-in profile.
- The VMaaS external-IP fixtures allocate from `172.27.0.0/16` (`/24` and `/30` chunks). Lab routes and Netris policy must cover that range without overlap with fabric or management CIDRs.

## File Structure

- `../enhancement-proposals/enhancements/OSAC-4291-cudn-evpn-k8s-manager-phase-1-networking/design.md` — authoritative OSAC-4291 infrastructure prerequisites and supported-platform statement.
- `infra/netris/inventory/group_vars/all.yml` — opt-in OCP underlay interface, peer addressing, ASN, and external-IP route settings.
- `infra/netris/netris-lab/group_vars/all.yml` and `infra/netris/netris-lab/roles/topology/templates/north-south.tf.j2` — Netris side of the dedicated OCP BGP underlay link.
- `infra/netris/roles/configure-cudn-evpn/` — idempotent OCP CNO, NMState, VTEP, FRRConfiguration, and RouteAdvertisements setup and readiness checks.
- `infra/netris/playbooks/deploy-ocp.yml` and `infra/netris/Makefile` — order cluster configuration after cluster restore and provide the VMaaS setup target.
- `infra/netris/roles/gather-infra/tasks/` — collect OCP networking and Netris peer diagnostics when setup or E2E fails.
- `infra/netris/capabilities`, `infra/contract.md`, root `Makefile`, and `README.md` — expose the `vmaas` suite, its environment contract, the mono-repo test command, and the opt-in invocation.

---

### Task 1: Document the complete OSAC-4291 infrastructure contract

**Files:**
- Modify: `../enhancement-proposals/enhancements/OSAC-4291-cudn-evpn-k8s-manager-phase-1-networking/design.md`
- Reference: `../enhancement-proposals/enhancements/OSAC-4291-cudn-evpn-k8s-manager-phase-1-networking/clarifications.md`

- [ ] Replace the current `Infrastructure Needed: None` statement with a prerequisites section that identifies the required OCP version/platform, OVN-Kubernetes, CNO FRR-K8s provider, route-advertisement setting, host routing and global forwarding settings, NMState operator, VTEP plus per-node `/32`, physical underlay interface, Netris underlay link, BGP ASN/address pairing, EVPN FRRConfiguration, RouteAdvertisements selector, and route policy for tenant VM and ExternalIP prefixes.
- [ ] State the ordering: enable CNO capabilities and install NMState first; configure the node interface and VTEP; establish and verify underlay BGP; create OSAC's EVPN FRRConfiguration and RouteAdvertisements; only then install the `cudn_evpn` NetworkClass and create tenant networks.
- [ ] Correct references to a generic “FRR operator” to the OpenShift CNO FRR-K8s provider, while retaining the design's label contract (`evpn: "true"`) and `frrConfigurationSelector` relationship used by OVN-Kubernetes.
- [ ] Document the platform limit and the lab status explicitly: OCP 4.22 BGP EVPN primary CUDNs require a bare-metal platform; the current Netris `sno-4-22` restore reports `platformStatus.type: None` and is not a supported target.
- [ ] Review the new section against OCP's [BGP EVPN](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/advanced_networking/bgp-evpn-for-user-defined-networks) and [route advertisement](https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/advanced_networking/route-advertisements) docs and the `clarifications.md` six-step list. Keep the design's current “Infrastructure Needed” section as the single authoritative checklist.

**Review gate:** This file is outside the writable root for this session; implementation must request the filesystem escalation needed to edit `../enhancement-proposals` before starting this task.

### Task 2: Add configurable Netris underlay connectivity for the OCP node

**Files:**
- Modify: `infra/netris/inventory/group_vars/all.yml`
- Modify: `infra/netris/netris-lab/group_vars/all.yml`
- Modify: `infra/netris/netris-lab/roles/topology/templates/north-south.tf.j2`
- Modify: `infra/netris/roles/deploy-infra/tasks/preflight.yml`
- Modify: `infra/netris/roles/deploy-infra/tasks/main.yml`

- [x] Add inventory variables for the Netris server port, operator-configured OCP Linux interface, dedicated point-to-point CIDR, OCP local ASN, and selected NS leaf. Keep these separate from the existing management VNet on `eth9`; fail preflight while the guest interface is unset or equals management.
- [x] Inspect the live VM interface list and CloudSim ordering. The existing evidence identifies the OCP VM's three Netris links as `eth9`–`eth11` but does not prove the new guest interface by MAC, so keep the guest mapping operator-configured and fail preflight until supplied.
- [x] Add an opt-in Netris link for `eth12` to the selected North-South leaf with a `/31` pair. Existing server links retain their underlay settings. The new link remains disabled by default.
- [ ] Configure a supported external OCP BGP EVPN peer and explicit route policy. **Blocked:** the pinned Netris Controller 4.16/provider 3.6.22 documentation does not establish an external EVPN AFI/SAFI peer contract; the documented EVPN-on-Host path requires a Netris host agent and EVPN-VTEP role and does not establish compatibility with OCP FRR-K8s. Do not invent provider fields or use the host agent as a substitute.
- [x] Add opt-in preflight checks for a valid `/31`, distinct endpoint addresses, overlap with Netris management/fabric/BGP/NAT/L4LB/OCP/OSAC/BMaaS ranges, selected leaf bounds, and local/derived ASN validity. Reject the `172.27.0.0/16` fixture pool when East-West is enabled because it overlaps `172.26.0.0/15`.
- [ ] Route `172.27.0.0/16` through the lab's Netris/ISP path. **Blocked:** this prefix is inside the ISP's existing `172.16.0.0/12` inbound deny. No narrow verified route and return policy was found, so do not add a host route, masquerade rule, or broader policy exception.

Task 2's scoped implementation is safe scaffolding only. `ocp_underlay_enabled` remains false by default and preflight rejects enabling it while either provider/policy blocker remains. VMaaS stays out of backend capabilities until a supported bare-metal target and a verified EVPN peer/data path exist.

### Task 3: Implement OpenShift CUDN EVPN prerequisites as an Ansible role

**Files:**
- Create: `infra/netris/roles/configure-cudn-evpn/tasks/main.yml`
- Create: `infra/netris/roles/configure-cudn-evpn/templates/`
- Modify: `infra/netris/inventory/group_vars/all.yml`

- [ ] Add an initial preflight that reads `Infrastructure/cluster` and `Network/cluster`, confirms OCP 4.22 and `platformStatus.type: BareMetal`, checks that the API is ready, and fails with the observed version/platform before applying any EVPN configuration. Do not patch `platformStatus` to fake a supported platform.
- [ ] Enable the CNO settings needed by the design: `additionalRoutingCapabilities.providers: [FRR]`, `defaultNetwork.ovnKubernetesConfig.routeAdvertisements: Enabled`, `gatewayConfig.routingViaHost: true`, and `ipForwarding: Global`. Wait for the CNO to roll out and the FRR-K8s APIs to become available.
- [ ] Ensure the NMState operator is installed from `redhat-operators` in `openshift-nmstate` (create the namespace, OperatorGroup, and `kubernetes-nmstate-operator` Subscription on its OCP 4.22 `stable` channel when absent); wait for its CSV, create the singleton `NMState/nmstate` instance, and wait for the `NodeNetworkConfigurationPolicy` API. Apply one NNCP for every node eligible to host KubeVirt VMs; configure the verified fabric interface and a dummy VTEP interface with the node's unique `/32` address.
- [ ] Create the unmanaged OVN `VTEP` resource named `tenant-vtep` with the reserved VTEP CIDR. The `VTEP` API has no node selector; configure each eligible node's unique `/32` through its NNCP so OVN-Kubernetes discovers the addresses.
- [ ] Render an `FRRConfiguration` in `openshift-frr-k8s` with label `evpn: "true"`, local OCP ASN, Netris leaf neighbor address/ASN, and the credentials Secret reference when MD5 authentication is configured. Define the default-VRF underlay router and neighbors only; OCP 4.22 docs say OVN-Kubernetes reads these peers and adds the CUDN EVPN configuration, so do not invent an EVPN address-family field in the input CR.
- [ ] Create the `RouteAdvertisements` resource with `targetVRF: auto`, `advertisements: [PodNetwork]`, `nodeSelector: {}`, `frrConfigurationSelector.matchLabels.evpn: "true"`, and the CUDN selector from the design. OCP 4.22 EVPN supports pod-route advertisements only; service ExternalIPs and EgressIPs need separate fabric reachability/policy.
- [ ] Wait for the VTEP to report `Accepted=True`/`Allocated`, NNCPs to report Available, the FRR BGP neighbor to be Established, and RouteAdvertisements to report `Accepted=True` before the role succeeds.
- [ ] Make the role idempotent: a second run must update the same named resources and must not append duplicate BGP neighbors, NNCPs, VTEPs, or route advertisements.

**Interfaces:**
- Consumes: inventory variables added in Task 2 and the documented resource labels/selectors from Task 1.
- Produces: cluster-wide `tenant-vtep`, `evpn: "true"` FRRConfiguration, selected RouteAdvertisements, node interface configuration, and a fail-closed readiness result for later OSAC deployment.

### Task 4: Wire the supported cluster and OSAC profile into the VMaaS lifecycle

**Files:**
- Modify: root Makefile
- Modify: infra/netris/playbooks/deploy-ocp.yml
- Create: infra/netris/playbooks/setup-vmaas.yml
- Modify: infra/netris/Makefile
- Modify: infra/netris/capabilities
- Modify: infra/netris/roles/prep-osac/tasks/main.yml

- [x] Preserve the current CaaS/BMaaS restore flow. Run configure-cudn-evpn after restore only for SUITE=vmaas. This backend has no supported BareMetal OCP target, so keep vmaas out of SUPPORTED_SUITES and stop the root lifecycle at the capability gate until the target exists.
- [x] Select OSAC_PROFILE=cudn-evpn-netris-test by default only for SUITE=vmaas; keep caas-ci as the existing default for CaaS/BMaaS, and preserve an explicit user override.
- [x] Pass root SUITE through backend orchestration sub-makes so the backend can select its suite-specific profile and lifecycle.
- [x] Validate the selected CUDN EVPN profile's VMaaS enablement, Netris/cudn_evpn manager registrations, and explicit default NetworkClass manager pair in prep-osac. Keep the existing Netris site/controller preparation and do not overwrite the profile's NetworkClass.
- [x] Add read-only setup-vmaas and gather-vmaas readiness targets, plus a no-op destroy-vmaas; no tenant resources are created. Check the OCP EVPN/FRR readiness, OSAC VMaaS controller, ComputeInstance API, and manager registration ConfigMaps.
- [x] Keep vmaas capability closed with a targeted message naming the missing OCP 4.22 BareMetal target, external EVPN peer, and 172.27.0.0/16 return path. The supported VM template and live NetworkClass cannot be verified here through Kubernetes; do not guess Fulfillment API endpoints.

### Task 5: Complete VMaaS suite contract, command, and operational documentation

**Files:**
- Modify: `Makefile`
- Modify: `infra/contract.md`
- Modify: `README.md`
- Modify: `infra/netris/README.md`
- Modify: `infra/netris/Makefile`

- [ ] Change the root `test-vmaas` recipe to run the moved suite from `$(OSAC_MONO_DIR)` using `uv run pytest tests/e2e/vmaas/`, matching the existing mono-repo pattern in `test-bmaas-networking`; keep the JUnit report under this repository's `reports/` directory.
- [ ] Make the emitted `.env.infra` values configurable and complete for VMaaS: `KUBECONFIG`, `OSAC_NAMESPACE`, and `OSAC_VM_KUBECONFIG`. Default the VM kubeconfig to the same supported cluster for this single-cluster profile; allow a separate workload kubeconfig when the deployment actually uses one.
- [ ] Document the `vmaas` backend contract, that `OSAC_VM_KUBECONFIG` is mandatory for the VMaaS suite, the profile selection, and the bare-metal platform requirement.
- [ ] Add a runnable example: `make e2e INFRA=netris SUITE=vmaas OSAC_DEPLOY_MODE=fresh`. Include the explicit `OSAC_PROFILE=cudn-evpn-netris-test` override and explain that the default is selected by the backend for this suite.
- [ ] Document that the suite fixtures create their own VirtualNetwork/Subnet/SecurityGroup and ExternalIPPool resources; lab setup owns only the cluster/fabric prerequisites and route for `172.27.0.0/16`.

### Task 6: Add targeted networking diagnostics and verify the full setup path

**Files:**
- Modify: `infra/netris/roles/gather-infra/tasks/main.yml`
- Create or modify: `infra/netris/roles/gather-infra/tasks/cudn-evpn.yml`
- Modify: `infra/netris/README.md`

- [ ] Capture the cluster platform/version, `Network/cluster` CNO settings, NNCP and VTEP status, RouteAdvertisements conditions, FRRConfiguration, FRR-K8s pod logs, `show bgp summary`, `show bgp l2vpn evpn`, and the Netris OCP peer/link state. Redact BGP authentication secrets.
- [ ] Run `ansible-playbook --syntax-check playbooks/deploy-ocp.yml` and `ansible-playbook --syntax-check playbooks/gather-infra.yml` from `infra/netris/`; run `make -n e2e INFRA=netris SUITE=vmaas` to confirm the selected profile and target sequence without provisioning the lab.
- [ ] On a supported lab, verify the fabric port comes up, underlay BGP reaches Established, FRR learns and advertises EVPN Type-2/3/5 routes after the VMaaS fixture creates a CUDN, the CUDN is Ready, and the VM's assigned subnet address is reachable across the Netris fabric. Do not claim these live data-path checks without a supported target.
- [ ] Verify ExternalIP route coverage for a pool allocated from `172.27.0.0/16`; confirm both the Netris peer route policy and the lab host route point the pool back through the fabric.
- [ ] The existing VMaaS suite lives in the mono-repo (`/opt/osac/tests/e2e/vmaas/`); the user did not request test execution, so do not run it in this task. Do not add or change tests in `osac-test-infra`.
- [ ] Keep `SUITE=caas` and `SUITE=bmaas_networking` behavior unchanged by reviewing the dry-run plans and existing backend defaults.

## Acceptance Criteria

- OSAC-4291's design has one accurate and complete infrastructure checklist, including the supported OCP platform limitation.
- A VMaaS run cannot proceed on the present `platformStatus.type: None` SNO snapshot and cannot silently mutate that field to pass.
- On a supported bare-metal OCP target, CNO, NMState, VTEP, FRRConfiguration, RouteAdvertisements, Netris BGP, and route readiness are all observable and gated before VMaaS E2E starts.
- `make e2e INFRA=netris SUITE=vmaas` selects the CUDN EVPN profile, supplies VMaaS kubeconfigs, and runs the mono-repo suite.
- Tenant test resources remain fixture-owned; CaaS and BMaaS workflows remain unchanged.

## Known Execution Dependency

The repository currently restores `sno-4-22` to `hgx-pod00-su0-h00`; the available local evidence identifies that cluster as a virtualized SNO and its `platformStatus.type` as `None`. No supported bare-metal VMaaS target was found in the backend. Task 4 therefore has an intentional gate: a real bare-metal target must be selected or provisioned before adding `vmaas` to backend capabilities. The rest of the Ansible and documentation work can be prepared, but the E2E path must remain unavailable until that prerequisite is satisfied.
