REPORTS_DIR ?= reports

.PHONY: test lint format test-vmaas test-caas test-storage test-bmaas test-bmaas-networking test-metering test-references

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
	pytest tests/vmaas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/vmaas.xml

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
	cd $(OSAC_MONO_DIR) && uv run pytest tests/e2e/bmaas/regression/networking/ -x -n 0 -v --tb=short $(if $(TEST),-k "$(TEST)") --junitxml=$(CURDIR)/$(REPORTS_DIR)/bmaas-networking.xml

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
OSAC_DEPLOY_MODE   ?= fresh
OSAC_PROFILE       ?=
INFRA_DIR           = infra/$(INFRA)
_FWD_PROFILE        = $(if $(OSAC_PROFILE),OSAC_PROFILE=$(OSAC_PROFILE))

.PHONY: e2e deploy-setup setup-infra deploy-infra deploy-ocp deploy-osac setup-suite run-tests \
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
	@if [ -f tests/$(SUITE)/contract ]; then \
		set -a && . $(INFRA_DIR)/.env.infra && set +a && \
		. tests/$(SUITE)/contract && \
		missing="" && \
		for var in $$REQUIRED_VARS; do \
			eval val="\$$$$var" && \
			if [ -z "$$val" ]; then missing="$$missing $$var"; fi; \
		done && \
		if [ -n "$$missing" ]; then \
			echo "ERROR: backend '$(INFRA)' missing required vars for suite '$(SUITE)':$$missing"; \
			exit 1; \
		fi; \
	fi

e2e: _validate-backend deploy-setup deploy-infra deploy-ocp deploy-osac setup-suite run-tests

deploy-setup: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-setup EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)

# Backward compatibility alias
setup-infra: deploy-setup

deploy-infra: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-infra EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)

deploy-ocp: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-ocp EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)

deploy-osac: _validate-backend
	$(MAKE) -C $(INFRA_DIR) deploy-osac EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)

setup-suite: _validate-backend
	$(MAKE) -C $(INFRA_DIR) setup-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)' OSAC_DEPLOY_MODE=$(OSAC_DEPLOY_MODE) $(_FWD_PROFILE)

run-tests: _validate-suite-contract
	@set -a && . $(INFRA_DIR)/.env.infra && set +a && \
		$(MAKE) test-$(SUITE)

destroy-ocp:
	$(MAKE) -C $(INFRA_DIR) destroy-ocp EXTRA_VARS='$(EXTRA_VARS)'

destroy-osac:
	$(MAKE) -C $(INFRA_DIR) destroy-osac EXTRA_VARS='$(EXTRA_VARS)' $(_FWD_PROFILE)

destroy-infra:
	$(MAKE) -C $(INFRA_DIR) destroy-infra EXTRA_VARS='$(EXTRA_VARS)'

destroy-setup:
	$(MAKE) -C $(INFRA_DIR) destroy-setup EXTRA_VARS='$(EXTRA_VARS)'

destroy-suite:
	$(MAKE) -C $(INFRA_DIR) destroy-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)'

gather-infra:
	$(MAKE) -C $(INFRA_DIR) gather-infra EXTRA_VARS='$(EXTRA_VARS)'

gather-suite:
	$(MAKE) -C $(INFRA_DIR) gather-$(SUITE) EXTRA_VARS='$(EXTRA_VARS)'

redeploy-osac: destroy-osac deploy-osac
