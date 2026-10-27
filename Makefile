REPORTS_DIR ?= reports

# SUITE selects the deployment scenario; TEST_SCOPE narrows VMaaS/BMaaS tests.
# Supported scopes are service (default), networking, and all.
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
	@if [ "$(TEST_SCOPE)" = "service" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/vmaas/ -v -m "not requires_netris" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-service.xml; \
	elif [ "$(TEST_SCOPE)" = "networking" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/vmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_vmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-networking.xml; \
	elif [ "$(TEST_SCOPE)" = "all" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/vmaas/ -v -m "not requires_netris" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-service.xml && \
	  uv run pytest tests/e2e/networking/vmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_vmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/vmaas-networking.xml; \
	else \
	  echo "ERROR: TEST_SCOPE must be service, networking, or all (got '$(TEST_SCOPE)')" >&2; exit 2; \
	fi

test-stretched:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/stretched/ -x -n 0 -v --tb=short -m "requires_stretched_networking" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/stretched.xml

test-caas:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/caas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/caas.xml

test-storage:
	mkdir -p $(REPORTS_DIR)
	pytest tests/storage/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/storage.xml

# Optional: MARKER=sanity or MARKER=regression (pytest -m)
# -n 0 for local full-suite runs so BMH-consuming tests do not race.
test-bmaas:
	mkdir -p $(REPORTS_DIR)
	@if [ "$(TEST_SCOPE)" = "service" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/bmaas/ -n 0 -v --tb=short $(if $(MARKER),-m "$(MARKER)") $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/bmaas-service.xml; \
	elif [ "$(TEST_SCOPE)" = "networking" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/networking/bmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_bmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/bmaas-networking.xml; \
	elif [ "$(TEST_SCOPE)" = "all" ]; then \
	  cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/bmaas/ -n 0 -v --tb=short $(if $(MARKER),-m "$(MARKER)") $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/bmaas-service.xml && \
	  uv run pytest tests/e2e/networking/bmaas/ -x -n 0 -v --tb=short -m "requires_netris and requires_netris_bmaas" $(if $(TEST),-k "$(TEST)") --junitxml=$(abspath $(REPORTS_DIR))/bmaas-networking.xml; \
	else \
	  echo "ERROR: TEST_SCOPE must be service, networking, or all (got '$(TEST_SCOPE)')" >&2; exit 2; \
	fi

OSAC_MONO_DIR ?= /opt/osac

# CaaS is not provisioned by setup-full. Include the implemented VMaaS and BMaaS
# Netris networking suites, plus stretched connectivity; keep the slow cross-tenant
# BMaaS networking test in its dedicated workflow.
FULL_TEST_MARKER = not requires_caas and (not requires_netris or requires_stretched_networking or requires_netris_vmaas or requires_netris_bmaas)
FULL_TEST_FILTER = not test_cluster_provisioning_chain_by_name and not test_cross_tenant_cluster_template_reference and not test_create_subnet_with_virtual_network_by_name and not test_disabled_services and not external_ip and not TestCrossTenantBmaasNetworking
test-full:
	mkdir -p $(REPORTS_DIR)
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/ --ignore=tests/e2e/caas -n 0 -v --tb=short -m "$(FULL_TEST_MARKER)" -k "$(if $(TEST),($(TEST)) and )$(FULL_TEST_FILTER)" --junitxml=$(abspath $(REPORTS_DIR))/full.xml

test-metering:
	mkdir -p $(REPORTS_DIR)
	pytest -m metering tests/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/metering.xml

test-references:
	mkdir -p $(REPORTS_DIR)
	pytest tests/references/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/references.xml

# ─── Infrastructure orchestration ───────────────────────────────────

INFRA              ?= netris
SUITE              ?= caas
TEST_SCOPE         ?= service
EXTRA_VARS         ?=
KUBECONFIG         ?= /root/.kube/config
OSAC_VM_KUBECONFIG ?= $(KUBECONFIG)
OSAC_DEPLOY_MODE   ?= fresh
OSAC_PROFILE       ?=
INFRA_DIR           = infra/$(INFRA)
_FWD_KUBECONFIG     = KUBECONFIG=$(abspath $(KUBECONFIG))
_FWD_VM_KUBECONFIG = OSAC_VM_KUBECONFIG=$(abspath $(OSAC_VM_KUBECONFIG))
_FWD_PROFILE        = $(if $(OSAC_PROFILE),OSAC_PROFILE=$(OSAC_PROFILE))
_FWD_SUITE          = SUITE=$(SUITE)

# Snapshot refresh does not apply the generated Netris runtime overlay or the
# per-profile Netris overlay. Restrict it to the supported CaaS snapshot path.
_DEPLOY_GOALS = e2e deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac
ifneq ($(filter $(_DEPLOY_GOALS),$(MAKECMDGOALS)),)
ifneq ($(filter-out fresh snapshot,$(OSAC_DEPLOY_MODE)),)
$(error OSAC_DEPLOY_MODE must be fresh or snapshot (got '$(OSAC_DEPLOY_MODE)'))
endif
ifeq ($(OSAC_DEPLOY_MODE),snapshot)
ifneq ($(SUITE),caas)
$(error Snapshot mode supports only SUITE=caas with OSAC_PROFILE=caas-ci; use fresh mode for Netris VMaaS, BMaaS, stretched, or full deployments)
endif
ifneq ($(strip $(OSAC_PROFILE)),)
ifneq ($(OSAC_PROFILE),caas-ci)
$(error Snapshot mode supports only OSAC_PROFILE=caas-ci; use fresh mode for Netris service profiles)
endif
endif
endif
endif

.PHONY: e2e deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac setup-suite run-tests \
        destroy-ocp destroy-osac destroy-infra destroy-setup destroy-suite gather-infra gather-suite \
        redeploy-osac _validate-backend _validate-suite-contract _validate-test-scope

_validate-test-scope:
	@if [ "$(SUITE)" = "vmaas" ] || [ "$(SUITE)" = "bmaas" ]; then \
	  case "$(TEST_SCOPE)" in service|networking|all) ;; \
	    *) echo "ERROR: TEST_SCOPE for SUITE=$(SUITE) must be service, networking, or all"; exit 1 ;; \
	  esac; \
	fi

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
	@set -e; \
	suite_contracts='$(INFRA_DIR)/contracts/$(SUITE)'; \
	if { [ "$(TEST_SCOPE)" = "networking" ] || [ "$(TEST_SCOPE)" = "all" ]; } && { [ "$(SUITE)" = "vmaas" ] || [ "$(SUITE)" = "bmaas" ]; }; then \
	  networking_contract='$(INFRA_DIR)/contracts/$(SUITE)-networking'; \
	  if [ "$(TEST_SCOPE)" = "networking" ]; then suite_contracts="$$networking_contract"; \
	  else suite_contracts="$$suite_contracts $$networking_contract"; fi; \
	fi; \
	set -a; . "$(INFRA_DIR)/.env.infra"; set +a; \
	for suite_contract in $$suite_contracts; do \
		if [ -f "$$suite_contract" ]; then \
			. "$$suite_contract"; \
			missing=""; \
			for var in $$REQUIRED_VARS; do \
				val=$$(printenv "$$var" || true); \
				if [ -z "$$val" ]; then missing="$$missing $$var"; fi; \
			done; \
			if [ -n "$$missing" ]; then \
				echo "ERROR: backend '$(INFRA)' missing required vars for suite '$(SUITE)' (TEST_SCOPE=$(TEST_SCOPE)):$$missing"; \
				exit 1; \
			fi; \
		fi; \
	done

e2e: _validate-backend _validate-test-scope deploy-setup deploy-infra deploy-ocp deploy-osac setup-suite run-tests

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

run-tests: _validate-test-scope _validate-suite-contract
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
	$(MAKE) test-$(SUITE) TEST_SCOPE=$(TEST_SCOPE)

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
