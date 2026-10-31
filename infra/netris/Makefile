# ─── Variables ──────────────────────────────────────────────────────

ifneq ($(origin SUITE),undefined)
$(error SUITE was replaced: use DEPLOY_SUITE and TEST_SUITE, see targets.md)
endif
ifneq ($(origin OSAC_DEPLOY_MODE),undefined)
$(error OSAC_DEPLOY_MODE was removed: every OSAC install is fresh)
endif
EXTRA_VARS ?=
ENV_INFRA := .env.infra

# DEPLOY_SUITE selects the profile and setup. The root TEST_SUITE selects the tests.
# DEPLOY_SUITE=caas (default) — setup-caas / deploy-caas.
# DEPLOY_SUITE=maas — setup-maas / deploy-maas.
DEPLOY_SUITE ?= caas

OSAC_NAMESPACE ?= osac-e2e-ci
KUBECONFIG ?= /root/.kube/config
OSAC_VM_KUBECONFIG ?= $(KUBECONFIG)
export KUBECONFIG
ifeq ($(origin OSAC_PROFILE),command line)
$(error OSAC_PROFILE is derived from DEPLOY_SUITE; choose the deployment scenario with DEPLOY_SUITE)
endif
OSAC_PROFILE = $(if $(filter vmaas,$(DEPLOY_SUITE)),vmaas-ci,$(if $(filter bmaas,$(DEPLOY_SUITE)),bmaas-ci,$(if $(filter full,$(DEPLOY_SUITE)),full-ci,caas-ci)))
OSAC_PULL_SECRET_PATH ?= /root/pull-secret
export OSAC_PULL_SECRET_PATH
KUBECONFIG_ABS = $(abspath $(KUBECONFIG))
OSAC_VM_KUBECONFIG_ABS = $(abspath $(OSAC_VM_KUBECONFIG))

# Public Make inputs are translated here into Ansible variables. User
# EXTRA_VARS comes first; backend-owned deployment context and path mappings
# follow so each value has one canonical public input.
ANSIBLE_EXTRA = $(if $(EXTRA_VARS),-e '$(EXTRA_VARS)') \
	-e deploy_suite=$(DEPLOY_SUITE) \
	-e bare_metal_services=$(BARE_METAL_SERVICES) \
	-e osac_profile=$(OSAC_PROFILE) \
	-e osac_namespace=$(OSAC_NAMESPACE) \
	-e osac_values_file=values/$(OSAC_PROFILE)/instance.yaml \
	-e 'kubeconfig_path=$(KUBECONFIG_ABS)' \
	-e 'pull_secret_path=$(OSAC_PULL_SECRET_PATH)'

# vmaas and full configure the OCP host as an EVPN VTEP.
EVPN_DEPLOY_SUITES = vmaas full
VMAAS_UNDERLAY_EXTRA = $(if $(filter $(EVPN_DEPLOY_SUITES),$(DEPLOY_SUITE)),-e ocp_underlay_enabled=true,)
OCP_ANSIBLE_EXTRA = $(ANSIBLE_EXTRA) $(VMAAS_UNDERLAY_EXTRA)
EVPN_VALUES_FILE = $(abspath .evpn-values.yaml)
VMAAS_EVPN_VALUES_EXTRA = $(if $(filter $(EVPN_DEPLOY_SUITES),$(DEPLOY_SUITE)),-f $(EVPN_VALUES_FILE))
# Netris values live in the service profile directories. prep-osac always
# generates the runtime overlay; only these three profiles have a committed
# netris.yaml.
NETRIS_PROFILE_VALUES = $(if $(filter vmaas-ci bmaas-ci full-ci,$(OSAC_PROFILE)),-f values/$(OSAC_PROFILE)/netris.yaml)
NETRIS_INSTANCE_VALUES_EXTRA = $(strip $(NETRIS_PROFILE_VALUES) -f values/$(OSAC_PROFILE)/netris-runtime.yaml)
OSAC_TEST_TENANT_NAME ?= tenant1
CAAS_BAREMETAL_INSTANCE_TYPE ?= ci-worker
CAAS_DISK_IMAGE_NAME ?= rhcos-4-22-amd64
CAAS_DISK_IMAGE_SOURCE_REF ?= oci://quay.io/osac-project/rhcos:4.22.0
CAAS_WORKER_COUNT ?= 1
# CaaS and MaaS always provision workers through BMaaS.
CAAS_WORKER_VCPU ?= 4
CAAS_WORKER_MEMORY_MB ?= 16384
# BMaaS, CaaS, MaaS and full need the BMC service infrastructure.
BARE_METAL_SERVICES ?= $(if $(filter caas maas bmaas full,$(DEPLOY_SUITE)),true,false)

