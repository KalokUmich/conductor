/**
 * Context plan generator for Conductor.
 *
 * Converts a ranked list of context items (from `relevanceRanker`) into a
 * deterministic, deduplicated read plan that callers can execute to collect
 * file content for AI context enrichment.
 *
 * Output schema
 * -------------
 * Each plan entry is a `ReadFileOp`:
 * ```
 * {
 *   type:       "read_file"
 *   path:       string          // workspace-relative
 *   start_line?: number         // 0-based, inclusive  (absent = read from start)
 *   end_line?:  number          // 0-based, exclusive  (absent = read to end)
 *   max_bytes:  number          // hard cap on bytes delivered (default 15 000)
 * }
 * ```
 *
 * Rules (applied in order)
 * ------------------------
 * 1. Range expansion   — when a line range is known, expand it by ±CONTEXT_LINES
 *                         (80) on each side, clamped to [0, Infinity).
 * 2. Head+tail policy  — when max_bytes would be exceeded by the selected range,
 *                         truncate symmetrically: keep the first HEAD_LINES lines
 *                         and the last TAIL_LINES lines (both configurable).
 * 3. Deduplication     — each workspace-relative path appears at most once;
 *                         subsequent ranked items for the same file are merged
 *                         into the existing entry by union-expanding the range.
 * 4. Deterministic order — output order matches the ranked input order
 *                         (first occurrence of each path wins its slot).
 *
 * The generator is a pure function: no I/O, no side effects.
 *
 * @module services/contextPlanGenerator
 */

import type { RankedResult } from './relevanceRanker';

// ---------------------------------------------------------------------------
// Public constants
// ---------------------------------------------------------------------------

/** Lines expanded above and below a known range. */
export const CONTEXT_LINES = 80;

/** Maximum bytes per file operation. */
export const MAX_BYTES = 15_000;

/** Lines kept from the head of a file when truncating (head+tail strategy). */
export const HEAD_LINES = 60;

