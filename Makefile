REPORTS_DIR ?= reports

.PHONY: test lint format test-vmaas test-vmaas-networking test-vmaas_networking \
        test-stretched-networking test-stretched_networking test-caas \
        test-caas-networking test-caas_networking test-maas test-maas-networking \
        test-maas_networking test-storage test-bmaas test-bmaas-networking \
        test-bmaas_networking test-metering test-references test-full

test:
	mkdir -p $(REPORTS_DIR)
	pytest tests/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/results.xml

lint:
	ruff check tests/
	ruff format --check tests/

format:
	ruff format tests/

test-vmaas:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/vmaas/ -v -m "not requires_netris" $(if $(TEST),-k "$(TEST)") --junitxml=$(CURDIR)/$(REPORTS_DIR)/vmaas.xml

test-vmaas-networking test-vmaas_networking:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/vmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_vmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(CURDIR)/$(REPORTS_DIR)/vmaas-networking.xml

test-stretched-networking test-stretched_networking:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/stretched/ -x -n 0 -v --tb=short -m "requires_stretched_networking" $(if $(TEST),-k "$(TEST)") --junitxml=$(CURDIR)/$(REPORTS_DIR)/stretched-networking.xml

test-caas:
	mkdir -p $(REPORTS_DIR)
	pytest tests/caas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/caas.xml

test-storage:
	mkdir -p $(REPORTS_DIR)
	pytest tests/storage/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/storage.xml

# Optional: MARKER=sanity or MARKER=regression (pytest -m)
# -n 0 for local full-suite runs so BMH-consuming tests do not race.
test-bmaas:
	mkdir -p $(REPORTS_DIR)
	pytest tests/bmaas/ --ignore=tests/bmaas/networking -n 0 -v --tb=short $(if $(MARKER),-m "$(MARKER)") $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/bmaas.xml

OSAC_MONO_DIR ?= /opt/osac

test-bmaas-networking test-bmaas_networking:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/bmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_bmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(CURDIR)/$(REPORTS_DIR)/bmaas-networking.xml

# full-ci enables all services. Run regular E2E plus only the stretched Netris test; keep the full run serial.
FULL_TEST_MARKER = not requires_netris or requires_stretched_networking
FULL_TEST_FILTER = not test_cluster_provisioning_chain_by_name and not test_cross_tenant_cluster_template_reference and not test_create_subnet_with_virtual_network_by_name and not test_disabled_services and not external_ip
test-full:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/ -n 0 -v --tb=short -m "$(FULL_TEST_MARKER)" -k "$(if $(TEST),($(TEST)) and )$(FULL_TEST_FILTER)" --junitxml=$(CURDIR)/$(REPORTS_DIR)/full.xml

# Dedicated Netris suites can be added to the OSAC mono-repo later. Their
# pytest fixtures will create the shared tenants when those suites run.
OSAC_CAAS_NETRIS_TEST_PATH ?= tests/e2e/caas/regression/netris
OSAC_MAAS_NETRIS_TEST_PATH ?= tests/e2e/maas/regression/netris

test-caas-networking test-caas_networking:
	@if [ -e "$(OSAC_MONO_DIR)/$(OSAC_CAAS_NETRIS_TEST_PATH)" ]; then \
		cd $(OSAC_MONO_DIR) && uv run pytest $(OSAC_CAAS_NETRIS_TEST_PATH) -x -n 0 -v --tb=short; \
	else echo "ERROR: CaaS Netris tests do not exist at $(OSAC_MONO_DIR)/$(OSAC_CAAS_NETRIS_TEST_PATH)" >&2; exit 2; fi

test-maas-networking test-maas_networking:
	@if [ -e "$(OSAC_MONO_DIR)/$(OSAC_MAAS_NETRIS_TEST_PATH)" ]; then \
		cd $(OSAC_MONO_DIR) && uv run pytest $(OSAC_MAAS_NETRIS_TEST_PATH) -x -n 0 -v --tb=short; \
	else echo "ERROR: MaaS Netris tests do not exist at $(OSAC_MONO_DIR)/$(OSAC_MAAS_NETRIS_TEST_PATH)" >&2; exit 2; fi

# MaaS is also a persistent deployment suite; its dedicated pytest suite is pending.
test-maas: test-maas_networking

test-metering:
	mkdir -p $(REPORTS_DIR)
	pytest -m metering tests/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/metering.xml

test-references:
	mkdir -p $(REPORTS_DIR)
	pytest tests/references/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/references.xml

# ─── Infrastructure orchestration ───────────────────────────────────