# ─── Convenience targets ───────────────────────────────────────────
#
#   make deploy       — full fresh install: deploy-infra → deploy-ocp → deploy-osac

deploy: deploy-infra deploy-ocp deploy-osac

# Fresh VMaaS/EVPN installs must use images built from the branch under test
# (operator, fulfillment-service, AAP bootstrap); see check_images.yml.
# CI builds the images after deploy-ocp, so prep-osac checks them there.
_check-osac-images:
	@if [ -n "$$GITHUB_ACTIONS" ]; then echo "Image overrides are checked by prep-osac in CI."; else ansible-playbook playbooks/check-osac-images.yml $(ANSIBLE_EXTRA); fi

# ─── Contract targets ──────────────────────────────────────────────
#
# Called by the root Makefile (make -C infra/netris <target>).
# See infra/contract.md for the interface spec.

deploy-setup: _check-osac-images
	ansible-playbook playbooks/deploy-setup.yml $(ANSIBLE_EXTRA)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra: _check-osac-images
	ansible-playbook playbooks/deploy-infra.yml $(ANSIBLE_EXTRA) $(VMAAS_UNDERLAY_EXTRA)

deploy-ocp:
	@rm -f .evpn-values.yaml
	ansible-playbook playbooks/deploy-ocp.yml $(OCP_ANSIBLE_EXTRA)

deploy-osac:
	@$(MAKE) install-osac
	@printf '%s\n' \
		'KUBECONFIG=$(KUBECONFIG_ABS)' \
		'OSAC_NAMESPACE=$(OSAC_NAMESPACE)' \
		'OSAC_VM_KUBECONFIG=$(if $(filter $(EVPN_DEPLOY_SUITES),$(DEPLOY_SUITE)),$(OSAC_VM_KUBECONFIG_ABS),$(KUBECONFIG_ABS))' \
		'OSAC_PULL_SECRET_PATH=$(OSAC_PULL_SECRET_PATH)' \
		'OSAC_E2E_FABRIC_SNAPSHOT_CMD=$(abspath scripts/fabric-snapshot.sh)' \
		> $(ENV_INFRA)

# VMaaS setup/gather is read-only: tenant resources remain owned by the suite fixtures.
setup-vmaas: DEPLOY_SUITE = vmaas
setup-vmaas:
	ansible-playbook playbooks/setup-vmaas.yml $(OCP_ANSIBLE_EXTRA)

gather-vmaas: DEPLOY_SUITE = vmaas
gather-vmaas:
	ansible-playbook playbooks/gather-vmaas.yml $(ANSIBLE_EXTRA) -e 'osac_vm_kubeconfig=$(OSAC_VM_KUBECONFIG_ABS)'

destroy-vmaas:
	@echo "VMaaS setup creates no tenant resources; cluster prerequisites are removed with destroy-ocp. Removing helper resources left by aborted test runs."
	-KUBECONFIG=$(KUBECONFIG_ABS) kubectl delete pod,secret,networkpolicy -A -l osac-e2e/ssh-helper=true --ignore-not-found

# Keep suite teardown backend-owned: full destroys the whole lab and is not a
# suite-level cleanup operation. The root dispatcher uses this contract target.
ifeq ($(DEPLOY_SUITE),full)
destroy-suite:
	@echo "ERROR: destroy-suite DEPLOY_SUITE=full is not supported; use 'make -C infra/netris destroy-full' only when you intend to destroy the entire lab." >&2
	@exit 2
else
destroy-suite:
	$(MAKE) destroy-$(DEPLOY_SUITE) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
endif

test-caas-networking test-caas_networking:
	$(MAKE) -C ../.. run-tests INFRA=netris DEPLOY_SUITE=caas TEST_SUITE=caas_networking

test-maas-networking test-maas_networking:
	$(MAKE) -C ../.. run-tests INFRA=netris DEPLOY_SUITE=maas TEST_SUITE=maas_networking

