# Conductor Project Makefile
# ===========================

# Python virtual environment
VENV_DIR := .venv

PYTHON := $(CURDIR)/$(VENV_DIR)/bin/python
PIP := $(CURDIR)/$(VENV_DIR)/bin/pip
PYTEST := $(CURDIR)/$(VENV_DIR)/bin/pytest
UVICORN := $(PYTHON) -m uvicorn
LIQUIBASE_IMAGE := liquibase/liquibase:4.29
LIQUIBASE := docker run --rm --network conductor-net \
	-v $(CURDIR)/database:/liquibase/changelog \
	$(LIQUIBASE_IMAGE) \
	--defaults-file=/liquibase/changelog/liquibase.properties \
	--search-path=/liquibase/changelog/changelog \
	--url=jdbc:postgresql://$${POSTGRES_HOST:-conductor-postgres}:$${POSTGRES_PORT:-5432}/$${POSTGRES_DB:-conductor} \
	--username=$${POSTGRES_USER:-conductor} \
	--password=$${POSTGRES_PASSWORD:-conductor}

# Docker compose files
DATA_COMPOSE := docker/docker-compose.data.yaml
APP_COMPOSE := docker/docker-compose.app.yaml
LANGFUSE_COMPOSE := docker/docker-compose.langfuse.yaml

# WebSocket Configuration
WS_PING_INTERVAL := 20.0
WS_PING_TIMEOUT := 20.0
WS_OPTIONS := --ws-ping-interval $(WS_PING_INTERVAL) --ws-ping-timeout $(WS_PING_TIMEOUT)

# Default target
all: setup

# ===========================
# Setup
# ===========================
.PHONY: setup setup-backend setup-extension venv ensure-backend-deps install browser-install

## Create venv and install all dependencies
setup: venv setup-backend setup-extension
	@echo "Setup complete!"

## Setup backend (venv + dependencies)
setup-backend: venv
	@echo "Installing backend dependencies..."
	$(PYTHON) -m pip install -r backend/requirements.txt
	@echo "Backend setup complete!"

## Setup extension (npm install)
setup-extension:
	@echo "Installing extension dependencies..."
	cd extension && npm install
	@echo "Extension setup complete!"

## Install Playwright browsers (Chromium) for web browsing tools
browser-install: venv
	@echo "Installing Playwright Chromium browser..."
	$(PYTHON) -m playwright install chromium
	@echo "Playwright Chromium installed!"

## Create Python virtual environment if it doesn't exist
venv:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "Creating virtual environment..."; \
		python3 -m venv $(VENV_DIR) || { \
			echo "Failed to create venv. Try: sudo apt install python3-venv"; \
			exit 1; \
		}; \
		echo "Virtual environment created at $(VENV_DIR)"; \
	else \
		echo "Virtual environment already exists at $(VENV_DIR)"; \
	fi
	@# Repair stale interpreter symlinks when python moved (for example /usr/local/bin -> /usr/bin)
	@if [ -L "$(PYTHON)" ] && [ ! -e "$(PYTHON)" ]; then \
		echo "Virtual environment interpreter symlink is stale. Repairing..."; \
		ln -sf "$$(command -v python3)" "$(PYTHON)"; \
	fi
	@# Verify venv is usable
	@if [ ! -x "$(PYTHON)" ] || ! "$(PYTHON)" -V >/dev/null 2>&1 || ! "$(PYTHON)" -m pip --version >/dev/null 2>&1; then \
		echo "Virtual environment is broken. Recreating $(VENV_DIR)..."; \
		rm -rf $(VENV_DIR); \
		python3 -m venv $(VENV_DIR) || { \
			echo "Failed to recreate venv. Try: sudo apt install python3-venv"; \
			exit 1; \
		}; \
		echo "Virtual environment recreated at $(VENV_DIR)"; \
	fi

## Ensure backend dependencies are installed in the venv
ensure-backend-deps: venv
	@if ! "$(PYTHON)" -c "import fastapi, pytest, uvicorn" >/dev/null 2>&1; then \
		echo "Backend dependencies missing -- installing..."; \
		$(PYTHON) -m pip install -r backend/requirements.txt; \
		echo "Backend dependencies ready"; \
	fi

