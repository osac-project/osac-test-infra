# ─── Variables ──────────────────────────────────────────────────────

OSAC_DEPLOY_MODE ?= fresh
EXTRA_VARS ?=
ANSIBLE_EXTRA = $(if $(EXTRA_VARS),-e '$(EXTRA_VARS)') -e osac_deploy_mode=$(OSAC_DEPLOY_MODE) -e osac_values_file=$(OSAC_VALUES_FILE)
ENV_INFRA := .env.infra

OSAC_NAMESPACE ?= osac-e2e-ci
OSAC_VALUES_FILE ?= values/caas-ci/values.yaml
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

setup-infra:
	ansible-playbook playbooks/setup-infra.yml $(ANSIBLE_EXTRA)

deploy-infra:
	ansible-playbook playbooks/deploy-infra.yml $(ANSIBLE_EXTRA)

deploy-ocp:
	ansible-playbook playbooks/deploy-ocp.yml $(ANSIBLE_EXTRA)

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
		INSTALLER_NAMESPACE=$(OSAC_NAMESPACE) \
		VALUES_FILE=$(OSAC_VALUES_FILE) 2>/dev/null || true
	rm -rf /opt/osac
	@rm -f $(ENV_INFRA)

destroy-ocp:
	ansible-playbook playbooks/destroy-ocp.yml $(ANSIBLE_EXTRA)

destroy-infra:
	ansible-playbook playbooks/destroy-infra.yml $(ANSIBLE_EXTRA)
	@rm -f $(ENV_INFRA)

destroy-caas:
	ansible-playbook playbooks/destroy-caas.yml $(ANSIBLE_EXTRA)

gather-infra:
	ansible-playbook playbooks/gather-infra.yml $(ANSIBLE_EXTRA)

gather-mgmt-cluster:
	ansible-playbook playbooks/gather-mgmt-cluster.yml $(ANSIBLE_EXTRA)

gather-caas:
	ansible-playbook playbooks/gather-caas.yml $(ANSIBLE_EXTRA)

deploy-lab:
	ansible-playbook playbooks/deploy-lab.yml $(ANSIBLE_EXTRA)

prep-bmaas:
	ansible-playbook playbooks/prep-bmaas.yml $(ANSIBLE_EXTRA)

setup-bmaas:
	ansible-playbook playbooks/setup-bmaas.yml $(ANSIBLE_EXTRA)

destroy-bmaas:
	ansible-playbook playbooks/destroy-bmaas.yml $(ANSIBLE_EXTRA)

gather-bmaas:
	ansible-playbook playbooks/gather-bmaas.yml $(ANSIBLE_EXTRA)

# ─── Internal sub-targets (deploy-osac implementation) ─────────────

# Fresh install: clone mono-repo, patch Helm values, run make install
install-osac: prep-osac run-osac-setup post-osac

prep-osac:
	ansible-playbook playbooks/prep-osac.yml $(ANSIBLE_EXTRA)

run-osac-setup:
	@echo "=== Installing OSAC via Helm (make install) ==="
	cd /opt/osac/osac-installer && make install \
		INSTALLER_NAMESPACE=$(OSAC_NAMESPACE) \
		VALUES_FILE=$(OSAC_VALUES_FILE)

post-osac:
	ansible-playbook playbooks/post-osac.yml $(ANSIBLE_EXTRA)

# Snapshot refresh: clone mono-repo, patch values, run refresh-after-snapshot.py
refresh-osac: prep-refresh-osac run-refresh-osac post-refresh-osac

prep-refresh-osac:
	ansible-playbook playbooks/prep-refresh-osac.yml $(ANSIBLE_EXTRA)

run-refresh-osac:
	@echo "=== Running OSAC refresh ==="
	cd /opt/osac/osac-installer && \
		KUBECONFIG=/root/.kube/config \
		VALUES_FILE=$(OSAC_VALUES_FILE) \
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

redeploy-fresh:
	@echo "=== Destroying all infrastructure ==="
	-$(MAKE) destroy-full
	@sync
	@echo "=== Starting fresh deploy ==="
	scripts/redeploy-server.sh --fresh "$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

redeploy-continue:
	scripts/redeploy-server.sh "$(EXTRA_VARS)" 2>&1 | tee -a /root/deploy.log

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
        setup-infra deploy-infra deploy-ocp deploy-osac \
        setup-caas deploy-caas \
        destroy-osac destroy-ocp destroy-infra destroy-caas destroy-full \
        gather-infra gather-mgmt-cluster gather-caas \
        deploy-lab prep-bmaas setup-bmaas destroy-bmaas gather-bmaas \
        install-osac prep-osac run-osac-setup post-osac \
        refresh-osac prep-refresh-osac run-refresh-osac post-refresh-osac \
        connectivity vendor-update lint cleanup-dns \
        redeploy-fresh redeploy-continue deploy-jump disk-setup access-doc post-install health-check