# E2E setup stops before tenant creation; pytest fixtures provide test tenants.
prepare-suite:
	@case "$(DEPLOY_SUITE)" in caas|maas|bmaas|vmaas|full) ;; *) echo "Unsupported deployment suite: $(DEPLOY_SUITE)" >&2; exit 2;; esac
	$(MAKE) setup-$(DEPLOY_SUITE) DEPLOY_SUITE=$(DEPLOY_SUITE)

# Persistent deployment playbooks prepare tenants and Netris networking before workloads.
# The target name is unrelated to the DEPLOY_SUITE variable.
SUITE_STAGE ?= all
deploy-suite:
	@case "$(DEPLOY_SUITE)" in caas|maas|bmaas) ;; *) echo "Unsupported deployment suite: $(DEPLOY_SUITE)" >&2; exit 2;; esac
	@case "$(SUITE_STAGE)" in \
		all) $(MAKE) prepare-suite DEPLOY_SUITE=$(DEPLOY_SUITE) && $(MAKE) deploy-$(DEPLOY_SUITE) DEPLOY_SUITE=$(DEPLOY_SUITE) ;; \
		setup) $(MAKE) setup-$(DEPLOY_SUITE) DEPLOY_SUITE=$(DEPLOY_SUITE) ;; \
		workload) $(MAKE) deploy-$(DEPLOY_SUITE) DEPLOY_SUITE=$(DEPLOY_SUITE) ;; \
		*) echo "Unsupported deploy-suite stage: $(SUITE_STAGE)" >&2; exit 2 ;; \
	esac

setup-caas:
	ansible-playbook playbooks/setup-caas.yml \
	  -e 'caas_baremetal_instance_type=$(CAAS_BAREMETAL_INSTANCE_TYPE)' \
	  -e 'caas_disk_image_name=$(CAAS_DISK_IMAGE_NAME)' \
	  -e 'caas_disk_image_source_ref=$(CAAS_DISK_IMAGE_SOURCE_REF)' \
	  -e 'caas_worker_count=$(CAAS_WORKER_COUNT)' \
	  -e 'caas_worker_vcpu=$(CAAS_WORKER_VCPU)' \
	  -e 'caas_worker_memory_mb=$(CAAS_WORKER_MEMORY_MB)' \
	  -e 'osac_test_tenant_name=$(OSAC_TEST_TENANT_NAME)' \
	  $(ANSIBLE_EXTRA)

deploy-caas:
	ansible-playbook playbooks/deploy-caas.yml \
	  -e 'caas_baremetal_instance_type=$(CAAS_BAREMETAL_INSTANCE_TYPE)' \
	  -e 'caas_disk_image_name=$(CAAS_DISK_IMAGE_NAME)' \
	  -e 'caas_worker_count=$(CAAS_WORKER_COUNT)' \
	  -e 'osac_test_tenant_name=$(OSAC_TEST_TENANT_NAME)' \
	  $(ANSIBLE_EXTRA)

destroy-osac:
	@echo "=== Tearing down OSAC ==="
	cd /opt/osac/osac-installer && KUBECONFIG=$(KUBECONFIG_ABS) make uninstall \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE) 2>/dev/null || true
	rm -rf /opt/osac
	@rm -f $(ENV_INFRA)

destroy-ocp:
	ansible-playbook playbooks/destroy-ocp.yml $(OCP_ANSIBLE_EXTRA)
	@rm -f .evpn-values.yaml

destroy-infra:
	ansible-playbook playbooks/destroy-infra.yml $(ANSIBLE_EXTRA)
	@rm -f $(ENV_INFRA)

destroy-setup:
	ansible-playbook playbooks/destroy-setup.yml $(ANSIBLE_EXTRA)

destroy-caas:
	ansible-playbook playbooks/destroy-caas.yml \
	  -e 'osac_test_tenant_name=$(OSAC_TEST_TENANT_NAME)' \
	  $(ANSIBLE_EXTRA)

force-destroy-caas:
	ansible-playbook playbooks/force-destroy-caas.yml \
	  -e 'osac_test_tenant_name=$(OSAC_TEST_TENANT_NAME)' \
	  $(ANSIBLE_EXTRA)

