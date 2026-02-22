/**
 * Hybrid relevance ranking engine for Conductor V1.
 *
 * Combines three orthogonal signals into one deterministic ranked list:
 *
 *   1. Structural priority — hard ordering anchored by LSP knowledge
 *      (definition > reference > semantic-only).
 *   2. Graph boost — proximity within the dependency graph
 *      (import neighbor, same module, same file).
 *   3. Semantic contribution — cosine similarity from the local vector index,
 *      weighted at 0.3 so it never overrides a structural advantage.
 *
 * Formula
 * -------
 *   structuralScore = baseScore + sameFileBonus + graphBoost
 *   finalScore      = structuralScore + semanticScore * 0.3
 *
 * The definition entry is always placed at rank #1 as a hard invariant (not
 * just a higher numeric score) because the semantic contribution can, in
 * theory, push a reference's finalScore above a bare-definition finalScore.
 *
 * Caps
 * ----
 *   - Max 5 distinct files in the output
 *   - Max 10 symbols total
 *
 * Guarantees
 * ----------
 *   - Pure function: no I/O, no network, no side-effects.
 *   - Deterministic: same inputs always produce the same ordered output.
 *   - Degrades gracefully: works with empty LSP results, empty semantic
 *     results, or both.
 *
 * @module services/relevanceRanker
 */

import * as path from 'path';

import type { LspResolveResult } from './lspResolver';
import type { SearchResult }     from './vectorIndex';

// ---------------------------------------------------------------------------
// Public constants
// ---------------------------------------------------------------------------

/** Maximum number of reference entries accepted from LSP. */
export const MAX_REFERENCES = 3;

/** Maximum number of distinct files in the ranked output. */
export const MAX_FILES = 5;

/** Maximum number of symbol entries in the ranked output. */
export const MAX_SYMBOLS = 10;

/** Semantic score multiplier — keeps semantic signal from overriding structural. */
export const SEMANTIC_WEIGHT = 0.3;

// ---------------------------------------------------------------------------
// Score constants (internal)
// ---------------------------------------------------------------------------

const SCORE_DEFINITION  = 1.0;
const SCORE_REFERENCE   = 0.6;
const BONUS_SAME_FILE   = 0.15;
const BOOST_IMPORT      = 0.2;
const BOOST_SAME_MODULE = 0.1;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type ResultSource = 'definition' | 'reference' | 'semantic';

export interface RankedResult {
    /**
     * Stable identifier.
     * - DB `symbol_id` for semantic-originated entries.
     * - Synthetic `"<path>:<line>"` for LSP-originated entries without a DB ID.
     */
    id: string;

    /** Workspace-relative path to the file containing this symbol. */
    path: string;

    /** 0-based start line, when known from an LSP location. */
    line?: number;

    /** Additive structural component (base + same-file + graph boosts). */
    structuralScore: number;

    /** Raw cosine similarity in [−1, 1] from the vector index (0 when absent). */
    semanticScore: number;

    /** `structuralScore + semanticScore * SEMANTIC_WEIGHT`. */
    finalScore: number;

    /** Primary signal that contributed this entry. */
    source: ResultSource;
}

export interface RankInput {
    /** Workspace-relative path of the file where the cursor lives. */
    currentFile: string;

    /** LSP definition + references for the symbol under the cursor. */
    lsp: LspResolveResult;

    /**
     * Workspace-relative paths of files **directly imported** by `currentFile`.
     * The caller is responsible for resolving raw import strings (e.g. `./utils`)
     * to workspace-relative paths before passing them here.
     */
    importNeighbors: string[];

    /**
     * Semantic search results from `VectorIndex.search()`.
     * Pass an empty array when the semantic layer is unavailable.
     */
    semanticResults: SearchResult[];

    /**
     * Optional map from `symbol_id` → workspace-relative file path.
     * Enables file-level graph boosts for semantic-only entries.
     * If absent, semantic entries without an LSP match are excluded.
     */
    symbolPaths?: Map<string, string>;
}

