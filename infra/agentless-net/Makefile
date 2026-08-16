# ─── Variables ──────────────────────────────────────────────────────

OSAC_DEPLOY_MODE ?= fresh
EXTRA_VARS ?=
ENV_INFRA := .env.infra
SCRIPTS := scripts

MGMT_CLONE_NAME ?= agentless-lab-mgmt
OSAC_NAMESPACE ?= osac-e2e-ci
OSAC_VALUES_FILE ?= values/caas-ci/values.yaml
OSAC_PULL_SECRET_PATH ?= /root/pull-secret

export MGMT_CLONE_NAME OSAC_NAMESPACE OSAC_VALUES_FILE OSAC_PULL_SECRET_PATH

# ─── Contract targets ──────────────────────────────────────────────
#
# Called individually from the workflow or command line.
# Order: setup-infra → deploy-ocp → deploy-osac → deploy-fabric → setup-caas → test-e2e

setup-infra:
	@echo "=== Installing prerequisites ==="
	$(SCRIPTS)/setup-infra.sh

deploy-ocp:
	@echo "=== Booting SNO from snapshot via cluster-tool, fixing DNS ==="
	$(SCRIPTS)/deploy-ocp.sh

deploy-osac:
	@echo "=== Installing OSAC via Helm ==="
	$(SCRIPTS)/deploy-osac.sh
	@printf '%s\n' \
		'KUBECONFIG=$(HOME)/.kube/$(MGMT_CLONE_NAME).kubeconfig' \
		'OSAC_NAMESPACE=$(OSAC_NAMESPACE)' \
		'OSAC_PULL_SECRET_PATH=$(OSAC_PULL_SECRET_PATH)' \
		> $(ENV_INFRA)

deploy-fabric:
	@echo "=== Deploying containerlab topology, configuring switches, BGP ==="
	$(SCRIPTS)/deploy-fabric.sh

setup-caas:
	@echo "=== Registering host type, creating InfraEnv, booting VMs, approving agents ==="
	$(SCRIPTS)/setup-caas.sh

test-e2e:
	@echo "=== Running E2E tests ==="
	$(SCRIPTS)/test-e2e.sh

destroy-osac:
	@echo "=== Tearing down OSAC ==="
	$(SCRIPTS)/destroy-osac.sh
	@rm -f $(ENV_INFRA)

destroy-ocp:
	@echo "=== Tearing down OCP ==="
	$(SCRIPTS)/destroy-ocp.sh
	@rm -f $(ENV_INFRA)

destroy-fabric:
	@echo "=== Tearing down containerlab fabric ==="
	$(SCRIPTS)/destroy-fabric.sh

destroy-caas:
	@echo "=== Tearing down CaaS VMs ==="
	$(SCRIPTS)/destroy-caas.sh

gather-infra:
	@echo "=== Gathering infra diagnostics ==="
	$(SCRIPTS)/gather-infra.sh || true

gather-e2e:
	@echo "=== Gathering E2E diagnostics ==="
	$(SCRIPTS)/gather-e2e.sh || true

# ─── .PHONY ────────────────────────────────────────────────────────

.PHONY: setup-infra deploy-ocp deploy-osac deploy-fabric \
        setup-caas test-e2e \
        destroy-osac destroy-ocp destroy-fabric destroy-caas \
        gather-infra gather-e2e
