REPORTS_DIR ?= reports

.PHONY: test lint format test-vmaas test-vmaas-parallel test-caas

test:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/results.xml

lint:
	ruff check tests/
	ruff format --check tests/

format:
	ruff format tests/

test-vmaas:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/vmaas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/vmaas.xml

test-vmaas-parallel:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/vmaas/ -n 3 -m "not serial" -v --junitxml=$(REPORTS_DIR)/vmaas-parallel.xml
	uv run pytest tests/vmaas/ -m "serial" -v --junitxml=$(REPORTS_DIR)/vmaas-serial.xml

test-caas:
	mkdir -p $(REPORTS_DIR)
	uv run pytest tests/caas/ -v $(if $(TEST),-k "$(TEST)") --junitxml=$(REPORTS_DIR)/caas.xml
