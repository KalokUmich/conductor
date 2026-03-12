export interface NgrokTunnel {
    publicUrl: string;
    proto: string;
    config?: { addr?: string } | null;
}

export function selectPreferredNgrokUrl(
    tunnels: NgrokTunnel[],
    preferredPort: string = '8000',
): string | null {
    const preferredTunnel = tunnels.find(tunnel =>
        tunnel.proto === 'https' && tunnel.config?.addr?.includes(preferredPort),
    );

    if (preferredTunnel) {
        return preferredTunnel.publicUrl;
    }

    return tunnels.find(tunnel => tunnel.proto === 'https')?.publicUrl ?? null;
}

export interface BackendConnectionDiagnosisInput {
    backendUrl: string;
    backendHealthy: boolean;
    hasConnectedBefore: boolean;
    reconnectAttempts: number;
    maxReconnectAttempts: number;
}

export interface BackendConnectionDiagnosis {
    status: string;
    isError: boolean;
}

export function diagnoseBackendConnection(
    input: BackendConnectionDiagnosisInput,
): BackendConnectionDiagnosis {
    const attempts = Math.min(input.reconnectAttempts, input.maxReconnectAttempts);
    const attemptsLabel = `(${attempts}/${input.maxReconnectAttempts})`;
    const exhausted = input.reconnectAttempts >= input.maxReconnectAttempts;

    if (!input.backendHealthy) {
        return {
            status: exhausted
                ? `❌ Backend unreachable at ${input.backendUrl}. Start the backend or update aiCollab.backendUrl.`
                : `❌ Backend unreachable at ${input.backendUrl}. Retrying ${attemptsLabel}...`,
            isError: true,
        };
    }

    if (!input.hasConnectedBefore) {
        return {
            status: exhausted
                ? '❌ Backend is reachable, but the chat socket could not be established.'
                : `⚠️ Backend is reachable, but the chat socket could not be established. Retrying ${attemptsLabel}...`,
            isError: exhausted,
        };
    }

    if (exhausted) {
        return {
            status: '❌ Connection lost. Unable to reconnect to chat.',
            isError: true,
        };
    }

    return {
        status: `⚠️ Connection dropped. Reconnecting ${attemptsLabel}...`,
        isError: false,
    };
}