## Install all dependencies (alias for setup)
install: setup

# ===========================
# Run Servers
# ===========================
.PHONY: run-backend run-backend-prod run-backend-port

## Start backend server (development mode with auto-reload)
run-backend: ensure-backend-deps
	@echo "Starting backend server..."
	@echo "   Swagger UI: http://localhost:8000/docs"
	@echo "   ReDoc: http://localhost:8000/redoc"
	@echo "   WebSocket: ws://localhost:8000/ws/chat/{room_id}"
	cd backend && $(UVICORN) app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000 $(WS_OPTIONS)

## Start backend server (production mode)
run-backend-prod: ensure-backend-deps
	@echo "Starting backend server (production)..."
	cd backend && $(UVICORN) app.main:app --host 0.0.0.0 --port 8000 --workers 4 $(WS_OPTIONS)

## Start backend with custom port (usage: make run-backend-port PORT=8001)
run-backend-port: ensure-backend-deps
	@echo "Starting backend server on port $(PORT)..."
	cd backend && $(UVICORN) app.main:app --reload --reload-dir app --host 0.0.0.0 --port $(PORT) $(WS_OPTIONS)

# ===========================
# Testing
# ===========================
.PHONY: test test-backend test-extension test-parity integration-test

## Run all tests (backend + extension + parity)
test: test-backend test-extension test-parity
	@echo "All tests passed!"

## Run backend tests
test-backend: ensure-backend-deps
	@echo "Running backend tests..."
	cd backend && $(PYTHON) -m pytest tests/ -v

## Run extension tests
test-extension:
	@echo "Running extension tests..."
	@if [ -f "extension/package.json" ] && grep -q '"test"' extension/package.json; then \
		cd extension && npm test; \
	else \
		echo "No extension tests configured"; \
	fi

## Run backend integration tests (requires real API credentials)
integration-test: ensure-backend-deps
	@echo "Running backend integration tests (requires API credentials)..."
	cd backend && $(PYTHON) -m pytest tests/ -v -s -m integration

## Validate Python↔TS tool parity (shared contract + cross-language tests)
test-parity: ensure-backend-deps
	@echo "Step 1: Check contract matches Python schemas..."
	cd backend && $(PYTHON) ../scripts/generate_tool_contracts.py --check
	@echo "Step 2: Compile extension & validate TS + subprocess tools against contract..."
	cd extension && npm run compile
	cd extension && node tests/validate_contract.js
	@echo "Step 3: Run cross-language parity tests..."
	cd backend && $(PYTHON) -m pytest tests/test_tool_parity_subprocess.py tests/test_tool_parity_deep.py tests/test_tool_parity_ast.py -v
	@echo "All parity checks passed."

# ===========================
# Build / Compile
# ===========================
.PHONY: compile compile-ts compile-css package update-contracts update-prompt-library

## Compile extension (TypeScript + Tailwind CSS)
compile: compile-ts compile-css
	@echo "Extension compiled!"

## Compile TypeScript
compile-ts:
	@echo "Compiling TypeScript..."
	cd extension && npm run compile

## Compile Tailwind CSS
compile-css:
	@echo "Building Tailwind CSS..."
	cd extension && npm run build:css

## Package extension as .vsix (compiles first)
package: compile
	@echo "Packaging VS Code extension..."
	cd extension && npx @vscode/vsce package
	@echo "Extension packaged! (.vsix file in extension/)"

## Regenerate tool contracts after changing Python schemas
update-contracts: ensure-backend-deps
	cd backend && $(PYTHON) ../scripts/generate_tool_contracts.py
	@echo "Contracts updated. Commit contracts/tool_contracts.json and extension/src/services/toolContracts.d.ts"

## Download latest prompt library from prompts.chat (reference for agent design)
update-prompt-library:
	@bash scripts/update-prompt-library.sh

# ===========================
# Data Tier (Postgres + Redis)
# ===========================
.PHONY: data-up data-down data-logs

