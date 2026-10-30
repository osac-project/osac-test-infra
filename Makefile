REPORTS_DIR ?= reports

# DEPLOY_SUITE selects what is deployed; TEST_SUITE selects the OSAC tests and
# defaults to the same name. See infra/netris/targets.md.
.PHONY: test lint format test-vmaas test-stretched test-caas test-caas-networking \
        test-caas_networking test-maas test-maas-networking test-maas_networking \
        test-storage test-bmaas test-metering test-references test-full

test:
	mkdir -p $(REPORTS_DIR)
	pytest tests/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/results.xml

lint:
	ruff check tests/
	ruff format --check tests/

format:
	ruff format tests/

# Ordered module: no -x, so a NAT failure does not hide what follows, and never slice it with TEST.
test-vmaas:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/vmaas/ -n 0 -v --tb=short $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-networking.xml

test-stretched:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/stretched/ -x -n 0 -v --tb=short $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/stretched.xml

test-caas:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/caas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/caas.xml

test-storage:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/storage/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/storage.xml

# -n 0 keeps the BMH-consuming networking tests serial.
test-bmaas:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/bmaas/ -x -n 0 -v --tb=short $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/bmaas-networking.xml

OSAC_MONO_DIR ?= /opt/osac

# full-ci enables all services. Run regular E2E plus the stretched test, serially.
# networking/vmaas is one ordered module and must not be filtered; networking/bmaas
# keeps PROTOCOL_ICMP rules, which the API rejects with cudn_evpn.
FULL_TEST_IGNORE = --ignore=tests/e2e/networking/vmaas --ignore=tests/e2e/networking/bmaas
FULL_TEST_FILTER = not test_cluster_provisioning_chain_by_name and not test_cross_tenant_cluster_template_reference and not test_create_subnet_with_virtual_network_by_name and not test_disabled_services and not external_ip
test-full:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/ $(FULL_TEST_IGNORE) -n 0 -v --tb=short -k "$(if $(TEST),($(TEST)) and )$(FULL_TEST_FILTER)" --junitxml=$(abspath $(REPORTS_DIR))/full.xml

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
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/ -m metering -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/metering.xml

test-references:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/references/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/references.xml

# ─── Infrastructure orchestration ───────────────────────────────────

ifneq ($(origin SUITE),undefined)
$(error SUITE was replaced: use DEPLOY_SUITE and TEST_SUITE, see infra/netris/targets.md)
endif
ifneq ($(origin OSAC_DEPLOY_MODE),undefined)
$(error OSAC_DEPLOY_MODE was removed: every OSAC install is fresh)
endif

INFRA              ?=
DEPLOY_SUITE       ?= $(shell if [ -r "$(INFRA_DIR)/capabilities" ]; then . "$(INFRA_DIR)/capabilities"; printf '%s' "$${DEFAULT_DEPLOY_SUITE:-}"; fi)
TEST_SUITE         ?= $(DEPLOY_SUITE)
EXTRA_VARS         ?=
SUITE_STAGE        ?= all
INFRA_DIR           = infra/$(INFRA)
_FWD_KUBECONFIG     = $(if $(KUBECONFIG),KUBECONFIG=$(abspath $(KUBECONFIG)))
_FWD_VM_KUBECONFIG = $(if $(OSAC_VM_KUBECONFIG),OSAC_VM_KUBECONFIG=$(abspath $(OSAC_VM_KUBECONFIG)))
_FWD_PULL_SECRET    = $(if $(OSAC_PULL_SECRET_PATH),OSAC_PULL_SECRET_PATH=$(OSAC_PULL_SECRET_PATH))
_FWD_DEPLOY_SUITE   = DEPLOY_SUITE=$(DEPLOY_SUITE)

.PHONY: e2e deploy-suite deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac setup-suite prepare-suite run-tests \
        destroy-ocp destroy-osac destroy-infra destroy-setup destroy-suite gather-infra gather-suite \
        redeploy-osac _require-backend _validate-backend _validate-suite-contract

