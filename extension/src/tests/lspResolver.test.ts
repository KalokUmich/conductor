/**
 * Unit tests for lspResolver — pure helper functions.
 *
 * The VS Code-dependent `resolveLspContext` is not tested here because it
 * requires an active VS Code extension host.  All other exported functions
 * (`refPriority`, `rankReferences`, `resolveFromRawResults`) are pure and
 * need no VS Code runtime.
 *
 * Run after compilation:
 *   node --test out/tests/lspResolver.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';
import * as path from 'node:path';

import {
    refPriority,
    rankReferences,
    resolveFromRawResults,
    MAX_RELATED,
    LocLike,
} from '../services/lspResolver';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal LocLike fixture. */
function makeLoc(fsPath: string, line = 0, character = 0): LocLike {
    return {
        uri: { fsPath },
        range: {
            start: { line, character },
            end: { line, character: character + 1 },
        },
    };
}

/**
 * Simple relative-path helper used in resolveFromRawResults tests:
 * strips the leading `/workspace/` prefix if present, else returns as-is.
 */
function rel(fsPath: string): string {
    const base = '/workspace/';
    return fsPath.startsWith(base) ? fsPath.slice(base.length) : fsPath;
}

const SOURCE = '/workspace/src/app.ts';

// ---------------------------------------------------------------------------
// refPriority
// ---------------------------------------------------------------------------

describe('refPriority', () => {
    it('returns 0 for the same file', () => {
        assert.equal(refPriority(SOURCE, SOURCE), 0);
    });

    it('returns 1 for a different file in the same directory', () => {
        const sibling = '/workspace/src/utils.ts';
        assert.equal(refPriority(sibling, SOURCE), 1);
    });

    it('returns 1 for a file that shares only the immediate parent dir', () => {
        const sibling = '/workspace/src/types.ts';
        assert.equal(refPriority(sibling, SOURCE), 1);
    });

    it('returns 2 for a file in a completely different directory tree', () => {
        const cross = '/workspace/lib/helper.ts';
        assert.equal(refPriority(cross, SOURCE), 2);
    });

    it('returns 2 for a file in a child directory of the source directory', () => {
        // src/sub/ is a subdirectory of src/, so dirname differs
        const child = '/workspace/src/sub/foo.ts';
        assert.equal(refPriority(child, SOURCE), 2);
    });

    it('returns 2 for a file in a parent directory of the source directory', () => {
        const parent = '/workspace/index.ts';
        assert.equal(refPriority(parent, SOURCE), 2);
    });

    it('handles Windows-style paths on the same platform', () => {
        const src = path.join('C:\\workspace\\src', 'app.ts');
        const sibling = path.join('C:\\workspace\\src', 'utils.ts');
        const cross = path.join('C:\\workspace\\lib', 'helper.ts');
        assert.equal(refPriority(src, src), 0);
        assert.equal(refPriority(sibling, src), 1);
        assert.equal(refPriority(cross, src), 2);
    });
});

// ---------------------------------------------------------------------------
// rankReferences
// ---------------------------------------------------------------------------

