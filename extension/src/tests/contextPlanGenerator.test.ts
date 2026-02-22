/**
 * Tests for contextPlanGenerator — buildContextPlan().
 *
 * All tests use synthetic in-memory RankedResult fixtures; no I/O.
 *
 * Run after compilation:
 *   node --test out/tests/contextPlanGenerator.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    buildContextPlan,
    ReadFileOp,
    CONTEXT_LINES,
    MAX_BYTES,
    HEAD_LINES,
    TAIL_LINES,
} from '../services/contextPlanGenerator';
import type { RankedResult } from '../services/relevanceRanker';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeItem(path: string, line?: number, source: 'definition' | 'reference' | 'semantic' = 'reference'): RankedResult {
    return {
        id:              line !== undefined ? `${path}:${line}` : path,
        path,
        line,
        structuralScore: 0.6,
        semanticScore:   0,
        finalScore:      0.6,
        source,
    };
}

// ---------------------------------------------------------------------------
// Empty / edge inputs
// ---------------------------------------------------------------------------

describe('buildContextPlan — empty / edge inputs', () => {
    it('returns [] for empty input', () => {
        assert.deepEqual(buildContextPlan([]), []);
    });

    it('skips items with empty path', () => {
        const items = [makeItem('')];
        assert.deepEqual(buildContextPlan(items), []);
    });

    it('every operation has type "read_file"', () => {
        const plan = buildContextPlan([makeItem('src/app.ts', 10)]);
        assert.ok(plan.every(op => op.type === 'read_file'));
    });

    it('every operation carries max_bytes', () => {
        const plan = buildContextPlan([makeItem('src/app.ts', 10)]);
        assert.ok(plan.every(op => typeof op.max_bytes === 'number' && op.max_bytes > 0));
    });
});

// ---------------------------------------------------------------------------
// Range expansion
// ---------------------------------------------------------------------------

describe('buildContextPlan — range expansion', () => {
    it('expands a known line by ±CONTEXT_LINES', () => {
        const line = 200;
        const [op] = buildContextPlan([makeItem('src/app.ts', line)]);
        assert.equal(op.start_line, Math.max(0, line - CONTEXT_LINES));
        assert.equal(op.end_line, line + 1 + CONTEXT_LINES);
    });

    it('clamps start_line to 0 when line is near the top', () => {
        const [op] = buildContextPlan([makeItem('src/app.ts', 5)]);
        assert.equal(op.start_line, 0);
    });

    it('items without a line produce a whole-file read (no start_line/end_line)', () => {
        const [op] = buildContextPlan([makeItem('src/app.ts')]);
        assert.equal(op.start_line, undefined);
        assert.equal(op.end_line, undefined);
    });

    it('contextLines option overrides default CONTEXT_LINES', () => {
        const [op] = buildContextPlan([makeItem('src/app.ts', 100)], { contextLines: 10 });
        assert.equal(op.start_line, 90);
        assert.equal(op.end_line,  111); // 100 + 1 + 10
    });
});

// ---------------------------------------------------------------------------
// Deduplication
// ---------------------------------------------------------------------------

describe('buildContextPlan — deduplication', () => {
    it('emits each path exactly once', () => {
        const items = [
            makeItem('src/app.ts', 10),
            makeItem('src/app.ts', 50),
            makeItem('src/app.ts', 90),
        ];
        const plan = buildContextPlan(items);
        assert.equal(plan.length, 1);
        assert.equal(plan[0].path, 'src/app.ts');
    });

    it('merges multiple ranges for the same file into their union', () => {
        const items = [
            makeItem('src/app.ts', 100),
            makeItem('src/app.ts', 500),
        ];
        // maxBytes large enough that the merged range is never truncated.
        const [op] = buildContextPlan(items, { contextLines: 0, maxBytes: 1_000_000 });
        // Without expansion (contextLines=0): range from 100 to 501
        assert.equal(op.start_line, 100);
        assert.equal(op.end_line, 501);
    });

    it('upgrading to whole-file when a later item has no line', () => {
        const items = [
            makeItem('src/app.ts', 50),  // has a range
            makeItem('src/app.ts'),       // no range → whole file
        ];
        const [op] = buildContextPlan(items);
        assert.equal(op.start_line, undefined);
        assert.equal(op.end_line, undefined);
    });

    it('whole-file entry stays whole-file even when later items add ranges', () => {
        const items = [
            makeItem('src/app.ts'),       // whole file first
            makeItem('src/app.ts', 100), // line-range second
        ];
        const [op] = buildContextPlan(items);
        assert.equal(op.start_line, undefined, 'whole-file should not be narrowed');
    });

    it('two different files produce two operations', () => {
        const plan = buildContextPlan([
            makeItem('src/a.ts', 10),
            makeItem('src/b.ts', 20),
        ]);
        assert.equal(plan.length, 2);
        assert.ok(plan.some(op => op.path === 'src/a.ts'));
        assert.ok(plan.some(op => op.path === 'src/b.ts'));
    });
});

// ---------------------------------------------------------------------------
// Deterministic ordering
// ---------------------------------------------------------------------------

describe('buildContextPlan — deterministic ordering', () => {
    it('output order matches first-occurrence order in input', () => {
        const plan = buildContextPlan([
            makeItem('src/z.ts', 1),
            makeItem('src/a.ts', 2),
            makeItem('src/m.ts', 3),
        ]);
        assert.equal(plan[0].path, 'src/z.ts');
        assert.equal(plan[1].path, 'src/a.ts');
        assert.equal(plan[2].path, 'src/m.ts');
    });

    it('duplicate items do not change the slot of the first occurrence', () => {
        const plan = buildContextPlan([
            makeItem('src/a.ts', 10),
            makeItem('src/b.ts', 20),
            makeItem('src/a.ts', 30), // duplicate — should not re-order
        ]);
        assert.equal(plan[0].path, 'src/a.ts');
        assert.equal(plan[1].path, 'src/b.ts');
    });

    it('repeated identical inputs produce identical output', () => {
        const items = [
            makeItem('src/a.ts', 10),
            makeItem('src/b.ts', 20, 'definition'),
        ];
        const r1 = buildContextPlan(items);
        const r2 = buildContextPlan(items);
        assert.deepEqual(r1, r2);
    });
});

// ---------------------------------------------------------------------------
// Head+tail truncation
// ---------------------------------------------------------------------------

describe('buildContextPlan — head+tail truncation', () => {
    it('emits start_line and end_line when range fits within max_bytes', () => {
        // Small range: 5 lines × 80 bytes = 400 bytes — well under 15 000
        const [op] = buildContextPlan([makeItem('src/app.ts', 100)], {
            contextLines: 2,
            maxBytes: 15_000,
            bytesPerLine: 80,
        });
        // 100 - 2 = 98, 100 + 1 + 2 = 103
        assert.equal(op.start_line, 98);
        assert.equal(op.end_line, 103);
    });

    it('truncates a huge range to head+tail window', () => {
        // Force overflow: large contextLines, tiny bytesPerLine budget
        const [op] = buildContextPlan([makeItem('src/large.ts', 5000)], {
            contextLines: 10_000,  // would produce 20 000 lines
            maxBytes:     500,     // very small
            bytesPerLine: 80,      // ~2 million bytes estimated
            headLines:    3,
            tailLines:    2,
        });
        // Total window = 5 lines
        const window = (op.end_line ?? 0) - (op.start_line ?? 0);
        assert.equal(window, 5, `expected 5-line window, got ${window}`);
    });

    it('truncated op still carries max_bytes', () => {
        const [op] = buildContextPlan([makeItem('src/huge.ts', 10_000)], {
            contextLines: 5_000,
            maxBytes:     200,
            bytesPerLine: 80,
        });
        assert.equal(op.max_bytes, 200);
    });

    it('maxBytes option overrides the default', () => {
        const plan = buildContextPlan([makeItem('src/app.ts', 10)], { maxBytes: 4096 });
        assert.equal(plan[0].max_bytes, 4096);
    });
});

// ---------------------------------------------------------------------------
// Full scenario
// ---------------------------------------------------------------------------

describe('buildContextPlan — full scenario', () => {
    it('produces a well-formed plan from a realistic ranked list', () => {
        const items: RankedResult[] = [
            { id: 'def',  path: 'src/utils.ts',    line: 42,  source: 'definition', structuralScore: 1.0, semanticScore: 0.9, finalScore: 1.27 },
            { id: 'ref1', path: 'src/handlers.ts', line: 120, source: 'reference',  structuralScore: 0.6, semanticScore: 0.5, finalScore: 0.75 },
            { id: 'ref2', path: 'src/app.ts',      line: 5,   source: 'reference',  structuralScore: 0.6, semanticScore: 0.0, finalScore: 0.60 },
            { id: 'sem1', path: 'src/utils.ts',    line: 200, source: 'semantic',   structuralScore: 0.0, semanticScore: 0.8, finalScore: 0.24 },
        ];

        const plan = buildContextPlan(items);

        // Correct number of unique paths
        assert.equal(plan.length, 3);  // src/utils.ts deduplicated

        // Output order: utils, handlers, app (first-occurrence order)
        assert.equal(plan[0].path, 'src/utils.ts');
        assert.equal(plan[1].path, 'src/handlers.ts');
        assert.equal(plan[2].path, 'src/app.ts');

        // src/utils.ts got a range (merged from line 42 and line 200).
        // The merged range before truncation spans both anchors; after
        // head+tail truncation it is still a valid non-empty range.
        const utils = plan[0];
        assert.ok(utils.start_line !== undefined, 'utils should have a start_line');
        assert.ok(utils.end_line   !== undefined, 'utils should have an end_line');
        assert.ok((utils.end_line ?? 0) > (utils.start_line ?? 0), 'range should be non-empty');
        // The range size equals at least HEAD_LINES + TAIL_LINES when truncated.
        const rangeSize = (utils.end_line ?? 0) - (utils.start_line ?? 0);
        assert.ok(rangeSize >= HEAD_LINES + TAIL_LINES || rangeSize <= 2 * CONTEXT_LINES + 2, 'range size is in expected bounds');

        // All ops have the expected type and max_bytes
        for (const op of plan) {
            assert.equal(op.type, 'read_file');
            assert.equal(op.max_bytes, MAX_BYTES);
        }
    });

    it('handles the definition having no line (whole-file)', () => {
        const items: RankedResult[] = [
            { id: 'def', path: 'lib/types.ts', line: undefined, source: 'definition',
              structuralScore: 1.0, semanticScore: 0, finalScore: 1.0 },
        ];
        const [op] = buildContextPlan(items);
        assert.equal(op.path, 'lib/types.ts');
        assert.equal(op.start_line, undefined);
        assert.equal(op.end_line, undefined);
        assert.equal(op.max_bytes, MAX_BYTES);
    });
});
