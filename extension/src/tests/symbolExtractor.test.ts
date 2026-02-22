/**
 * Tests for symbolExtractor — extractSymbols().
 *
 * All test cases write temporary source files to disk so that the TypeScript
 * compiler API resolves the correct ScriptKind from the file extension.
 *
 * Run after compilation:
 *   node --test out/tests/symbolExtractor.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

import { extractSymbols, MAX_FILE_BYTES, ExtractedSymbols, FileSymbol } from '../services/symbolExtractor';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

function setup(): void {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'conductor-sym-test-'));
}

function teardown(): void {
    fs.rmSync(tmpDir, { recursive: true, force: true });
}

/** Write content to a file inside tmpDir and return its absolute path. */
function write(name: string, content: string): string {
    const p = path.join(tmpDir, name);
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, content, 'utf-8');
    return p;
}

/** Return the symbol with the given name from a result, or throw. */
function sym(result: ExtractedSymbols, name: string): FileSymbol {
    const s = result.symbols.find(s => s.name === name);
    assert.ok(s, `Expected symbol "${name}" not found in [${result.symbols.map(s => s.name).join(', ')}]`);
    return s;
}

// ---------------------------------------------------------------------------
// TypeScript — imports
// ---------------------------------------------------------------------------

describe('TypeScript — imports', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a named import', () => {
        const p = write('a.ts', `import { foo } from './foo';\n`);
        const r = extractSymbols(p);
        assert.ok(r.imports.some(i => i.includes('foo')));
    });

    it('extracts a namespace import', () => {
        const p = write('a.ts', `import * as fs from 'fs';\n`);
        const r = extractSymbols(p);
        assert.ok(r.imports.some(i => i.includes('* as fs')));
    });

    it('extracts a type-only import', () => {
        const p = write('a.ts', `import type { Bar } from './bar';\n`);
        const r = extractSymbols(p);
        assert.ok(r.imports.some(i => i.includes('Bar')));
    });

    it('extracts multiple imports', () => {
        const p = write('a.ts', [
            `import fs from 'fs';`,
            `import path from 'path';`,
            `import os from 'os';`,
        ].join('\n'));
        const r = extractSymbols(p);
        assert.equal(r.imports.length, 3);
    });

    it('does not include imports in the symbols list', () => {
        const p = write('a.ts', `import { foo } from './foo';\n`);
        const r = extractSymbols(p);
        assert.equal(r.symbols.length, 0);
    });
});

// ---------------------------------------------------------------------------
// TypeScript — function declarations
// ---------------------------------------------------------------------------

describe('TypeScript — function declarations', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a plain function', () => {
        const p = write('a.ts', `function greet(name: string): string {\n  return name;\n}\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'greet');
        assert.equal(s.kind, 'function');
    });

    it('extracts an exported async function', () => {
        const p = write('a.ts', `export async function fetch(url: string): Promise<void> {}\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'fetch');
        assert.equal(s.kind, 'function');
        assert.ok(s.signature.includes('fetch'));
        assert.ok(s.signature.includes('url'));
    });

    it('signature does not contain the function body', () => {
        const p = write('a.ts', [
            'function compute(x: number): number {',
            '    return x * 2;',
            '}',
        ].join('\n'));
        const r = extractSymbols(p);
        assert.ok(!sym(r, 'compute').signature.includes('return'));
    });

    it('signature stops before the opening brace', () => {
        const p = write('a.ts', `function add(a: number, b: number): number { return a + b; }\n`);
        const r = extractSymbols(p);
        assert.ok(!sym(r, 'add').signature.includes('{'));
    });

    it('range start.line is 0-based and points to the function keyword', () => {
        const p = write('a.ts', `\nfunction foo(): void {}\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'foo').range.start.line, 1); // second line (0-based)
    });

    it('range end.line >= start.line', () => {
        const p = write('a.ts', `function multiline(\n  x: number\n): void {}\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'multiline');
        assert.ok(s.range.end.line >= s.range.start.line);
    });

    it('captures anonymous default export as <anonymous>', () => {
        const p = write('a.ts', `export default function() {}\n`);
        const r = extractSymbols(p);
        assert.ok(r.symbols.some(s => s.name === '<anonymous>' && s.kind === 'function'));
    });
});

