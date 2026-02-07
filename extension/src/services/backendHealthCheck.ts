/**
 * Backend health check service.
 *
 * Pure async function that calls GET /health on a given backend URL and
 * returns a boolean indicating whether the backend is reachable and healthy.
 *
 * This module is intentionally free of VS Code API dependencies and state
 * machine dependencies so that it can be unit-tested in isolation.
 *
 * @module services/backendHealthCheck
 */

/** Default timeout for the health check request (milliseconds). */
const DEFAULT_TIMEOUT_MS = 5_000;

/**
 * Options for the health check request.
 */
export interface HealthCheckOptions {
    /** Request timeout in milliseconds. Defaults to 5 000 ms. */
    timeoutMs?: number;
}

/**
 * Check whether the backend is reachable and healthy.
 *
 * Sends a GET request to `${backendUrl}/health` and returns `true` if
 * the response status is 200 OK.  Returns `false` for any network error,
 * non-200 status, or timeout.
 *
 * @param backendUrl - Base URL of the backend (e.g. "http://localhost:8000").
 * @param options    - Optional configuration (timeout, etc.).
 * @returns `true` if the backend responded with 200 OK, `false` otherwise.
 */
export async function checkBackendHealth(
    backendUrl: string,
    options: HealthCheckOptions = {},
): Promise<boolean> {
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;

    try {
        const response = await fetch(`${backendUrl}/health`, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            signal: AbortSignal.timeout(timeoutMs),
        });
        return response.ok && response.status === 200;
    } catch {
        // Network error, DNS failure, timeout, etc.
        return false;
    }
}