/**
 * Optional per-call overrides for the ranking caps.
 * Values from `WorkspaceConfig` can be passed here so users can tune the
 * ranking behaviour through `.conductor/config.json` without touching code.
 */
export interface RankOptions {
    /** Override `MAX_REFERENCES`. */
    maxReferences?: number;
    /** Override `MAX_FILES`. */
    maxFiles?: number;
    /** Override `MAX_SYMBOLS`. */
    maxSymbols?: number;
}

// ---------------------------------------------------------------------------
// Internal accumulator type (not exported)
// ---------------------------------------------------------------------------

interface Candidate {
    id: string;
    path: string;
    line?: number;
    baseStructural: number;  // definition=1.0, reference=0.6, semantic-only=0.0
    source: ResultSource;
    semanticScore: number;   // from VectorIndex, 0 if absent
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Rank code symbols by hybrid relevance.
 *
 * @param input    All available signals for the current context.
 * @param options  Optional cap overrides (e.g. from WorkspaceConfig).
 * @returns        Up to MAX_SYMBOLS results spanning up to MAX_FILES files,
 *                 ordered by descending `finalScore` with the LSP definition
 *                 always at position 0 (when present).
 */
export function rank(input: RankInput, options?: RankOptions): RankedResult[] {
    const maxRefs = options?.maxReferences ?? MAX_REFERENCES;
    const candidates = _buildCandidates(input, maxRefs);
    const scored     = _scoreAll(candidates, input);
    const sorted     = _sort(scored);
    return _applyFileCap(
        sorted,
        options?.maxFiles   ?? MAX_FILES,
        options?.maxSymbols ?? MAX_SYMBOLS,
    );
}

// ---------------------------------------------------------------------------
// Step 1 — accumulate candidates from all sources
// ---------------------------------------------------------------------------

function _buildCandidates(input: RankInput, maxReferences: number): Map<string, Candidate> {
    const map = new Map<string, Candidate>();

    // ---- LSP definition -------------------------------------------------------
    const def = input.lsp.definition;
    if (def) {
        const id = `${def.path}:${def.range.start.line}`;
        map.set(id, {
            id,
            path: def.path,
            line: def.range.start.line,
            baseStructural: SCORE_DEFINITION,
            source: 'definition',
            semanticScore: 0,
        });
    }

    // ---- LSP references (limited by maxReferences) ----------------------------
    const refs = input.lsp.references.slice(0, maxReferences);
    for (const ref of refs) {
        const id = `${ref.path}:${ref.range.start.line}`;
        if (!map.has(id)) {
            map.set(id, {
                id,
                path: ref.path,
                line: ref.range.start.line,
                baseStructural: SCORE_REFERENCE,
                source: 'reference',
                semanticScore: 0,
            });
        }
    }

    // ---- Semantic results ------------------------------------------------------
    // If a semantic symbol_id collides with an existing LSP entry id, merge
    // the semanticScore in (take the max).  Otherwise, create a new candidate
    // if we can resolve its file path.
    for (const sem of input.semanticResults) {
        if (map.has(sem.symbol_id)) {
            // Merge into an existing LSP entry with the same id.
            // Preserve the raw semantic score so consumers can inspect it;
            // negative clamping happens later in the finalScore formula.
            const c = map.get(sem.symbol_id)!;
            c.semanticScore = sem.score;
        } else {
            // Pure semantic entry — needs a resolvable path.
            const resolvedPath = input.symbolPaths?.get(sem.symbol_id);
            if (!resolvedPath) {
                // Cannot determine file → skip (keeps engine pure).
                continue;
            }
            map.set(sem.symbol_id, {
                id: sem.symbol_id,
                path: resolvedPath,
                line: undefined,
                baseStructural: 0,
                source: 'semantic',
                semanticScore: sem.score,
            });
        }
    }

    return map;
}

// ---------------------------------------------------------------------------
// Step 2 — compute scores for every candidate
// ---------------------------------------------------------------------------

function _scoreAll(candidates: Map<string, Candidate>, input: RankInput): RankedResult[] {
    const importSet   = new Set(input.importNeighbors);
    const currentDir  = path.dirname(input.currentFile);
    const results: RankedResult[] = [];

    for (const c of candidates.values()) {
        // ---- Same-file structural bonus -----------------------------------------
        const sameFileBonus = c.path === input.currentFile ? BONUS_SAME_FILE : 0;

        // ---- Graph boosts --------------------------------------------------------
        let graphBoost = 0;
        if (c.path) {
            if (importSet.has(c.path))                              graphBoost += BOOST_IMPORT;
            if (path.dirname(c.path) === currentDir && c.path !== input.currentFile)
                                                                    graphBoost += BOOST_SAME_MODULE;
        }

        // ---- Combine ------------------------------------------------------------
        const structuralScore = c.baseStructural + sameFileBonus + graphBoost;
        // Clamp semanticScore to [0, 1] — negative cosine similarity is not useful.
        const semanticScore   = Math.max(0, c.semanticScore);
        const finalScore      = structuralScore + semanticScore * SEMANTIC_WEIGHT;

        results.push({
            id: c.id,
            path: c.path,
            line: c.line,
            structuralScore,
            semanticScore: c.semanticScore,
            finalScore,
            source: c.source,
        });
    }

    return results;
}

// ---------------------------------------------------------------------------
// Step 3 — deterministic sort
// ---------------------------------------------------------------------------

// Source ordering used for tiebreaking (lower = higher priority).
const SOURCE_ORDER: Record<ResultSource, number> = {
    definition: 0,
    reference:  1,
    semantic:   2,
};

/**
 * Sort rules (applied in order):
 *
 * 1. Definition is always first — hard invariant, not score-based.
 * 2. All other entries: descending `finalScore`.
 * 3. On equal score: source type (reference > semantic).
 * 4. Final tiebreaker: lexicographic `id` — ensures full determinism.
 */
function _sort(results: RankedResult[]): RankedResult[] {
    return results.slice().sort((a, b) => {
        // Hard rule: definition always first.
        const aIsDef = a.source === 'definition';
        const bIsDef = b.source === 'definition';
        if (aIsDef !== bIsDef) return aIsDef ? -1 : 1;

        // Descending finalScore.
        const scoreDiff = b.finalScore - a.finalScore;
        if (Math.abs(scoreDiff) > 1e-9) return scoreDiff;

        // Tiebreaker 1: source type.
        const srcDiff = SOURCE_ORDER[a.source] - SOURCE_ORDER[b.source];
        if (srcDiff !== 0) return srcDiff;

        // Tiebreaker 2: lexicographic id.
        return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
}

// ---------------------------------------------------------------------------
// Step 4 — apply file and symbol caps
// ---------------------------------------------------------------------------

/**
 * Walk the sorted list and collect entries until either `maxSymbols` entries
 * are collected or `maxFiles` distinct file paths are exhausted.  An entry
 * whose file would exceed the file cap is skipped (not truncating the scan —
 * later entries with already-seen file paths can still be admitted).
 */
function _applyFileCap(sorted: RankedResult[], maxFiles: number, maxSymbols: number): RankedResult[] {
    const seenFiles = new Set<string>();
    const out: RankedResult[] = [];

    for (const r of sorted) {
        if (out.length >= maxSymbols) break;

        if (!seenFiles.has(r.path)) {
            if (seenFiles.size >= maxFiles) {
                // File-cap reached; skip this entry but keep scanning
                // in case later entries belong to already-admitted files.
                continue;
            }
            seenFiles.add(r.path);
        }

        out.push(r);
    }

    return out;
}