// ---------------------------------------------------------------------------
// TypeScript — class declarations
// ---------------------------------------------------------------------------

describe('TypeScript — class declarations', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a class', () => {
        const p = write('a.ts', `class Animal { speak() {} }\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'Animal').kind, 'class');
    });

    it('extracts an exported class with extends', () => {
        const p = write('a.ts', `export class Dog extends Animal {}\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'Dog');
        assert.equal(s.kind, 'class');
        assert.ok(s.signature.includes('Dog'));
        assert.ok(s.signature.includes('Animal'));
    });

    it('class signature does not include the class body', () => {
        const p = write('a.ts', [
            'class Foo {',
            '  bar() { return 42; }',
            '}',
        ].join('\n'));
        const r = extractSymbols(p);
        assert.ok(!sym(r, 'Foo').signature.includes('bar'));
    });

    it('class range spans the whole declaration', () => {
        const p = write('a.ts', [
            'class Multi {',
            '  x = 1;',
            '  y = 2;',
            '}',
        ].join('\n'));
        const r = extractSymbols(p);
        const s = sym(r, 'Multi');
        assert.ok(s.range.end.line > s.range.start.line);
    });
});

// ---------------------------------------------------------------------------
// TypeScript — interface declarations
// ---------------------------------------------------------------------------

describe('TypeScript — interface declarations', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts an interface', () => {
        const p = write('a.ts', `interface Shape { area(): number; }\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'Shape').kind, 'interface');
    });

    it('interface signature includes the name and extends clause', () => {
        const p = write('a.ts', `export interface Circle extends Shape { radius: number; }\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'Circle');
        assert.ok(s.signature.includes('Circle'));
        assert.ok(s.signature.includes('Shape'));
    });
});

// ---------------------------------------------------------------------------
// TypeScript — type aliases
// ---------------------------------------------------------------------------

