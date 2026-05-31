# Conductor Project Makefile
# ===========================

# Python virtual environment
VENV_DIR := .venv

PYTHON := $(CURDIR)/$(VENV_DIR)/bin/python
PIP := $(CURDIR)/$(VENV_DIR)/bin/pip
PYTEST := $(CURDIR)/$(VENV_DIR)/bin/pytest

# Claude Code CLI — runtime dependency of claude-agent-sdk (the SDK drives the
# CLI as a subprocess). Pinned here once; used by both `make setup-claude-cli`
# (dev/venv host) and backend/Dockerfile (ECS image) so behavior is identical.
CLAUDE_CLI_VERSION := 2.1.158
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

# Default service for single-service app-tier targets (override: make app-rebuild SVC=foo)
SVC ?= backend

# WebSocket Configuration
WS_PING_INTERVAL := 20.0
WS_PING_TIMEOUT := 20.0
WS_OPTIONS := --ws-ping-interval $(WS_PING_INTERVAL) --ws-ping-timeout $(WS_PING_TIMEOUT)

# Bare `make` shows help instead of silently running a heavy full setup.
.DEFAULT_GOAL := help

# Explicit full-setup target (was the old default)
.PHONY: all
all: setup

# ===========================
# Setup
# ===========================
##@ Setup
.PHONY: setup setup-backend setup-extension setup-claude-cli venv ensure-backend-deps install browser-install

## Create venv and install all dependencies
setup: venv setup-backend setup-claude-cli setup-extension
	@echo "Setup complete!"

## Setup backend (venv + dependencies)
setup-backend: venv
	@echo "Installing backend dependencies..."
	$(PYTHON) -m pip install -r backend/requirements.txt
	@touch $(VENV_DIR)/.backend-deps-stamp
	@echo "Backend setup complete!"

## Install the Claude Code CLI (runtime dep of claude-agent-sdk) on the host.
## Needed when running the Python backend directly on the host (not in Docker) —
## the SDK spawns `claude` as a subprocess. Same pinned version as the image.
setup-claude-cli:
	@echo "Installing Claude Code CLI @ $(CLAUDE_CLI_VERSION)..."
	@if command -v npm >/dev/null 2>&1; then \
		npm install -g @anthropic-ai/claude-code@$(CLAUDE_CLI_VERSION); \
		claude --version || echo "WARN: 'claude' not on PATH after install — check your npm global bin is on PATH"; \
	else \
		echo "WARN: npm not found — install Node.js, then re-run 'make setup-claude-cli'"; \
	fi

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

# (internal) Create Python virtual environment if it doesn't exist
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

# (internal) Ensure backend dependencies are installed and in sync with requirements.txt.
# Reinstalls when core imports are missing OR requirements.txt is newer than
# the install stamp (mirrors ensure-extension-deps' lockfile-staleness check).
ensure-backend-deps: venv
	@if ! "$(PYTHON)" -c "import fastapi, pytest, uvicorn" >/dev/null 2>&1 \
	   || [ backend/requirements.txt -nt $(VENV_DIR)/.backend-deps-stamp ]; then \
		echo "Backend dependencies missing or stale -- installing..."; \
		$(PYTHON) -m pip install -r backend/requirements.txt; \
		touch $(VENV_DIR)/.backend-deps-stamp; \
		echo "Backend dependencies ready"; \
	fi

# (internal) Ensure extension dependencies are installed and in sync with the lockfile.
# Triggers `npm install` (which fires the postinstall hook → grammar download
# + SHA verification) when node_modules is missing or package-lock.json is
# newer than node_modules. No-op on a normal incremental build.
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
##@ Run Servers
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
##@ Testing
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

