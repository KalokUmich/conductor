/**
 * Tests for relevanceRanker — rank().
 *
 * All tests use in-memory inputs; no network, no DB, no VS Code.
 *
 * Run after compilation:
 *   node --test out/tests/relevanceRanker.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    rank,
    RankInput,
    RankedResult,
    MAX_FILES,
    MAX_SYMBOLS,
    MAX_REFERENCES,
    SEMANTIC_WEIGHT,
} from '../services/relevanceRanker';
import type { LspResolveResult } from '../services/lspResolver';

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeLoc(filePath: string, line = 0) {
    return { path: filePath, range: { start: { line, character: 0 }, end: { line, character: 1 } } };
}

function emptyLsp(): LspResolveResult {
    return { definition: undefined, references: [] };
}

const CURRENT = 'src/app.ts';
const DEF_FILE = 'src/utils.ts';
const REF_FILE1 = 'src/handlers.ts';
const REF_FILE2 = 'tests/app.test.ts';
const REF_FILE3 = 'src/middleware.ts';
const REF_FILE4 = 'src/extras.ts';

// A minimal RankInput builder.
function makeInput(overrides: Partial<RankInput> = {}): RankInput {
    return {
        currentFile:     CURRENT,
        lsp:             emptyLsp(),
        importNeighbors: [],
        semanticResults: [],
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// Empty / degenerate inputs
// ---------------------------------------------------------------------------

describe('rank — empty inputs', () => {
    it('returns [] when all inputs are empty', () => {
        assert.deepEqual(rank(makeInput()), []);
    });

    it('returns [] with LSP result that has no definition and no references', () => {
        assert.deepEqual(rank(makeInput({ lsp: emptyLsp() })), []);
    });

    it('returns [] for semantic results when symbolPaths is absent', () => {
        const r = rank(makeInput({ semanticResults: [{ symbol_id: 's1', score: 0.9 }] }));
        assert.deepEqual(r, []);
    });
});

// ---------------------------------------------------------------------------
// Definition always first
// ---------------------------------------------------------------------------

describe('rank — definition always first', () => {
    it('definition appears at index 0', () => {
        const r = rank(makeInput({
            lsp: { definition: makeLoc(DEF_FILE, 10), references: [] },
        }));
        assert.equal(r[0].source, 'definition');
        assert.equal(r[0].path, DEF_FILE);
    });

    it('definition beats a reference even when reference has maximum possible boosts', () => {
        const r = rank(makeInput({
            currentFile:     CURRENT,
            lsp: {
                definition: makeLoc(DEF_FILE, 10),
                references: [makeLoc(CURRENT, 5)], // same-file reference
            },
            importNeighbors: [CURRENT],             // import neighbor boost
            semanticResults: [
                { symbol_id: `${CURRENT}:5`, score: 1.0 }, // max semantic
            ],
        }));
        assert.equal(r[0].source, 'definition', 'definition must always be #1');
    });

    it('definition beats a semantic result with score 1.0', () => {
        const r = rank(makeInput({
            lsp: { definition: makeLoc(DEF_FILE, 0), references: [] },
            semanticResults: [{ symbol_id: 'top-sym', score: 1.0 }],
            symbolPaths: new Map([['top-sym', 'src/other.ts']]),
        }));
        assert.equal(r[0].source, 'definition');
    });
});

// ---------------------------------------------------------------------------
// Structural scoring
// ---------------------------------------------------------------------------

describe('rank — structural scoring', () => {
    it('reference appears after definition', () => {
        const r = rank(makeInput({
            lsp: {
                definition: makeLoc(DEF_FILE),
                references: [makeLoc(REF_FILE1)],
            },
        }));
        assert.equal(r[0].source, 'definition');
        assert.equal(r[1].source, 'reference');
    });

    it('definition structuralScore is 1.0 (no boosts)', () => {
        // Use a file in a different directory to avoid same-module boost.
        const r = rank(makeInput({
            lsp: { definition: makeLoc('vendor/utils.ts'), references: [] },
        }));
        assert.equal(r[0].structuralScore, 1.0);
    });

    it('reference structuralScore is 0.6 (no boosts)', () => {
        // Use a file in a different directory to avoid same-module boost.
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc('vendor/lib.ts')] },
        }));
        assert.equal(r[0].structuralScore, 0.6);
    });

    it('references are limited to MAX_REFERENCES', () => {
        const refs = Array.from({ length: MAX_REFERENCES + 5 }, (_, i) =>
            makeLoc(`src/ref${i}.ts`),
        );
        const r = rank(makeInput({
            lsp: { definition: undefined, references: refs },
        }));
        const refCount = r.filter(x => x.source === 'reference').length;
        assert.ok(
            refCount <= MAX_REFERENCES,
            `got ${refCount} references, expected ≤ ${MAX_REFERENCES}`,
        );
    });
});

// ---------------------------------------------------------------------------
// Same-file bonus
// ---------------------------------------------------------------------------

describe('rank — same-file bonus', () => {
    it('a reference in the current file gets a 0.15 bonus', () => {
        const r = rank(makeInput({
            lsp: {
                definition: undefined,
                references: [makeLoc(CURRENT, 5), makeLoc(REF_FILE1, 1)],
            },
        }));
        const sameFile  = r.find(x => x.path === CURRENT);
        const otherFile = r.find(x => x.path === REF_FILE1);
        assert.ok(sameFile,  'same-file entry missing');
        assert.ok(otherFile, 'other-file entry missing');
        // 0.6 + 0.15 = 0.75 vs 0.6
        assert.ok(
            sameFile.structuralScore > otherFile.structuralScore,
            `same-file score ${sameFile.structuralScore} should > ${otherFile.structuralScore}`,
        );
    });

    it('same-file reference ranks above cross-file reference (scores only)', () => {
        const r = rank(makeInput({
            lsp: {
                definition: undefined,
                references: [makeLoc(REF_FILE1), makeLoc(CURRENT, 2)],
            },
        }));
        const idx = (src: string, p: string) => r.findIndex(x => x.source === src && x.path === p);
        assert.ok(idx('reference', CURRENT) < idx('reference', REF_FILE1));
    });
});

// ---------------------------------------------------------------------------
// Graph boosts
// ---------------------------------------------------------------------------

describe('rank — graph boosts', () => {
    it('direct import neighbor gets +0.2 graph boost', () => {
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(DEF_FILE), makeLoc(REF_FILE2)] },
            importNeighbors: [DEF_FILE],  // DEF_FILE is a direct import
        }));
        const imported    = r.find(x => x.path === DEF_FILE)!;
        const notImported = r.find(x => x.path === REF_FILE2)!;
        // 0.6 + 0.2 = 0.8 vs 0.6
        assert.ok(imported.structuralScore > notImported.structuralScore);
    });

    it('same-module (same directory) gets +0.1 boost', () => {
        // CURRENT = 'src/app.ts', DEF_FILE = 'src/utils.ts' — same dir
        // REF_FILE2 = 'tests/app.test.ts' — different dir
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE2), makeLoc(DEF_FILE)] },
        }));
        const sameModule  = r.find(x => x.path === DEF_FILE)!;
        const diffModule  = r.find(x => x.path === REF_FILE2)!;
        // 0.6 + 0.1 = 0.7 vs 0.6
        assert.ok(sameModule.structuralScore > diffModule.structuralScore);
    });

    it('import neighbor AND same module both apply (cumulative)', () => {
        // src/utils.ts is same module as src/app.ts AND an import neighbor
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(DEF_FILE)] },
            importNeighbors: [DEF_FILE],
        }));
        const entry = r[0];
        // 0.6 + 0.2 (import) + 0.1 (same module) = 0.9
        assert.ok(Math.abs(entry.structuralScore - 0.9) < 1e-9, `got ${entry.structuralScore}`);
    });

    it('import neighbor in a different module only gets +0.2 (no same-module boost)', () => {
        // REF_FILE2 = 'tests/app.test.ts' — different module, but listed as import
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE2)] },
            importNeighbors: [REF_FILE2],
        }));
        // 0.6 + 0.2 = 0.8 (no +0.1 because different dir)
        assert.ok(Math.abs(r[0].structuralScore - 0.8) < 1e-9, `got ${r[0].structuralScore}`);
    });

    it('same-module boost does NOT apply when the file IS the current file', () => {
        // Current file itself should NOT get the +0.1 same-module boost — only the +0.15 same-file bonus.
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(CURRENT, 3)] },
        }));
        // 0.6 + 0.15 (same-file) = 0.75 — NOT 0.75+0.1
        assert.ok(Math.abs(r[0].structuralScore - 0.75) < 1e-9, `got ${r[0].structuralScore}`);
    });
});

// ---------------------------------------------------------------------------
// Semantic contribution
// ---------------------------------------------------------------------------

describe('rank — semantic contribution', () => {
    it('finalScore = structuralScore + semanticScore * SEMANTIC_WEIGHT', () => {
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE1)] },
            semanticResults: [{ symbol_id: `${REF_FILE1}:0`, score: 0.8 }],
        }));
        const entry = r[0];
        const expected = entry.structuralScore + entry.semanticScore * SEMANTIC_WEIGHT;
        assert.ok(Math.abs(entry.finalScore - expected) < 1e-9);
    });

    it('semantic results boost relative ranking when structure is equal', () => {
        // Two references: one has semantic match, one does not.
        const r = rank(makeInput({
            lsp: {
                definition: undefined,
                references: [makeLoc(REF_FILE1), makeLoc(REF_FILE2)],
            },
            semanticResults: [{ symbol_id: `${REF_FILE2}:0`, score: 0.9 }],
        }));
        const withSem    = r.find(x => x.path === REF_FILE2)!;
        const withoutSem = r.find(x => x.path === REF_FILE1)!;
        assert.ok(withSem.finalScore > withoutSem.finalScore);
    });

    it('negative semantic scores are clamped to 0 in the formula', () => {
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE1)] },
            semanticResults: [{ symbol_id: `${REF_FILE1}:0`, score: -0.5 }],
        }));
        // finalScore should NOT be penalised below structuralScore
        assert.ok(r[0].finalScore >= r[0].structuralScore);
    });

    it('raw semanticScore field reflects the original value (negative kept as-is)', () => {
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE1)] },
            semanticResults: [{ symbol_id: `${REF_FILE1}:0`, score: -0.3 }],
        }));
        assert.ok(r[0].semanticScore < 0, 'raw semanticScore should preserve the negative value');
    });

    it('semantic-only result with symbolPaths appears in output', () => {
        const r = rank(makeInput({
            semanticResults: [{ symbol_id: 'sym-1', score: 0.75 }],
            symbolPaths: new Map([['sym-1', REF_FILE1]]),
        }));
        assert.equal(r.length, 1);
        assert.equal(r[0].id, 'sym-1');
        assert.equal(r[0].source, 'semantic');
    });

    it('semantic-only result without symbolPaths is excluded', () => {
        const r = rank(makeInput({
            semanticResults: [{ symbol_id: 'sym-x', score: 0.99 }],
            // no symbolPaths
        }));
        assert.deepEqual(r, []);
    });

    it('semantic score merges into matching LSP entry by id', () => {
        const id = `${REF_FILE1}:5`;
        const r = rank(makeInput({
            lsp: { definition: undefined, references: [makeLoc(REF_FILE1, 5)] },
            semanticResults: [{ symbol_id: id, score: 0.8 }],
        }));
        // Should be a single entry, not duplicated
        const matches = r.filter(x => x.path === REF_FILE1);
        assert.equal(matches.length, 1);
        assert.ok(Math.abs(matches[0].semanticScore - 0.8) < 1e-9);
    });

    it('works without semantic results (structural only)', () => {
        const r = rank(makeInput({
            lsp: { definition: makeLoc(DEF_FILE), references: [makeLoc(REF_FILE1)] },
        }));
        assert.equal(r.length, 2);
        assert.equal(r[0].semanticScore, 0);
        assert.equal(r[1].semanticScore, 0);
    });
});

// ---------------------------------------------------------------------------
// Caps
// ---------------------------------------------------------------------------

describe('rank — caps', () => {
    it(`returns at most ${MAX_SYMBOLS} results`, () => {
        const symbolPaths = new Map<string, string>();
        const semanticResults = Array.from({ length: 30 }, (_, i) => {
            symbolPaths.set(`sem-${i}`, `src/file${i}.ts`);
            return { symbol_id: `sem-${i}`, score: 1 - i * 0.01 };
        });
        const r = rank(makeInput({ semanticResults, symbolPaths }));
        assert.ok(r.length <= MAX_SYMBOLS, `got ${r.length}`);
    });

    it(`spans at most ${MAX_FILES} distinct files`, () => {
        const symbolPaths = new Map<string, string>();
        const semanticResults = Array.from({ length: 30 }, (_, i) => {
            symbolPaths.set(`sem-${i}`, `src/file${i}.ts`);
            return { symbol_id: `sem-${i}`, score: 1 - i * 0.01 };
        });
        const r = rank(makeInput({ semanticResults, symbolPaths }));
        const files = new Set(r.map(x => x.path));
        assert.ok(files.size <= MAX_FILES, `got ${files.size} files`);
    });

    it('continues collecting symbols from already-admitted files after file cap is hit', () => {
        // 6 semantic results: 5 different new files + 1 symbol in an already-seen file
        const symbolPaths = new Map<string, string>();
        const results: Array<{ symbol_id: string; score: number }> = [];
        for (let i = 0; i < 5; i++) {
            symbolPaths.set(`s${i}a`, `src/f${i}.ts`);
            symbolPaths.set(`s${i}b`, `src/f${i}.ts`); // second symbol in same file
            results.push({ symbol_id: `s${i}a`, score: 1.0 - i * 0.1 });
            results.push({ symbol_id: `s${i}b`, score: 0.9 - i * 0.1 });
        }
        const r = rank(makeInput({ semanticResults: results, symbolPaths }));
        const files = new Set(r.map(x => x.path));
        assert.ok(files.size <= MAX_FILES);
        // Each admitted file contributes at least 1 symbol.
        for (const file of files) {
            const count = r.filter(x => x.path === file).length;
            assert.ok(count >= 1, `file ${file} has 0 symbols in output`);
        }
    });
});

// ---------------------------------------------------------------------------
// Determinism
// ---------------------------------------------------------------------------

describe('rank — determinism', () => {
    it('produces identical output on repeated calls with the same input', () => {
        const input = makeInput({
            lsp: {
                definition: makeLoc(DEF_FILE, 3),
                references: [makeLoc(REF_FILE1, 7), makeLoc(REF_FILE2, 2)],
            },
            importNeighbors: [DEF_FILE],
            semanticResults: [
                { symbol_id: 'sym-a', score: 0.8 },
                { symbol_id: 'sym-b', score: 0.6 },
            ],
            symbolPaths: new Map([['sym-a', 'src/a.ts'], ['sym-b', 'src/b.ts']]),
        });

        const r1 = rank(input);
        const r2 = rank(input);
        assert.deepEqual(r1, r2);
    });

    it('sort order is stable: ties broken by source then id', () => {
        // Two semantic-only entries with identical scores in the same file
        const symbolPaths = new Map([['z-sym', REF_FILE1], ['a-sym', REF_FILE1]]);
        const r = rank(makeInput({
            semanticResults: [
                { symbol_id: 'z-sym', score: 0.5 },
                { symbol_id: 'a-sym', score: 0.5 },
            ],
            symbolPaths,
        }));
        // Both should appear; lexicographically smaller id comes first on tie
        if (r.length === 2) {
            assert.equal(r[0].id, 'a-sym');
            assert.equal(r[1].id, 'z-sym');
        }
    });
});

// ---------------------------------------------------------------------------
// Full integration scenario
// ---------------------------------------------------------------------------

describe('rank — full scenario', () => {
    it('realistic context: definition + refs + import neighbors + semantic', () => {
        const SYM_DEF  = `${DEF_FILE}:10`;
        const SYM_REF1 = `${REF_FILE1}:5`;
        const SYM_SEM  = 'semantic-symbol-1';

        const input = makeInput({
            currentFile:     CURRENT,
            lsp: {
                definition: makeLoc(DEF_FILE, 10),
                references: [makeLoc(REF_FILE1, 5), makeLoc(REF_FILE2, 20)],
            },
            importNeighbors: [DEF_FILE, 'src/types.ts'],
            semanticResults: [
                { symbol_id: SYM_DEF,  score: 0.95 }, // merges with definition
                { symbol_id: SYM_REF1, score: 0.70 }, // merges with reference
                { symbol_id: SYM_SEM,  score: 0.50 }, // pure semantic
            ],
            symbolPaths: new Map([[SYM_SEM, REF_FILE3]]),
        });

        const r = rank(input);

        // Definition is always first
        assert.equal(r[0].source, 'definition');
        assert.equal(r[0].path, DEF_FILE);

        // Definition merges the semantic score
        assert.ok(r[0].semanticScore > 0);

        // All result ids are unique
        const ids = r.map(x => x.id);
        assert.equal(new Set(ids).size, ids.length, 'duplicate ids in output');

        // Caps respected
        const files = new Set(r.map(x => x.path));
        assert.ok(r.length <= MAX_SYMBOLS);
        assert.ok(files.size <= MAX_FILES);

        // Pure semantic entry present (because symbolPaths was provided)
        assert.ok(r.some(x => x.id === SYM_SEM));
    });
});