describe('rankReferences', () => {
    it('sorts same-file locations first', () => {
        const locs = [
            makeLoc('/workspace/lib/other.ts', 10),
            makeLoc(SOURCE, 5),
        ];
        const ranked = rankReferences(locs, SOURCE);
        assert.equal(ranked[0].uri.fsPath, SOURCE);
        assert.equal(ranked[1].uri.fsPath, '/workspace/lib/other.ts');
    });

    it('sorts same-module (same dir) before cross-module', () => {
        const locs = [
            makeLoc('/workspace/lib/cross.ts', 1),
            makeLoc('/workspace/src/sibling.ts', 2),
        ];
        const ranked = rankReferences(locs, SOURCE);
        assert.equal(ranked[0].uri.fsPath, '/workspace/src/sibling.ts');
        assert.equal(ranked[1].uri.fsPath, '/workspace/lib/cross.ts');
    });

    it('full ordering: same-file → same-module → cross-module', () => {
        const locs = [
            makeLoc('/workspace/lib/far.ts', 1),
            makeLoc('/workspace/src/near.ts', 2),
            makeLoc(SOURCE, 3),
        ];
        const ranked = rankReferences(locs, SOURCE);
        assert.equal(ranked[0].uri.fsPath, SOURCE);
        assert.equal(ranked[1].uri.fsPath, '/workspace/src/near.ts');
        assert.equal(ranked[2].uri.fsPath, '/workspace/lib/far.ts');
    });

    it('preserves original order within the same priority bucket (stable)', () => {
        const locs = [
            makeLoc('/workspace/src/a.ts', 1),
            makeLoc('/workspace/src/b.ts', 2),
            makeLoc('/workspace/src/c.ts', 3),
        ];
        const ranked = rankReferences(locs, SOURCE);
        // All same-module, so relative order should be preserved
        assert.equal(ranked[0].uri.fsPath, '/workspace/src/a.ts');
        assert.equal(ranked[1].uri.fsPath, '/workspace/src/b.ts');
        assert.equal(ranked[2].uri.fsPath, '/workspace/src/c.ts');
    });

    it('does not mutate the original array', () => {
        const locs = [
            makeLoc('/workspace/lib/far.ts'),
            makeLoc(SOURCE),
        ];
        const original = locs.map(l => l.uri.fsPath);
        rankReferences(locs, SOURCE);
        assert.deepEqual(locs.map(l => l.uri.fsPath), original);
    });

    it('returns empty array for empty input', () => {
        assert.deepEqual(rankReferences([], SOURCE), []);
    });

    it('handles a single location', () => {
        const locs = [makeLoc(SOURCE, 5)];
        assert.deepEqual(rankReferences(locs, SOURCE), locs);
    });
});

// ---------------------------------------------------------------------------
// resolveFromRawResults — definition
// ---------------------------------------------------------------------------

describe('resolveFromRawResults — definition', () => {
    it('returns undefined definition when rawDefs is empty', () => {
        const result = resolveFromRawResults([], [], SOURCE, rel);
        assert.equal(result.definition, undefined);
    });

    it('picks the first definition location', () => {
        const defs = [
            makeLoc('/workspace/src/types.ts', 10, 4),
            makeLoc('/workspace/lib/base.ts', 20, 0),
        ];
        const result = resolveFromRawResults(defs, [], SOURCE, rel);
        assert.ok(result.definition);
        assert.equal(result.definition.path, 'src/types.ts');
        assert.equal(result.definition.range.start.line, 10);
        assert.equal(result.definition.range.start.character, 4);
    });

    it('definition path is workspace-relative (via toRelative)', () => {
        const defs = [makeLoc('/workspace/src/api.ts', 0)];
        const result = resolveFromRawResults(defs, [], SOURCE, rel);
        assert.equal(result.definition?.path, 'src/api.ts');
    });

    it('definition range copies start and end correctly', () => {
        const defs = [makeLoc('/workspace/src/x.ts', 7, 12)];
        const result = resolveFromRawResults(defs, [], SOURCE, rel);
        assert.deepEqual(result.definition?.range.start, { line: 7, character: 12 });
        assert.deepEqual(result.definition?.range.end, { line: 7, character: 13 });
    });
});

// ---------------------------------------------------------------------------
// resolveFromRawResults — references
// ---------------------------------------------------------------------------

