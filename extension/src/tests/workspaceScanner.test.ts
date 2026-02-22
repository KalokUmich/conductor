/**
 * Tests for workspaceScanner — scanWorkspaceV1.
 *
 * Run after compilation:
 *   node --test out/tests/workspaceScanner.test.js
 */
import { describe, it, beforeEach, afterEach } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import * as crypto from 'node:crypto';

import { scanWorkspaceV1 } from '../services/workspaceScanner';
import { ConductorDb } from '../services/conductorDb';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

let tmpDir: string;

/** Write a file relative to tmpDir, creating parent dirs as needed. Returns abs path. */
function write(relPath: string, content: string): string {
    const abs = path.join(tmpDir, relPath);
    fs.mkdirSync(path.dirname(abs), { recursive: true });
    fs.writeFileSync(abs, content, 'utf-8');
    return abs;
}

/** Open the scanner's DB (requires scanWorkspaceV1 to have run first). */
function openDb(): ConductorDb {
    return new ConductorDb(path.join(tmpDir, '.conductor', 'cache.db'));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('scanWorkspaceV1', () => {
    beforeEach(() => {
        tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'conductor-scanner-test-'));
    });

    afterEach(() => {
        fs.rmSync(tmpDir, { recursive: true, force: true });
    });

    // -----------------------------------------------------------------------
    // Setup
    // -----------------------------------------------------------------------

    it('creates .conductor directory when missing', async () => {
        await scanWorkspaceV1(tmpDir);
        assert.ok(fs.existsSync(path.join(tmpDir, '.conductor')));
    });

    it('completes without error on an empty workspace', async () => {
        await assert.doesNotReject(scanWorkspaceV1(tmpDir));
    });

    // -----------------------------------------------------------------------
    // File discovery
    // -----------------------------------------------------------------------

    it('stores all recognised source files in the database', async () => {
        write('src/index.ts', 'const x = 1;');
        write('src/utils.js', 'function foo() {}');
        write('main.py', 'print("hello")');
        write('App.java', 'public class App {}');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const paths = db.getAllFiles().map(f => f.path);
        db.close();

        assert.ok(paths.includes('src/index.ts'));
        assert.ok(paths.includes('src/utils.js'));
        assert.ok(paths.includes('main.py'));
        assert.ok(paths.includes('App.java'));
    });

    it('ignores files with non-source extensions', async () => {
        write('README.md', '# Hello');
        write('config.yaml', 'key: value');
        write('.env', 'SECRET=1');
        write('src/index.ts', 'const x = 1;');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const paths = db.getAllFiles().map(f => f.path);
        db.close();

        assert.ok(!paths.includes('README.md'));
        assert.ok(!paths.includes('config.yaml'));
        assert.ok(!paths.includes('.env'));
        assert.ok(paths.includes('src/index.ts'));
    });

    it('uses workspace-relative paths (not absolute)', async () => {
        write('deep/nested/util.py', 'x = 1');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const files = db.getAllFiles();
        db.close();

        assert.ok(files.some(f => f.path === 'deep/nested/util.py'));
        assert.ok(!files.some(f => path.isAbsolute(f.path)));
    });

    // -----------------------------------------------------------------------
    // Ignored directories
    // -----------------------------------------------------------------------

    const IGNORED = ['.git', 'node_modules', 'dist', 'build', 'out', 'target'];

    for (const dir of IGNORED) {
        it(`ignores the ${dir} directory`, async () => {
            write(`${dir}/internal.ts`, 'const x = 1;');
            write('src/real.ts', 'const y = 2;');

            await scanWorkspaceV1(tmpDir);

            const db = openDb();
            const paths = db.getAllFiles().map(f => f.path);
            db.close();

            assert.ok(!paths.some(p => p.startsWith(`${dir}/`)), `${dir}/ should be ignored`);
            assert.ok(paths.includes('src/real.ts'));
        });
    }

    // -----------------------------------------------------------------------
    // Language detection
    // -----------------------------------------------------------------------

    it('detects TypeScript for .ts and .tsx files', async () => {
        write('a.ts', '');
        write('b.tsx', '');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const files = db.getAllFiles();
        db.close();

        assert.equal(files.find(f => f.path === 'a.ts')?.lang, 'typescript');
        assert.equal(files.find(f => f.path === 'b.tsx')?.lang, 'typescript');
    });

    it('detects JavaScript for .js and .jsx files', async () => {
        write('a.js', '');
        write('b.jsx', '');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const files = db.getAllFiles();
        db.close();

        assert.equal(files.find(f => f.path === 'a.js')?.lang, 'javascript');
        assert.equal(files.find(f => f.path === 'b.jsx')?.lang, 'javascript');
    });

    it('detects Python for .py files', async () => {
        write('main.py', 'x = 1');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const files = db.getAllFiles();
        db.close();

        assert.equal(files.find(f => f.path === 'main.py')?.lang, 'python');
    });

    it('detects Java for .java files', async () => {
        write('Main.java', 'public class Main {}');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const files = db.getAllFiles();
        db.close();

        assert.equal(files.find(f => f.path === 'Main.java')?.lang, 'java');
    });

    // -----------------------------------------------------------------------
    // Metadata correctness
    // -----------------------------------------------------------------------

    it('stores sha1 as a 40-character lowercase hex string', async () => {
        write('src/code.ts', 'const x = 42;');

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const row = db.getAllFiles().find(f => f.path === 'src/code.ts');
        db.close();

        assert.ok(row, 'row should exist');
        assert.match(row.sha1, /^[0-9a-f]{40}$/, 'sha1 must be 40-char hex');
    });

    it('sha1 matches the expected crypto.createHash result', async () => {
        const content = 'export const answer = 42;\n';
        write('src/answer.ts', content);

        await scanWorkspaceV1(tmpDir);

        const expected = crypto.createHash('sha1').update(content, 'utf-8').digest('hex');

        const db = openDb();
        const row = db.getAllFiles().find(f => f.path === 'src/answer.ts');
        db.close();

        assert.ok(row, 'row should exist');
        assert.equal(row.sha1, expected);
    });

    it('stores correct file size in bytes', async () => {
        const absPath = write('src/index.ts', 'const x = 1;');
        const { size } = fs.statSync(absPath);

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const row = db.getAllFiles().find(f => f.path === 'src/index.ts');
        db.close();

        assert.ok(row);
        assert.equal(row.size, size);
    });

    it('stores mtime in milliseconds matching fs.stat', async () => {
        const absPath = write('src/index.ts', 'const x = 1;');
        const { mtimeMs } = fs.statSync(absPath);

        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const row = db.getAllFiles().find(f => f.path === 'src/index.ts');
        db.close();

        assert.ok(row);
        assert.equal(row.mtime, mtimeMs);
    });

    // -----------------------------------------------------------------------
    // Incremental behaviour
    // -----------------------------------------------------------------------

    it('reuses sha1 on a second scan when the file is unchanged', async () => {
        write('src/stable.ts', 'const x = 1;');

        await scanWorkspaceV1(tmpDir); // cold scan

        const db1 = openDb();
        const before = db1.getAllFiles().find(f => f.path === 'src/stable.ts');
        db1.close();
        assert.ok(before?.sha1, 'sha1 should exist after first scan');

        await scanWorkspaceV1(tmpDir); // warm scan — nothing changed

        const db2 = openDb();
        const after = db2.getAllFiles().find(f => f.path === 'src/stable.ts');
        db2.close();

        assert.equal(after?.sha1, before.sha1, 'sha1 should be unchanged on warm scan');
    });

    it('updates sha1 when file content changes between scans', async () => {
        const absPath = write('src/change.ts', 'const x = 1;');

        await scanWorkspaceV1(tmpDir);

        const db1 = openDb();
        const before = db1.getAllFiles().find(f => f.path === 'src/change.ts');
        db1.close();
        assert.ok(before?.sha1);

        // Overwrite the file and advance mtime so the scanner detects the change.
        fs.writeFileSync(absPath, 'const x = 999;', 'utf-8');
        const futureSec = Date.now() / 1000 + 60;
        fs.utimesSync(absPath, futureSec, futureSec);

        await scanWorkspaceV1(tmpDir);

        const db2 = openDb();
        const after = db2.getAllFiles().find(f => f.path === 'src/change.ts');
        db2.close();

        assert.notEqual(after?.sha1, before.sha1, 'sha1 should be updated after modification');
    });

    it('picks up newly added files on a second scan', async () => {
        write('src/original.ts', 'const a = 1;');
        await scanWorkspaceV1(tmpDir);

        write('src/new.ts', 'const b = 2;');
        await scanWorkspaceV1(tmpDir);

        const db = openDb();
        const paths = db.getAllFiles().map(f => f.path);
        db.close();

        assert.ok(paths.includes('src/original.ts'));
        assert.ok(paths.includes('src/new.ts'));
    });
});
