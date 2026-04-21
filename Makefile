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

## Ensure extension dependencies are installed and in sync with the lockfile.
## Triggers `npm install` (which fires the postinstall hook → grammar download
## + SHA verification) when node_modules is missing or package-lock.json is
## newer than node_modules. No-op on a normal incremental build.
ensure-extension-deps:
	@if [ ! -d extension/node_modules ] || [ extension/package-lock.json -nt extension/node_modules ]; then \
		echo "Extension dependencies missing or stale -- running npm install..."; \
		cd extension && npm install; \
		echo "Extension dependencies ready"; \
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
.PHONY: test test-backend test-extension test-webview test-frontend test-parity integration-test postdeploy-check

## Run all tests (backend + extension + webview + parity)
test: test-backend test-extension test-webview test-parity
	@echo "All tests passed!"

## Run backend tests
test-backend: ensure-backend-deps
	@echo "Running backend tests..."
	cd backend && $(PYTHON) -m pytest tests/ -v

## Run extension service tests (node:test — FSM, controllers, services)
test-extension: ensure-extension-deps
	@echo "Running extension service tests..."
	cd extension && npm test

## Run React WebView tests (vitest — components, reducers, pure logic)
test-webview: ensure-extension-deps
	@echo "Running WebView tests..."
	cd extension && npm run test:webview

## Run all frontend tests (extension + webview)
test-frontend: test-extension test-webview
	@echo "All frontend tests passed!"

## Run backend integration tests (requires real API credentials)
integration-test: ensure-backend-deps
	@echo "Running backend integration tests (requires API credentials)..."
	cd backend && $(PYTHON) -m pytest tests/ -v -s -m integration

## PR Brain regression harness — runs requests + greptile-sentry +
## greptile-grafana + greptile-keycloak in parallel under the current
## coordinator config, logs each suite's summary to
## /tmp/brain-regression-<suite>-<tag>.log, and prints a consolidated
## composite + Judge table on completion.
##
## Usage:
##   make eval-brain-regression TAG=v2k
##   make eval-brain-regression TAG=p3-p2 MODEL=eu.anthropic.claude-sonnet-4-6
##   PARALLELISM=3 make eval-brain-regression TAG=fast   # override (risky on large Java repos)
##
## Default PARALLELISM is 2 — higher values can trigger OOM-kill on the
## largest Java/Go repos (observed with 3 concurrent keycloak cases
## hitting ~14 GB RSS per worker). Override only if your machine has
## ≥32 GB RAM.
##
## Requires valid AWS_PROFILE / AWS_SESSION_TOKEN for Bedrock.
eval-brain-regression: ensure-backend-deps
	@if [ -z "$(TAG)" ]; then echo "TAG is required, e.g. make eval-brain-regression TAG=v2k"; exit 1; fi
	@MODEL=$${MODEL:-eu.anthropic.claude-sonnet-4-6}; \
	 EXPLORER=$${EXPLORER:-eu.anthropic.claude-haiku-4-5-20251001-v1:0}; \
	 PARALLELISM=$${PARALLELISM:-2}; \
	 TAG=$(TAG); \
	 echo "=== PR Brain regression suite: TAG=$$TAG MODEL=$$MODEL PARALLELISM=$$PARALLELISM ==="; \
	 PIDS=""; \
	 for suite in requests greptile-sentry greptile-grafana greptile-keycloak; do \
	   LOG=/tmp/brain-regression-$$suite-$$TAG.log; \
	   echo "[$$(date +%H:%M:%S)] launching $$suite -> $$LOG"; \
	   CONDUCTOR_PR_BRAIN_V2=1 $(PYTHON) eval/code_review/run.py --brain \
	     --provider bedrock --model $$MODEL --explorer-model $$EXPLORER \
	     --filter $$suite --parallelism $$PARALLELISM --verbose \
	     > $$LOG 2>&1 & \
	   PIDS="$$PIDS $$!"; \
	 done; \
	 FAIL=0; \
	 for pid in $$PIDS; do \
	   wait $$pid || FAIL=$$((FAIL + 1)); \
	 done; \
	 echo ""; \
	 echo "=== Consolidated results (TAG=$$TAG) ==="; \
	 for suite in requests greptile-sentry greptile-grafana greptile-keycloak; do \
	   LOG=/tmp/brain-regression-$$suite-$$TAG.log; \
	   echo "--- $$suite ---"; \
	   tail -30 $$LOG | grep -E "^Aggregate|^Case|LLM Judge Verdicts|Catch rate" || true; \
	   echo ""; \
	 done; \
	 if [ $$FAIL -gt 0 ]; then \
	   echo "!! $$FAIL of 4 suites exited with non-zero status — check logs for truncated runs (OOM, bedrock throttle, etc.)"; \
	   exit 1; \
	 fi

## Validate Python↔TS tool parity (shared contract + cross-language tests)
test-parity: ensure-backend-deps ensure-extension-deps
	@echo "Step 1: Check contract matches Python schemas..."
	cd backend && $(PYTHON) ../scripts/generate_tool_contracts.py --check
	@echo "Step 2: Compile extension & validate TS + subprocess tools against contract..."
	cd extension && npm run compile
	cd extension && node tests/validate_contract.js
	@echo "Step 3: Run cross-language parity tests..."
	cd backend && $(PYTHON) -m pytest tests/test_tool_parity_subprocess.py tests/test_tool_parity_deep.py tests/test_tool_parity_ast.py -v
	@echo "All parity checks passed."