describe('resolveFromRawResults — references', () => {
    it('returns empty references when rawRefs is empty', () => {
        const result = resolveFromRawResults([], [], SOURCE, rel);
        assert.deepEqual(result.references, []);
    });

    it('limits references to MAX_RELATED', () => {
        const refs = [
            makeLoc('/workspace/a/f1.ts', 1),
            makeLoc('/workspace/b/f2.ts', 2),
            makeLoc('/workspace/c/f3.ts', 3),
            makeLoc('/workspace/d/f4.ts', 4),
            makeLoc('/workspace/e/f5.ts', 5),
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.equal(result.references.length, MAX_RELATED);
    });

    it('respects a custom max parameter', () => {
        const refs = Array.from({ length: 10 }, (_, i) =>
            makeLoc(`/workspace/lib/f${i}.ts`, i),
        );
        const result = resolveFromRawResults([], refs, SOURCE, rel, 2);
        assert.equal(result.references.length, 2);
    });

    it('ranks same-file references first', () => {
        const refs = [
            makeLoc('/workspace/lib/cross.ts', 5),
            makeLoc(SOURCE, 2),
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.equal(result.references[0].path, 'src/app.ts');
    });

    it('ranks same-module references before cross-module', () => {
        const refs = [
            makeLoc('/workspace/other/x.ts', 1),
            makeLoc('/workspace/src/sibling.ts', 2),
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.equal(result.references[0].path, 'src/sibling.ts');
        assert.equal(result.references[1].path, 'other/x.ts');
    });

    it('full priority ordering: same-file → same-module → cross-module', () => {
        const refs = [
            makeLoc('/workspace/other/c.ts', 3),
            makeLoc('/workspace/src/b.ts', 2),
            makeLoc(SOURCE, 1),
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel, 3);
        assert.equal(result.references[0].path, 'src/app.ts');
        assert.equal(result.references[1].path, 'src/b.ts');
        assert.equal(result.references[2].path, 'other/c.ts');
    });

    it('deduplicates identical locations', () => {
        const refs = [
            makeLoc(SOURCE, 5, 0),
            makeLoc(SOURCE, 5, 0),   // exact duplicate
            makeLoc(SOURCE, 5, 0),   // exact duplicate
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.equal(result.references.length, 1);
    });

    it('does not deduplicate refs at different positions in the same file', () => {
        const refs = [
            makeLoc(SOURCE, 5, 0),
            makeLoc(SOURCE, 10, 0),
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel, 2);
        assert.equal(result.references.length, 2);
    });

    it('deduplicates only refs with identical fsPath, line, and character', () => {
        const refs = [
            makeLoc(SOURCE, 5, 0),
            makeLoc(SOURCE, 5, 4),  // same line, different character — NOT a dup
        ];
        const result = resolveFromRawResults([], refs, SOURCE, rel, 2);
        assert.equal(result.references.length, 2);
    });

    it('reference paths are workspace-relative', () => {
        const refs = [makeLoc('/workspace/src/util.ts', 0)];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.equal(result.references[0].path, 'src/util.ts');
    });

    it('reference range copies start and end', () => {
        const refs = [makeLoc('/workspace/src/x.ts', 3, 7)];
        const result = resolveFromRawResults([], refs, SOURCE, rel);
        assert.deepEqual(result.references[0].range.start, { line: 3, character: 7 });
        assert.deepEqual(result.references[0].range.end, { line: 3, character: 8 });
    });
});

// ---------------------------------------------------------------------------
// resolveFromRawResults — combined (def + refs)
// ---------------------------------------------------------------------------

describe('resolveFromRawResults — combined', () => {
    it('returns both definition and references independently', () => {
        const defs = [makeLoc('/workspace/lib/types.ts', 5)];
        const refs = [
            makeLoc(SOURCE, 10),
            makeLoc('/workspace/src/consumer.ts', 20),
        ];
        const result = resolveFromRawResults(defs, refs, SOURCE, rel);

        assert.ok(result.definition);
        assert.equal(result.definition.path, 'lib/types.ts');
        assert.equal(result.references.length, 2);
    });

    it('definition and references are independent — refs do not affect definition', () => {
        const defs = [makeLoc('/workspace/lib/base.ts', 1)];
        const refs = Array.from({ length: 10 }, (_, i) =>
            makeLoc(`/workspace/consumers/c${i}.ts`, i),
        );
        const result = resolveFromRawResults(defs, refs, SOURCE, rel);
        assert.equal(result.definition?.path, 'lib/base.ts');
        assert.equal(result.references.length, MAX_RELATED);
    });

    it('returns empty result when both inputs are empty', () => {
        const result = resolveFromRawResults([], [], SOURCE, rel);
        assert.equal(result.definition, undefined);
        assert.deepEqual(result.references, []);
    });
});

// ---------------------------------------------------------------------------
// MAX_RELATED constant
// ---------------------------------------------------------------------------

describe('MAX_RELATED', () => {
    it('is a positive integer', () => {
        assert.ok(Number.isInteger(MAX_RELATED));
        assert.ok(MAX_RELATED > 0);
    });

    it('equals 3', () => {
        assert.equal(MAX_RELATED, 3);
    });
});
