/**
 * LSP-driven context resolver for Conductor.
 *
 * Given a file URI and cursor position, calls VS Code's built-in LSP command
 * layer to retrieve a symbol's definition and its call-site references, then
 * ranks and trims the references by proximity:
 *
 *   1. Same-file references (priority 0)
 *   2. Same-module (same directory) references (priority 1)
 *   3. Cross-module references (priority 2)
 *
 * Falls back gracefully — returning an empty result — when no LSP provider is
 * registered or when the underlying command throws.
 *
 * Architecture note
 * -----------------
 * `import type` is used for the `vscode` namespace so that the compiled
 * CommonJS output contains **no** top-level `require('vscode')`.  This lets
 * the module be safely imported by the Node.js test runner without a VS Code
 * host.  The actual `require('vscode')` is deferred to inside `resolveLspContext`
 * and is only executed when that function is called at VS Code runtime.
 *
 * @module services/lspResolver
 */

import * as path from 'path';

// Type-only import — fully erased by the TypeScript compiler (no require in output).
import type * as vscodeT from 'vscode';

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

/** A half-open character range, mirroring vscode.Range but without the VS Code dependency. */
export interface LspRange {
    start: { line: number; character: number };
    end: { line: number; character: number };
}

/** A resolved symbol location with a workspace-relative path. */
export interface LspLocation {
    /** Workspace-relative path to the file. */
    path: string;
    range: LspRange;
}

export interface LspResolveResult {
    /** The primary definition of the symbol, if any LSP provider returned one. */
    definition?: LspLocation;
    /** Up to MAX_RELATED references, ranked by proximity to the source file. */
    references: LspLocation[];
}

/** Priority bucket assigned to a reference. Lower = closer to the source file. */
export type RefPriority = 0 | 1 | 2;

/**
 * Minimal structural type for a location object.
 * Compatible with `vscode.Location` and with plain test fixtures that do not
 * depend on the `vscode` module.
 */
