/**
 * Tests for xmlPromptAssembler — assembleXmlPrompt().
 *
 * All tests use in-memory inputs; no I/O, no VS Code.
 *
 * Run after compilation:
 *   node --test out/tests/xmlPromptAssembler.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import {
    assembleXmlPrompt,
    AssemblerInput,
    FileSnippet,
    MAX_TOTAL_CHARS,
} from '../services/xmlPromptAssembler';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function snippet(path: string, content: string, role: FileSnippet['role'] = 'related'): FileSnippet {
    return { path, content, role };
}

function baseInput(overrides: Partial<AssemblerInput> = {}): AssemblerInput {
    return {
        currentFile:  snippet('src/app.ts', 'const x = 1;', 'current'),
        relatedFiles: [],
        question:     'What does this code do?',
        ...overrides,
    };
}

// ---------------------------------------------------------------------------
// Output structure
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — output structure', () => {
    it('output starts with <context> and ends with </question>', () => {
        const { xml } = assembleXmlPrompt(baseInput());
        assert.ok(xml.startsWith('<context>'), `starts with: ${xml.slice(0, 30)}`);
        assert.ok(xml.endsWith('</question>'), `ends with: ${xml.slice(-20)}`);
    });

    it('contains a <question> element', () => {
        const { xml } = assembleXmlPrompt(baseInput({ question: 'Explain this.' }));
        assert.ok(xml.includes('<question>'), 'missing <question>');
        assert.ok(xml.includes('</question>'), 'missing </question>');
    });

    it('question content is wrapped in CDATA', () => {
        const { xml } = assembleXmlPrompt(baseInput({ question: 'My question?' }));
        assert.ok(xml.includes('<![CDATA[My question?]]>'), 'question should be CDATA wrapped');
    });

    it('file elements have path and role attributes', () => {
        const { xml } = assembleXmlPrompt(baseInput());
        assert.match(xml, /path="src\/app\.ts"/);
        assert.match(xml, /role="current"/);
    });

    it('file content is wrapped in CDATA', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/app.ts', 'function greet() {}', 'current'),
        }));
        assert.ok(xml.includes('<![CDATA[function greet() {}]]>'));
    });

    it('charCount matches the actual xml string length', () => {
        const result = assembleXmlPrompt(baseInput());
        assert.equal(result.charCount, result.xml.length);
    });
});

// ---------------------------------------------------------------------------
// Ordering
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — ordering', () => {
    it('current file appears before definition', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            definition: snippet('src/utils.ts', 'export function foo() {}', 'definition'),
        }));
        const currentPos    = xml.indexOf('role="current"');
        const definitionPos = xml.indexOf('role="definition"');
        assert.ok(currentPos < definitionPos, 'current should precede definition');
    });

    it('definition appears before related files', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            definition:   snippet('src/utils.ts', 'def', 'definition'),
            relatedFiles: [snippet('src/other.ts', 'rel', 'related')],
        }));
        const defPos = xml.indexOf('role="definition"');
        const relPos = xml.indexOf('role="related"');
        assert.ok(defPos < relPos, 'definition should precede related');
    });

    it('related files appear in supplied order', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            relatedFiles: [
                snippet('src/z.ts', 'z', 'related'),
                snippet('src/a.ts', 'a', 'related'),
            ],
        }));
        const zPos = xml.indexOf('"src/z.ts"');
        const aPos = xml.indexOf('"src/a.ts"');
        assert.ok(zPos < aPos, 'z.ts should come before a.ts (input order)');
    });

    it('same input always produces the same output', () => {
        const input = baseInput({
            definition:   snippet('src/utils.ts', 'code', 'definition'),
            relatedFiles: [snippet('src/helpers.ts', 'helpers', 'related')],
        });
        const r1 = assembleXmlPrompt(input);
        const r2 = assembleXmlPrompt(input);
        assert.equal(r1.xml, r2.xml);
    });
});

// ---------------------------------------------------------------------------
// File attributes
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — file path attributes', () => {
    it('path attribute uses the supplied path string', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('deep/nested/file.ts', 'x', 'current'),
        }));
        assert.ok(xml.includes('path="deep/nested/file.ts"'));
    });

    it('path attribute special characters are XML-escaped', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('path/with<angle>&and"quotes".ts', 'x', 'current'),
        }));
        assert.ok(xml.includes('&lt;angle&gt;'));
        assert.ok(xml.includes('&amp;'));
        assert.ok(xml.includes('&quot;'));
    });

    it('all three role values appear when all sections present', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            definition:   snippet('src/d.ts', 'd', 'definition'),
            relatedFiles: [snippet('src/r.ts', 'r', 'related')],
        }));
        assert.match(xml, /role="current"/);
        assert.match(xml, /role="definition"/);
        assert.match(xml, /role="related"/);
    });

    it('no definition element when definition is absent', () => {
        const { xml } = assembleXmlPrompt(baseInput({ definition: undefined }));
        assert.ok(!xml.includes('role="definition"'), 'should not contain definition role');
    });
});

// ---------------------------------------------------------------------------
// CDATA preservation
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — CDATA preservation', () => {
    it('preserves leading indentation in content', () => {
        const indented = '    if (x) {\n        return true;\n    }';
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/app.ts', indented, 'current'),
        }));
        assert.ok(xml.includes('    if (x) {'));
        assert.ok(xml.includes('        return true;'));
    });

    it('preserves angle brackets inside CDATA', () => {
        const code = 'const x: Array<string> = [];';
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/app.ts', code, 'current'),
        }));
        // The raw string should be present verbatim inside CDATA
        assert.ok(xml.includes('Array<string>'));
    });

    it('escapes ]]> inside CDATA content', () => {
        const tricky = 'const x = "a]]>b";';
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/app.ts', tricky, 'current'),
        }));
        // The ]]> should NOT appear verbatim (it would close CDATA early)
        const rawClose = ']]>';
        // Only valid occurrences: actual CDATA close tags (after content), not inside
        // We check that the content can be parsed: count CDATA sections
        const openCount  = (xml.match(/<!\[CDATA\[/g) ?? []).length;
        const closeCount = (xml.match(/]]>/g) ?? []).length;
        assert.equal(openCount, closeCount, 'every CDATA open must have a matching close');
    });

    it('preserves newlines in question', () => {
        const q = 'Line 1\nLine 2\nLine 3';
        const { xml } = assembleXmlPrompt(baseInput({ question: q }));
        assert.ok(xml.includes('Line 1\nLine 2\nLine 3'));
    });
});

// ---------------------------------------------------------------------------
// Budget trimming
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — budget trimming', () => {
    it('wasTrimmed is false when output fits within budget', () => {
        const { wasTrimmed } = assembleXmlPrompt(baseInput(), MAX_TOTAL_CHARS);
        assert.equal(wasTrimmed, false);
    });

    it('wasTrimmed is true when budget is exceeded', () => {
        const big = 'x'.repeat(200);
        const { wasTrimmed } = assembleXmlPrompt(
            baseInput({ currentFile: snippet('src/app.ts', big, 'current') }),
            50,   // tiny budget
        );
        assert.equal(wasTrimmed, true);
    });

    it('output length does not exceed maxChars', () => {
        const MAX = 500;
        const large = 'A'.repeat(1_000);
        const { xml, charCount } = assembleXmlPrompt(
            baseInput({
                currentFile:  snippet('src/app.ts', large, 'current'),
                definition:   snippet('src/d.ts',   large, 'definition'),
                relatedFiles: [snippet('src/r.ts',  large, 'related')],
            }),
            MAX,
        );
        assert.ok(charCount <= MAX, `charCount=${charCount} exceeds MAX=${MAX}`);
        assert.ok(xml.length <= MAX, `xml.length=${xml.length} exceeds MAX=${MAX}`);
    });

    it('related files are trimmed before definition', () => {
        const large = 'B'.repeat(2_000);
        const input = baseInput({
            currentFile:  snippet('src/app.ts', 'short', 'current'),
            definition:   snippet('src/d.ts',   large,   'definition'),
            relatedFiles: [snippet('src/r.ts',  large,   'related')],
        });
        const { xml } = assembleXmlPrompt(input, 800);
        // Definition should still be present (but related might be trimmed)
        assert.ok(xml.includes('role="definition"'), 'definition should survive trimming first');
    });

    it('current file is trimmed last', () => {
        const large = 'C'.repeat(5_000);
        const tiny  = 'D'.repeat(10);
        const input = baseInput({
            currentFile:  snippet('src/app.ts', large, 'current'),
            definition:   snippet('src/d.ts',   tiny,  'definition'),
            relatedFiles: [snippet('src/r.ts',  tiny,  'related')],
        });
        // Very small budget to force trimming
        const { xml } = assembleXmlPrompt(input, 400);
        // All files should still appear (even if trimmed)
        assert.ok(xml.includes('role="current"'));
    });

    it('trimmed content contains the truncation marker', () => {
        const { xml, wasTrimmed } = assembleXmlPrompt(
            baseInput({
                currentFile: snippet('src/app.ts', 'x'.repeat(500), 'current'),
            }),
            100,
        );
        if (wasTrimmed && xml.includes('<![CDATA[')) {
            // If CDATA content was trimmed, it should contain the marker
            // (or be empty — both are valid outcomes)
            const hasMarker = xml.includes('[truncated]');
            const hasEmpty  = xml.includes('<![CDATA[]]>') || xml.includes('<![CDATA[\n… [truncated]]]>');
            assert.ok(hasMarker || hasEmpty, 'trimmed CDATA should have marker or be empty');
        }
    });

    it('default maxChars is MAX_TOTAL_CHARS', () => {
        // Assemble with and without explicit maxChars — should give identical result.
        const input = baseInput();
        const r1 = assembleXmlPrompt(input);
        const r2 = assembleXmlPrompt(input, MAX_TOTAL_CHARS);
        assert.equal(r1.xml, r2.xml);
    });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe('assembleXmlPrompt — edge cases', () => {
    it('empty question produces empty CDATA question', () => {
        const { xml } = assembleXmlPrompt(baseInput({ question: '' }));
        assert.ok(xml.includes('<question><![CDATA[]]></question>'));
    });

    it('empty content in current file is allowed', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/empty.ts', '', 'current'),
        }));
        assert.ok(xml.includes('path="src/empty.ts"'));
    });

    it('no related files produces no role="related" elements', () => {
        const { xml } = assembleXmlPrompt(baseInput({ relatedFiles: [] }));
        assert.ok(!xml.includes('role="related"'));
    });

    it('multiple related files all appear', () => {
        const { xml } = assembleXmlPrompt(baseInput({
            relatedFiles: [
                snippet('src/a.ts', 'a', 'related'),
                snippet('src/b.ts', 'b', 'related'),
                snippet('src/c.ts', 'c', 'related'),
            ],
        }));
        assert.equal((xml.match(/role="related"/g) ?? []).length, 3);
    });

    it('handles unicode in content without corruption', () => {
        const unicode = '// こんにちは\nconst 名前 = "世界";';
        const { xml } = assembleXmlPrompt(baseInput({
            currentFile: snippet('src/unicode.ts', unicode, 'current'),
        }));
        assert.ok(xml.includes('こんにちは'));
        assert.ok(xml.includes('名前'));
    });
});
