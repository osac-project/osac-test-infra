# ─── Variables ──────────────────────────────────────────────────────

OSAC_DEPLOY_MODE ?= fresh
EXTRA_VARS ?=
ANSIBLE_EXTRA = $(if $(EXTRA_VARS),-e '$(EXTRA_VARS)') -e osac_deploy_mode=$(OSAC_DEPLOY_MODE) -e osac_profile=$(OSAC_PROFILE) -e osac_values_file=values/$(OSAC_PROFILE)/instance.yaml
ENV_INFRA := .env.infra

# Suite selects CaaS vs MaaS for restore disk grow + redeploy-server.sh flow.
# SUITE=caas (default) — setup-caas / deploy-caas.
# SUITE=maas — grow h00 to ocp_snapshot_disk_gb; setup-maas / deploy-maas.
SUITE ?= caas

OSAC_NAMESPACE ?= osac-e2e-ci
OSAC_PROFILE ?= caas-ci
OSAC_PULL_SECRET_PATH ?= /root/pull-secret

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
	ansible-playbook playbooks/deploy-infra.yml $(ANSIBLE_EXTRA)

deploy-ocp:
	ansible-playbook playbooks/deploy-ocp.yml -e suite=$(SUITE) $(ANSIBLE_EXTRA)

deploy-osac:
	@if [ "$(OSAC_DEPLOY_MODE)" = "snapshot" ]; then \
		$(MAKE) refresh-osac OSAC_DEPLOY_MODE=snapshot; \
	else \
		$(MAKE) install-osac; \
	fi
	@printf '%s\n' \
		'KUBECONFIG=/root/.kube/config' \
		'OSAC_NAMESPACE=$(OSAC_NAMESPACE)' \
		'OSAC_VM_KUBECONFIG=/root/.kube/config' \
		'OSAC_PULL_SECRET_PATH=$(OSAC_PULL_SECRET_PATH)' \
		> $(ENV_INFRA)

setup-caas:
	ansible-playbook playbooks/setup-caas.yml $(ANSIBLE_EXTRA)

deploy-caas:
	ansible-playbook playbooks/deploy-caas.yml $(ANSIBLE_EXTRA)

destroy-osac:
	@echo "=== Tearing down OSAC ==="
	cd /opt/osac/osac-installer && make uninstall \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE) 2>/dev/null || true
	rm -rf /opt/osac
	@rm -f $(ENV_INFRA)

destroy-ocp:
	ansible-playbook playbooks/destroy-ocp.yml $(ANSIBLE_EXTRA)

destroy-infra:
	ansible-playbook playbooks/destroy-infra.yml $(ANSIBLE_EXTRA)
	@rm -f $(ENV_INFRA)

destroy-setup:
	ansible-playbook playbooks/destroy-setup.yml $(ANSIBLE_EXTRA)

destroy-caas:
	ansible-playbook playbooks/destroy-caas.yml $(ANSIBLE_EXTRA)

force-destroy-caas:
	ansible-playbook playbooks/force-destroy-caas.yml $(ANSIBLE_EXTRA)

gather-infra:
	ansible-playbook playbooks/gather-infra.yml $(ANSIBLE_EXTRA)

gather-mgmt-cluster:
	ansible-playbook playbooks/gather-mgmt-cluster.yml $(ANSIBLE_EXTRA)

gather-caas:
	ansible-playbook playbooks/gather-caas.yml $(ANSIBLE_EXTRA)

setup-bmc:
	ansible-playbook playbooks/setup-bmc.yml $(ANSIBLE_EXTRA)

setup-bmaas:
	ansible-playbook playbooks/setup-bmaas.yml $(ANSIBLE_EXTRA)

destroy-bmaas:
	ansible-playbook playbooks/destroy-bmaas.yml $(ANSIBLE_EXTRA)

gather-bmaas:
	ansible-playbook playbooks/gather-bmaas.yml $(ANSIBLE_EXTRA)

# bmaas_networking shares infra with bmaas (same BMHs, same sushy-tools)
setup-bmaas_networking: setup-bmaas
destroy-bmaas_networking: destroy-bmaas
gather-bmaas_networking: gather-bmaas

# ---------------------------------------------------------------------------
# MaaS — thin wrappers over CaaS playbooks with MaaS-specific variable overrides
# Override any default with MAAS_CLUSTER_NAME=..., MAAS_HOST_TYPE_ID=..., etc.
# ---------------------------------------------------------------------------
MAAS_CLUSTER_NAME     ?= maas-cluster
MAAS_HOST_TYPE_ID     ?= g5
MAAS_CLUSTER_TEMPLATE ?= osac.templates.ocp_4_20_ai_maas
MAAS_CATALOG_ITEM     ?= maas-ocp
# Discovery VM sizing for setup-maas only (CaaS keeps inventory defaults: 4 vCPU / 16GiB).
# Default MaaS: 10 vCPU / 20GiB for h02–h03; h01 stays at 4 vCPU / 16GiB (unused for MaaS workers).
MAAS_DISCOVERY_VCPU      ?= 10
MAAS_DISCOVERY_MEMORY_MB ?= 20480
MAAS_DISCOVERY_VCPU_OVERRIDES ?= {"hgx-pod00-su0-h01":4}
MAAS_DISCOVERY_MEMORY_MB_OVERRIDES ?= {"hgx-pod00-su0-h01":16384}
# Only these inventory hostnames get resource_class=g5 (and approval). Empty = all agents.
# Comma-separated. Keeps h01 out of the MaaS claim pool; setup-caas still labels all as ci-worker.
MAAS_LABEL_HOSTNAMES ?= hgx-pod00-su0-h02,hgx-pod00-su0-h03
# Template -p flags for osac create (deploy-maas only). Comma-separated key=value.
MAAS_CLUSTER_PARAMS   ?= enable_maas=true,enable_keycloak=true,enable_observability=true,enable_dashboard=true
MAAS_PARAMS_JSON = $(shell python3 -c 'import json; print(json.dumps([p.strip() for p in """$(MAAS_CLUSTER_PARAMS)""".split(",") if p.strip()]))')
MAAS_LABEL_HOSTNAMES_JSON = $(shell python3 -c 'import json; print(json.dumps([h.strip() for h in """$(MAAS_LABEL_HOSTNAMES)""".split(",") if h.strip()]))')

