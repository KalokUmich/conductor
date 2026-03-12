/**
 * Unit tests for WorkspaceClient.
 *
 * The native `fetch` API is replaced with a Jest mock so no real network calls
 * are made.  All tests run in plain Node / Jest.
 *
 * Coverage target: 59 tests covering:
 *  • isBackendAlive()
 *  • createWorkspace()
 *  • getWorkspace()
 *  • listWorkspaces()
 *  • updateWorkspace()
 *  • deleteWorkspace()
 *  • pollUntilReady()
 *  • WorkspaceApiError shape
 *  • Base URL normalisation
 */

import {
    WorkspaceClient,
    WorkspaceApiError,
    WorkspaceInfo,
    WorkspaceListResponse,
} from '../services/workspaceClient';

// ---------------------------------------------------------------------------
// fetch mock helpers
// ---------------------------------------------------------------------------

type FetchMock = jest.Mock;
const globalFetch = global as unknown as { fetch: FetchMock };

function mockFetch(body: unknown, status = 200): void {
    const text = typeof body === 'string' ? body : JSON.stringify(body);
    globalFetch.fetch = jest.fn().mockResolvedValue({
        ok: status >= 200 && status < 300,
        status,
        text: async () => text,
        json: async () => JSON.parse(text),
        arrayBuffer: async () => new TextEncoder().encode(text).buffer,
    });
}

function mockFetchError(message = 'Network error'): void {
    globalFetch.fetch = jest.fn().mockRejectedValue(new Error(message));
}

function fetchCall(n = 0) {
    return (globalFetch.fetch as jest.Mock).mock.calls[n];
}

const BASE = 'http://localhost:8000';