## Release gate: simulate a fresh deploy by deleting all wasm grammars,
## re-downloading them from GitHub (the same path that runs in production
## via npm postinstall), then running the full test suite to verify the
## downloaded grammars produce working AST tools.
##
## NOT part of `make test` — requires network, ~8MB download, slower.
## Run before release. CI should run this on a release branch.
postdeploy-check: ensure-extension-deps
	@echo "=== Post-deploy check: forcing grammar re-download ==="
	@echo "Removing all wasm grammars to simulate Azure DevOps deploy env..."
	rm -f extension/grammars/tree-sitter-*.wasm \
	      extension/grammars/web-tree-sitter.wasm
	@echo ""
	@echo "Downloading fresh wasms from GitHub releases..."
	cd extension && bash scripts/download-grammars.sh
	@echo ""
	@echo "=== Running full test suite with freshly downloaded wasms ==="
	$(MAKE) test
	@echo ""
	@echo "[ok] Post-deploy check passed — tools work with downloaded grammars"

# ===========================
# Build / Compile
# ===========================
.PHONY: compile compile-all compile-ts compile-webview compile-css package package-teams-bot update-contracts update-prompt-library

## Compile extension (TypeScript + React WebView + Tailwind CSS)
compile: compile-all
	@echo "Extension compiled!"

## Compile all (TS + WebView + CSS via npm run compile)
compile-all: ensure-extension-deps
	@echo "Compiling extension (TS + React WebView + CSS)..."
	cd extension && npm run compile

## Compile TypeScript only
compile-ts: ensure-extension-deps
	@echo "Compiling TypeScript..."
	cd extension && npm run compile:ts

## Compile React WebView only
compile-webview: ensure-extension-deps
	@echo "Building React WebView..."
	cd extension && npm run compile:webview

## Compile Tailwind CSS only
compile-css: ensure-extension-deps
	@echo "Building Tailwind CSS..."
	cd extension && npm run build:css

## Package extension as .vsix (compiles first)
package: compile
	@echo "Packaging VS Code extension..."
	cd extension && npx @vscode/vsce package
	@echo "Extension packaged! (.vsix file in extension/)"

## Package Microsoft Teams bot app for sideloading (Phase 1).
## Reads bot_id from config/conductor.secrets.local.yaml (teams.app_id) by default.
## Tunnel host MUST be provided; set TEAMS_TUNNEL_HOST once in your shell.
##
## Usage:
##   export TEAMS_TUNNEL_HOST=kalok-test.ngrok.app
##   make package-teams-bot
## Or one-shot:
##   make package-teams-bot TEAMS_TUNNEL_HOST=kalok-test.ngrok.app
## Override bot_id with TEAMS_BOT_ID=<...> if you want to package against a different app.
package-teams-bot: ensure-backend-deps
	@if [ -z "$(TEAMS_TUNNEL_HOST)" ]; then \
		echo "Error: TEAMS_TUNNEL_HOST not set."; \
		echo "  One-shot:   make package-teams-bot TEAMS_TUNNEL_HOST=kalok-test.ngrok.app"; \
		echo "  Persistent: export TEAMS_TUNNEL_HOST=kalok-test.ngrok.app"; \
		exit 1; \
	fi
	@bot_id="$(TEAMS_BOT_ID)"; \
	if [ -z "$$bot_id" ]; then \
		bot_id=$$(cd backend && $(PYTHON) -c "from app.config import get_config; print(get_config().teams_secrets.app_id)" 2>/dev/null); \
	fi; \
	if [ -z "$$bot_id" ]; then \
		echo "Error: bot_id not resolved."; \
		echo "  Set teams.app_id in config/conductor.secrets.local.yaml,"; \
		echo "  or override with: make package-teams-bot TEAMS_BOT_ID=<client-id> TEAMS_TUNNEL_HOST=<host>"; \
		exit 1; \
	fi; \
	echo "Packaging Teams bot app..."; \
	echo "  bot-id:      $$bot_id"; \
	echo "  tunnel-host: $(TEAMS_TUNNEL_HOST)"; \
	cd teams-bot && $(PYTHON) build.py --bot-id "$$bot_id" --tunnel-host "$(TEAMS_TUNNEL_HOST)"

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
	@docker image prune -f --filter "label=com.docker.compose.project=docker" >/dev/null 2>&1 || true
	@echo "App tier starting. Backend: localhost:8000, Langfuse: localhost:3001"

## Rebuild and restart a single app service (usage: make app-rebuild SVC=backend)
app-rebuild:
	@echo "Rebuilding $(SVC)..."
	docker compose -f $(APP_COMPOSE) up -d --build --force-recreate $(SVC)
	@docker image prune -f --filter "label=com.docker.compose.project=docker" >/dev/null 2>&1 || true
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
	-docker image prune -f --filter "label=com.docker.compose.project=docker" 2>/dev/null
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
	@echo "  make test               Run all tests (backend + extension + webview + parity)"
	@echo "  make test-backend       Run backend unit tests"
	@echo "  make test-extension     Run extension service tests (node:test)"
	@echo "  make test-webview       Run React WebView tests (vitest)"
	@echo "  make test-frontend      Run all frontend tests (extension + webview)"
	@echo "  make test-parity        Validate Python<>TS tool parity"
	@echo "  make integration-test   Run integration tests (needs API keys)"
	@echo ""
	@echo "Build:"
	@echo "  make compile            Compile extension (TypeScript + CSS)"
	@echo "  make package            Package extension as .vsix"
	@echo "  make package-teams-bot TEAMS_TUNNEL_HOST=<host>  Package Teams bot app (.zip)"
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
