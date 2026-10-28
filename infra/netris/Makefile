# ─── Variables ──────────────────────────────────────────────────────

OSAC_DEPLOY_MODE ?= fresh
EXTRA_VARS ?=
ANSIBLE_EXTRA = $(if $(EXTRA_VARS),-e '$(EXTRA_VARS)') -e suite=$(SUITE) -e bare_metal_services=$(BARE_METAL_SERVICES) -e osac_deploy_mode=$(OSAC_DEPLOY_MODE) -e osac_profile=$(OSAC_PROFILE) -e osac_values_file=values/$(OSAC_PROFILE)/instance.yaml
ENV_INFRA := .env.infra

# Suite selects the setup and deploy flow; every suite gets the configured SNO disk size.
# SUITE=caas (default) — setup-caas / deploy-caas.
# SUITE=maas — setup-maas / deploy-maas.
SUITE ?= caas
BASE_SUITE = $(patsubst %_networking,%,$(SUITE))

OSAC_NAMESPACE ?= osac-e2e-ci
KUBECONFIG ?= /root/.kube/config
OSAC_VM_KUBECONFIG ?= $(KUBECONFIG)
OSAC_PROFILE ?= $(if $(filter vmaas vmaas_networking,$(SUITE)),cudn-evpn-netris-test,$(if $(filter full stretched_networking,$(SUITE)),full-ci,caas-ci))
OSAC_PULL_SECRET_PATH ?= /root/pull-secret
_KUBECONFIG_ABS = $(abspath $(KUBECONFIG))
_OSAC_VM_KUBECONFIG_ABS = $(abspath $(OSAC_VM_KUBECONFIG))

# VMaaS selectors and full-ci configure the OCP host as an EVPN VTEP; legacy suites keep their established root path.
EVPN_SUITES = vmaas vmaas_networking stretched_networking full
OCP_KUBECONFIG = $(if $(filter $(EVPN_SUITES),$(SUITE)),$(_KUBECONFIG_ABS),/root/.kube/config)
VMAAS_UNDERLAY_EXTRA = $(if $(filter $(EVPN_SUITES),$(SUITE)),-e ocp_underlay_enabled=true,)
OCP_ANSIBLE_EXTRA = $(ANSIBLE_EXTRA) -e 'ocp_evpn_kubeconfig_path=$(OCP_KUBECONFIG)' $(VMAAS_UNDERLAY_EXTRA)
EVPN_VALUES_FILE = $(abspath .evpn-values.yaml)
VMAAS_EVPN_VALUES_EXTRA = $(if $(filter $(EVPN_SUITES),$(SUITE)),-f $(EVPN_VALUES_FILE))
OSAC_TEST_TENANT_NAME ?= tenant1
CAAS_BAREMETAL_INSTANCE_TYPE ?= ci-worker
CAAS_DISK_IMAGE_NAME ?= rhcos-4-22-amd64
CAAS_DISK_IMAGE_SOURCE_REF ?= oci://quay.io/osac-project/rhcos:4.22.0
CAAS_WORKER_COUNT ?= 1
# CaaS and MaaS always provision workers through BMaaS.
CAAS_WORKER_VCPU ?= 4
CAAS_WORKER_MEMORY_MB ?= 16384
# BMaaS, CaaS, and MaaS need the BMC service infrastructure.
BARE_METAL_SERVICES ?= $(if $(filter caas maas bmaas stretched full,$(BASE_SUITE)),true,false)

# ─── Convenience targets ───────────────────────────────────────────
#
#   make deploy       — full fresh install (default)
#   make deploy-fast  — snapshot restore + OSAC refresh (~25 min)
#
# Both run: deploy-infra → deploy-ocp → deploy-osac
# The difference is OSAC_DEPLOY_MODE, which controls:
#   - deploy-ocp: which snapshot flavor to use
#   - deploy-osac: fresh Helm install vs snapshot refresh

deploy: deploy-infra deploy-ocp deploy-osac

deploy-fast: deploy-infra deploy-ocp deploy-osac
deploy-fast: OSAC_DEPLOY_MODE = snapshot

# ─── Contract targets ──────────────────────────────────────────────
#
# Called by the root Makefile (make -C infra/netris <target>).
# See infra/contract.md for the interface spec.

deploy-setup:
	ansible-playbook playbooks/deploy-setup.yml $(ANSIBLE_EXTRA)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra:
	ansible-playbook playbooks/deploy-infra.yml $(ANSIBLE_EXTRA) $(VMAAS_UNDERLAY_EXTRA)