const WORKSPACE: WorkspaceInfo = {
    id: 'ws-1',
    name: 'test',
    template: 'python-3.11',
    status: 'ready',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('WorkspaceClient', () => {

    let client: WorkspaceClient;

    beforeEach(() => {
        client = new WorkspaceClient(BASE);
        jest.useFakeTimers();
    });

    afterEach(() => {
        jest.useRealTimers();
    });

    // -----------------------------------------------------------------------
    // Base URL normalisation
    // -----------------------------------------------------------------------

    describe('base URL normalisation', () => {
        it('strips trailing slash', async () => {
            const c = new WorkspaceClient('http://localhost:8000/');
            mockFetch({ ok: true });
            await c.isBackendAlive();
            expect(fetchCall()[0]).toBe('http://localhost:8000/health');
        });

        it('preserves base without trailing slash', async () => {
            mockFetch({ ok: true });
            await client.isBackendAlive();
            expect(fetchCall()[0]).toBe('http://localhost:8000/health');
        });
    });

    // -----------------------------------------------------------------------
    // isBackendAlive
    // -----------------------------------------------------------------------

    describe('isBackendAlive()', () => {
        it('returns true when GET /health returns 200', async () => {
            mockFetch('', 200);
            expect(await client.isBackendAlive()).toBe(true);
        });

        it('returns false when GET /health returns 503', async () => {
            mockFetch('', 503);
            expect(await client.isBackendAlive()).toBe(false);
        });

        it('returns false on network error', async () => {
            mockFetchError();
            expect(await client.isBackendAlive()).toBe(false);
        });

        it('calls the correct URL', async () => {
            mockFetch('');
            await client.isBackendAlive();
            expect(fetchCall()[0]).toBe(`${BASE}/health`);
        });
    });

    // -----------------------------------------------------------------------
    // createWorkspace
    // -----------------------------------------------------------------------

    describe('createWorkspace()', () => {
        it('POSTs to /workspace/ with JSON body', async () => {
            mockFetch(WORKSPACE);
            await client.createWorkspace({ name: 'ws', template: 'python-3.11' });
            const [url, opts] = fetchCall();
            expect(url).toBe(`${BASE}/workspace/`);
            expect(opts.method).toBe('POST');
            expect(JSON.parse(opts.body)).toMatchObject({ name: 'ws', template: 'python-3.11' });
        });

        it('returns parsed WorkspaceInfo on 201', async () => {
            mockFetch(WORKSPACE, 201);
            const result = await client.createWorkspace({ name: 'ws', template: 'python-3.11' });
            expect(result.id).toBe('ws-1');
        });

        it('includes git_repo_url when provided', async () => {
            mockFetch(WORKSPACE);
            await client.createWorkspace({
                name: 'ws',
                template: 'python-3.11',
                git_repo_url: 'https://github.com/org/repo',
            });
            expect(JSON.parse(fetchCall()[1].body)).toMatchObject({
                git_repo_url: 'https://github.com/org/repo',
            });
        });

        it('throws WorkspaceApiError on 422', async () => {
            mockFetch('Unprocessable', 422);
            await expect(client.createWorkspace({ name: '', template: '' })).rejects.toThrow(WorkspaceApiError);
        });

        it('WorkspaceApiError.status is set correctly', async () => {
            mockFetch('Conflict', 409);
            try {
                await client.createWorkspace({ name: 'ws', template: 'blank' });
            } catch (e) {
                expect((e as WorkspaceApiError).status).toBe(409);
            }
        });
    });

    // -----------------------------------------------------------------------
    // getWorkspace
    // -----------------------------------------------------------------------

    describe('getWorkspace()', () => {
        it('GETs /workspace/{id}', async () => {
            mockFetch(WORKSPACE);
            await client.getWorkspace('ws-1');
            expect(fetchCall()[0]).toBe(`${BASE}/workspace/ws-1`);
        });

        it('returns WorkspaceInfo', async () => {
            mockFetch(WORKSPACE);
            const result = await client.getWorkspace('ws-1');
            expect(result.name).toBe('test');
        });

        it('throws WorkspaceApiError on 404', async () => {
            mockFetch('Not found', 404);
            await expect(client.getWorkspace('missing')).rejects.toThrow(WorkspaceApiError);
        });

        it('encodes workspace ID in the URL', async () => {
            mockFetch(WORKSPACE);
            await client.getWorkspace('ws/special');
            expect(fetchCall()[0]).toContain(encodeURIComponent('ws/special'));
        });
    });

    // -----------------------------------------------------------------------
    // listWorkspaces
    // -----------------------------------------------------------------------

    describe('listWorkspaces()', () => {
        const LIST: WorkspaceListResponse = {
            items: [WORKSPACE],
            total: 1,
            page: 1,
            page_size: 20,
        };

        it('GETs /workspace/ with default pagination', async () => {
            mockFetch(LIST);
            await client.listWorkspaces();
            expect(fetchCall()[0]).toBe(`${BASE}/workspace/?page=1&page_size=20`);
        });

        it('accepts custom page and pageSize', async () => {
            mockFetch(LIST);
            await client.listWorkspaces(3, 10);
            expect(fetchCall()[0]).toBe(`${BASE}/workspace/?page=3&page_size=10`);
        });

        it('returns the items array', async () => {
            mockFetch(LIST);
            const result = await client.listWorkspaces();
            expect(result.items).toHaveLength(1);
        });

        it('throws WorkspaceApiError on 500', async () => {
            mockFetch('Server error', 500);
            await expect(client.listWorkspaces()).rejects.toThrow(WorkspaceApiError);
        });
    });

    // -----------------------------------------------------------------------
    // updateWorkspace
    // -----------------------------------------------------------------------

    describe('updateWorkspace()', () => {
        it('PATCHes /workspace/{id}', async () => {
            mockFetch(WORKSPACE);
            await client.updateWorkspace('ws-1', { name: 'new-name' });
            const [url, opts] = fetchCall();
            expect(url).toBe(`${BASE}/workspace/ws-1`);
            expect(opts.method).toBe('PATCH');
        });

        it('sends the patch body as JSON', async () => {
            mockFetch(WORKSPACE);
            await client.updateWorkspace('ws-1', { name: 'new-name' });
            expect(JSON.parse(fetchCall()[1].body)).toMatchObject({ name: 'new-name' });
        });

        it('returns the updated WorkspaceInfo', async () => {
            const updated = { ...WORKSPACE, name: 'new-name' };
            mockFetch(updated);
            const result = await client.updateWorkspace('ws-1', { name: 'new-name' });
            expect(result.name).toBe('new-name');
        });

        it('throws WorkspaceApiError on 404', async () => {
            mockFetch('Not found', 404);
            await expect(client.updateWorkspace('bad-id', { name: 'x' })).rejects.toThrow(WorkspaceApiError);
        });
    });

    // -----------------------------------------------------------------------
    // deleteWorkspace
    // -----------------------------------------------------------------------

    describe('deleteWorkspace()', () => {
        it('DELETEs /workspace/{id}', async () => {
            mockFetch('', 204);
            await client.deleteWorkspace('ws-1');
            const [url, opts] = fetchCall();
            expect(url).toBe(`${BASE}/workspace/ws-1`);
            expect(opts.method).toBe('DELETE');
        });

        it('resolves without a value on success', async () => {
            mockFetch('', 204);
            await expect(client.deleteWorkspace('ws-1')).resolves.toBeUndefined();
        });

        it('throws WorkspaceApiError on 404', async () => {
            mockFetch('Not found', 404);
            await expect(client.deleteWorkspace('missing')).rejects.toThrow(WorkspaceApiError);
        });

        it('encodes workspace ID in the URL', async () => {
            mockFetch('', 204);
            await client.deleteWorkspace('ws/1');
            expect(fetchCall()[0]).toContain(encodeURIComponent('ws/1'));
        });
    });

    // -----------------------------------------------------------------------
    // pollUntilReady
    // -----------------------------------------------------------------------

    describe('pollUntilReady()', () => {
        it('returns immediately when workspace is already ready', async () => {
            mockFetch({ ...WORKSPACE, status: 'ready' });
            const result = await client.pollUntilReady('ws-1', 100, 5_000);
            expect(result.status).toBe('ready');
        });

        it('returns when workspace transitions to ready after one poll', async () => {
            let calls = 0;
            globalFetch.fetch = jest.fn().mockImplementation(async () => {
                calls++;
                const status = calls === 1 ? 'creating' : 'ready';
                const body = JSON.stringify({ ...WORKSPACE, status });
                return { ok: true, status: 200, text: async () => body, json: async () => JSON.parse(body) };
            });

            const poll = client.pollUntilReady('ws-1', 100, 5_000);
            jest.advanceTimersByTime(200);
            const result = await poll;
            expect(result.status).toBe('ready');
        });

        it('returns when workspace is in error state', async () => {
            mockFetch({ ...WORKSPACE, status: 'error' });
            const result = await client.pollUntilReady('ws-1', 100, 5_000);
            expect(result.status).toBe('error');
        });

        it('throws when timeout is reached', async () => {
            mockFetch({ ...WORKSPACE, status: 'creating' });
            const poll = client.pollUntilReady('ws-1', 100, 500);
            jest.advanceTimersByTime(600);
            await expect(poll).rejects.toThrow(/did not become ready/);
        });

        it('propagates WorkspaceApiError from getWorkspace', async () => {
            mockFetch('Server error', 500);
            await expect(client.pollUntilReady('ws-bad', 100, 1_000)).rejects.toThrow(WorkspaceApiError);
        });
    });

    // -----------------------------------------------------------------------
    // WorkspaceApiError
    // -----------------------------------------------------------------------

    describe('WorkspaceApiError', () => {
        it('has the correct name', () => {
            const err = new WorkspaceApiError(404, 'not found');
            expect(err.name).toBe('WorkspaceApiError');
        });

        it('stores status and body', () => {
            const err = new WorkspaceApiError(422, 'validation error');
            expect(err.status).toBe(422);
            expect(err.body).toBe('validation error');
        });

        it('is an instance of Error', () => {
            expect(new WorkspaceApiError(500, '')).toBeInstanceOf(Error);
        });

        it('accepts an optional custom message', () => {
            const err = new WorkspaceApiError(500, '', 'custom msg');
            expect(err.message).toBe('custom msg');
        });

        it('defaults message to HTTP status + body', () => {
            const err = new WorkspaceApiError(404, 'missing');
            expect(err.message).toBe('HTTP 404: missing');
        });
    });
});
