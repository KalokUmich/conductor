# Conductor Project Makefile
# ===========================

.PHONY: all setup setup-backend setup-extension venv install test test-backend test-extension compile clean help

# Python virtual environment
VENV_DIR := .venv
PYTHON := $(VENV_DIR)/bin/python
PIP := $(VENV_DIR)/bin/pip
PYTEST := $(VENV_DIR)/bin/pytest

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
	$(PIP) install -r backend/requirements.txt
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
	@# Verify venv is usable
	@if [ ! -f "$(PIP)" ]; then \
		echo "❌ Virtual environment is broken. Removing and please try again."; \
		rm -rf $(VENV_DIR); \
		exit 1; \
	fi

# ===========================
# Install (alias for setup)
# ===========================

## Install all dependencies (alias for setup)
install: setup

# ===========================
# Testing
# ===========================

## Run all tests
test: test-backend test-extension
	@echo "✅ All tests passed!"

## Run backend tests
test-backend: venv
	@echo "🧪 Running backend tests..."
	cd backend && $(CURDIR)/$(PYTEST) tests/ -v

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
	@echo "  make setup          - Create venv and install all dependencies"
	@echo "  make setup-backend  - Setup backend only (venv + pip install)"
	@echo "  make setup-extension- Setup extension only (npm install)"
	@echo "  make venv           - Create Python virtual environment"
	@echo ""
	@echo "Testing:"
	@echo "  make test           - Run all tests"
	@echo "  make test-backend   - Run backend tests only"
	@echo "  make test-extension - Run extension tests only"
	@echo ""
	@echo "Build:"
	@echo "  make compile        - Compile extension (TypeScript + CSS)"
	@echo "  make compile-ts     - Compile TypeScript only"
	@echo "  make compile-css    - Build Tailwind CSS only"
	@echo ""
	@echo "Other:"
	@echo "  make clean          - Remove all generated files"
	@echo "  make help           - Show this help message"