## Run all 4 PR Brain eval suites serially + print consolidated composite table
##
## Runs requests + greptile-sentry + greptile-grafana + greptile-keycloak
## **sequentially** (one suite at a time) under the current coordinator
## config, logs each suite's summary to /tmp/brain-regression-<suite>-<tag>.log,
## and prints a consolidated composite + Judge table on completion.
##
## Usage:
##   make eval-brain-regression TAG=v2k
##   make eval-brain-regression TAG=p3-p2 MODEL=eu.anthropic.claude-sonnet-4-6
##   PARALLELISM=3 make eval-brain-regression TAG=fast   # override case-level concurrency
##
## **Suite scheduling is serial**, not parallel. Each suite runs its
## own process (sharing a per-repo tree-sitter graph internally), and
## PARALLELISM controls case-level concurrency within that one
## process. Running suites in parallel was the historical default,
## but on machines with < 40 GB RAM it OOM-kills — 4 concurrent
## tree-sitter graphs at 12-15 GB each (sentry / grafana / keycloak)
## exhaust memory and the OOM-killer drops runs at random.
##
## Default PARALLELISM is 2 (case-level). On truly RAM-constrained
## boxes drop to PARALLELISM=1; on ≥32 GB override to 3+. This
## dimension is independent of suite scheduling and safe to tune.
##
## Requires valid AWS_PROFILE / AWS_SESSION_TOKEN for Bedrock.
eval-brain-regression: ensure-backend-deps
	@if [ -z "$(TAG)" ]; then echo "TAG is required, e.g. make eval-brain-regression TAG=v2k"; exit 1; fi
	@MODEL=$${MODEL:-eu.anthropic.claude-sonnet-4-6}; \
	 EXPLORER=$${EXPLORER:-eu.anthropic.claude-haiku-4-5-20251001-v1:0}; \
	 PARALLELISM=$${PARALLELISM:-2}; \
	 TAG=$(TAG); \
	 echo "=== PR Brain regression suite: TAG=$$TAG MODEL=$$MODEL PARALLELISM=$$PARALLELISM (serial suites) ==="; \
	 FAIL=0; \
	 for suite in requests greptile-sentry greptile-grafana greptile-keycloak; do \
	   LOG=/tmp/brain-regression-$$suite-$$TAG.log; \
	   echo "[$$(date +%H:%M:%S)] running $$suite -> $$LOG"; \
	   if CONDUCTOR_PR_BRAIN_V2=1 $(PYTHON) eval/code_review/run.py --brain \
	     --provider bedrock --model $$MODEL --explorer-model $$EXPLORER \
	     --filter $$suite --parallelism $$PARALLELISM --verbose \
	     > $$LOG 2>&1; then \
	     echo "[$$(date +%H:%M:%S)] $$suite OK"; \
	   else \
	     echo "[$$(date +%H:%M:%S)] $$suite FAILED (exit $$?)"; \
	     FAIL=$$((FAIL + 1)); \
	   fi; \
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

## Release gate — re-download wasm grammars from scratch, then run full test suite
##
## Simulates a fresh deploy by deleting all wasm grammars, re-downloading them
## from GitHub (the same path that runs in production via npm postinstall),
## then running the full test suite to verify the downloaded grammars produce
## working AST tools.
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
# Diagnostics
# ===========================
##@ Diagnostics
.PHONY: bedrock-check bedrock-check-docker

## Fast Bedrock reachability check (direct Converse, ~1s, never hangs)
## Run before eval / SDK tests — surfaces expired tokens instantly instead of
## letting the CLI path hang. Override model with BEDROCK_CHECK_MODEL=<id>.
bedrock-check: ensure-backend-deps
	@cd backend && $(PYTHON) scripts/bedrock_check.py

## Same Bedrock reachability check, but INSIDE the running backend container.
## Validates the container resolves local creds (SSO profile via the ~/.aws
## mount, or a bearer token from secrets). Needs `make app-up` running first.
bedrock-check-docker:
	docker exec -w /app conductor-backend python scripts/bedrock_check.py

# ===========================
# Build / Compile
# ===========================
##@ Build / Compile
.PHONY: compile compile-all compile-ts compile-webview compile-css package package-teams-bot update-contracts update-prompt-library

## Compile extension (TypeScript + React WebView + Tailwind CSS)
compile: compile-all
	@echo "Extension compiled!"

# (internal) Compile all (TS + WebView + CSS via npm run compile)
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
##@ Docker — Data Tier
.PHONY: data-up data-down data-logs