deploy-ocp:
	@rm -f .evpn-values.yaml
	ansible-playbook playbooks/deploy-ocp.yml -e suite=$(SUITE) $(OCP_ANSIBLE_EXTRA)

deploy-osac:
	@if [ "$(OSAC_DEPLOY_MODE)" = "snapshot" ]; then \
		$(MAKE) refresh-osac OSAC_DEPLOY_MODE=snapshot; \
	else \
		$(MAKE) install-osac; \
	fi
	@printf '%s\n' \
		'KUBECONFIG=$(OCP_KUBECONFIG)' \
		'OSAC_NAMESPACE=$(OSAC_NAMESPACE)' \
		'OSAC_VM_KUBECONFIG=$(if $(filter vmaas vmaas_networking stretched_networking full,$(SUITE)),$(_OSAC_VM_KUBECONFIG_ABS),$(OCP_KUBECONFIG))' \
		'OSAC_PULL_SECRET_PATH=$(OSAC_PULL_SECRET_PATH)' \
		> $(ENV_INFRA)

# VMaaS setup/gather is read-only: tenant resources remain owned by the suite fixtures.
setup-vmaas: SUITE = vmaas
setup-vmaas:
	ansible-playbook playbooks/setup-vmaas.yml $(OCP_ANSIBLE_EXTRA)

gather-vmaas: SUITE = vmaas
gather-vmaas:
	ansible-playbook playbooks/gather-infra.yml $(OCP_ANSIBLE_EXTRA)

destroy-vmaas:
	@echo "VMaaS setup creates no tenant resources; cluster prerequisites are removed with destroy-ocp."

setup-vmaas_networking: SUITE = vmaas_networking
setup-vmaas_networking:
	ansible-playbook playbooks/setup-vmaas.yml $(OCP_ANSIBLE_EXTRA)

# Stretched networking provisions the full profile and needs both BMaaS and VMaaS service prerequisites.
setup-stretched_networking: SUITE = stretched_networking
setup-stretched_networking:
	$(MAKE) setup-full

gather-vmaas_networking: SUITE = vmaas_networking
gather-vmaas_networking:
	ansible-playbook playbooks/gather-infra.yml $(OCP_ANSIBLE_EXTRA)

destroy-vmaas_networking:
	@echo "VMaaS networking setup creates no tenant resources; cluster prerequisites are removed with destroy-ocp."

destroy-stretched_networking:
	@echo "Stretched networking tests own and clean up their tenant resources; full-profile prerequisites are removed with destroy-ocp."

gather-stretched_networking: SUITE = stretched_networking
gather-stretched_networking:
	$(MAKE) gather-full

setup-caas_networking: setup-caas
setup-maas_networking: setup-maas

test-caas-networking test-caas_networking:
	$(MAKE) -C ../.. run-tests INFRA=netris SUITE=caas_networking

test-maas-networking test-maas_networking:
	$(MAKE) -C ../.. run-tests INFRA=netris SUITE=maas_networking

# E2E setup stops before tenant creation; pytest fixtures provide test tenants.
prepare-suite:
	@case "$(SUITE)" in caas|caas_networking|maas|maas_networking|bmaas|bmaas_networking|vmaas|vmaas_networking|stretched_networking|full) ;; *) echo "Unsupported suite: $(SUITE)" >&2; exit 2;; esac
	$(MAKE) setup-$(SUITE) SUITE=$(SUITE)

# Persistent deployment playbooks prepare tenants and Netris networking before workloads.
SUITE_STAGE ?= all
deploy-suite:
	@case "$(BASE_SUITE)" in caas|maas|bmaas) ;; *) echo "Unsupported suite: $(SUITE)" >&2; exit 2;; esac
	@case "$(SUITE_STAGE)" in \
		all) $(MAKE) prepare-suite SUITE=$(BASE_SUITE) && $(MAKE) deploy-$(BASE_SUITE) SUITE=$(BASE_SUITE) ;; \
		setup) $(MAKE) setup-$(BASE_SUITE) SUITE=$(BASE_SUITE) ;; \
		workload) $(MAKE) deploy-$(BASE_SUITE) SUITE=$(BASE_SUITE) ;; \
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
	cd /opt/osac/osac-installer && KUBECONFIG=$(OCP_KUBECONFIG) make uninstall \
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
	$(MAKE) setup-caas SUITE=caas
	$(MAKE) setup-bmaas SUITE=bmaas
	$(MAKE) setup-vmaas SUITE=vmaas OSAC_PROFILE=$(OSAC_PROFILE)

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
	$(MAKE) gather-infra