## Start Postgres + Redis containers
data-up:
	@echo "Starting data tier (Postgres + Redis)..."
	docker compose -f $(DATA_COMPOSE) up -d
	@echo "Data tier starting. Postgres: localhost:5432, Redis: localhost:6379"

## Stop data tier
data-down:
	@echo "Stopping data tier..."
	docker compose -f $(DATA_COMPOSE) down
	@echo "Data tier stopped."

## View data tier logs
data-logs:
	docker compose -f $(DATA_COMPOSE) logs -f

# ===========================
# App Tier (Backend + Langfuse)
# ===========================
.PHONY: app-up app-rebuild app-restart app-down app-logs

## Start backend + Langfuse containers (builds backend image if missing)
app-up:
	@echo "Starting app tier (Backend + Langfuse)..."
	docker compose -f $(APP_COMPOSE) up -d --build
	@echo "App tier starting. Backend: localhost:8000, Langfuse: localhost:3001"

## Rebuild and restart a single app service (usage: make app-rebuild SVC=backend)
app-rebuild:
	@echo "Rebuilding $(SVC)..."
	docker compose -f $(APP_COMPOSE) up -d --build --force-recreate $(SVC)
	@echo "$(SVC) rebuilt and restarted."

## Restart backend after config/secrets change (no rebuild needed)
app-restart:
	@echo "Restarting backend (config reload)..."
	docker restart conductor-backend
	@echo "Backend restarted. New config/secrets are now active."

## Stop app tier
app-down:
	@echo "Stopping app tier..."
	docker compose -f $(APP_COMPOSE) down
	@echo "App tier stopped."

## View app tier logs
app-logs:
	docker compose -f $(APP_COMPOSE) logs -f

# ===========================
# Full Stack Docker
# ===========================
.PHONY: docker-up docker-down docker-clean

## Start full stack (data tier, schema, then app tier)
docker-up: data-up
	@echo "Waiting for data tier to be healthy..."
	@sleep 3
	@$(MAKE) db-update
	@$(MAKE) app-up
	@echo "Full stack started!"

## Stop full stack
docker-down: app-down data-down
	@echo "Full stack stopped."

## Stop all containers and remove all conductor-related images
docker-clean: docker-down
	@echo "Removing conductor containers and images..."
	-docker rm -f conductor-backend conductor-postgres conductor-redis conductor-langfuse 2>/dev/null
	-docker rmi conductor/backend:latest postgres:16-alpine redis:7-alpine langfuse/langfuse:2 2>/dev/null
	@echo "Docker clean complete."

# ===========================
# Database Schema (Liquibase)
# ===========================
.PHONY: db-update db-status db-rollback-one

## Apply pending Liquibase changesets
db-update:
	@echo "Running Liquibase update..."
	$(LIQUIBASE) update
	@echo "Schema update complete."

## Show pending changesets (dry run)
db-status:
	@echo "Checking pending changesets..."
	$(LIQUIBASE) status --verbose

## Rollback last changeset
db-rollback-one:
	@echo "Rolling back last changeset..."
	$(LIQUIBASE) rollback-count 1
	@echo "Rollback complete."

# ===========================
# Langfuse (Observability)
# ===========================
.PHONY: langfuse-up langfuse-down langfuse-logs

## Start Langfuse (requires data tier for shared Postgres)
langfuse-up: data-up
	@echo "Starting Langfuse on http://localhost:3001 ..."
	docker compose -f $(LANGFUSE_COMPOSE) up -d
	@echo "Langfuse is starting. User/org/project auto-provisioned on first run."
	@echo "  Login: admin@conductor.dev / conductor"
	@echo "  API keys: pk-lf-conductor-dev / sk-lf-conductor-dev"

## Stop Langfuse stack
langfuse-down:
	@echo "Stopping Langfuse..."
	docker compose -f $(LANGFUSE_COMPOSE) down
	@echo "Langfuse stopped."

## View Langfuse logs
langfuse-logs:
	docker compose -f $(LANGFUSE_COMPOSE) logs -f langfuse

# ===========================
# Lint & Format
# ===========================
.PHONY: lint format lint-check

## Lint backend Python code (auto-fix)
lint:
	@echo "Running ruff (lint + isort)..."
	cd backend && $(PYTHON) -m ruff check --fix .
	@echo "Lint complete."

