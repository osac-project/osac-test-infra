REPORTS_DIR ?= reports

# DEPLOY_SUITE selects the deployment scenario. TEST_SUITE selects the OSAC test path;
# by default both use the same name.
.PHONY: test lint format test-vmaas test-stretched test-caas test-storage test-bmaas test-metering test-references test-full

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
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/vmaas/ -x -n 0 -v --tb=short $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-networking.xml

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

# Run the full networking selection serially. Keep slow or separately provisioned
# networking cases in their dedicated flows.
FULL_TEST_FILTER = not external_ip and not TestCrossTenantBmaasNetworking
test-full:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/ -n 0 -v --tb=short -k "$(if $(TEST),($(TEST)) and )$(FULL_TEST_FILTER)" --junitxml=$(abspath $(REPORTS_DIR))/full.xml

test-metering:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/ -m metering -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/metering.xml

test-references:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/references/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/references.xml

# ─── Infrastructure orchestration ───────────────────────────────────

INFRA              ?=
DEPLOY_SUITE       ?= $(shell if [ -r "$(INFRA_DIR)/capabilities" ]; then . "$(INFRA_DIR)/capabilities"; printf '%s' "$${DEFAULT_DEPLOY_SUITE:-}"; fi)
TEST_SUITE         ?= $(DEPLOY_SUITE)
EXTRA_VARS         ?=
INFRA_DIR           = infra/$(INFRA)
_FWD_KUBECONFIG     = $(if $(KUBECONFIG),KUBECONFIG=$(abspath $(KUBECONFIG)))
_FWD_VM_KUBECONFIG = $(if $(OSAC_VM_KUBECONFIG),OSAC_VM_KUBECONFIG=$(abspath $(OSAC_VM_KUBECONFIG)))
_FWD_DEPLOY_MODE    = $(if $(OSAC_DEPLOY_MODE),OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE))
_FWD_DEPLOY_SUITE   = DEPLOY_SUITE=$(DEPLOY_SUITE)

.PHONY: e2e deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac setup-suite run-tests \
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

e2e: _validate-backend deploy-setup deploy-infra deploy-ocp deploy-osac setup-suite run-tests

deploy-setup: _validate-backend
	$(MAKE) -C $(INFRA_DIR) setup-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_MODE) $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-infra EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_MODE) $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-ocp: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-ocp EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_MODE) $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

deploy-osac: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-osac EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_MODE) $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

setup-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) setup-$(DEPLOY_SUITE) EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_DEPLOY_MODE) $(_FWD_DEPLOY_SUITE) $(_FWD_KUBECONFIG) $(_FWD_VM_KUBECONFIG)

run-tests: _validate-backend _validate-suite-contract
	@set -e; \
	set -a; . $(INFRA_DIR)/.env.infra; set +a; \
	if [ -z "$${METERING_ADAPTER_URL:-}" ]; then \
		metering_service=$$(oc get svc -n "$$OSAC_NAMESPACE" -l app.kubernetes.io/component=echo-adapter -o jsonpath='{.items[0].metadata.name}'); \
		if [ -n "$$metering_service" ]; then \
			if ! oc get route metering-test-adapter -n "$$OSAC_NAMESPACE" >/dev/null 2>&1; then \
				oc expose "svc/$$metering_service" -n "$$OSAC_NAMESPACE" --name=metering-test-adapter; \
			fi; \
			route_host=$$(oc get route metering-test-adapter -n "$$OSAC_NAMESPACE" -o jsonpath='{.spec.host}'); \
			if [ -z "$$route_host" ]; then echo "ERROR: metering-test-adapter route has no host" >&2; exit 1; fi; \
			export METERING_ADAPTER_URL="https://$$route_host"; \
		fi; \
	fi; \
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