# bmaas_networking shares infra with bmaas (same BMHs, same sushy-tools)
setup-bmaas_networking: setup-bmaas
destroy-bmaas_networking: destroy-bmaas
gather-bmaas_networking: gather-bmaas

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
install-osac: prep-osac run-osac-setup post-osac

prep-osac:
	ansible-playbook playbooks/prep-osac.yml $(OCP_ANSIBLE_EXTRA)

run-osac-setup:
	@if [ -n "$(VMAAS_EVPN_VALUES_EXTRA)" ] && [ ! -s "$(EVPN_VALUES_FILE)" ]; then \
		echo "Missing $(EVPN_VALUES_FILE); run make deploy-ocp SUITE=$(SUITE) first." >&2; \
		exit 1; \
	fi
	@echo "=== Installing OSAC infrastructure via Helm (make install-infra) ==="
	cd /opt/osac/osac-installer && KUBECONFIG=$(OCP_KUBECONFIG) make install-infra \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE) \
		INFRA_VALUES_EXTRA="$(VMAAS_EVPN_VALUES_EXTRA)"
	@echo "=== Creating external Netris credentials ==="
	ansible-playbook playbooks/create-netris-credentials.yml $(OCP_ANSIBLE_EXTRA)
	@echo "=== Installing OSAC application via Helm (make install-osac) ==="
	cd /opt/osac/osac-installer && KUBECONFIG=$(OCP_KUBECONFIG) make install-osac \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE)

post-osac:
	ansible-playbook playbooks/post-osac.yml $(OCP_ANSIBLE_EXTRA)

# Snapshot refresh: clone mono-repo, patch values, run refresh-after-snapshot.py
# post-refresh-osac → patch-osac-refresh (incl. AAP project pin sync + CaC)
refresh-osac: prep-refresh-osac run-refresh-osac post-refresh-osac

prep-refresh-osac:
	ansible-playbook playbooks/prep-refresh-osac.yml $(OCP_ANSIBLE_EXTRA)

run-refresh-osac:
	@echo "=== Running OSAC refresh ==="
	cd /opt/osac/osac-installer && \
		KUBECONFIG=$(OCP_KUBECONFIG) \
		VALUES_FILE=values/$(OSAC_PROFILE)/instance.yaml \
		INSTALLER_NAMESPACE=$(OSAC_NAMESPACE) \
		python3 -u scripts/refresh-after-snapshot.py

post-refresh-osac:
	ansible-playbook playbooks/post-refresh-osac.yml $(OCP_ANSIBLE_EXTRA)

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

# OSAC_DEPLOY_MODE=fresh (default) or snapshot — same meaning as deploy / deploy-fast.
# SUITE=caas (default), maas, or bmaas — selects setup/deploy flow.
redeploy-fresh redeploy-continue: SHELL := /bin/bash
redeploy-fresh:
	@echo "=== Destroying all infrastructure ==="
	-$(MAKE) destroy-full
	@sync
	@echo "=== Starting deploy (SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE)) ==="
	@set -o pipefail; SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) \
		scripts/redeploy-server.sh --fresh \
		$(if $(filter snapshot,$(OSAC_DEPLOY_MODE)),--snapshot) \
		"$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

redeploy-continue:
	@set -o pipefail; SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) \
		scripts/redeploy-server.sh \
		$(if $(filter snapshot,$(OSAC_DEPLOY_MODE)),--snapshot) \
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

.PHONY: deploy deploy-fast \
        deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac \
        setup-caas deploy-caas setup-vmaas gather-vmaas destroy-vmaas \
		setup-vmaas_networking gather-vmaas_networking destroy-vmaas_networking \
		setup-stretched_networking gather-stretched_networking destroy-stretched_networking \
        setup-caas setup-caas_networking setup-maas_networking deploy-caas \
        test-caas-networking test-caas_networking test-maas-networking test-maas_networking \
        prepare-suite deploy-suite \
		destroy-osac destroy-ocp destroy-infra destroy-setup destroy-full \
		destroy-caas force-destroy-caas \
        setup-maas deploy-maas destroy-maas force-destroy-maas \
        gather-infra gather-mgmt-cluster gather-caas \
        setup-bmc setup-bmaas deploy-bmaas destroy-bmaas gather-bmaas \
        setup-bmaas_networking destroy-bmaas_networking gather-bmaas_networking \
        install-osac prep-osac run-osac-setup post-osac \
        refresh-osac prep-refresh-osac run-refresh-osac post-refresh-osac \
        connectivity vendor-update lint cleanup-dns \
        redeploy-fresh redeploy-continue deploy-jump disk-setup access-doc post-install health-check
