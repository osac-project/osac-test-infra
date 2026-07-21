.PHONY: deploy deploy-fast setup deploy-lab deploy-ocp deploy-ocp-snapshot deploy-osac \
       restore-ocp-snapshot snapshot-refresh prep-snapshot-refresh \
       run-snapshot-refresh post-snapshot-refresh \
       setup-caas deploy-caas \
       deploy-vmaas deploy-bmaas \
       destroy destroy-osac destroy-ocp destroy-caas destroy-vmaas destroy-bmaas \
       connectivity prep-osac run-osac-setup post-osac vendor-update lint \
       gather gather-lab gather-caas \
       cleanup-dns

EXTRA_VARS ?=
ANSIBLE_EXTRA = $(if $(EXTRA_VARS),-e '$(EXTRA_VARS)')

# Full shared pipeline
deploy: deploy-lab deploy-ocp deploy-osac

setup:
	ansible-playbook playbooks/setup.yml $(ANSIBLE_EXTRA)

deploy-lab:
	ansible-playbook playbooks/deploy-lab.yml $(ANSIBLE_EXTRA)

connectivity:
	ansible-playbook playbooks/connectivity-lab.yml $(ANSIBLE_EXTRA)

deploy-ocp:
	ansible-playbook playbooks/setup-ocp.yml $(ANSIBLE_EXTRA)
	ansible-playbook playbooks/install-ocp.yml $(ANSIBLE_EXTRA)

deploy-osac: prep-osac run-osac-setup post-osac

prep-osac:
	ansible-playbook playbooks/prep-osac.yml $(ANSIBLE_EXTRA)

run-osac-setup:
	@echo "=== Rebuilding Helm chart dependencies ==="
	cd /opt/osac-installer && helm dependency update charts/osac/
	@echo "=== Running OSAC setup.sh with live output ==="
	@for attempt in $$(seq 1 10); do \
		echo "--- Attempt $$attempt of 10 ---"; \
		cd /opt/osac-installer && source /tmp/osac-setup.env && ./scripts/setup.sh && break; \
		if [ $$attempt -eq 10 ]; then echo "setup.sh failed after 10 attempts"; exit 1; fi; \
		echo "setup.sh failed, retrying in 3 minutes..."; sleep 180; \
	done

post-osac:
	ansible-playbook playbooks/post-osac.yml $(ANSIBLE_EXTRA)

# Per-flow targets — run after deploy
setup-caas:
	ansible-playbook playbooks/setup-caas.yml $(ANSIBLE_EXTRA)

deploy-caas:
	ansible-playbook playbooks/deploy-caas.yml $(ANSIBLE_EXTRA)

deploy-vmaas:
	@echo "VMaaS flow is not yet implemented"

deploy-bmaas:
	@echo "BMaaS flow is not yet implemented"

# Snapshot-based fast deployment
deploy-fast: deploy-lab deploy-ocp-snapshot

deploy-ocp-snapshot: restore-ocp-snapshot snapshot-refresh

restore-ocp-snapshot:
	ansible-playbook playbooks/restore-ocp-snapshot.yml $(ANSIBLE_EXTRA)

snapshot-refresh: prep-snapshot-refresh run-snapshot-refresh post-snapshot-refresh

prep-snapshot-refresh:
	ansible-playbook playbooks/prep-snapshot-refresh.yml $(ANSIBLE_EXTRA)

run-snapshot-refresh:
	@echo "=== Running OSAC refresh with live output ==="
	cd /opt/osac-installer && \
		KUBECONFIG=/root/.kube/config \
		VALUES_FILE=$(or $(SNAPSHOT_VALUES_FILE),values/caas-ci/values.yaml) \
		INSTALLER_NAMESPACE=$(or $(SNAPSHOT_NAMESPACE),osac-e2e-ci) \
		python3 -u scripts/refresh-after-snapshot.py

post-snapshot-refresh:
	ansible-playbook playbooks/post-snapshot-refresh.yml $(ANSIBLE_EXTRA)

# Destroy targets
destroy:
	ansible-playbook playbooks/destroy.yml $(ANSIBLE_EXTRA)

destroy-osac:
	@echo "=== Tearing down OSAC ==="
	cd /opt/osac-installer && source /tmp/osac-setup.env && \
		EXTRA_SERVICES=true ./scripts/teardown.sh || true
	oc delete namespace osac-devel shared --ignore-not-found --wait=false 2>/dev/null || true
	rm -rf /opt/osac-installer
	rm -f /tmp/osac-setup.env

destroy-ocp:
	ansible-playbook playbooks/destroy-ocp.yml $(ANSIBLE_EXTRA)

destroy-caas:
	@echo "CaaS teardown is not yet implemented"

destroy-vmaas:
	@echo "VMaaS teardown is not yet implemented"

destroy-bmaas:
	@echo "BMaaS teardown is not yet implemented"

# Utilities
vendor-update:
	rm -rf vendor/ansible_collections
	ansible-galaxy collection install -r requirements.yml -p vendor --force
	ansible-galaxy collection install ansible.utils -p vendor --force

lint:
	ansible-lint

gather:
	ansible-playbook playbooks/gather.yml $(ANSIBLE_EXTRA)

gather-lab:
	ansible-playbook playbooks/gather-lab.yml $(ANSIBLE_EXTRA)

gather-caas:
	ansible-playbook playbooks/gather-caas.yml $(ANSIBLE_EXTRA)

cleanup-dns:
	ansible-playbook playbooks/cleanup-dns.yml $(ANSIBLE_EXTRA)