## Start Postgres + Redis containers (blocks until both healthchecks pass)
data-up:
	@echo "Starting data tier (Postgres + Redis)..."
	docker compose -f $(DATA_COMPOSE) up -d --wait
	@echo "Data tier healthy. Postgres: localhost:5432, Redis: localhost:6379"

## Stop data tier
data-down:
	@echo "Stopping data tier..."
	docker compose -f $(DATA_COMPOSE) down
	@echo "Data tier stopped."

## View data tier logs
data-logs:
	docker compose -f $(DATA_COMPOSE) logs -f

# ===========================
# App Tier (Backend)
# ===========================
##@ Docker — App Tier
.PHONY: app-up app-rebuild app-restart app-down app-logs

## Start backend container (builds backend image if missing)
app-up:
	@echo "Starting app tier (Backend)..."
	docker compose -f $(APP_COMPOSE) up -d --build
	@docker image prune -f --filter "label=com.docker.compose.project=docker" >/dev/null 2>&1 || true
	@echo "App tier starting. Backend: localhost:8000"

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
##@ Docker — Full Stack
.PHONY: docker-up docker-down docker-clean

## Start full stack (data tier, schema, then app tier)
docker-up: data-up
	@$(MAKE) db-update
	@$(MAKE) app-up
	@echo "Full stack started!"

## Stop full stack
docker-down: app-down data-down
	@echo "Full stack stopped."

## Stop all containers and remove all conductor-related images
docker-clean: docker-down
	@echo "Removing conductor containers and images..."
	-docker rm -f conductor-backend conductor-postgres conductor-redis 2>/dev/null
	-docker rmi conductor/backend:latest postgres:16-alpine redis:7-alpine 2>/dev/null
	-docker image prune -f --filter "label=com.docker.compose.project=docker" 2>/dev/null
	@echo "Docker clean complete."

# ===========================
# Database Schema (Liquibase)
# ===========================
##@ Database
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
# Lint & Format
# ===========================
##@ Lint, Format & Types
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

## Type-check strict-audit modules (Phase 11.3). Expects zero errors.
## Permissive across the rest of the codebase — legacy modules have
## accumulated type debt that's out of scope for this phase.
.PHONY: typecheck-strict
typecheck-strict:
	@echo "Type-checking strict modules..."
	$(PYTHON) -m mypy \
	  backend/app/code_review/splitter.py \
	  backend/app/code_review/translate.py \
	  backend/app/scratchpad/
	@echo "Strict-module typecheck passed."

## Type-check the whole backend (informational — does NOT gate CI yet).
## Reports ~40 pre-existing errors mostly in ai_provider resolver +
## older tool helpers. Used to track progress reducing the permissive
## module list as legacy code gets annotated.
.PHONY: typecheck
typecheck:
	@echo "Type-checking full backend (informational)..."
	$(PYTHON) -m mypy backend/app || true
	@echo "Full typecheck complete."

# ===========================
# Clean
# ===========================
##@ Clean & Help
.PHONY: clean clean-all

## Clean build artifacts + caches (keeps venv + node_modules)
clean:
	@echo "Cleaning caches and build artifacts..."
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache backend/htmlcov
	rm -f backend/*.duckdb backend/*.duckdb.wal
	rm -rf extension/out
	@echo "Clean complete! (run 'make clean-all' to also remove venv + node_modules)"

## Deep clean: everything above PLUS venv and node_modules (forces a fresh setup)
clean-all: clean
	@echo "Removing virtual environment and node_modules..."
	rm -rf $(VENV_DIR)
	rm -rf extension/node_modules
	@echo "Deep clean complete! Run 'make setup' to reinstall."

# ===========================
# Help
# ===========================
.PHONY: help

## Show this help message (auto-generated from '##' comments above each target)
help:
	@echo "Conductor Project - Available Commands"
	@echo "======================================="
	@awk ' \
		/^$$/ { doc="" } \
		/^##@ / { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^## / { if (doc == "") doc=substr($$0, 4) } \
		/^[a-zA-Z0-9_-]+:/ { \
			if (doc != "") { \
				name=$$0; sub(/:.*/, "", name); \
				printf "  \033[36m%-22s\033[0m %s\n", name, doc; \
				doc=""; \
			} \
		} \
	' $(MAKEFILE_LIST)