export interface LocLike {
    uri: { fsPath: string };
    range: {
        start: { line: number; character: number };
        end: { line: number; character: number };
    };
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Maximum number of references to include in the resolved result. */
export const MAX_RELATED = 3;

const LOG = '[LspResolver]';

// ---------------------------------------------------------------------------
// Pure helpers — no VS Code dependency, fully unit-testable
// ---------------------------------------------------------------------------

/**
 * Assign a proximity priority to a reference relative to the source file.
 *
 * | Priority | Meaning                               |
 * |----------|---------------------------------------|
 * | 0        | Same file (highest relevance)         |
 * | 1        | Same directory / module               |
 * | 2        | Different directory (cross-module)    |
 */
export function refPriority(refFsPath: string, sourceFsPath: string): RefPriority {
    if (refFsPath === sourceFsPath) {
        return 0;
    }
    if (path.dirname(refFsPath) === path.dirname(sourceFsPath)) {
        return 1;
    }
    return 2;
}

/**
 * Stable-sort an array of location-like objects by proximity to `sourceFsPath`.
 *
 * Locations in the same file come first, then same-directory locations, then
 * everything else.  The original LSP-server order is preserved within each
 * priority bucket (stable sort via index tie-breaking).
 *
 * Generic so it can be called with `vscode.Location[]` at runtime or with
 * plain test fixtures that do not import `vscode`.
 */
export function rankReferences<L extends { uri: { fsPath: string } }>(
    locations: L[],
    sourceFsPath: string,
): L[] {
    return locations
        .map((loc, idx) => ({ loc, idx, pri: refPriority(loc.uri.fsPath, sourceFsPath) }))
        .sort((a, b) => a.pri - b.pri || a.idx - b.idx)
        .map(({ loc }) => loc);
}

/**
 * Core result-building logic: selects the best definition and up to `max`
 * proximity-ranked, deduplicated references.
 *
 * This function is **pure** (no VS Code calls, no side effects) and is
 * exported specifically so it can be tested in a Node.js environment without
 * a VS Code host.
 *
 * @param rawDefs      Normalised definition locations from the LSP provider.
 * @param rawRefs      Reference locations from the LSP provider.
 * @param sourceFsPath Absolute filesystem path of the file under edit.
 * @param toRelative   Converts an absolute `fsPath` to a workspace-relative string.
 * @param max          Maximum references to return (default: MAX_RELATED).
 */
export function resolveFromRawResults(
    rawDefs: LocLike[],
    rawRefs: LocLike[],
    sourceFsPath: string,
    toRelative: (fsPath: string) => string,
    max: number = MAX_RELATED,
): LspResolveResult {
    const result: LspResolveResult = { references: [] };

    // --- Definition: take the first location the LSP server returns -----------
    if (rawDefs.length > 0) {
        const def = rawDefs[0];
        result.definition = {
            path: toRelative(def.uri.fsPath),
            range: {
                start: { ...def.range.start },
                end: { ...def.range.end },
            },
        };
    }

    // --- References: rank by proximity, deduplicate, cap at `max` ------------
    const ranked = rankReferences(rawRefs, sourceFsPath);
    const seen = new Set<string>();

    for (const ref of ranked) {
        if (result.references.length >= max) {
            break;
        }
        // Deduplicate by exact position to avoid duplicate entries from LSP.
        const key = `${ref.uri.fsPath}\0${ref.range.start.line}\0${ref.range.start.character}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);

        result.references.push({
            path: toRelative(ref.uri.fsPath),
            range: {
                start: { ...ref.range.start },
                end: { ...ref.range.end },
            },
        });
    }

    return result;
}

// ---------------------------------------------------------------------------
// VS Code shell — deferred require, not called by tests
// ---------------------------------------------------------------------------

/**
 * Normalise the union return value of `vscode.executeDefinitionProvider`.
 * The command can return `Location[]` or `LocationLink[]`; both are collapsed
 * to the common `LocLike` shape.
 */
function normaliseDefinitions(raw: unknown[]): LocLike[] {
    return raw.map(item => {
        const loc = item as Record<string, unknown>;
        if ('targetUri' in loc) {
            // LocationLink — use targetSelectionRange if present, else targetRange.
            const uri = loc['targetUri'] as { fsPath: string };
            const range = (loc['targetSelectionRange'] ?? loc['targetRange']) as LocLike['range'];
            return { uri, range };
        }
        return item as LocLike;
    });
}

/**
 * Resolve the definition and up to `MAX_RELATED` proximity-ranked references
 * for the symbol at `position` inside `uri`.
 *
 * Uses VS Code's built-in LSP command layer:
 * - `vscode.executeDefinitionProvider`
 * - `vscode.executeReferenceProvider`
 *
 * Each step is individually guarded: a missing or throwing provider produces
 * a partial result rather than an error.
 *
 * @param uri      URI of the file that contains the symbol of interest.
 *                 Typically `editor.document.uri` from the active text editor.
 * @param position Cursor position within `uri` (0-based line/character).
 *                 Typically derived from `editor.selection.active`.
 */
export async function resolveLspContext(
    uri: vscodeT.Uri,
    position: vscodeT.Position,
): Promise<LspResolveResult> {
    // Deferred require — only runs inside VS Code; never executed by tests.
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const vscode = require('vscode') as typeof vscodeT;

    const result: LspResolveResult = { references: [] };

    // Helper: convert an absolute fsPath → workspace-relative string.
    const toRelative = (fsPath: string): string =>
        vscode.workspace.asRelativePath(vscode.Uri.file(fsPath), false);

    // ---- Definition ---------------------------------------------------------

    console.log(
        `${LOG} resolving definition at ${uri.fsPath}:${position.line}:${position.character}`,
    );

    try {
        const raw = await vscode.commands.executeCommand<unknown[]>(
            'vscode.executeDefinitionProvider',
            uri,
            position,
        ) ?? [];

        const defs = normaliseDefinitions(raw);
        console.log(`${LOG} raw definition count: ${defs.length}`);

        if (defs.length > 0) {
            const partial = resolveFromRawResults(defs, [], uri.fsPath, toRelative);
            result.definition = partial.definition;
            console.log(
                `${LOG} definition → ${result.definition?.path}:${result.definition?.range.start.line}`,
            );
        } else {
            console.log(`${LOG} no definition returned by provider`);
        }
    } catch (err) {
        console.log(`${LOG} definition provider unavailable —`, err);
    }

    // ---- References ---------------------------------------------------------

    console.log(`${LOG} resolving references`);

    try {
        const raw = await vscode.commands.executeCommand<LocLike[]>(
            'vscode.executeReferenceProvider',
            uri,
            position,
        ) ?? [];

        console.log(`${LOG} raw reference count: ${raw.length}`);

        // Log the ranked order before trimming so callers can see prioritisation.
        const ranked = rankReferences(raw, uri.fsPath);
        const preview = ranked.slice(0, MAX_RELATED * 2);
        for (const [i, ref] of preview.entries()) {
            const p = refPriority(ref.uri.fsPath, uri.fsPath);
            console.log(
                `${LOG}   candidate[${i}] priority=${p} ${ref.uri.fsPath}:${ref.range.start.line}`,
            );
        }

        const partial = resolveFromRawResults([], raw, uri.fsPath, toRelative);
        result.references = partial.references;

        for (const [i, ref] of result.references.entries()) {
            console.log(`${LOG} ref[${i}] → ${ref.path}:${ref.range.start.line}`);
        }
    } catch (err) {
        console.log(`${LOG} reference provider unavailable —`, err);
    }

    console.log(
        `${LOG} resolved: definition=${result.definition ? 'yes' : 'no'}, ` +
        `references=${result.references.length}`,
    );

    return result;
}