MAAS_EXTRA = \
  -e 'caas_cluster_name=$(MAAS_CLUSTER_NAME)' \
  -e 'caas_host_type_id=$(MAAS_HOST_TYPE_ID)' \
  -e 'caas_host_type_title=MaaS Node' \
  -e 'caas_host_type_description=Bare-metal nodes for MaaS testing' \
  -e 'caas_cluster_template=$(MAAS_CLUSTER_TEMPLATE)' \
  -e 'caas_catalog_item=$(MAAS_CATALOG_ITEM)' \
  -e 'caas_catalog_item_title=MaaS OCP' \
  -e 'caas_catalog_item_description=OCP cluster for MaaS testing' \
  -e 'caas_discovery_vcpu=$(MAAS_DISCOVERY_VCPU)' \
  -e 'caas_discovery_memory_mb=$(MAAS_DISCOVERY_MEMORY_MB)' \
  -e '{"caas_discovery_vcpu_overrides":$(MAAS_DISCOVERY_VCPU_OVERRIDES)}' \
  -e '{"caas_discovery_memory_mb_overrides":$(MAAS_DISCOVERY_MEMORY_MB_OVERRIDES)}' \
  -e '{"caas_resource_class_hostnames":$(MAAS_LABEL_HOSTNAMES_JSON)}'

setup-maas:
	ansible-playbook playbooks/setup-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

deploy-maas:
	ansible-playbook playbooks/deploy-caas.yml $(MAAS_EXTRA) -e '{"caas_cluster_params":$(MAAS_PARAMS_JSON)}' $(ANSIBLE_EXTRA)

destroy-maas:
	ansible-playbook playbooks/destroy-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

force-destroy-maas:
	ansible-playbook playbooks/force-destroy-caas.yml $(MAAS_EXTRA) $(ANSIBLE_EXTRA)

# ─── Internal sub-targets (deploy-osac implementation) ─────────────

# Fresh install: clone mono-repo, patch Helm values, run make install
install-osac: prep-osac run-osac-setup post-osac

prep-osac:
	ansible-playbook playbooks/prep-osac.yml $(ANSIBLE_EXTRA)

run-osac-setup:
	@echo "=== Installing OSAC via Helm (make install) ==="
	cd /opt/osac/osac-installer && KUBECONFIG=/root/.kube/config make install \
		PLATFORM=openshift \
		PROFILE=$(OSAC_PROFILE) \
		NS=$(OSAC_NAMESPACE)

post-osac:
	ansible-playbook playbooks/post-osac.yml $(ANSIBLE_EXTRA)

# Snapshot refresh: clone mono-repo, patch values, run refresh-after-snapshot.py
# post-refresh-osac → patch-osac-refresh (incl. AAP project pin sync + CaC)
refresh-osac: prep-refresh-osac run-refresh-osac post-refresh-osac

prep-refresh-osac:
	ansible-playbook playbooks/prep-refresh-osac.yml $(ANSIBLE_EXTRA)

run-refresh-osac:
	@echo "=== Running OSAC refresh ==="
	cd /opt/osac/osac-installer && \
		KUBECONFIG=/root/.kube/config \
		VALUES_FILE=values/$(OSAC_PROFILE)/instance.yaml \
		INSTALLER_NAMESPACE=$(OSAC_NAMESPACE) \
		python3 -u scripts/refresh-after-snapshot.py

post-refresh-osac:
	ansible-playbook playbooks/post-refresh-osac.yml $(ANSIBLE_EXTRA)

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
# SUITE=caas (default) or maas — selects setup/deploy flow in redeploy-server.sh.
redeploy-fresh:
	@echo "=== Destroying all infrastructure ==="
	-$(MAKE) destroy-full
	@sync
	@echo "=== Starting deploy (SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE)) ==="
	SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) \
		scripts/redeploy-server.sh --fresh \
		$(if $(filter snapshot,$(OSAC_DEPLOY_MODE)),--snapshot) \
		"$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

redeploy-continue:
	SUITE=$(SUITE) OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) \
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
        setup-caas deploy-caas \
		destroy-osac destroy-ocp destroy-infra destroy-setup destroy-full \
		destroy-caas force-destroy-caas \
        setup-maas deploy-maas destroy-maas force-destroy-maas \
        gather-infra gather-mgmt-cluster gather-caas \
        setup-bmc setup-bmaas destroy-bmaas gather-bmaas \
        setup-bmaas_networking destroy-bmaas_networking gather-bmaas_networking \
        install-osac prep-osac run-osac-setup post-osac \
        refresh-osac prep-refresh-osac run-refresh-osac post-refresh-osac \
        connectivity vendor-update lint cleanup-dns \
        redeploy-fresh redeploy-continue deploy-jump disk-setup access-doc post-install health-check
