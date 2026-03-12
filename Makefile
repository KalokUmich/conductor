# Conductor Project Makefile
# ===========================

.PHONY: all setup setup-backend setup-extension venv ensure-backend-deps install run-backend run-backend-prod run-backend-port test test-backend test-extension integration-test compile compile-ts compile-css package clean help

# Python virtual environment
VENV_DIR := .venv

PYTHON := $(CURDIR)/$(VENV_DIR)/bin/python
PIP := $(CURDIR)/$(VENV_DIR)/bin/pip
PYTEST := $(CURDIR)/$(VENV_DIR)/bin/pytest
UVICORN := $(PYTHON) -m uvicorn

# Default target
all: setup

# ===========================
# Setup
# ===========================

## Create venv and install all dependencies
setup: venv setup-backend setup-extension
	@echo "✅ Setup complete!"

## Setup backend (venv + dependencies)
setup-backend: venv
	@echo "📦 Installing backend dependencies..."
	$(PYTHON) -m pip install -r backend/requirements.txt
	@echo "✅ Backend setup complete!"

## Setup extension (npm install)
setup-extension:
	@echo "📦 Installing extension dependencies..."
	cd extension && npm install
	@echo "✅ Extension setup complete!"

# ===========================
# Virtual Environment
# ===========================

## Create Python virtual environment if it doesn't exist
venv:
	@if [ ! -d "$(VENV_DIR)" ]; then \
		echo "🐍 Creating virtual environment..."; \
		python3 -m venv $(VENV_DIR) || { \
			echo "❌ Failed to create venv. Try: sudo apt install python3-venv"; \
			exit 1; \
		}; \
		echo "✅ Virtual environment created at $(VENV_DIR)"; \
	else \
		echo "✅ Virtual environment already exists at $(VENV_DIR)"; \
	fi
	@# Repair stale interpreter symlinks when python moved (for example /usr/local/bin -> /usr/bin)
	@if [ -L "$(PYTHON)" ] && [ ! -e "$(PYTHON)" ]; then \
		echo "⚠️  Virtual environment interpreter symlink is stale. Repairing..."; \
		ln -sf "$$(command -v python3)" "$(PYTHON)"; \
	fi
	@# Verify venv is usable
	@if [ ! -x "$(PYTHON)" ] || ! "$(PYTHON)" -V >/dev/null 2>&1 || ! "$(PYTHON)" -m pip --version >/dev/null 2>&1; then \
		echo "❌ Virtual environment is broken. Recreating $(VENV_DIR)..."; \
		rm -rf $(VENV_DIR); \
		python3 -m venv $(VENV_DIR) || { \
			echo "❌ Failed to recreate venv. Try: sudo apt install python3-venv"; \
			exit 1; \
		}; \
		echo "✅ Virtual environment recreated at $(VENV_DIR)"; \
	fi

## Ensure backend dependencies are installed in the venv
ensure-backend-deps: venv
	@if ! "$(PYTHON)" -c "import fastapi, pytest, uvicorn" >/dev/null 2>&1; then \
		echo "📦 Backend dependencies missing — installing..."; \
		$(PYTHON) -m pip install -r backend/requirements.txt; \
		echo "✅ Backend dependencies ready"; \
	fi

# ===========================
# Install (alias for setup)
# ===========================

## Install all dependencies (alias for setup)
install: setup

# ===========================
# Run Servers
# ===========================

# WebSocket Configuration
# - ws-ping-interval: Ping every 20 seconds to check connection health
# - ws-ping-timeout: Wait 20 seconds for pong response before closing
WS_PING_INTERVAL := 20.0
WS_PING_TIMEOUT := 20.0
WS_OPTIONS := --ws-ping-interval $(WS_PING_INTERVAL) --ws-ping-timeout $(WS_PING_TIMEOUT)

## Start backend server (development mode with auto-reload)
run-backend: ensure-backend-deps
	@echo "🚀 Starting backend server..."
	@echo "   Swagger UI: http://localhost:8000/docs"
	@echo "   ReDoc: http://localhost:8000/redoc"
	@echo "   WebSocket: ws://localhost:8000/ws/chat/{room_id}"
	@echo "   WebSocket Ping: $(WS_PING_INTERVAL)s interval, $(WS_PING_TIMEOUT)s timeout"
	cd backend && $(UVICORN) app.main:app --reload --reload-dir app --host 0.0.0.0 --port 8000 $(WS_OPTIONS)