describe('TypeScript — type aliases', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a type alias', () => {
        const p = write('a.ts', `type UserId = string;\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'UserId').kind, 'type');
    });

    it('extracts a complex generic type alias', () => {
        const p = write('a.ts', `export type Result<T> = { ok: boolean; value: T };\n`);
        const r = extractSymbols(p);
        const s = sym(r, 'Result');
        assert.equal(s.kind, 'type');
        assert.ok(s.signature.includes('Result'));
    });
});

// ---------------------------------------------------------------------------
// TypeScript — enum declarations
// ---------------------------------------------------------------------------

describe('TypeScript — enum declarations', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts an enum', () => {
        const p = write('a.ts', `enum Direction { Up, Down, Left, Right }\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'Direction').kind, 'enum');
    });

    it('extracts a const enum', () => {
        const p = write('a.ts', `export const enum Status { Active = 'active', Inactive = 'inactive' }\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'Status').kind, 'enum');
    });
});

// ---------------------------------------------------------------------------
// TypeScript — exported variable / arrow-function statements
// ---------------------------------------------------------------------------

describe('TypeScript — exported variables and arrow functions', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('exported arrow function → kind function', () => {
        const p = write('a.ts', `export const double = (x: number): number => x * 2;\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'double').kind, 'function');
    });

    it('exported async arrow function → kind function', () => {
        const p = write('a.ts', `export const run = async (cmd: string): Promise<void> => { };\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'run').kind, 'function');
    });

    it('exported function expression → kind function', () => {
        const p = write('a.ts', `export const handler = function(req: Request) {};\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'handler').kind, 'function');
    });

    it('exported non-function variable → kind variable', () => {
        const p = write('a.ts', `export const VERSION = '1.2.3';\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'VERSION').kind, 'variable');
    });

    it('exported object variable → kind variable', () => {
        const p = write('a.ts', `export const CONFIG = { host: 'localhost', port: 8080 };\n`);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'CONFIG').kind, 'variable');
    });

    it('non-exported variable is not extracted', () => {
        const p = write('a.ts', `const secret = 'do-not-export';\n`);
        const r = extractSymbols(p);
        assert.equal(r.symbols.length, 0);
    });

    it('exported variable signature does not include function body', () => {
        const p = write('a.ts', [
            'export const transform = (s: string): string => {',
            '    return s.toUpperCase();',
            '};',
        ].join('\n'));
        const r = extractSymbols(p);
        assert.ok(!sym(r, 'transform').signature.includes('toUpperCase'));
    });
});

// ---------------------------------------------------------------------------
// TypeScript — mixed file
// ---------------------------------------------------------------------------

describe('TypeScript — mixed file', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts all top-level symbol types from one file', () => {
        const src = [
            `import { readFile } from 'fs/promises';`,
            ``,
            `export type Id = string;`,
            ``,
            `export interface Entity { id: Id; }`,
            ``,
            `export enum Role { Admin, User }`,
            ``,
            `export class Repo {`,
            `    find(id: Id): Entity | undefined { return undefined; }`,
            `}`,
            ``,
            `export function create(id: Id): Entity { return { id }; }`,
            ``,
            `export const MAX = 100;`,
        ].join('\n');

        const p = write('mixed.ts', src);
        const r = extractSymbols(p);

        assert.equal(r.imports.length, 1);
        assert.ok(r.imports[0].includes('readFile'));

        const names = r.symbols.map(s => s.name);
        assert.ok(names.includes('Id'));
        assert.ok(names.includes('Entity'));
        assert.ok(names.includes('Role'));
        assert.ok(names.includes('Repo'));
        assert.ok(names.includes('create'));
        assert.ok(names.includes('MAX'));
    });

    it('inner class methods are NOT extracted (top-level only)', () => {
        const src = [
            `class Service {`,
            `    public doWork(): void {}`,
            `    private helper(): string { return ''; }`,
            `}`,
        ].join('\n');
        const p = write('svc.ts', src);
        const r = extractSymbols(p);

        // Only the class itself, not its methods
        assert.equal(r.symbols.length, 1);
        assert.equal(r.symbols[0].name, 'Service');
    });
});

// ---------------------------------------------------------------------------
// TypeScript — TSX and JSX files
// ---------------------------------------------------------------------------

describe('TypeScript — TSX / JSX', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('parses .tsx without error', () => {
        const src = [
            `import React from 'react';`,
            `export function Button({ label }: { label: string }) {`,
            `    return <button>{label}</button>;`,
            `}`,
        ].join('\n');
        const p = write('btn.tsx', src);
        assert.doesNotThrow(() => extractSymbols(p));
        const r = extractSymbols(p);
        assert.equal(sym(r, 'Button').kind, 'function');
    });

    it('parses .jsx without error', () => {
        const src = [
            `import React from 'react';`,
            `export function App() { return <div />; }`,
        ].join('\n');
        const p = write('app.jsx', src);
        assert.doesNotThrow(() => extractSymbols(p));
        const r = extractSymbols(p);
        assert.equal(sym(r, 'App').kind, 'function');
    });
});

// ---------------------------------------------------------------------------
// JavaScript files
// ---------------------------------------------------------------------------

describe('JavaScript — .js files', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts functions from a .js file', () => {
        const p = write('util.js', [
            `const x = require('path');`,
            `function add(a, b) { return a + b; }`,
            `module.exports = { add };`,
        ].join('\n'));
        const r = extractSymbols(p);
        assert.equal(sym(r, 'add').kind, 'function');
    });
});

// ---------------------------------------------------------------------------
// Python extractor
// ---------------------------------------------------------------------------

describe('Python — imports', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a plain import', () => {
        const p = write('a.py', `import os\n`);
        assert.ok(extractSymbols(p).imports.includes('import os'));
    });

    it('extracts a from-import', () => {
        const p = write('a.py', `from pathlib import Path\n`);
        assert.ok(extractSymbols(p).imports.some(i => i.includes('pathlib')));
    });

    it('does not extract indented imports', () => {
        const src = [
            `def load():`,
            `    import json`,
            `    return json.load`,
        ].join('\n');
        const p = write('a.py', src);
        assert.equal(extractSymbols(p).imports.length, 0);
    });
});

describe('Python — functions and classes', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a top-level def', () => {
        const p = write('a.py', `def greet(name: str) -> str:\n    return name\n`);
        assert.equal(sym(extractSymbols(p), 'greet').kind, 'function');
    });

    it('extracts an async def', () => {
        const p = write('a.py', `async def fetch(url: str) -> bytes:\n    pass\n`);
        assert.equal(sym(extractSymbols(p), 'fetch').kind, 'function');
    });

    it('function signature does not include the trailing colon', () => {
        const p = write('a.py', `def process(x: int) -> None:\n    pass\n`);
        assert.ok(!sym(extractSymbols(p), 'process').signature.endsWith(':'));
    });

    it('extracts a top-level class', () => {
        const p = write('a.py', `class Animal:\n    pass\n`);
        assert.equal(sym(extractSymbols(p), 'Animal').kind, 'class');
    });

    it('extracts a class with bases', () => {
        const p = write('a.py', `class Dog(Animal):\n    pass\n`);
        const s = sym(extractSymbols(p), 'Dog');
        assert.equal(s.kind, 'class');
        assert.ok(s.signature.includes('Animal'));
    });

    it('does not extract indented (inner) functions', () => {
        const src = [
            `class Outer:`,
            `    def method(self):`,
            `        def inner():`,
            `            pass`,
        ].join('\n');
        const p = write('a.py', src);
        const r = extractSymbols(p);
        // Only 'Outer' at top-level, no 'method' or 'inner'
        assert.equal(r.symbols.length, 1);
        assert.equal(r.symbols[0].name, 'Outer');
    });

    it('range start.line is 0-based', () => {
        const p = write('a.py', `\ndef second_line():\n    pass\n`);
        const s = sym(extractSymbols(p), 'second_line');
        assert.equal(s.range.start.line, 1);
    });

    it('class end_line spans the full class body', () => {
        const src = [
            'class MyModel:',          // line 0
            '    name: str',            // line 1
            '    age: int',             // line 2
            '',                         // line 3
            '    def validate(self):',  // line 4
            '        pass',             // line 5
        ].join('\n');
        const p = write('a.py', src);
        const s = sym(extractSymbols(p), 'MyModel');
        assert.equal(s.range.start.line, 0);
        assert.equal(s.range.end.line, 5);
    });

    it('function end_line spans the full function body', () => {
        const src = [
            'def process(x: int) -> None:',   // line 0
            '    result = x * 2',              // line 1
            '    if result > 10:',             // line 2
            '        return result',           // line 3
            '    return 0',                    // line 4
        ].join('\n');
        const p = write('a.py', src);
        const s = sym(extractSymbols(p), 'process');
        assert.equal(s.range.start.line, 0);
        assert.equal(s.range.end.line, 4);
    });

    it('async function end_line spans the full body', () => {
        const src = [
            'async def fetch_data(url: str) -> bytes:',  // line 0
            '    response = await get(url)',              // line 1
            '    return response.body',                  // line 2
        ].join('\n');
        const p = write('a.py', src);
        const s = sym(extractSymbols(p), 'fetch_data');
        assert.equal(s.range.start.line, 0);
        assert.equal(s.range.end.line, 2);
    });

    it('class end_line stops at the next top-level declaration', () => {
        const src = [
            'class First:',             // line 0
            '    x = 1',                // line 1
            '',                         // line 2
            'class Second:',            // line 3
            '    y = 2',                // line 4
        ].join('\n');
        const p = write('a.py', src);
        const first = sym(extractSymbols(p), 'First');
        const second = sym(extractSymbols(p), 'Second');
        // First class should end before Second starts
        assert.ok(first.range.end.line < 3, `First end_line=${first.range.end.line} should be < 3`);
        assert.equal(second.range.start.line, 3);
        assert.equal(second.range.end.line, 4);
    });

    it('function end_line stops at the next top-level declaration', () => {
        const src = [
            'def first():',             // line 0
            '    return 1',             // line 1
            '',                         // line 2
            'def second():',            // line 3
            '    return 2',             // line 4
        ].join('\n');
        const p = write('a.py', src);
        const first = sym(extractSymbols(p), 'first');
        const second = sym(extractSymbols(p), 'second');
        assert.ok(first.range.end.line < 3, `first end_line=${first.range.end.line} should be < 3`);
        assert.equal(second.range.start.line, 3);
        assert.equal(second.range.end.line, 4);
    });
});

// ---------------------------------------------------------------------------
// Java extractor
// ---------------------------------------------------------------------------

describe('Java — imports', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a plain import', () => {
        const p = write('A.java', `import java.util.List;\n`);
        assert.ok(extractSymbols(p).imports.includes('import java.util.List;'));
    });

    it('extracts a static import', () => {
        const p = write('A.java', `import static java.lang.Math.PI;\n`);
        assert.ok(extractSymbols(p).imports.some(i => i.includes('Math.PI')));
    });
});

describe('Java — classes and methods', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('extracts a public class', () => {
        const p = write('A.java', `public class Animal {}\n`);
        assert.equal(sym(extractSymbols(p), 'Animal').kind, 'class');
    });

    it('extracts an interface', () => {
        const p = write('A.java', `public interface Runnable { void run(); }\n`);
        assert.equal(sym(extractSymbols(p), 'Runnable').kind, 'interface');
    });

    it('extracts an enum', () => {
        const p = write('A.java', `public enum Day { MON, TUE, WED }\n`);
        assert.equal(sym(extractSymbols(p), 'Day').kind, 'enum');
    });

    it('extracts a public method', () => {
        const src = [
            `public class Calc {`,
            `    public int add(int a, int b) {`,
            `        return a + b;`,
            `    }`,
            `}`,
        ].join('\n');
        const p = write('Calc.java', src);
        assert.ok(extractSymbols(p).symbols.some(s => s.name === 'add' && s.kind === 'function'));
    });

    it('class signature does not include the body', () => {
        const p = write('A.java', `public class Service extends Base implements I {\n}\n`);
        const s = sym(extractSymbols(p), 'Service');
        assert.ok(!s.signature.includes('{'));
    });
});

// ---------------------------------------------------------------------------
// Edge cases
// ---------------------------------------------------------------------------

describe('Edge cases', () => {
    beforeEach(setup);
    afterEach(teardown);

    it('returns empty result for a non-existent file', () => {
        const r = extractSymbols('/does/not/exist.ts');
        assert.deepEqual(r, { imports: [], symbols: [] });
    });

    it('returns empty result for an empty file', () => {
        const p = write('empty.ts', '');
        assert.deepEqual(extractSymbols(p), { imports: [], symbols: [] });
    });

    it('returns empty result for an unsupported extension', () => {
        const p = write('data.json', '{ "key": "value" }');
        assert.deepEqual(extractSymbols(p), { imports: [], symbols: [] });
    });

    it('does not throw on a file with syntax errors', () => {
        // TypeScript compiler is resilient — createSourceFile never throws.
        const p = write('broken.ts', `export function @broken((({\n`);
        assert.doesNotThrow(() => extractSymbols(p));
    });

    it('handles a file exactly at the size cap without truncation', () => {
        // A valid TS file padded to exactly MAX_FILE_BYTES with whitespace.
        const header  = `export function boundary(): void {}\n`;
        const padding = ' '.repeat(MAX_FILE_BYTES - header.length);
        const p = write('boundary.ts', header + padding);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'boundary').kind, 'function');
    });

    it('truncates oversized files and still extracts symbols at the top', () => {
        // Write a file larger than MAX_FILE_BYTES. The symbol is at the top
        // so it must survive truncation.
        const header  = `export function topSymbol(): void {}\n`;
        // Pad well beyond the cap (600 KB total).
        const padding = '// filler\n'.repeat(Math.ceil((600 * 1024) / '// filler\n'.length));
        const p = write('large.ts', header + padding);
        const r = extractSymbols(p);
        assert.equal(sym(r, 'topSymbol').kind, 'function');
    });

    it('signature length is at most 200 characters', () => {
        // A function with an absurdly long parameter list.
        const params = Array.from({ length: 30 }, (_, i) => `param${i}: string`).join(', ');
        const p = write('long.ts', `function huge(${params}): void {}\n`);
        const r = extractSymbols(p);
        assert.ok(sym(r, 'huge').signature.length <= 200);
    });
});