gather-infra:
	ansible-playbook playbooks/gather-infra.yml $(OCP_ANSIBLE_EXTRA)

gather-mgmt-cluster:
	ansible-playbook playbooks/gather-mgmt-cluster.yml $(ANSIBLE_EXTRA)

gather-caas:
	ansible-playbook playbooks/gather-caas.yml $(ANSIBLE_EXTRA)

setup-bmc:
	ansible-playbook playbooks/setup-bmc.yml $(ANSIBLE_EXTRA)

setup-bmaas:
	ansible-playbook playbooks/setup-bmaas.yml $(ANSIBLE_EXTRA)

# Keep the upstream full-profile order: CaaS prerequisites, BMaaS defaults, then EVPN.
setup-full:
	$(MAKE) setup-caas DEPLOY_SUITE=caas
	$(MAKE) setup-bmaas DEPLOY_SUITE=bmaas
	$(MAKE) setup-vmaas DEPLOY_SUITE=vmaas

deploy-bmaas:
	ansible-playbook playbooks/deploy-bmaas.yml $(ANSIBLE_EXTRA)

destroy-bmaas:
	ansible-playbook playbooks/destroy-bmaas.yml $(ANSIBLE_EXTRA)

gather-bmaas:
	ansible-playbook playbooks/gather-bmaas.yml $(ANSIBLE_EXTRA)

# Collect diagnostics for each service configured by the full-ci profile.
gather-full:
	$(MAKE) gather-caas
	$(MAKE) gather-bmaas
	$(MAKE) gather-vmaas
	$(MAKE) gather-infra

# ---------------------------------------------------------------------------
# MaaS — thin wrappers over CaaS playbooks with MaaS-specific variable overrides
# Override defaults with MAAS_CLUSTER_NAME=... and MAAS_BAREMETAL_INSTANCE_TYPE=....
# ---------------------------------------------------------------------------
MAAS_CLUSTER_NAME     ?= maas-cluster
# MaaS selects g5 workers; CaaS retains the ci-worker default.
MAAS_BAREMETAL_INSTANCE_TYPE ?= g5
MAAS_WORKER_COUNT ?= 2
MAAS_CLUSTER_TEMPLATE ?= osac.templates.ocp_4_20_ai_maas
MAAS_CATALOG_ITEM     ?= maas-ocp
# MaaS currently uses the same RHCOS 4.22 DiskImage as CaaS; override independently if needed.
MAAS_DISK_IMAGE_NAME ?= $(CAAS_DISK_IMAGE_NAME)
MAAS_DISK_IMAGE_SOURCE_REF ?= $(CAAS_DISK_IMAGE_SOURCE_REF)
# All three MaaS BMaaS worker VMs use the same size; MAAS_WORKER_COUNT controls the initial cluster size.
MAAS_WORKER_VCPU      ?= 10
MAAS_WORKER_MEMORY_MB ?= 20480

MAAS_EXTRA = \
  -e 'caas_cluster_name=$(MAAS_CLUSTER_NAME)' \
  -e 'caas_worker_count=$(MAAS_WORKER_COUNT)' \
  -e 'caas_disk_image_name=$(MAAS_DISK_IMAGE_NAME)' \
  -e 'caas_baremetal_instance_type=$(MAAS_BAREMETAL_INSTANCE_TYPE)' \
  -e 'caas_cluster_template=$(MAAS_CLUSTER_TEMPLATE)' \
  -e 'caas_catalog_item=$(MAAS_CATALOG_ITEM)' \
  -e 'caas_catalog_item_title=MaaS OCP' \
  -e 'caas_catalog_item_description=OCP cluster for MaaS testing' \
  -e 'caas_worker_vcpu=$(MAAS_WORKER_VCPU)' \
  -e 'caas_worker_memory_mb=$(MAAS_WORKER_MEMORY_MB)' \
  -e 'osac_test_tenant_name=$(OSAC_TEST_TENANT_NAME)'

setup-maas:
	ansible-playbook playbooks/setup-caas.yml \
	  $(MAAS_EXTRA) \
	  -e 'caas_disk_image_source_ref=$(MAAS_DISK_IMAGE_SOURCE_REF)' \
	  $(ANSIBLE_EXTRA)

deploy-maas:
	ansible-playbook playbooks/deploy-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