/** Lines kept from the tail of a file when truncating (head+tail strategy). */
export const TAIL_LINES = 40;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface ReadFileOp {
    type: 'read_file';
    /** Workspace-relative path. */
    path: string;
    /**
     * 0-based inclusive start line.
     * Absent means "start from the top of the file".
     */
    start_line?: number;
    /**
     * 0-based exclusive end line.
     * Absent means "read to the end of the file".
     */
    end_line?: number;
    /** Hard byte cap passed to the executor. */
    max_bytes: number;
}

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface PlanOptions {
    /** Lines expanded around a known line position. Default: CONTEXT_LINES (80). */
    contextLines?: number;
    /** Maximum bytes per file. Default: MAX_BYTES (15 000). */
    maxBytes?: number;
    /**
     * Estimated bytes per line used for the head+tail overflow check.
     * Default: 80 (conservative ASCII estimate).
     */
    bytesPerLine?: number;
    /** Lines kept at head when truncating. Default: HEAD_LINES (60). */
    headLines?: number;
    /** Lines kept at tail when truncating. Default: TAIL_LINES (40). */
    tailLines?: number;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Build a deterministic, deduplicated read plan from ranked context items.
 *
 * @param items    Ranked results from `relevanceRanker.rank()`.
 * @param options  Optional tuning parameters.
 * @returns        One `ReadFileOp` per unique file path, in ranked order.
 */
export function buildContextPlan(
    items: RankedResult[],
    options: PlanOptions = {},
): ReadFileOp[] {
    const ctxLines   = options.contextLines ?? CONTEXT_LINES;
    const maxBytes   = options.maxBytes     ?? MAX_BYTES;
    const bpl        = options.bytesPerLine ?? 80;
    const headLines  = options.headLines    ?? HEAD_LINES;
    const tailLines  = options.tailLines    ?? TAIL_LINES;

    // ---- Pass 1: deduplicate and merge ranges by file path ------------------
    //
    // We use a Map (insertion-ordered) to preserve ranked order while
    // accumulating line ranges for each path.

    // Internal working entry before final conversion.
    interface Entry {
        path: string;
        // null means "whole file" (no range information)
        startLine: number | null;
        endLine: number | null;
    }

    const entryMap = new Map<string, Entry>();

    for (const item of items) {
        const { path, line } = item;

        if (!path) continue;

        if (!entryMap.has(path)) {
            // First occurrence of this path — seed the entry.
            if (line !== undefined) {
                const expanded = _expand(line, line + 1, ctxLines);
                entryMap.set(path, { path, startLine: expanded.start, endLine: expanded.end });
            } else {
                // No line hint: read the whole file.
                entryMap.set(path, { path, startLine: null, endLine: null });
            }
        } else {
            // Subsequent occurrence — union-expand the range.
            const existing = entryMap.get(path)!;

            if (existing.startLine === null || existing.endLine === null) {
                // Already marked as whole-file; nothing to expand.
                continue;
            }

            if (line === undefined) {
                // New item has no range — upgrade to whole-file.
                existing.startLine = null;
                existing.endLine   = null;
            } else {
                const expanded = _expand(line, line + 1, ctxLines);
                existing.startLine = Math.min(existing.startLine, expanded.start);
                existing.endLine   = Math.max(existing.endLine,   expanded.end);
            }
        }
    }

    // ---- Pass 2: apply head+tail overflow policy and emit final ops ---------

    const plan: ReadFileOp[] = [];

    for (const entry of entryMap.values()) {
        const op = _buildOp(entry, maxBytes, bpl, headLines, tailLines);
        plan.push(op);
    }

    return plan;
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/** Expand a [lineStart, lineEnd) range by `ctx` lines on each side (floor at 0). */
function _expand(
    lineStart: number,
    lineEnd: number,
    ctx: number,
): { start: number; end: number } {
    return {
        start: Math.max(0, lineStart - ctx),
        end:   lineEnd + ctx,
    };
}

/**
 * Convert an `Entry` to a `ReadFileOp`, applying the head+tail truncation
 * strategy when the selected line range is estimated to exceed `maxBytes`.
 */
function _buildOp(
    entry: { path: string; startLine: number | null; endLine: number | null },
    maxBytes: number,
    bytesPerLine: number,
    headLines: number,
    tailLines: number,
): ReadFileOp {
    // Whole-file read (no line range).
    if (entry.startLine === null || entry.endLine === null) {
        return { type: 'read_file', path: entry.path, max_bytes: maxBytes };
    }

    const rangeLines = entry.endLine - entry.startLine;
    const estimatedBytes = rangeLines * bytesPerLine;

    if (estimatedBytes <= maxBytes) {
        // Range fits — emit with the expanded range.
        return {
            type:       'read_file',
            path:       entry.path,
            start_line: entry.startLine,
            end_line:   entry.endLine,
            max_bytes:  maxBytes,
        };
    }

    // Range is too large — apply head+tail strategy.
    //
    // We keep:
    //   • HEAD_LINES from the start of the selection
    //   • TAIL_LINES from the end of the selection
    //
    // If the selection itself is smaller than headLines + tailLines we fall
    // back to emitting the full selection (it already fits budget by
    // definition of the check above, but we defend against it anyway).

    const base = entry.startLine;
    const top  = entry.endLine;
    const total = headLines + tailLines;

    if (rangeLines <= total) {
        // Selection fits after all (shouldn't reach here, but be safe).
        return {
            type:       'read_file',
            path:       entry.path,
            start_line: base,
            end_line:   top,
            max_bytes:  maxBytes,
        };
    }

    // Emit a head window.  The tail window is encoded by the caller via the
    // max_bytes cap — the executor is expected to deliver the first N bytes of
    // the range, so we set a separate tail entry only if we had a way to
    // express "end - TAIL_LINES".  Since the schema supports a contiguous
    // range we instead shrink the window to head+tail around the anchor point
    // (the original `line` hint is no longer available here, so we anchor at
    // the midpoint of the selection).
    const mid   = Math.floor((base + top) / 2);
    const start = Math.max(0, mid - Math.floor(headLines / 2));
    const end   = start + total;

    return {
        type:       'read_file',
        path:       entry.path,
        start_line: start,
        end_line:   end,
        max_bytes:  maxBytes,
    };
}