## Format backend Python code (auto-fix)
format:
	@echo "Running black..."
	cd backend && $(PYTHON) -m black .
	@echo "Running ruff format..."
	cd backend && $(PYTHON) -m ruff format .
	@echo "Format complete."

## Lint + format check only (no changes, for CI)
lint-check:
	@echo "Checking ruff..."
	cd backend && $(PYTHON) -m ruff check .
	@echo "Checking black..."
	cd backend && $(PYTHON) -m black --check .
	@echo "All lint checks passed."

# ===========================
# Clean
# ===========================
.PHONY: clean

## Clean all generated files
clean:
	@echo "Cleaning..."
	rm -rf $(VENV_DIR)
	rm -rf backend/__pycache__ backend/**/__pycache__
	rm -rf backend/.pytest_cache
	rm -f backend/*.duckdb backend/*.duckdb.wal
	rm -rf extension/out
	rm -rf extension/node_modules
	@echo "Clean complete!"

# ===========================
# Help
# ===========================
.PHONY: help

## Show this help message
help:
	@echo "Conductor Project - Available Commands"
	@echo "======================================="
	@echo ""
	@echo "Setup:"
	@echo "  make setup              Create venv and install all dependencies"
	@echo "  make setup-backend      Setup backend only (venv + pip install)"
	@echo "  make setup-extension    Setup extension only (npm install)"
	@echo "  make browser-install    Install Playwright Chromium for web browsing tools"
	@echo ""
	@echo "Run Servers:"
	@echo "  make run-backend        Start backend (dev mode, auto-reload)"
	@echo "  make run-backend-prod   Start backend (production, 4 workers)"
	@echo "  make run-backend-port PORT=8001  Start on custom port"
	@echo ""
	@echo "Testing:"
	@echo "  make test               Run all tests (backend + extension)"
	@echo "  make test-backend       Run backend unit tests"
	@echo "  make test-extension     Run extension tests"
	@echo "  make test-parity        Validate Python<>TS tool parity"
	@echo "  make integration-test   Run integration tests (needs API keys)"
	@echo ""
	@echo "Build:"
	@echo "  make compile            Compile extension (TypeScript + CSS)"
	@echo "  make package            Package extension as .vsix"
	@echo "  make update-contracts   Regenerate tool contracts from Python schemas"
	@echo "  make update-prompt-library  Download latest prompts.chat CSV"
	@echo ""
	@echo "Docker (Data Tier):"
	@echo "  make data-up            Start Postgres + Redis"
	@echo "  make data-down          Stop data tier"
	@echo "  make data-logs          View data tier logs"
	@echo ""
	@echo "Docker (App Tier):"
	@echo "  make app-up             Start Backend + Langfuse"
	@echo "  make app-rebuild SVC=x  Rebuild and restart a single service"
	@echo "  make app-restart        Restart backend (config/secrets reload)"
	@echo "  make app-down           Stop app tier"
	@echo "  make app-logs           View app tier logs"
	@echo ""
	@echo "Docker (Full Stack):"
	@echo "  make docker-up          Start everything (data + schema + app)"
	@echo "  make docker-down        Stop everything"
	@echo "  make docker-clean       Stop + remove conductor images"
	@echo ""
	@echo "Database:"
	@echo "  make db-update          Apply pending Liquibase changesets"
	@echo "  make db-status          Show pending changesets (dry run)"
	@echo "  make db-rollback-one    Rollback last changeset"
	@echo ""
	@echo "Langfuse:"
	@echo "  make langfuse-up        Start Langfuse (Docker)"
	@echo "  make langfuse-down      Stop Langfuse"
	@echo "  make langfuse-logs      View Langfuse logs"
	@echo ""
	@echo "Lint & Format:"
	@echo "  make lint               Lint backend Python (ruff, auto-fix)"
	@echo "  make format             Format backend Python (black + ruff format)"
	@echo "  make lint-check         Lint + format check only (CI mode, no changes)"
	@echo ""
	@echo "Other:"
	@echo "  make clean              Remove all generated files"
	@echo "  make help               Show this help message"