destroy-maas:
	ansible-playbook playbooks/destroy-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

force-destroy-maas:
	ansible-playbook playbooks/force-destroy-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

# ─── Internal sub-targets (deploy-osac implementation) ─────────────

# Fresh install: clone mono-repo, patch Helm values, run make install
install-osac: prep-osac run-osac-setup

prep-osac:
	ansible-playbook playbooks/prep-osac.yml $(OCP_ANSIBLE_EXTRA)

run-osac-setup:
	@if [ -n "$(VMAAS_EVPN_VALUES_EXTRA)" ] && [ ! -s "$(EVPN_VALUES_FILE)" ]; then \
		echo "Missing $(EVPN_VALUES_FILE); run make deploy-ocp DEPLOY_SUITE=$(DEPLOY_SUITE) first." >&2; \
		exit 1; \
	fi
	@echo "=== Installing OSAC infrastructure via Helm (make install-infra) ==="
	cd /opt/osac/osac-installer && KUBECONFIG=$(KUBECONFIG_ABS) make install-infra \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE) \
		INFRA_VALUES_EXTRA="$(VMAAS_EVPN_VALUES_EXTRA)"
	@echo "=== Installing OSAC application via Helm (make install-osac) ==="
	cd /opt/osac/osac-installer && KUBECONFIG=$(KUBECONFIG_ABS) make install-osac \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE) \
		INSTANCE_VALUES_EXTRA="$(NETRIS_INSTANCE_VALUES_EXTRA)"

# ─── Utilities ─────────────────────────────────────────────────────

connectivity:
	ansible-playbook playbooks/connectivity.yml $(ANSIBLE_EXTRA)

vendor-update:
	rm -rf vendor/ansible_collections
	ansible-galaxy collection install -r requirements.yml -p vendor --force
	ansible-galaxy collection install ansible.utils -p vendor --force

lint:
	ansible-lint

cleanup-dns:
	ansible-playbook playbooks/cleanup-dns.yml $(ANSIBLE_EXTRA)

# ─── Operational tooling (BM lab management) ───────────────────────

destroy-full:
	ansible-playbook playbooks/destroy-full.yml $(ANSIBLE_EXTRA)

# DEPLOY_SUITE=caas (default), maas, or bmaas — selects setup/deploy flow.
redeploy-fresh redeploy-continue: SHELL := /bin/bash
redeploy-fresh:
	@echo "=== Destroying all infrastructure ==="
	-$(MAKE) destroy-full
	@sync
	@echo "=== Starting deploy (DEPLOY_SUITE=$(DEPLOY_SUITE)) ==="
	@set -o pipefail; DEPLOY_SUITE=$(DEPLOY_SUITE) \
		scripts/redeploy-server.sh --fresh \
		"$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

redeploy-continue:
	@set -o pipefail; DEPLOY_SUITE=$(DEPLOY_SUITE) \
		scripts/redeploy-server.sh \
		"$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

deploy-jump:
	@scripts/deploy-jump.sh

disk-setup:
	ansible-playbook playbooks/disk-setup.yml $(ANSIBLE_EXTRA)

access-doc:
	ansible-playbook playbooks/access-doc.yml $(ANSIBLE_EXTRA)

post-install:
	ansible-playbook playbooks/post-install.yml $(ANSIBLE_EXTRA)

health-check:
	scripts/health-check.sh

# ─── .PHONY ────────────────────────────────────────────────────────

.PHONY: deploy \
        deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac _check-osac-images \
        setup-caas deploy-caas setup-vmaas gather-vmaas destroy-vmaas \
        test-caas-networking test-caas_networking test-maas-networking test-maas_networking \
        prepare-suite deploy-suite \
        destroy-osac destroy-ocp destroy-infra destroy-setup destroy-full destroy-suite \
        destroy-caas force-destroy-caas \
        setup-maas deploy-maas destroy-maas force-destroy-maas \
        gather-infra gather-mgmt-cluster gather-caas \
        setup-bmc setup-bmaas deploy-bmaas destroy-bmaas gather-bmaas \
        setup-full gather-full \
        install-osac prep-osac run-osac-setup \
        connectivity vendor-update lint cleanup-dns \
        redeploy-fresh redeploy-continue deploy-jump disk-setup access-doc post-install health-check
