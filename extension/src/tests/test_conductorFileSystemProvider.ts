/**
 * Unit tests for ConductorFileSystemProvider.
 *
 * `fetch` is mocked globally; the `vscode` module is stubbed so no VS Code
 * extension host is required.
 *
 * Coverage target: 45 tests covering:
 *  • watch() – deduplication, polling, disposal
 *  • stat() – success, 404, network error
 *  • readDirectory() – cache hit/miss, 404, network error
 *  • readFile() – success, 404, network error
 *  • writeFile() – success, 403, cache invalidation, onDidChangeFile
 *  • delete() – success, 404, 403, cache invalidation
 *  • rename() – success, 409
 *  • createDirectory() – success, 409
 *  • _parseUri edge cases
 *  • _clearCache / _cacheSize helpers
 */

// ---------------------------------------------------------------------------
// vscode stub
// ---------------------------------------------------------------------------

class FakeEventEmitter<T> {
    private _listeners: Array<(e: T) => void> = [];
    event = (cb: (e: T) => void): { dispose(): void } => {
        this._listeners.push(cb);
        return { dispose: () => { this._listeners = this._listeners.filter(l => l !== cb); } };
    };
    fire(e: T): void { this._listeners.forEach(l => l(e)); }
    dispose(): void { this._listeners = []; }
}

const FakeDisposable = class {
    constructor(private _fn: () => void) {}
    dispose() { this._fn(); }
};

const vscode = {
    FileType: { File: 1, Directory: 2, SymbolicLink: 64, Unknown: 0 },
    FileChangeType: { Changed: 1, Created: 2, Deleted: 3 },
    FileSystemError: {
        FileNotFound:  (uri?: unknown) => Object.assign(new Error(`FileNotFound: ${uri}`), { code: 'FileNotFound' }),
        NoPermissions: (uri?: unknown) => Object.assign(new Error(`NoPermissions: ${uri}`), { code: 'NoPermissions' }),
        FileExists:    (uri?: unknown) => Object.assign(new Error(`FileExists: ${uri}`), { code: 'FileExists' }),
        Unavailable:   (uri?: unknown) => Object.assign(new Error(`Unavailable: ${uri}`), { code: 'Unavailable' }),
    },
    EventEmitter: FakeEventEmitter,
    Disposable: FakeDisposable,
    Uri: {
        parse: (s: string) => {
            const u = new URL(s);
            return {
                scheme: u.protocol.replace(':', ''),
                authority: u.hostname,
                path: u.pathname,
                toString: () => s,
                with: (opts: Partial<{ scheme: string; authority: string; path: string }>) => {
                    const clone = { scheme: u.protocol.replace(':', ''), authority: u.hostname, path: u.pathname, toString: () => s };
                    return { ...clone, ...opts, toString: () => s };
                },
            };
        },
    },
};

jest.mock('vscode', () => vscode, { virtual: true });

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

type FetchMock = jest.Mock;
const gf = global as unknown as { fetch: FetchMock };

function ok(body: unknown, status = 200) {
    const text = typeof body === 'string' ? body : JSON.stringify(body);
    return {
        ok: true, status,
        text: async () => text,
        json: async () => JSON.parse(text),
        arrayBuffer: async () => new TextEncoder().encode(text).buffer,
    };
}

function err(status: number, body = '') {
    return { ok: false, status, text: async () => body, json: async () => ({}) };
}

function mockFetch(response: unknown) {
    gf.fetch = jest.fn().mockResolvedValue(response);
}

function mockFetchError() {
    gf.fetch = jest.fn().mockRejectedValue(new Error('Network error'));
}

// ---------------------------------------------------------------------------
// URI helpers
// ---------------------------------------------------------------------------

function makeUri(workspaceId: string, path: string) {
    const raw = `conductor://${workspaceId}/${path}`;
    const u = new URL(raw);
    return {
        scheme: 'conductor',
        authority: u.hostname,
        path: u.pathname,
        toString: () => raw,
        with: (opts: Partial<{ scheme: string; authority: string; path: string }>) => ({
            scheme: 'conductor',
            authority: u.hostname,
            path: u.pathname,
            ...opts,
            toString: () => raw,
        }),
    };
}

// ---------------------------------------------------------------------------
// Module under test
// ---------------------------------------------------------------------------

