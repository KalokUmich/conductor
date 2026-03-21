#!/usr/bin/env bash
# download-grammars.sh
# Downloads pre-built tree-sitter .wasm grammar files for web-tree-sitter.
#
# Grammars are fetched from the tree-sitter GitHub releases. Each language
# grammar repo publishes a tree-sitter-<lang>.wasm artifact that is compatible
# with the web-tree-sitter runtime.
#
# Usage:
#   bash scripts/download-grammars.sh          # from extension/ directory
#   npm run download-grammars                  # via npm script
#
# The downloaded .wasm files are placed in extension/grammars/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTENSION_DIR="$(dirname "$SCRIPT_DIR")"
GRAMMARS_DIR="$EXTENSION_DIR/grammars"
USE_LATEST=false

# Parse flags
for arg in "$@"; do
    case "$arg" in
        --latest) USE_LATEST=true ;;
    esac
done

mkdir -p "$GRAMMARS_DIR"

# ---- Configuration --------------------------------------------------------
# Each entry: "lang  wasm_filename  github_org/repo  pinned_tag"
# Pinned tags are known-good versions compatible with web-tree-sitter 0.26.x.
# Use --latest to ignore pinned tags and always fetch the latest release.

LANGUAGES=(
    "python      tree-sitter-python.wasm        tree-sitter/tree-sitter-python       v0.23.6"
    "javascript  tree-sitter-javascript.wasm     tree-sitter/tree-sitter-javascript   v0.23.1"
    "typescript  tree-sitter-typescript.wasm     tree-sitter/tree-sitter-typescript   v0.23.2"
    "java        tree-sitter-java.wasm           tree-sitter/tree-sitter-java         v0.23.5"
    "go          tree-sitter-go.wasm             tree-sitter/tree-sitter-go           v0.23.4"
    "rust        tree-sitter-rust.wasm           tree-sitter/tree-sitter-rust         v0.23.2"
    "c           tree-sitter-c.wasm              tree-sitter/tree-sitter-c            v0.23.4"
    "cpp         tree-sitter-cpp.wasm            tree-sitter/tree-sitter-cpp          v0.23.4"
)

# ---- Helpers --------------------------------------------------------------

download_grammar() {
    local lang="$1"
    local wasm_file="$2"
    local repo="$3"
    local tag="$4"
    local dest="$GRAMMARS_DIR/$wasm_file"

    if [[ -f "$dest" ]] && [[ "$USE_LATEST" == false ]]; then
        echo "  [skip] $wasm_file already exists (use --latest to force re-download)"
        return 0
    fi

    # With --latest, always use latest release URL; otherwise use pinned tag
    local url
    if [[ "$USE_LATEST" == true ]]; then
        url="https://github.com/$repo/releases/latest/download/$wasm_file"
        echo "  [download] $lang (latest): $url"
    else
        url="https://github.com/$repo/releases/download/$tag/$wasm_file"
        echo "  [download] $lang ($tag): $url"
    fi

    local fallback_url="https://github.com/$repo/releases/latest/download/$wasm_file"

    if command -v curl &>/dev/null; then
        if ! curl -fsSL --retry 3 -o "$dest" "$url"; then
            if [[ "$USE_LATEST" == false ]]; then
                echo "  [warn] Pinned tag failed, trying latest release..."
                if ! curl -fsSL --retry 3 -o "$dest" "$fallback_url"; then
                    echo "  [error] Could not download $wasm_file."
                    rm -f "$dest"
                    return 1
                fi
            else
                echo "  [error] Could not download $wasm_file."
                rm -f "$dest"
                return 1
            fi
        fi
    elif command -v wget &>/dev/null; then
        if ! wget -q -O "$dest" "$url"; then
            if [[ "$USE_LATEST" == false ]]; then
                if ! wget -q -O "$dest" "$fallback_url"; then
                    echo "  [error] Could not download $wasm_file."
                    rm -f "$dest"
                    return 1
                fi
            else
                echo "  [error] Could not download $wasm_file."
                rm -f "$dest"
                return 1
            fi
        fi
    else
        echo "  [error] Neither curl nor wget found. Cannot download grammars."
        return 1
    fi

    echo "  [ok] $wasm_file"
}

# ---- Main -----------------------------------------------------------------

echo "Downloading tree-sitter grammar .wasm files to $GRAMMARS_DIR"
echo ""

FAILED=0
for entry in "${LANGUAGES[@]}"; do
    # shellcheck disable=SC2086
    set -- $entry
    lang="$1"
    wasm_file="$2"
    repo="$3"
    tag="$4"

    if ! download_grammar "$lang" "$wasm_file" "$repo" "$tag"; then
        FAILED=$((FAILED + 1))
    fi
done

echo ""
if [[ "$FAILED" -gt 0 ]]; then
    echo "Warning: $FAILED grammar(s) failed to download."
    echo "The tree-sitter service will fall back to regex extraction for missing languages."
    # Don't exit with error -- partial grammars are still useful
else
    echo "All grammars downloaded successfully."
fi

# Also ensure the tree-sitter runtime .wasm file is available.
# web-tree-sitter ships it inside node_modules/web-tree-sitter/.
# 0.26.x renamed it from tree-sitter.wasm to web-tree-sitter.wasm.
TS_WASM_NEW="$EXTENSION_DIR/node_modules/web-tree-sitter/web-tree-sitter.wasm"
TS_WASM_OLD="$EXTENSION_DIR/node_modules/web-tree-sitter/tree-sitter.wasm"
if [[ -f "$TS_WASM_NEW" ]]; then
    cp "$TS_WASM_NEW" "$GRAMMARS_DIR/web-tree-sitter.wasm"
    echo "Copied web-tree-sitter.wasm runtime to grammars/."
elif [[ -f "$TS_WASM_OLD" ]]; then
    cp "$TS_WASM_OLD" "$GRAMMARS_DIR/tree-sitter.wasm"
    echo "Copied tree-sitter.wasm runtime to grammars/."
else
    echo "Note: web-tree-sitter runtime wasm not found."
    echo "      Run 'npm install' first, then re-run this script."
fi

echo "Done."