INFRA              ?= netris
SUITE              ?= caas
EXTRA_VARS         ?=
KUBECONFIG         ?= /root/.kube/config
OSAC_VM_KUBECONFIG ?= $(KUBECONFIG)
OSAC_DEPLOY_MODE   ?= fresh
OSAC_PROFILE       ?=
SUITE_STAGE        ?= all
INFRA_DIR           = infra/$(INFRA)
_FWD_KUBECONFIG     = KUBECONFIG=$(abspath $(KUBECONFIG))
_FWD_VM_KUBECONFIG = OSAC_VM_KUBECONFIG=$(abspath $(OSAC_VM_KUBECONFIG))
_FWD_PROFILE        = $(if $(OSAC_PROFILE),OSAC_PROFILE=$(OSAC_PROFILE))
_FWD_SUITE          = SUITE=$(SUITE)

.PHONY: e2e deploy-suite deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac setup-suite prepare-suite run-tests \
        destroy-ocp destroy-osac destroy-infra destroy-setup destroy-suite gather-infra gather-suite \
        redeploy-osac _validate-backend _validate-suite-contract

_validate-backend:
	@if [ ! -f $(INFRA_DIR)/Makefile ]; then \
		echo "ERROR: backend '$(INFRA)' not found at $(INFRA_DIR)/"; exit 1; \
	fi
	@. $(INFRA_DIR)/capabilities && \
		echo "$$SUPPORTED_SUITES" | tr ' ' '\n' | grep -qx "$(SUITE)" || \
		{ echo "ERROR: backend '$(INFRA)' does not support suite '$(SUITE)'"; \
		  echo "Supported: $$(. $(INFRA_DIR)/capabilities && echo $$SUPPORTED_SUITES)"; \
		  exit 1; }

_validate-suite-contract:
	@if [ ! -f $(INFRA_DIR)/.env.infra ]; then \
		echo "ERROR: $(INFRA_DIR)/.env.infra not found. Run 'make deploy-osac' first."; exit 1; \
	fi
	@suite_contract='$(INFRA_DIR)/contracts/$(SUITE)'; \
	if [ -f "$$suite_contract" ]; then \
		set -a && . "$(INFRA_DIR)/.env.infra" && set +a && \
		. "$$suite_contract" && \
		missing="" && \
		for var in $$REQUIRED_VARS; do \
			val=$$(printenv "$$var" || true); \
			if [ -z "$$val" ]; then missing="$$missing $$var"; fi; \
		done && \
		if [ -n "$$missing" ]; then \
			echo "ERROR: backend '$(INFRA)' missing required vars for suite '$(SUITE)':$$missing"; \
			exit 1; \
		fi; \
	fi

e2e: _validate-backend
	$(MAKE) deploy-setup INFRA=$(INFRA) SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)
	$(MAKE) deploy-infra INFRA=$(INFRA) SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)
	$(MAKE) deploy-ocp INFRA=$(INFRA) SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)
	$(MAKE) deploy-osac INFRA=$(INFRA) SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)
	$(MAKE) prepare-suite INFRA=$(INFRA) SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)
	$(MAKE) run-tests INFRA=$(INFRA) SUITE=$(SUITE)

deploy-setup: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-setup EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-infra EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-ocp: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-ocp EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-osac: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-osac EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

setup-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) setup-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

prepare-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) prepare-suite SUITE=$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

# Operates on an installed OSAC lab; the backend prepares and deploys the suite.
deploy-suite: _validate-backend _validate-suite-contract
	$(MAKE) -C $(INFRA_DIR) deploy-suite SUITE=$(SUITE) SUITE_STAGE=$(SUITE_STAGE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

run-tests: _validate-suite-contract
	@set -a && . $(INFRA_DIR)/.env.infra && set +a && \
		$(MAKE) test-$(SUITE)

destroy-ocp:
	$(MAKE) -C $(INFRA_DIR) destroy-ocp EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

destroy-osac:
	$(MAKE) -C $(INFRA_DIR) destroy-osac EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_PROFILE) $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

destroy-infra:
	$(MAKE) -C $(INFRA_DIR) destroy-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE)

destroy-setup:
	$(MAKE) -C $(INFRA_DIR) destroy-setup EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE)

destroy-suite:
	@if [ "$(SUITE)" = "full" ]; then \
		echo "ERROR: destroy-suite SUITE=full is not supported; use 'make -C infra/netris destroy-full' only when you intend to destroy the entire lab." >&2; \
		exit 2; \
	fi
	$(MAKE) -C $(INFRA_DIR) destroy-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE)

gather-infra:
	$(MAKE) -C $(INFRA_DIR) gather-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

gather-suite:
	$(MAKE) -C $(INFRA_DIR) gather-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

redeploy-osac: destroy-osac deploy-osac