_require-backend:
	@if [ -z "$(INFRA)" ]; then \
		echo "ERROR: INFRA is required; choose a backend under infra/"; exit 1; \
	fi
	@if [ ! -f $(INFRA_DIR)/Makefile ]; then \
		echo "ERROR: backend '$(INFRA)' not found at $(INFRA_DIR)/"; exit 1; \
	fi
	@if [ ! -f $(INFRA_DIR)/capabilities ]; then \
		echo "ERROR: backend '$(INFRA)' is missing $(INFRA_DIR)/capabilities"; exit 1; \
	fi

_validate-backend: _require-backend
	@. $(INFRA_DIR)/capabilities && \
		echo "$$SUPPORTED_DEPLOY_SUITES" | tr ' ' '\n' | grep -qx "$(DEPLOY_SUITE)" || \
		{ echo "ERROR: backend '$(INFRA)' does not support deployment suite '$(DEPLOY_SUITE)'"; \
		  echo "Supported: $$(. $(INFRA_DIR)/capabilities && echo $$SUPPORTED_DEPLOY_SUITES)"; \
		  exit 1; }

_validate-suite-contract: _require-backend
	@if [ ! -f $(INFRA_DIR)/.env.infra ]; then \
		echo "ERROR: $(INFRA_DIR)/.env.infra not found. Run 'make deploy-osac' first."; exit 1; \
	fi
	@set -e; \
	suite_contract='$(INFRA_DIR)/contracts/$(TEST_SUITE)'; \
	set -a; . "$(INFRA_DIR)/.env.infra"; set +a; \
	if [ -f "$$suite_contract" ]; then \
		. "$$suite_contract"; \
		missing=""; \
		for var in $$REQUIRED_VARS; do \
			val=$$(printenv "$$var" || true); \
			if [ -z "$$val" ]; then missing="$$missing $$var"; fi; \
		done; \
		if [ -n "$$missing" ]; then \
			echo "ERROR: backend '$(INFRA)' missing required vars for test suite '$(TEST_SUITE)':$$missing"; \
			exit 1; \
		fi; \
	fi

e2e: _validate-backend
	$(MAKE) deploy-setup INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
	$(MAKE) deploy-infra INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
	$(MAKE) deploy-ocp INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
	$(MAKE) deploy-osac INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
	$(MAKE) prepare-suite INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)'
	$(MAKE) run-tests INFRA=$(INFRA) DEPLOY_SUITE=$(DEPLOY_SUITE) TEST_SUITE=$(TEST_SUITE)

deploy-setup: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-setup EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-ocp: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-ocp EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-osac: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-osac EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

setup-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) setup-$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

prepare-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) prepare-suite DEPLOY_SUITE=$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

# Operates on an installed OSAC lab; the backend prepares and deploys the suite.
# The target name is unrelated to the DEPLOY_SUITE variable.
deploy-suite: _validate-backend _validate-suite-contract
	$(MAKE) -C $(INFRA_DIR) deploy-suite DEPLOY_SUITE=$(DEPLOY_SUITE) SUITE_STAGE=$(SUITE_STAGE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_PULL_SECRET) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

run-tests: _validate-backend _validate-suite-contract
	@set -a && . $(INFRA_DIR)/.env.infra && set +a && \
		$(MAKE) test-$(TEST_SUITE)

destroy-ocp: _require-backend
	$(MAKE) -C $(INFRA_DIR) destroy-ocp EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

destroy-osac: _require-backend
	$(MAKE) -C $(INFRA_DIR) destroy-osac EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

destroy-infra: _require-backend
	$(MAKE) -C $(INFRA_DIR) destroy-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE)

destroy-setup: _require-backend
	$(MAKE) -C $(INFRA_DIR) destroy-setup EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE)

destroy-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) destroy-suite EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE)

gather-infra: _require-backend
	$(MAKE) -C $(INFRA_DIR) gather-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

gather-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) gather-$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

redeploy-osac: destroy-osac deploy-osac
