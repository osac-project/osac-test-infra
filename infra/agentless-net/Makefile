# ─── Variables ──────────────────────────────────────────────────────

EXTRA_VARS ?=
ENV_INFRA := .env.infra
SCRIPTS := scripts

# DEPLOY_SUITE selects the scenario. The profile is derived from it.
# caas (default) keeps the existing CaaS flow. bmaas installs the bmaas-ci profile.
# The root Makefile also forwards KUBECONFIG, OSAC_VM_KUBECONFIG, OSAC_PULL_SECRET_PATH,
# and EXTRA_VARS. This backend records KUBECONFIG and does not consume EXTRA_VARS.
DEPLOY_SUITE ?= caas
ifeq ($(origin OSAC_PROFILE),command line)
$(error OSAC_PROFILE is derived from DEPLOY_SUITE; choose the deployment scenario with DEPLOY_SUITE)
endif
OSAC_PROFILE = $(if $(filter bmaas,$(DEPLOY_SUITE)),bmaas-ci,caas-ci)

MGMT_CLONE_NAME ?= agentless-lab-mgmt
OSAC_NAMESPACE ?= osac-e2e-ci
OSAC_VALUES_FILE ?= values/caas-ci/values.yaml
OSAC_PULL_SECRET_PATH ?= /root/pull-secret
# command line and environment are explicit. The ?= default is not, or it
# would hide the kubeconfig recorded in .mgmt-network.
ifeq ($(origin KUBECONFIG),command line)
OSAC_KUBECONFIG_FROM_CALLER := yes
else ifeq ($(origin KUBECONFIG),environment)
OSAC_KUBECONFIG_FROM_CALLER := yes
else
OSAC_KUBECONFIG_FROM_CALLER :=
endif
KUBECONFIG ?= $(HOME)/.kube/$(MGMT_CLONE_NAME).kubeconfig

export DEPLOY_SUITE OSAC_PROFILE MGMT_CLONE_NAME OSAC_NAMESPACE OSAC_VALUES_FILE OSAC_PULL_SECRET_PATH KUBECONFIG OSAC_KUBECONFIG_FROM_CALLER

# ─── Contract targets ──────────────────────────────────────────────
#
# Called individually from the workflow or command line.
# Root order: deploy-setup → deploy-infra → deploy-ocp → deploy-osac → prepare-suite.
# Fabric attaches to the running management VM, so prepare-suite runs it after OSAC.
# BMaaS: DEPLOY_SUITE=bmaas. The profile is bmaas-ci. prove-bmi is the one-BMI proof
# until the mono-repo has a one-host test selection.

setup-infra:
	@echo "=== Installing prerequisites ==="
	$(SCRIPTS)/setup-infra.sh

deploy-ocp:
	@echo "=== Booting SNO from snapshot via cluster-tool, fixing DNS ==="
	$(SCRIPTS)/deploy-ocp.sh

deploy-osac:
	@echo "=== Installing OSAC via Helm ==="
	$(SCRIPTS)/deploy-osac.sh
	@kc="$$(cat .effective-kubeconfig)"; \
	printf 'KUBECONFIG=%q\nOSAC_NAMESPACE=%s\nOSAC_VM_KUBECONFIG=%q\nOSAC_PULL_SECRET_PATH=%q\n' \
		"$$kc" "$(OSAC_NAMESPACE)" "$$kc" "$(OSAC_PULL_SECRET_PATH)" \
		> $(ENV_INFRA)

deploy-fabric:
	@echo "=== Deploying containerlab topology, configuring switches, BGP ==="
	$(SCRIPTS)/deploy-fabric.sh

setup-caas:
	@echo "=== Registering host type, creating InfraEnv, booting VMs, approving agents ==="
	$(SCRIPTS)/setup-caas.sh

setup-bmaas:
	@echo "=== Registering one virtual BMH and publishing its AgentlessNet inventory ==="
	$(SCRIPTS)/setup-bmaas.sh

prove-bmi:
	@echo "=== Creating one BMI and waiting for its DHCP address ==="
	$(SCRIPTS)/prove-bmi.sh

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

destroy-bmaas:
	@echo "=== Tearing down the proof BMI and virtual BMH ==="
	$(SCRIPTS)/destroy-bmaas.sh

gather-infra:
	@echo "=== Gathering infra diagnostics ==="
	$(SCRIPTS)/gather-infra.sh || true

gather-e2e:
	@echo "=== Gathering E2E diagnostics ==="
	$(SCRIPTS)/gather-e2e.sh || true

gather-bmaas:
	@echo "=== Gathering virtual BMH and BMI diagnostics ==="
	$(SCRIPTS)/gather-bmaas.sh || true

# Root contract names. deploy-infra and destroy-setup have nothing to do on this backend.
deploy-setup: setup-infra

deploy-infra:
	@echo "=== agentless-net has no separate infra provisioning step ==="

destroy-setup:
	@echo "=== agentless-net setup leaves nothing to remove ==="

destroy-infra: destroy-fabric

prepare-suite:
	@case "$(DEPLOY_SUITE)" in caas|bmaas) ;; *) echo "Unsupported deployment suite: $(DEPLOY_SUITE)" >&2; exit 2;; esac
	$(MAKE) deploy-fabric
	$(MAKE) setup-$(DEPLOY_SUITE)

# all prepares the suite and then creates its workload. setup stops after
# preparation. workload assumes preparation already ran, which is how the
# root target orders validation before prove-bmi.
.PHONY: deploy-suite deploy-suite-workload
SUITE_STAGE ?= all
deploy-suite:
	@case "$(DEPLOY_SUITE)" in caas|bmaas) ;; *) echo "Unsupported deployment suite: $(DEPLOY_SUITE)" >&2; exit 2;; esac
	@case "$(SUITE_STAGE)" in \
		all) $(MAKE) prepare-suite && $(MAKE) deploy-suite-workload ;; \
		setup) $(MAKE) prepare-suite ;; \
		workload) $(MAKE) deploy-suite-workload ;; \
		*) echo "Unsupported deploy-suite stage: $(SUITE_STAGE)" >&2; exit 2 ;; \
	esac

deploy-suite-workload:
	@if [ "$(DEPLOY_SUITE)" = bmaas ]; then $(MAKE) prove-bmi; else echo "=== CaaS workload is created during prepare-suite ==="; fi

destroy-suite:
	@case "$(DEPLOY_SUITE)" in caas|bmaas) ;; *) echo "Unsupported deployment suite: $(DEPLOY_SUITE)" >&2; exit 2;; esac
	$(MAKE) destroy-$(DEPLOY_SUITE)
	@if [ "$(DEPLOY_SUITE)" = bmaas ]; then $(MAKE) destroy-fabric; fi

# ─── .PHONY ────────────────────────────────────────────────────────

.PHONY: setup-infra deploy-setup deploy-infra deploy-ocp deploy-osac deploy-fabric \
        setup-caas setup-bmaas prepare-suite deploy-suite prove-bmi test-e2e \
        destroy-osac destroy-ocp destroy-fabric destroy-caas destroy-bmaas \
        destroy-suite destroy-setup destroy-infra \
        gather-infra gather-e2e gather-bmaas