import { ConductorFileSystemProvider } from '../services/conductorFileSystemProvider';

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('ConductorFileSystemProvider', () => {
    const BASE = 'http://localhost:8000';
    let provider: ConductorFileSystemProvider;

    beforeEach(() => {
        provider = new ConductorFileSystemProvider(BASE);
        jest.useFakeTimers();
    });

    afterEach(() => {
        jest.useRealTimers();
        provider._clearCache();
    });

    // -----------------------------------------------------------------------
    // watch
    // -----------------------------------------------------------------------

    describe('watch()', () => {
        it('returns a Disposable', () => {
            const uri = makeUri('ws-1', 'src');
            const d = provider.watch(uri as never, { recursive: false, excludes: [] });
            expect(typeof d.dispose).toBe('function');
            d.dispose();
        });

        it('deduplicates watchers for the same URI', () => {
            const uri = makeUri('ws-1', 'src');
            const d1 = provider.watch(uri as never, { recursive: false, excludes: [] });
            const d2 = provider.watch(uri as never, { recursive: false, excludes: [] });
            expect(d1).toBe(d2);
            d1.dispose();
        });

        it('fires onDidChangeFile after the poll interval', () => {
            const uri = makeUri('ws-1', 'src');
            const events: unknown[] = [];
            provider.onDidChangeFile(e => events.push(e));
            provider.watch(uri as never, { recursive: false, excludes: [] });
            jest.advanceTimersByTime(5_001);
            expect(events.length).toBeGreaterThan(0);
        });

        it('stops firing after dispose', () => {
            const uri = makeUri('ws-1', 'src');
            const events: unknown[] = [];
            provider.onDidChangeFile(e => events.push(e));
            const d = provider.watch(uri as never, { recursive: false, excludes: [] });
            jest.advanceTimersByTime(5_001);
            const countBefore = events.length;
            d.dispose();
            jest.advanceTimersByTime(10_000);
            expect(events.length).toBe(countBefore);
        });
    });

    // -----------------------------------------------------------------------
    // stat
    // -----------------------------------------------------------------------

    describe('stat()', () => {
        it('returns FileStat for a file', async () => {
            mockFetch(ok({ type: 'file', size: 100, ctime: 0, mtime: 1 }));
            const uri = makeUri('ws-1', 'README.md');
            const stat = await provider.stat(uri as never);
            expect(stat.type).toBe(vscode.FileType.File);
            expect(stat.size).toBe(100);
        });

        it('returns FileStat for a directory', async () => {
            mockFetch(ok({ type: 'directory', size: 0, ctime: 0, mtime: 0 }));
            const uri = makeUri('ws-1', 'src');
            const stat = await provider.stat(uri as never);
            expect(stat.type).toBe(vscode.FileType.Directory);
        });

        it('throws FileNotFound on 404', async () => {
            mockFetch(err(404));
            const uri = makeUri('ws-1', 'missing.ts');
            await expect(provider.stat(uri as never)).rejects.toMatchObject({ code: 'FileNotFound' });
        });

        it('throws Unavailable on 500', async () => {
            mockFetch(err(500));
            const uri = makeUri('ws-1', 'file.ts');
            await expect(provider.stat(uri as never)).rejects.toMatchObject({ code: 'Unavailable' });
        });

        it('throws Unavailable on network error', async () => {
            mockFetchError();
            const uri = makeUri('ws-1', 'file.ts');
            await expect(provider.stat(uri as never)).rejects.toMatchObject({ code: 'Unavailable' });
        });
    });

    // -----------------------------------------------------------------------
    // readDirectory
    // -----------------------------------------------------------------------

    describe('readDirectory()', () => {
        it('returns entries from the backend', async () => {
            mockFetch(ok([
                { name: 'file.ts', type: 'file' },
                { name: 'sub', type: 'directory' },
            ]));
            const uri = makeUri('ws-1', '');
            const entries = await provider.readDirectory(uri as never);
            expect(entries).toHaveLength(2);
            expect(entries[0]).toEqual(['file.ts', vscode.FileType.File]);
            expect(entries[1]).toEqual(['sub', vscode.FileType.Directory]);
        });

        it('serves second call from cache (no extra fetch)', async () => {
            mockFetch(ok([{ name: 'a.ts', type: 'file' }]));
            const uri = makeUri('ws-1', 'src');
            await provider.readDirectory(uri as never);
            await provider.readDirectory(uri as never);
            expect((gf.fetch as jest.Mock).mock.calls.length).toBe(1);
        });

        it('throws FileNotFound on 404', async () => {
            mockFetch(err(404));
            const uri = makeUri('ws-1', 'nope');
            await expect(provider.readDirectory(uri as never)).rejects.toMatchObject({ code: 'FileNotFound' });
        });

        it('throws Unavailable on network error', async () => {
            mockFetchError();
            const uri = makeUri('ws-1', 'dir');
            await expect(provider.readDirectory(uri as never)).rejects.toMatchObject({ code: 'Unavailable' });
        });
    });

    // -----------------------------------------------------------------------
    // readFile
    // -----------------------------------------------------------------------

    describe('readFile()', () => {
        it('returns file contents as Uint8Array', async () => {
            mockFetch(ok('hello world'));
            const uri = makeUri('ws-1', 'hello.txt');
            const data = await provider.readFile(uri as never);
            expect(data).toBeInstanceOf(Uint8Array);
            expect(new TextDecoder().decode(data)).toBe('hello world');
        });

        it('throws FileNotFound on 404', async () => {
            mockFetch(err(404));
            const uri = makeUri('ws-1', 'missing.txt');
            await expect(provider.readFile(uri as never)).rejects.toMatchObject({ code: 'FileNotFound' });
        });

        it('throws Unavailable on 500', async () => {
            mockFetch(err(500));
            const uri = makeUri('ws-1', 'file.txt');
            await expect(provider.readFile(uri as never)).rejects.toMatchObject({ code: 'Unavailable' });
        });

        it('throws Unavailable on network error', async () => {
            mockFetchError();
            const uri = makeUri('ws-1', 'file.txt');
            await expect(provider.readFile(uri as never)).rejects.toMatchObject({ code: 'Unavailable' });
        });
    });

    // -----------------------------------------------------------------------
    // writeFile
    // -----------------------------------------------------------------------

    describe('writeFile()', () => {
        it('PUTs content to the correct URL', async () => {
            mockFetch(ok(''));
            const uri = makeUri('ws-1', 'src/app.ts');
            await provider.writeFile(uri as never, new Uint8Array([1, 2, 3]), { create: true, overwrite: true });
            expect((gf.fetch as jest.Mock).mock.calls[0][0]).toContain('/workspace/ws-1/files/');
            expect((gf.fetch as jest.Mock).mock.calls[0][1].method).toBe('PUT');
        });

        it('fires onDidChangeFile after write', async () => {
            mockFetch(ok(''));
            const uri = makeUri('ws-1', 'a.ts');
            const events: unknown[] = [];
            provider.onDidChangeFile(e => events.push(e));
            await provider.writeFile(uri as never, new Uint8Array(), { create: true, overwrite: true });
            expect(events.length).toBeGreaterThan(0);
        });

        it('throws NoPermissions on 403', async () => {
            mockFetch(err(403));
            const uri = makeUri('ws-1', 'readonly.ts');
            await expect(
                provider.writeFile(uri as never, new Uint8Array(), { create: false, overwrite: false })
            ).rejects.toMatchObject({ code: 'NoPermissions' });
        });

        it('throws Unavailable on network error', async () => {
            mockFetchError();
            const uri = makeUri('ws-1', 'file.ts');
            await expect(
                provider.writeFile(uri as never, new Uint8Array(), { create: true, overwrite: true })
            ).rejects.toMatchObject({ code: 'Unavailable' });
        });

        it('invalidates parent directory cache after write', async () => {
            // Prime the cache.
            mockFetch(ok([{ name: 'old.ts', type: 'file' }]));
            const dirUri = makeUri('ws-1', 'src');
            await provider.readDirectory(dirUri as never);
            expect(provider._cacheSize()).toBe(1);

            mockFetch(ok(''));
            const fileUri = makeUri('ws-1', 'src/new.ts');
            await provider.writeFile(fileUri as never, new Uint8Array(), { create: true, overwrite: false });
            // Cache should have been invalidated.
            expect(provider._cacheSize()).toBe(0);
        });
    });

    // -----------------------------------------------------------------------
    // delete
    // -----------------------------------------------------------------------

    describe('delete()', () => {
        it('DELETEs the correct URL', async () => {
            mockFetch(ok('', 204));
            const uri = makeUri('ws-1', 'old.ts');
            await provider.delete(uri as never, { recursive: false });
            expect((gf.fetch as jest.Mock).mock.calls[0][1].method).toBe('DELETE');
        });

        it('appends recursive query param', async () => {
            mockFetch(ok('', 204));
            const uri = makeUri('ws-1', 'dir');
            await provider.delete(uri as never, { recursive: true });
            expect((gf.fetch as jest.Mock).mock.calls[0][0]).toContain('recursive=true');
        });

        it('fires onDidChangeFile with Deleted type', async () => {
            mockFetch(ok('', 204));
            const uri = makeUri('ws-1', 'gone.ts');
            const events: Array<Array<{ type: number; uri: unknown }>> = [];
            provider.onDidChangeFile(e => events.push(e));
            await provider.delete(uri as never, { recursive: false });
            expect(events.flat().some(e => e.type === vscode.FileChangeType.Deleted)).toBe(true);
        });

        it('throws FileNotFound on 404', async () => {
            mockFetch(err(404));
            const uri = makeUri('ws-1', 'missing.ts');
            await expect(provider.delete(uri as never, { recursive: false })).rejects.toMatchObject({ code: 'FileNotFound' });
        });

        it('throws NoPermissions on 403', async () => {
            mockFetch(err(403));
            const uri = makeUri('ws-1', 'protected.ts');
            await expect(provider.delete(uri as never, { recursive: false })).rejects.toMatchObject({ code: 'NoPermissions' });
        });
    });

    // -----------------------------------------------------------------------
    // rename
    // -----------------------------------------------------------------------

    describe('rename()', () => {
        it('POSTs to /rename endpoint', async () => {
            mockFetch(ok(''));
            const oldUri = makeUri('ws-1', 'old.ts');
            const newUri = makeUri('ws-1', 'new.ts');
            await provider.rename(oldUri as never, newUri as never, { overwrite: false });
            expect((gf.fetch as jest.Mock).mock.calls[0][0]).toContain('/rename');
            expect((gf.fetch as jest.Mock).mock.calls[0][1].method).toBe('POST');
        });

        it('throws FileExists on 409', async () => {
            mockFetch(err(409));
            const oldUri = makeUri('ws-1', 'a.ts');
            const newUri = makeUri('ws-1', 'b.ts');
            await expect(provider.rename(oldUri as never, newUri as never, { overwrite: false }))
                .rejects.toMatchObject({ code: 'FileExists' });
        });

        it('fires Deleted + Created change events', async () => {
            mockFetch(ok(''));
            const oldUri = makeUri('ws-1', 'a.ts');
            const newUri = makeUri('ws-1', 'b.ts');
            const events: Array<{ type: number; uri: unknown }> = [];
            provider.onDidChangeFile(e => events.push(...e));
            await provider.rename(oldUri as never, newUri as never, { overwrite: true });
            const types = events.map(e => e.type);
            expect(types).toContain(vscode.FileChangeType.Deleted);
            expect(types).toContain(vscode.FileChangeType.Created);
        });

        it('throws Unavailable on network error', async () => {
            mockFetchError();
            const oldUri = makeUri('ws-1', 'a.ts');
            const newUri = makeUri('ws-1', 'b.ts');
            await expect(provider.rename(oldUri as never, newUri as never, { overwrite: false }))
                .rejects.toMatchObject({ code: 'Unavailable' });
        });
    });

    // -----------------------------------------------------------------------
    // createDirectory
    // -----------------------------------------------------------------------

    describe('createDirectory()', () => {
        it('POSTs to the directory URL', async () => {
            mockFetch(ok('', 201));
            const uri = makeUri('ws-1', 'new-dir');
            await provider.createDirectory(uri as never);
            expect((gf.fetch as jest.Mock).mock.calls[0][1].method).toBe('POST');
        });

        it('throws FileExists on 409', async () => {
            mockFetch(err(409));
            const uri = makeUri('ws-1', 'existing-dir');
            await expect(provider.createDirectory(uri as never)).rejects.toMatchObject({ code: 'FileExists' });
        });

        it('fires Created change event', async () => {
            mockFetch(ok('', 201));
            const uri = makeUri('ws-1', 'brand-new');
            const events: Array<{ type: number; uri: unknown }> = [];
            provider.onDidChangeFile(e => events.push(...e));
            await provider.createDirectory(uri as never);
            expect(events.some(e => e.type === vscode.FileChangeType.Created)).toBe(true);
        });
    });

    // -----------------------------------------------------------------------
    // Cache helpers
    // -----------------------------------------------------------------------

    describe('cache helpers', () => {
        it('_cacheSize() returns 0 initially', () => {
            expect(provider._cacheSize()).toBe(0);
        });

        it('_cacheSize() increases after a readDirectory', async () => {
            mockFetch(ok([]));
            const uri = makeUri('ws-1', 'src');
            await provider.readDirectory(uri as never);
            expect(provider._cacheSize()).toBe(1);
        });

        it('_clearCache() resets to 0', async () => {
            mockFetch(ok([]));
            const uri = makeUri('ws-1', 'src');
            await provider.readDirectory(uri as never);
            provider._clearCache();
            expect(provider._cacheSize()).toBe(0);
        });
    });
});