## Start backend server (production mode)
run-backend-prod: ensure-backend-deps
	@echo "🚀 Starting backend server (production)..."
	cd backend && $(UVICORN) app.main:app --host 0.0.0.0 --port 8000 --workers 4 $(WS_OPTIONS)

## Start backend with custom port (usage: make run-backend-port PORT=8001)
run-backend-port: ensure-backend-deps
	@echo "🚀 Starting backend server on port $(PORT)..."
	cd backend && $(UVICORN) app.main:app --reload --reload-dir app --host 0.0.0.0 --port $(PORT) $(WS_OPTIONS)

# ===========================
# Testing
# ===========================

## Run all tests
test: test-backend test-extension
	@echo "✅ All tests passed!"

## Run backend tests
test-backend: ensure-backend-deps
	@echo "🧪 Running backend tests..."
	cd backend && $(PYTHON) -m pytest tests/ -v

## Run backend integration tests (requires real API credentials)
integration-test: ensure-backend-deps
	@echo "🧪 Running backend integration tests (requires API credentials)..."
	cd backend && $(PYTHON) -m pytest tests/ -v -s -m integration

## Run extension tests (if any)
test-extension:
	@echo "🧪 Running extension tests..."
	@if [ -f "extension/package.json" ] && grep -q '"test"' extension/package.json; then \
		cd extension && npm test; \
	else \
		echo "⚠️  No extension tests configured"; \
	fi

# ===========================
# Build / Compile
# ===========================

## Compile extension (TypeScript + Tailwind CSS)
compile: compile-ts compile-css
	@echo "✅ Extension compiled!"

## Compile TypeScript
compile-ts:
	@echo "🔨 Compiling TypeScript..."
	cd extension && npm run compile

## Compile Tailwind CSS
compile-css:
	@echo "🎨 Building Tailwind CSS..."
	cd extension && npm run build:css

# ===========================
# Package
# ===========================

## Package extension as .vsix (compiles first)
package: compile
	@echo "📦 Packaging VS Code extension..."
	cd extension && npx @vscode/vsce package
	@echo "✅ Extension packaged! (.vsix file in extension/)"

# ===========================
# Clean
# ===========================

## Clean all generated files
clean:
	@echo "🧹 Cleaning..."
	rm -rf $(VENV_DIR)
	rm -rf backend/__pycache__ backend/**/__pycache__
	rm -rf backend/.pytest_cache
	rm -rf extension/out
	rm -rf extension/node_modules
	@echo "✅ Clean complete!"


# ===========================
# Help
# ===========================

## Show this help message
help:
	@echo "Conductor Project - Available Commands"
	@echo "======================================="
	@echo ""
	@echo "Setup:"
	@echo "  make setup            - Create venv and install all dependencies"
	@echo "  make setup-backend    - Setup backend only (venv + pip install)"
	@echo "  make setup-extension  - Setup extension only (npm install)"
	@echo "  make venv             - Create Python virtual environment"
	@echo ""
	@echo "Run Servers:"
	@echo "  make run-backend      - Start backend server (dev mode, auto-reload)"
	@echo "  make run-backend-prod - Start backend server (production, 4 workers)"
	@echo "  make run-backend-port PORT=8001 - Start on custom port"
	@echo ""
	@echo "Testing:"
	@echo "  make test             - Run all tests (unit only)"
	@echo "  make test-backend     - Run backend unit tests only"
	@echo "  make test-extension   - Run extension tests only"
	@echo "  make integration-test - Run backend integration tests (needs API keys)"
	@echo ""
	@echo "Build:"
	@echo "  make compile          - Compile extension (TypeScript + CSS)"
	@echo "  make compile-ts       - Compile TypeScript only"
	@echo "  make compile-css      - Build Tailwind CSS only"
	@echo "  make package          - Package extension as .vsix (compiles first)"
	@echo ""
	@echo "Other:"
	@echo "  make clean            - Remove all generated files"
	@echo "  make help             - Show this help message"
