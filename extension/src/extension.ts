/**
 * Conductor (AI Collab) VS Code Extension
 *
 * This extension enables real-time collaborative development with AI assistance.
 * It combines VS Code Live Share for code collaboration, WebSocket-based chat
 * for team communication, and AI-powered code generation.
 *
 * Main components:
 * - AICollabViewProvider: WebView panel for chat and AI interaction
 * - SessionService: Manages room/session state across reloads
 * - PermissionsService: Role-based access control (lead vs member)
 * - DiffPreviewService: Shows and applies AI-generated code changes
 *
 * @module extension
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as vsls from 'vsls/vscode';

import { checkBackendHealth } from './services/backendHealthCheck';
import { ConductorController } from './services/conductorController';
import {
    ConductorState,
    ConductorStateMachine,
} from './services/conductorStateMachine';
import { ChangeSet, FileChange, getDiffPreviewService } from './services/diffPreview';
import { getPermissionsService } from './services/permissions';
import { getSessionService } from './services/session';
import { wrapIdentity, getValidIdentity, getStoredProvider, isStale, SSOProvider } from './services/ssoIdentityCache';
import { detectWorkspaceLanguages, clearLanguageCache } from './services/languageDetector';

/** Output channel for logging invite links to the user. */
let outputChannel: vscode.OutputChannel;

/** GlobalState key for persisting FSM state across reloads. */
const FSM_STATE_KEY = 'conductor.fsmState';

/**
 * Get the backend server URL from configuration.
 * @returns The backend URL (e.g., "http://localhost:8000")
 */
function getBackendUrl(): string {
    return getSessionService().getBackendUrl();
}

/**
 * Extension activation handler.
 *
 * Called when the extension is first activated (on command execution or view open).
 * Initializes services, registers commands, and sets up the WebView provider.
 *
 * @param context - The VS Code extension context for managing subscriptions
 */
export function activate(context: vscode.ExtensionContext): void {
    console.log('AI Collab extension is now active');

    // Create output channel for logging invite links (visible in Output panel)
    outputChannel = vscode.window.createOutputChannel('Conductor Invite Links');
    context.subscriptions.push(outputChannel);

    // Initialize session service - must happen before WebView creation
    getSessionService().initialize(context);
    console.log(`[AI Collab] Session initialized with roomId: ${getSessionService().getRoomId()}`);

    // Clear language detection cache when workspace folders change
    context.subscriptions.push(
        vscode.workspace.onDidChangeWorkspaceFolders(() => clearLanguageCache())
    );

    // Detect ngrok URL if ngrok is running (async, non-blocking)
    getSessionService().detectNgrokUrl().then(ngrokUrl => {
        if (ngrokUrl) {
            console.log(`[AI Collab] Using ngrok URL: ${ngrokUrl}`);
            vscode.window.showInformationMessage(`🌐 Ngrok detected: ${ngrokUrl}`);
        } else {
            console.log('[AI Collab] Ngrok not detected, using localhost');
        }
    });

    // ---------------------------------------------------------------
    // Conductor FSM + Controller
    // ---------------------------------------------------------------

    // Always start fresh from Idle state on extension activation.
    // We don't restore Hosting/Joined states because:
    // 1. Live Share session may have ended
    // 2. WebSocket connections are lost on reload
    // 3. User should explicitly start a new session
    const fsm = new ConductorStateMachine();
    console.log('[Conductor] Starting FSM from Idle (fresh start)');

    // Create the controller with injected dependencies
    const controller = new ConductorController(
        fsm,
        checkBackendHealth,
        () => getSessionService().getBackendUrl(),
        () => {
            getSessionService().resetSession();
            return getSessionService().getRoomId();
        },
    );

    // Persist FSM state to globalState on every transition
    controller.onStateChange((_prev, next) => {
        context.globalState.update(FSM_STATE_KEY, next);
        console.log(`[Conductor] FSM state persisted: ${next}`);
    });

    // Auto-start the controller (health check) — async, non-blocking
    controller.start().then(state => {
        console.log(`[Conductor] Initial health check complete → ${state}`);
    }).catch(err => {
        console.warn('[Conductor] Health check start failed:', err);
    });

    // Register the WebView provider for the sidebar chat panel
    const provider = new AICollabViewProvider(context.extensionUri, context, controller);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('aiCollabView', provider, {
            webviewOptions: {
                retainContextWhenHidden: true  // Keep WebView state when hidden
            }
        })
    );

    // Register command to focus the AI Collab panel
    const disposable = vscode.commands.registerCommand('ai-collab.openPanel', () => {
        vscode.commands.executeCommand('aiCollabView.focus');
    });
    context.subscriptions.push(disposable);

    // Move view to secondary sidebar on first activation (right side layout)
    // Layout: [Activity Bar] [Primary Sidebar] [Editor] [AI Collab Sidebar]
    const hasMovedToSecondarySidebar = context.globalState.get<boolean>('hasMovedToSecondarySidebar', false);
    if (!hasMovedToSecondarySidebar) {
        setTimeout(async () => {
            try {
                await vscode.commands.executeCommand(
                    'workbench.action.moveViewContainerToSecondarySidebar',
                    'workbench.view.extension.ai-collab-sidebar'
                );
                context.globalState.update('hasMovedToSecondarySidebar', true);
                console.log('AI Collab view moved to secondary side bar');
            } catch (error) {
                console.log('Could not move view to secondary side bar:', error);
            }
        }, 1000);  // Delay to ensure view is registered
    }
}

/**
 * Extension deactivation handler.
 *
 * Called when the extension is deactivated (VS Code closing or extension disabled).
 * Performs cleanup of any resources.
 */
export function deactivate(): void {
    console.log('AI Collab extension is now deactivated');
}

/**
 * Policy evaluation result from the backend /policy/evaluate-auto-apply endpoint.
 */
interface PolicyResult {
    /** Whether auto-apply is allowed for this changeset. */
    allowed: boolean;
    /** Reasons why auto-apply was denied (empty if allowed). */
    reasons: string[];
    /** Number of files in the changeset. */
    files_count: number;
    /** Total lines changed across all files. */
    lines_changed: number;
}

/**
 * WebView View Provider for the AI Collab sidebar.
 *
 * This class manages the chat/AI WebView panel in the VS Code sidebar.
 * It handles:
 * - WebView HTML content generation
 * - Message passing between WebView and extension
 * - Live Share auto-start for lead users
 * - Code generation and application flow
 * - Sequential change review process
 *
 * The sequential review flow allows users to review and apply changes
 * one file at a time, with diff previews for each change.
 */
class AICollabViewProvider implements vscode.WebviewViewProvider {
    /** The WebView instance (undefined until resolved). */
    private _view?: vscode.WebviewView;
    /** Extension URI for loading local resources. */
    private _extensionUri: vscode.Uri;
    /** Extension context for accessing globalState. */
    private _context: vscode.ExtensionContext;
    /** Conductor controller for driving the state machine. */
    private _controller: ConductorController;
    /** Whether auto-apply is enabled for this session. */
    private _autoApplyEnabled: boolean = false;

    // SSO state
    /** Whether SSO polling is active. */
    private _ssoPolling: boolean = false;
    /** Timer ID for SSO polling interval. */
    private _ssoTimerId?: ReturnType<typeof setInterval>;
    /** Cached enabled SSO providers from backend /auth/providers. */
    private _enabledSSOProviders: { aws: boolean; google: boolean } = { aws: false, google: false };

    // Sequential change review queue
    /** Changes waiting to be reviewed/applied. */
    private _pendingChanges: FileChange[] = [];
    /** Index of the current change being reviewed. */
    private _currentChangeIndex: number = 0;
    /** Policy evaluation result for the current changeset. */
    private _policyResult?: PolicyResult;

    constructor(
        extensionUri: vscode.Uri,
        context: vscode.ExtensionContext,
        controller: ConductorController,
    ) {
        this._extensionUri = extensionUri;
        this._context = context;
        this._controller = controller;
        // Load auto-apply state from global state
        this._autoApplyEnabled = context.globalState.get<boolean>('autoApplyEnabled', false);

        // Push state updates to WebView whenever FSM state changes
        this._controller.onStateChange((_prev, next) => {
            this._sendConductorState(next);
            // Re-fetch providers when backend becomes reachable
            if (next === ConductorState.ReadyToHost) {
                this._fetchEnabledSSOProviders();
            }
        });
    }

    public resolveWebviewView(
        webviewView: vscode.WebviewView,
        _context: vscode.WebviewViewResolveContext,
        _token: vscode.CancellationToken
    ): void {
        this._view = webviewView;

        webviewView.webview.options = {
            enableScripts: true,
            localResourceRoots: [vscode.Uri.joinPath(this._extensionUri, 'media')]
        };

        webviewView.webview.html = this._getHtmlContent(webviewView.webview);

        // Fetch enabled SSO providers from backend and push to WebView
        this._fetchEnabledSSOProviders();

        // Handle messages from the webview
        webviewView.webview.onDidReceiveMessage(async message => {
            switch (message.command) {
                case 'alert':
                    vscode.window.showInformationMessage(message.text);
                    return;
                case 'getPermissions':
                    this._sendPermissions();
                    return;
                case 'generateChanges':
                    await this._handleGenerateChanges(message.filePath);
                    return;
                case 'applyChanges':
                    await this._handleApplyChanges(message.changeSet);
                    return;
                case 'viewDiff':
                    await getDiffPreviewService().showChangeSetDiff(message.changeSet);
                    return;
                case 'discardChanges':
                    vscode.window.showInformationMessage('Changes discarded');
                    return;
                case 'setAutoApply':
                    this._autoApplyEnabled = message.enabled;
                    this._context.globalState.update('autoApplyEnabled', message.enabled);
                    vscode.window.showInformationMessage(
                        message.enabled ? '🔄 Auto Apply enabled' : '⏸️ Auto Apply disabled'
                    );
                    return;
                case 'getAutoApplyState':
                    this._sendAutoApplyState();
                    return;
                case 'confirmEndChat':
                    // Show confirmation dialog and send result back to WebView
                    const confirmed = await vscode.window.showWarningMessage(
                        'Are you sure you want to end this chat session? This will disconnect all participants and clear the chat history.',
                        { modal: true },
                        'End Chat'
                    );
                    if (confirmed === 'End Chat') {
                        this._view?.webview.postMessage({ command: 'endChatConfirmed' });
                    }
                    return;
                case 'sessionEnded':
                    // Transition FSM back to ReadyToHost so the UI returns to start page
                    try {
                        const currentState = this._controller.getState();
                        if (currentState === 'Hosting') {
                            this._controller.stopHosting();
                        } else if (currentState === 'Joined') {
                            this._controller.leaveSession();
                        }
                    } catch (e) {
                        console.warn('[Conductor] FSM transition on sessionEnded failed:', e);
                    }
                    // Close Live Share session if active
                    this._closeLiveShare();
                    // Reset session state and generate new roomId
                    getSessionService().resetSession();
                    vscode.window.showInformationMessage('Chat session has ended.');
                    // Send updated state to WebView (FSM onStateChange may have already
                    // fired, but the session data needs refreshing too)
                    this._sendConductorState(this._controller.getState());
                    return;

                // ----- Conductor FSM commands -----

                case 'startSession':
                    this._handleStartSession();
                    return;
                case 'stopSession':
                    this._handleStopSession();
                    return;
                case 'retryConnection':
                    this._handleRetryConnection();
                    return;
                case 'getConductorState':
                    this._sendConductorState(this._controller.getState());
                    return;
                case 'copyInviteLink':
                    this._handleCopyInviteLink();
                    return;
                case 'joinSession':
                    this._handleJoinSession(message.inviteUrl);
                    return;
                case 'leaveSession':
                    this._handleLeaveSession();
                    return;
                case 'uploadFile':
                    this._handleUploadFile(message);
                    return;
                case 'checkDuplicateFile':
                    this._handleCheckDuplicateFile(message);
                    return;
                case 'downloadFile':
                    this._handleDownloadFile(message);
                    return;
                case 'getCodeSnippet':
                    this._handleGetCodeSnippet();
                    return;
                case 'navigateToCode':
                    this._handleNavigateToCode(message);
                    return;
                case 'getAiStatus':
                    console.log('[Conductor] Received getAiStatus message from WebView');
                    this._handleGetAiStatus();
                    return;
                case 'setAiModel':
                    console.log('[Conductor] Received setAiModel message from WebView:', message.modelId);
                    this._handleSetAiModel(message.modelId);
                    return;
                case 'summarize':
                    this._handleSummarizeAndPost(message.messages);
                    return;
                case 'generateCodePrompt':
                    this._handleGenerateCodePrompt(message.decisionSummary);
                    return;
                case 'generateCodePromptAndPost':
                    this._handleGenerateCodePromptAndPost(message.decisionSummary, message.roomId);
                    return;
                case 'generateCodePromptFromItemsAndPost':
                    this._handleGenerateCodePromptFromItemsAndPost(message.items, message.topic, message.roomId);
                    return;
                case 'getRoomSettings':
                    this._handleGetRoomSettings(message.roomId);
                    return;
                case 'getStyleTemplates':
                    this._handleGetStyleTemplates();
                    return;
                case 'saveRoomSettings':
                    this._handleSaveRoomSettings(message.roomId, message.codeStyle, message.outputMode);
                    return;
                case 'ssoLogin':
                    this._handleSSOLogin(message.provider || 'aws');
                    return;
                case 'ssoCancel':
                    this._handleSSOCancel();
                    this._view?.webview.postMessage({
                        command: 'ssoLoginResult',
                        error: 'Cancelled by user',
                    });
                    return;
                case 'ssoClearCache':
                    this._context.globalState.update('conductor.ssoIdentity', undefined);
                    this._view?.webview.postMessage({ command: 'ssoCacheCleared' });
                    console.log('[Conductor] SSO identity cache cleared');
                    return;
            }
        });

        // Listen for configuration changes to update permissions
        vscode.workspace.onDidChangeConfiguration(e => {
            if (e.affectsConfiguration('aiCollab.role')) {
                const newRole = getPermissionsService().getRole();
                console.log('aiCollab.role changed to:', newRole);
                vscode.window.showInformationMessage(`Role changed to: ${newRole}`);
                this._sendPermissions();
            }
        });

        // Note: We no longer auto-start Live Share here.
        // User must explicitly click "Start Session" to begin hosting.
    }

    /**
     * Send current permissions to the WebView.
     * Includes sessionRole based on FSM state (host/guest/none).
     */
    private _sendPermissions(): void {
        if (this._view) {
            const permissions = getPermissionsService().getPermissionsForWebView();
            const currentState = this._controller.getState();

            // Determine session role based on FSM state
            let sessionRole: 'host' | 'guest' | 'none' = 'none';
            if (currentState === ConductorState.Hosting) {
                sessionRole = 'host';
            } else if (currentState === ConductorState.Joined) {
                sessionRole = 'guest';
            }

            this._view.webview.postMessage({
                command: 'updatePermissions',
                permissions: {
                    ...permissions,
                    sessionRole
                }
            });
        }
    }

    /**
     * Send auto-apply state to the WebView.
     */
    private _sendAutoApplyState(): void {
        if (this._view) {
            this._view.webview.postMessage({
                command: 'autoApplyState',
                enabled: this._autoApplyEnabled
            });
        }
    }

    // ----- Conductor FSM helpers ----------------------------------------

    /**
     * Handle "Start Session" command from WebView.
     * 1. Checks if Live Share is already active (prompts user to close it)
     * 2. Runs health check if needed
     * 3. Resets session (new roomId)
     * 4. Transitions FSM to Hosting
     * 5. Starts Live Share and generates invite link
     */
    private async _handleStartSession(): Promise<void> {
        try {
            // Check if Live Share is already active
            const liveShareStatus = await this._checkLiveShareStatus();
            if (liveShareStatus.isActive) {
                const roleStr = liveShareStatus.role === vsls.Role.Host ? 'hosting' : 'in';
                const choice = await vscode.window.showWarningMessage(
                    `You are currently ${roleStr} a Live Share session. Please end it before starting a new Conductor session.`,
                    'End Live Share',
                    'Cancel'
                );

                if (choice === 'End Live Share') {
                    const liveShareApi = await vsls.getApi();
                    if (liveShareApi) {
                        await liveShareApi.end();
                        // Wait a moment for Live Share to fully close
                        await new Promise(resolve => setTimeout(resolve, 1000));
                    }
                } else {
                    // User cancelled
                    return;
                }
            }

            const currentState = this._controller.getState();

            // If in Idle or BackendDisconnected, run health check first
            if (
                currentState === ConductorState.Idle ||
                currentState === ConductorState.BackendDisconnected
            ) {
                const afterHealth = await this._controller.start();
                if (afterHealth !== ConductorState.ReadyToHost) {
                    // Health check failed — state already moved to BackendDisconnected
                    return;
                }
            }

            // Ensure ngrok URL is detected BEFORE sending session to WebView
            // This is critical for WebSocket connection to use the correct URL
            if (!getSessionService().getNgrokUrl()) {
                console.log('[Conductor] Detecting ngrok URL before starting session...');
                await getSessionService().detectNgrokUrl();
            }

            const roomId = this._controller.startHosting();
            console.log(`[Conductor] Hosting started, roomId=${roomId}`);
            // Send the fresh session state so the WebView can connect WebSocket
            // At this point, backendUrl will include ngrok URL if available
            this._sendSessionAndState();

            // Now start Live Share and generate invite link
            await this._startLiveShareAndGenerateInvite();
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.warn('[Conductor] startSession failed:', msg);
            vscode.window.showWarningMessage(`Cannot start session: ${msg}`);
        }
    }

    /**
     * Check if Live Share is currently active.
     * @returns Object containing isActive flag and role (Host/Guest/None)
     */
    private async _checkLiveShareStatus(): Promise<{ isActive: boolean; role: vsls.Role }> {
        try {
            const liveShareApi = await vsls.getApi();
            if (!liveShareApi) {
                // Live Share extension not installed or not activated
                return { isActive: false, role: vsls.Role.None };
            }

            const session = liveShareApi.session;
            const isActive = session.id !== null;
            const role = session.role;

            console.log(`[Conductor] Live Share status: isActive=${isActive}, role=${vsls.Role[role]}`);
            return { isActive, role };
        } catch (error) {
            console.warn('[Conductor] Failed to check Live Share status:', error);
            return { isActive: false, role: vsls.Role.None };
        }
    }

    /**
     * Close the active Live Share session, if any.
     * Called when the chat session ends so participants are fully disconnected.
     */
    private async _closeLiveShare(): Promise<void> {
        try {
            const liveShareApi = await vsls.getApi();
            if (!liveShareApi || liveShareApi.session.id === null) {
                return; // not active, nothing to close
            }
            console.log('[Conductor] Closing Live Share session...');
            await liveShareApi.end();
            console.log('[Conductor] Live Share session closed');
        } catch (error) {
            console.warn('[Conductor] Failed to close Live Share:', error);
        }
    }

    /**
     * Start Live Share session and generate invite link.
     * Called when user clicks "Start Session".
     * Note: ngrok URL detection is done in _handleStartSession before this is called.
     */
    private async _startLiveShareAndGenerateInvite(): Promise<void> {
        try {
            console.log('[Conductor] Starting Live Share session...');

            // Start Live Share session
            const liveShareResult = await vscode.commands.executeCommand('liveshare.start');

            let liveShareUrl: string | null = null;

            // Check if the command returned a URL directly
            if (liveShareResult && typeof liveShareResult === 'string') {
                liveShareUrl = liveShareResult;
                console.log('[Conductor] Live Share returned URL directly:', liveShareUrl);
            } else {
                // Live Share might have copied the URL to clipboard instead
                await new Promise(resolve => setTimeout(resolve, 500));
                const clipboardContent = await vscode.env.clipboard.readText();

                // Validate that it's a direct Live Share URL, not our invite URL
                // Live Share URLs look like: https://prod.liveshare.vsengsaas.visualstudio.com/join?XXXXX
                // Our invite URLs contain /invite? which we should NOT accept
                if (
                    clipboardContent &&
                    clipboardContent.includes('liveshare.vsengsaas.visualstudio.com') &&
                    !clipboardContent.includes('/invite?')
                ) {
                    liveShareUrl = clipboardContent;
                    console.log('[Conductor] Got Live Share URL from clipboard:', liveShareUrl);
                } else {
                    console.log('[Conductor] Could not get Live Share URL from clipboard. Content:', clipboardContent?.substring(0, 100));
                }
            }

            if (liveShareUrl) {
                // Store in session
                getSessionService().setLiveShareUrl(liveShareUrl);

                // Generate invite URL
                const inviteUrl = getSessionService().getInviteUrl();

                if (inviteUrl) {
                    // Log to output channel
                    outputChannel.appendLine('='.repeat(80));
                    outputChannel.appendLine('🎉 Conductor Session Started!');
                    outputChannel.appendLine('='.repeat(80));
                    outputChannel.appendLine('');
                    outputChannel.appendLine('📋 Share this link with your team:');
                    outputChannel.appendLine(inviteUrl);
                    outputChannel.appendLine('');
                    outputChannel.appendLine('📌 Room ID: ' + getSessionService().getRoomId());
                    outputChannel.appendLine('🔗 Live Share URL: ' + liveShareUrl);
                    outputChannel.appendLine('='.repeat(80));
                    outputChannel.show();

                    // Copy invite URL to clipboard
                    await vscode.env.clipboard.writeText(inviteUrl);
                    vscode.window.showInformationMessage('📋 Conductor invite link copied to clipboard!');
                }
            } else {
                vscode.window.showWarningMessage('Could not get Live Share URL. You can still use "Copy Invite Link" later.');
            }
        } catch (error) {
            console.error('[Conductor] Failed to start Live Share:', error);
            vscode.window.showWarningMessage('Failed to start Live Share. Make sure Live Share extension is installed.');
        }
    }

    /**
     * Handle "Retry Connection" command from WebView.
     * Re-runs the health check from BackendDisconnected (or Idle).
     */
    private async _handleRetryConnection(): Promise<void> {
        try {
            await this._controller.start();
            console.log('[Conductor] Retry connection → state:', this._controller.getState());
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.warn('[Conductor] retryConnection failed:', msg);
        }
    }

    /**
     * Handle "Stop Session" command from WebView.
     * Transitions FSM back to ReadyToHost.
     */
    private _handleStopSession(): void {
        try {
            this._controller.stopHosting();
            console.log('[Conductor] Hosting stopped');
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.warn('[Conductor] stopSession failed:', msg);
            vscode.window.showWarningMessage(`Cannot stop session: ${msg}`);
        }
    }

    /**
     * Send the current Conductor FSM state to the WebView.
     * Includes SSO identity so the WebView can restore it after re-creation.
     */
    private _sendConductorState(state: ConductorState): void {
        if (this._view) {
            this._view.webview.postMessage({
                command: 'conductorStateChanged',
                state,
                session: getSessionService().getSessionStateForWebView(),
                ssoIdentity: this._getValidSSOIdentity(),
                ssoProvider: this._getStoredSSOProvider(),
            });
        }
    }

    /**
     * Extract valid (non-expired) SSO identity from globalState, or null.
     */
    private _getValidSSOIdentity(): Record<string, unknown> | null {
        const stored = this._context.globalState.get('conductor.ssoIdentity');
        return getValidIdentity(stored);
    }

    /**
     * Extract the SSO provider from the stored identity wrapper, or undefined.
     */
    private _getStoredSSOProvider(): SSOProvider | undefined {
        const stored = this._context.globalState.get('conductor.ssoIdentity');
        return getStoredProvider(stored);
    }

    /**
     * Send both session state and conductor state to the WebView.
     * Also sends updated permissions (which include sessionRole based on FSM state).
     * Used after startHosting/joinSucceeded to give the WebView everything it needs.
     */
    private _sendSessionAndState(): void {
        this._sendConductorState(this._controller.getState());
        // Also send permissions so the role badge updates (host/guest)
        this._sendPermissions();
    }

    /**
     * Copy the invite link to the clipboard and show in the output channel.
     */
    private _handleCopyInviteLink(): void {
        const inviteUrl = getSessionService().getInviteUrl();
        if (inviteUrl) {
            vscode.env.clipboard.writeText(inviteUrl);
            vscode.window.showInformationMessage('📋 Invite link copied to clipboard!');
            outputChannel.appendLine(`📋 Invite link: ${inviteUrl}`);
        } else {
            // Build a simple invite URL with just roomId + backendUrl (no Live Share yet)
            const roomId = getSessionService().getRoomId();
            const backendUrl = getSessionService().getBackendUrl();
            const simpleUrl = `${backendUrl}/invite?roomId=${roomId}`;
            vscode.env.clipboard.writeText(simpleUrl);
            vscode.window.showInformationMessage('📋 Invite link copied (no Live Share URL yet)');
            outputChannel.appendLine(`📋 Invite link (no Live Share): ${simpleUrl}`);
        }
    }

    /**
     * Handle "Join Session" command from WebView.
     * Parses the invite URL, configures the session as a guest,
     * opens Live Share if a URL is present, and transitions FSM.
     */
    private async _handleJoinSession(inviteUrl: string): Promise<void> {
        try {
            const currentState = this._controller.getState();

            // If in Idle, run health check first to determine state
            // If in BackendDisconnected, we can still join (no local backend needed)
            if (currentState === ConductorState.Idle) {
                await this._controller.start();
                // Regardless of health check result (ReadyToHost or BackendDisHost),
                // we can proceed with joining since JOIN_SESSION is valid in both states
            }

            // Parse and transition to Joining
            // This works from both ReadyToHost and BackendDisconnected states
            const parsed = this._controller.startJoining(inviteUrl);
            console.log(
                `[Conductor] Joining session: roomId=${parsed.roomId}, ` +
                `backendUrl=${parsed.backendUrl}`,
            );

            // Configure session service as guest
            getSessionService().joinAsGuest(
                parsed.roomId,
                parsed.backendUrl,
                parsed.liveShareUrl,
            );

            // Transition to Joined FIRST so user can start chatting immediately
            // Don't wait for Live Share - it can connect in the background
            this._controller.joinSucceeded();
            console.log('[Conductor] Joined session successfully');
            this._sendSessionAndState();

            // Offer to join Live Share (optional - user can decline)
            // This is non-blocking - user can chat while deciding
            if (parsed.liveShareUrl) {
                // Ask user if they want to join Live Share
                // This is async and non-blocking
                vscode.window.showInformationMessage(
                    '🔗 This session has Live Share. Join to collaborate on code?',
                    'Join Live Share',
                    'Chat Only'
                ).then(async (choice) => {
                    if (choice === 'Join Live Share') {
                        try {
                            console.log('[Conductor] User chose to join Live Share...');
                            await vscode.commands.executeCommand(
                                'liveshare.join',
                                vscode.Uri.parse(parsed.liveShareUrl!),
                            );
                            console.log('[Conductor] Live Share joined successfully');
                        } catch (lsError) {
                            console.warn('[Conductor] Live Share join failed:', lsError);
                            vscode.window.showWarningMessage(
                                'Could not join Live Share. You can still use chat.'
                            );
                        }
                    } else {
                        console.log('[Conductor] User chose chat only, skipping Live Share');
                    }
                });
            }
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.warn('[Conductor] joinSession failed:', msg);
            vscode.window.showWarningMessage(`Cannot join session: ${msg}`);

            // If we're stuck in Joining (parse succeeded but later step failed),
            // transition back to ReadyToHost.
            if (this._controller.getState() === ConductorState.Joining) {
                this._controller.joinFailed();
            }
        }
    }

    /**
     * Handle "Leave Session" command from WebView.
     * Transitions FSM from Joined back to ReadyToHost.
     */
    private _handleLeaveSession(): void {
        try {
            this._controller.leaveSession();
            console.log('[Conductor] Left session');
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.warn('[Conductor] leaveSession failed:', msg);
            vscode.window.showWarningMessage(`Cannot leave session: ${msg}`);
        }
    }

    /**
     * Normalize backend URL for Node.js fetch compatibility.
     * Replaces 'localhost' with '127.0.0.1' to avoid IPv6 resolution issues
     * where Node.js resolves localhost to ::1 but the backend only listens on 127.0.0.1.
     */
    private _normalizeUrl(backendUrl: string): string {
        return backendUrl.replace('://localhost', '://127.0.0.1');
    }

    /**
     * Handle file upload from WebView.
     * WebView cannot make fetch requests due to CORS restrictions,
     * so we proxy the upload through the extension host.
     */
    private async _handleCheckDuplicateFile(message: {
        backendUrl: string;
        roomId: string;
        fileName: string;
    }): Promise<void> {
        try {
            const baseUrl = this._normalizeUrl(message.backendUrl);
            const url = `${baseUrl}/files/check-duplicate/${message.roomId}?filename=${encodeURIComponent(message.fileName)}`;
            console.log('[Conductor] Checking duplicate:', message.fileName);

            // Retry up to 3 times for transient connection failures
            // (same stale keep-alive issue as uploads)
            let response: Response | undefined;
            for (let attempt = 1; attempt <= 3; attempt++) {
                try {
                    response = await fetch(url);
                    break;
                } catch (e) {
                    console.warn(`[Conductor] Duplicate check attempt ${attempt}/3 failed:`, e instanceof Error ? e.message : e);
                    if (attempt < 3) {
                        await new Promise(r => setTimeout(r, 300 * attempt));
                    }
                }
            }

            if (response?.ok) {
                const data = await response.json() as { duplicate: boolean; existing_file: unknown };
                console.log('[Conductor] Duplicate check result:', data.duplicate);
                this._view?.webview.postMessage({
                    command: 'checkDuplicateFileResult',
                    duplicate: data.duplicate,
                    existing_file: data.existing_file,
                });
            } else {
                console.warn('[Conductor] Duplicate check failed, status:', response?.status ?? 'no response');
                this._view?.webview.postMessage({
                    command: 'checkDuplicateFileResult',
                    duplicate: false,
                    existing_file: null,
                });
            }
        } catch (error) {
            console.error('[Conductor] Duplicate check error:', error);
            this._view?.webview.postMessage({
                command: 'checkDuplicateFileResult',
                duplicate: false,
                existing_file: null,
            });
        }
    }

    private async _handleUploadFile(message: {
        backendUrl: string;
        roomId: string;
        userId: string;
        displayName: string;
        fileData: string;  // Base64 encoded file data
        fileName: string;
        mimeType: string;
        caption?: string;
    }): Promise<void> {
        try {
            console.log('[Conductor] Uploading file:', message.fileName);

            // Decode base64 file data
            const fileBuffer = Buffer.from(message.fileData, 'base64');
            console.log('[Conductor] File buffer size:', fileBuffer.length, 'bytes');

            // Build FormData using Node.js built-in API (available in Node 18+)
            const formData = new FormData();
            const fileBlob = new Blob([fileBuffer], { type: message.mimeType });
            formData.append('file', fileBlob, message.fileName);
            formData.append('user_id', message.userId);
            formData.append('display_name', message.displayName);
            if (message.caption) {
                formData.append('caption', message.caption);
            }

            const uploadUrl = `${this._normalizeUrl(message.backendUrl)}/files/upload/${message.roomId}`;
            console.log('[Conductor] Upload URL:', uploadUrl);

            // Retry up to 3 times for transient connection failures.
            // Node.js undici can fail with "fetch failed" when reusing a stale
            // keep-alive connection that the server has already closed.
            const maxAttempts = 3;
            let lastError: Error | undefined;
            let response: Response | undefined;

            for (let attempt = 1; attempt <= maxAttempts; attempt++) {
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 60000);
                try {
                    response = await fetch(uploadUrl, {
                        method: 'POST',
                        body: formData,
                        signal: controller.signal
                    });
                    break; // success, exit retry loop
                } catch (e) {
                    lastError = e instanceof Error ? e : new Error(String(e));
                    const isAbort = lastError.name === 'AbortError';
                    console.warn(
                        `[Conductor] Upload attempt ${attempt}/${maxAttempts} failed:`,
                        lastError.message,
                        isAbort ? '(timeout)' : ''
                    );
                    // Don't retry on explicit timeout/abort
                    if (isAbort) { break; }
                    if (attempt < maxAttempts) {
                        await new Promise(r => setTimeout(r, 500 * attempt));
                    }
                } finally {
                    clearTimeout(timeoutId);
                }
            }

            if (!response) {
                throw lastError ?? new Error('Upload failed after retries');
            }

            console.log('[Conductor] Upload response status:', response.status);

            if (!response.ok) {
                const responseText = await response.text();
                console.error('[Conductor] Upload error response:', responseText);
                let errorDetail = `HTTP ${response.status}`;
                try {
                    const errorData = JSON.parse(responseText) as { detail?: string };
                    errorDetail = errorData.detail || errorDetail;
                } catch {
                    errorDetail = responseText || errorDetail;
                }
                throw new Error(errorDetail);
            }

            const result = await response.json();
            console.log('[Conductor] File uploaded successfully:', result);

            // Send result back to WebView
            this._view?.webview.postMessage({
                command: 'uploadFileResult',
                success: true,
                result: result
            });

        } catch (error) {
            let msg = error instanceof Error ? error.message : String(error);
            if (error instanceof Error && error.name === 'AbortError') {
                msg = 'Upload timed out after 60 seconds';
            }
            // Include full error cause chain for debugging
            if (error instanceof Error && 'cause' in error && error.cause) {
                const cause = error.cause;
                const causeMsg = cause instanceof Error
                    ? (cause.message || cause.name || cause.constructor.name)
                    : String(cause);
                if (causeMsg) {
                    msg = `${msg} (${causeMsg})`;
                }
            }
            console.error('[Conductor] File upload failed:', msg);
            console.error('[Conductor] Error details:', error);

            // Send error back to WebView
            this._view?.webview.postMessage({
                command: 'uploadFileResult',
                success: false,
                error: msg
            });
        }
    }

    /**
     * Handle file download request from WebView.
     * Downloads file from backend and prompts user to save locally.
     */
    private async _handleDownloadFile(message: {
        fileId: string;
        fileName: string;
        downloadUrl: string;
    }): Promise<void> {
        try {
            console.log('[Conductor] Downloading file:', message.fileName);

            // Prompt user for save location
            const uri = await vscode.window.showSaveDialog({
                defaultUri: vscode.Uri.file(message.fileName),
                filters: {
                    'All Files': ['*']
                }
            });

            if (!uri) {
                console.log('[Conductor] Download cancelled by user');
                return;
            }

            // Fetch file from backend
            const response = await fetch(this._normalizeUrl(message.downloadUrl));
            if (!response.ok) {
                throw new Error(`Download failed: HTTP ${response.status}`);
            }

            const arrayBuffer = await response.arrayBuffer();
            const buffer = Buffer.from(arrayBuffer);

            // Write to file
            await vscode.workspace.fs.writeFile(uri, buffer);

            vscode.window.showInformationMessage(`File saved: ${uri.fsPath}`);
            console.log('[Conductor] File saved to:', uri.fsPath);

        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] File download failed:', msg);
            vscode.window.showErrorMessage(`Download failed: ${msg}`);
        }
    }

    /**
     * Handle request to get code snippet from the active editor.
     * Gets the current selection and sends it back to the WebView.
     */
    private _handleGetCodeSnippet(): void {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            this._view?.webview.postMessage({
                command: 'codeSnippet',
                error: 'No active editor. Please open a file and select some code.'
            });
            return;
        }

        const selection = editor.selection;
        const document = editor.document;

        if (selection.isEmpty) {
            this._view?.webview.postMessage({
                command: 'codeSnippet',
                error: 'No code selected. Please select some code in the editor first.'
            });
            return;
        }

        const selectedText = document.getText(selection);

        // Get relative path if in workspace
        const workspaceFolder = vscode.workspace.getWorkspaceFolder(document.uri);
        const relativePath = workspaceFolder
            ? vscode.workspace.asRelativePath(document.uri)
            : document.fileName;

        this._view?.webview.postMessage({
            command: 'codeSnippet',
            filename: document.fileName.split('/').pop() || document.fileName.split('\\').pop() || 'file',
            relativePath: relativePath,
            language: document.languageId,
            startLine: selection.start.line + 1,  // Convert to 1-based
            endLine: selection.end.line + 1,      // Convert to 1-based
            code: selectedText
        });

        console.log('[Conductor] Code snippet sent:', {
            filename: relativePath,
            language: document.languageId,
            lines: `${selection.start.line + 1}-${selection.end.line + 1}`,
            codeLength: selectedText.length
        });
    }

    /**
     * Handle navigation to a code location from a code snippet in chat.
     * Opens the file and navigates to the specified line range.
     */
    private async _handleNavigateToCode(message: {
        relativePath: string;
        startLine: number;
        endLine: number;
    }): Promise<void> {
        try {
            const { relativePath, startLine, endLine } = message;
            console.log('[Conductor] Navigating to code:', { relativePath, startLine, endLine });

            // Find the file in the workspace
            const workspaceFolders = vscode.workspace.workspaceFolders;
            if (!workspaceFolders || workspaceFolders.length === 0) {
                vscode.window.showWarningMessage('No workspace folder open. Cannot navigate to code.');
                return;
            }

            // Try to find the file
            let fileUri: vscode.Uri | undefined;

            // First, try as a relative path from workspace root
            for (const folder of workspaceFolders) {
                const possibleUri = vscode.Uri.joinPath(folder.uri, relativePath);
                try {
                    await vscode.workspace.fs.stat(possibleUri);
                    fileUri = possibleUri;
                    break;
                } catch {
                    // File not found in this folder, try next
                }
            }

            // If not found, try to find by filename using workspace search
            if (!fileUri) {
                const filename = relativePath.split('/').pop() || relativePath.split('\\').pop() || relativePath;
                const files = await vscode.workspace.findFiles(`**/${filename}`, '**/node_modules/**', 5);
                if (files.length > 0) {
                    // If multiple matches, prefer one that contains the relative path
                    fileUri = files.find(f => f.fsPath.includes(relativePath.replace(/\\/g, '/'))) || files[0];
                }
            }

            if (!fileUri) {
                vscode.window.showWarningMessage(`File not found: ${relativePath}`);
                return;
            }

            // Open the document
            const document = await vscode.workspace.openTextDocument(fileUri);
            const editor = await vscode.window.showTextDocument(document, {
                preview: false,
                preserveFocus: false
            });

            // Navigate to the line range (convert from 1-based to 0-based)
            const startLineIndex = Math.max(0, startLine - 1);
            const endLineIndex = Math.max(0, endLine - 1);

            // Create a selection that highlights the code range
            const startPosition = new vscode.Position(startLineIndex, 0);
            const endPosition = new vscode.Position(endLineIndex, document.lineAt(endLineIndex).text.length);
            const selection = new vscode.Selection(startPosition, endPosition);

            editor.selection = selection;
            editor.revealRange(selection, vscode.TextEditorRevealType.InCenter);

            console.log('[Conductor] Navigated to:', fileUri.fsPath, `lines ${startLine}-${endLine}`);
        } catch (error) {
            console.error('[Conductor] Navigation error:', error);
            vscode.window.showErrorMessage(`Failed to navigate to code: ${error instanceof Error ? error.message : 'Unknown error'}`);
        }
    }

    /**
     * Handle request to get AI status from the backend.
     * Fetches /ai/status and sends the result to the WebView.
     */
    private async _handleGetAiStatus(): Promise<void> {
        try {
            console.log('[Conductor] Fetching AI status...');
            const response = await fetch(`${getBackendUrl()}/ai/status`);

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] AI status request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'aiStatus',
                    data: { error: `Failed to fetch AI status: ${response.status}` }
                });
                return;
            }

            const data = await response.json() as {
                summary_enabled: boolean;
                active_provider: string | null;
                active_model: string | null;
                providers: Array<{ name: string; enabled: boolean; configured: boolean; healthy: boolean }>;
                models: Array<{ id: string; provider: string; display_name: string; available: boolean }>;
                default_model: string;
            };
            console.log('[Conductor] AI status received:', data);

            this._view?.webview.postMessage({
                command: 'aiStatus',
                data: data
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to get AI status:', msg);
            this._view?.webview.postMessage({
                command: 'aiStatus',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle request to set the active AI model.
     * Posts to /ai/model and sends the result to the WebView.
     */
    private async _handleSetAiModel(modelId: string): Promise<void> {
        try {
            console.log('[Conductor] Setting AI model to:', modelId);
            const response = await fetch(`${getBackendUrl()}/ai/model`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ model_id: modelId })
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Set AI model request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'setAiModelResult',
                    data: { error: `Failed to set model: ${response.status}` }
                });
                // Refresh status to restore UI to current state
                this._handleGetAiStatus();
                return;
            }

            const data = await response.json() as {
                success: boolean;
                active_model: string | null;
                message: string;
            };
            console.log('[Conductor] AI model set successfully:', data);

            this._view?.webview.postMessage({
                command: 'setAiModelResult',
                data: data
            });

            // Refresh full AI status to update all UI elements
            this._handleGetAiStatus();
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to set AI model:', msg);
            this._view?.webview.postMessage({
                command: 'setAiModelResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
            // Refresh status to restore UI to current state
            this._handleGetAiStatus();
        }
    }

    /**
     * Handle request to summarize chat messages and post as AI message.
     * Sends messages to /ai/summarize, then posts the summary to chat via /chat/{room_id}/ai-message.
     *
     * @param messages Array of message objects with role, text, and timestamp
     */
    private async _handleSummarizeAndPost(messages: Array<{ role: 'host' | 'engineer'; text: string; timestamp: number }>): Promise<void> {
        try {
            console.log('[Conductor] Requesting AI summary for', messages.length, 'messages');
            const response = await fetch(`${getBackendUrl()}/ai/summarize`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ messages })
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Summarize request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'summarizeResult',
                    data: { error: `Failed to summarize: ${response.status} - ${errorText}` }
                });
                return;
            }

            // Structured DecisionSummaryResponse from backend
            const summaryData = await response.json() as {
                type: 'decision_summary';
                topic: string;
                problem_statement: string;
                proposed_solution: string;
                requires_code_change: boolean;
                affected_components: string[];
                risk_level: 'low' | 'medium' | 'high';
                next_steps: string[];
                code_relevant_items?: Array<{
                    id: string;
                    type: string;
                    title: string;
                    problem: string;
                    proposed_change: string;
                    targets: string[];
                    risk_level: string;
                }>;
            };
            console.log('[Conductor] Summary received:', summaryData.topic);

            // Post the summary as an AI message to the chat
            const roomId = getSessionService().getRoomId();
            const contentText = `Topic: ${summaryData.topic}\n\nProblem: ${summaryData.problem_statement}\n\nSolution: ${summaryData.proposed_solution}`;

            const postParams = new URLSearchParams({
                message_type: 'ai_summary',
                model_name: 'claude_bedrock',  // TODO: Get from active provider
                content: contentText,
                ai_data: JSON.stringify(summaryData)
            });

            const postResponse = await fetch(`${getBackendUrl()}/chat/${roomId}/ai-message?${postParams.toString()}`, {
                method: 'POST'
            });

            if (!postResponse.ok) {
                const errorText = await postResponse.text();
                console.warn('[Conductor] Failed to post AI summary message:', postResponse.status, errorText);
                this._view?.webview.postMessage({
                    command: 'summarizeResult',
                    data: { error: `Failed to post summary to chat: ${postResponse.status}` }
                });
                return;
            }

            console.log('[Conductor] AI summary posted to chat successfully');

            // Send summarize result to close the modal
            this._view?.webview.postMessage({
                command: 'summarizeResult',
                data: summaryData
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to summarize:', msg);
            this._view?.webview.postMessage({
                command: 'summarizeResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle code prompt generation request.
     * Calls POST /ai/code-prompt with the decision summary.
     * @param decisionSummary The decision summary from the summarize result
     */
    private async _handleGenerateCodePrompt(decisionSummary: {
        type: 'decision_summary';
        topic: string;
        problem_statement: string;
        proposed_solution: string;
        requires_code_change: boolean;
        affected_components: string[];
        risk_level: 'low' | 'medium' | 'high';
        next_steps: string[];
    }): Promise<void> {
        try {
            console.log('[Conductor] Requesting code prompt for:', decisionSummary.topic);
            const roomId = getSessionService().getRoomId();
            const detectedLanguages = await detectWorkspaceLanguages();
            const response = await fetch(`${getBackendUrl()}/ai/code-prompt`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    decision_summary: decisionSummary,
                    room_id: roomId,
                    detected_languages: detectedLanguages,
                })
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Code prompt request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'codePromptResult',
                    data: { error: `Failed to generate code prompt: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json() as { code_prompt: string };
            console.log('[Conductor] Code prompt generated successfully');

            this._view?.webview.postMessage({
                command: 'codePromptResult',
                data: data
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to generate code prompt:', msg);
            this._view?.webview.postMessage({
                command: 'codePromptResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle code prompt generation request and post as AI message.
     * Calls POST /ai/code-prompt, then posts the prompt to chat via /chat/{room_id}/ai-message.
     * @param decisionSummary The decision summary from the summarize result
     * @param roomId The room ID to post the message to
     */
    private async _handleGenerateCodePromptAndPost(decisionSummary: {
        topic: string;
        problem_statement: string;
        proposed_solution: string;
        requires_code_change: boolean;
        affected_components: string[];
        risk_level: 'low' | 'medium' | 'high';
        next_steps: string[];
    }, roomId: string): Promise<void> {
        try {
            console.log('[Conductor] Requesting code prompt for:', decisionSummary.topic);
            const detectedLanguages = await detectWorkspaceLanguages();
            const response = await fetch(`${getBackendUrl()}/ai/code-prompt`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    decision_summary: { ...decisionSummary, type: 'decision_summary' },
                    room_id: roomId,
                    detected_languages: detectedLanguages,
                })
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Code prompt request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'codePromptPostResult',
                    data: { error: `Failed to generate code prompt: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json() as { code_prompt: string };
            console.log('[Conductor] Code prompt generated successfully');

            // Post the code prompt as an AI message to the chat
            const postParams = new URLSearchParams({
                message_type: 'ai_code_prompt',
                model_name: 'claude_bedrock',  // TODO: Get from active provider
                content: data.code_prompt
            });

            const postResponse = await fetch(`${getBackendUrl()}/chat/${roomId}/ai-message?${postParams.toString()}`, {
                method: 'POST'
            });

            if (!postResponse.ok) {
                const errorText = await postResponse.text();
                console.warn('[Conductor] Failed to post AI code prompt message:', postResponse.status, errorText);
                this._view?.webview.postMessage({
                    command: 'codePromptPostResult',
                    data: { error: `Failed to post code prompt to chat: ${postResponse.status}` }
                });
                return;
            }

            console.log('[Conductor] AI code prompt posted to chat successfully');

            // Notify WebView that code prompt was posted successfully
            this._view?.webview.postMessage({
                command: 'codePromptPostResult',
                data: { success: true }
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to generate/post code prompt:', msg);
            this._view?.webview.postMessage({
                command: 'codePromptPostResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle code prompt generation from selected implementation items.
     * Calls POST /ai/code-prompt/items with the selected items, then posts
     * the result to chat as an AI code prompt message.
     *
     * @param items Array of selected code-relevant items
     * @param topic The summary topic for context
     * @param roomId The room ID to post the result to
     */
    /**
     * Read a short snippet (~30 lines) from a workspace file for context injection.
     * Returns null if the file cannot be found or read.
     */
    private async _readSnippetFromTarget(relativePath: string): Promise<{ file_path: string; snippet: string } | null> {
        try {
            // Skip paths that don't look like real files (no extension)
            const lastDot = relativePath.lastIndexOf('.');
            const lastSlash = Math.max(relativePath.lastIndexOf('/'), relativePath.lastIndexOf('\\'));
            if (lastDot <= 0 || lastDot < lastSlash) {
                return null;
            }

            // Try to find the file in workspace; fetch up to 2 to detect ambiguity.
            // If more than one match exists we can't tell which is correct, so skip
            // the snippet and let the agent locate the right file itself.
            const matches = await vscode.workspace.findFiles(`**/${relativePath}`, '**/node_modules/**', 2);
            if (matches.length !== 1) {
                return null;
            }

            const doc = await vscode.workspace.openTextDocument(matches[0]);
            const maxLines = Math.min(doc.lineCount, 30);
            const snippet = doc.getText(new vscode.Range(0, 0, maxLines, 0));
            if (!snippet.trim()) {
                return null;
            }

            return { file_path: relativePath, snippet };
        } catch {
            return null;
        }
    }

    private async _handleGenerateCodePromptFromItemsAndPost(
        items: Array<{
            id: string;
            type: string;
            title: string;
            problem: string;
            proposed_change: string;
            targets: string[];
            risk_level: string;
        }>,
        topic: string,
        roomId: string,
    ): Promise<void> {
        try {
            console.log('[Conductor] Requesting code prompt from', items.length, 'selected items');
            const detectedLanguages = await detectWorkspaceLanguages();

            // Collect context snippets from target files
            const uniqueTargets = [...new Set(items.flatMap(i => i.targets || []))];
            const snippetPromises = uniqueTargets.map(t => this._readSnippetFromTarget(t));
            const snippetResults = await Promise.all(snippetPromises);
            const contextSnippets = snippetResults.filter(
                (s): s is { file_path: string; snippet: string } => s !== null
            );

            if (contextSnippets.length > 0) {
                console.log('[Conductor] Collected', contextSnippets.length, 'context snippets from target files');
            }

            const response = await fetch(`${getBackendUrl()}/ai/code-prompt/items`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    items,
                    topic,
                    room_id: roomId,
                    detected_languages: detectedLanguages,
                    ...(contextSnippets.length > 0 ? { context_snippets: contextSnippets } : {}),
                })
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Items code prompt request failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'codePromptPostResult',
                    data: { error: `Failed to generate code prompt: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json() as { code_prompt: string };
            console.log('[Conductor] Items code prompt generated successfully');

            // Post the code prompt as an AI message to the chat
            const postParams = new URLSearchParams({
                message_type: 'ai_code_prompt',
                model_name: 'claude_bedrock',
                content: data.code_prompt
            });

            const postResponse = await fetch(`${getBackendUrl()}/chat/${roomId}/ai-message?${postParams.toString()}`, {
                method: 'POST'
            });

            if (!postResponse.ok) {
                const errorText = await postResponse.text();
                console.warn('[Conductor] Failed to post AI code prompt message:', postResponse.status, errorText);
                this._view?.webview.postMessage({
                    command: 'codePromptPostResult',
                    data: { error: `Failed to post code prompt to chat: ${postResponse.status}` }
                });
                return;
            }

            console.log('[Conductor] AI code prompt (from items) posted to chat successfully');

            this._view?.webview.postMessage({
                command: 'codePromptPostResult',
                data: { success: true }
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to generate/post items code prompt:', msg);
            this._view?.webview.postMessage({
                command: 'codePromptPostResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    // ----- Room Settings handlers ------------------------------------------

    /**
     * Handle get room settings request from WebView.
     * Calls GET /rooms/{roomId}/settings and forwards response.
     */
    private async _handleGetRoomSettings(roomId: string): Promise<void> {
        try {
            console.log('[Conductor] Fetching room settings for:', roomId);
            const response = await fetch(`${getBackendUrl()}/rooms/${roomId}/settings`);

            if (!response.ok) {
                const errorText = await response.text();
                this._view?.webview.postMessage({
                    command: 'roomSettings',
                    data: { error: `Failed to fetch settings: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json();
            this._view?.webview.postMessage({
                command: 'roomSettings',
                data: data
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            this._view?.webview.postMessage({
                command: 'roomSettings',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle get style templates request from WebView.
     * Calls GET /ai/style-templates and forwards response.
     */
    private async _handleGetStyleTemplates(): Promise<void> {
        try {
            console.log('[Conductor] Fetching style templates');
            const response = await fetch(`${getBackendUrl()}/ai/style-templates`);

            if (!response.ok) {
                const errorText = await response.text();
                this._view?.webview.postMessage({
                    command: 'styleTemplates',
                    data: { error: `Failed to fetch templates: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json();
            this._view?.webview.postMessage({
                command: 'styleTemplates',
                data: data
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            this._view?.webview.postMessage({
                command: 'styleTemplates',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    /**
     * Handle save room settings request from WebView.
     * Calls PUT /rooms/{roomId}/settings and forwards result.
     */
    private async _handleSaveRoomSettings(roomId: string, codeStyle: string, outputMode?: string): Promise<void> {
        try {
            console.log('[Conductor] Saving room settings for:', roomId);
            const body: Record<string, string> = { code_style: codeStyle };
            if (outputMode !== undefined) {
                body.output_mode = outputMode;
            }
            const response = await fetch(`${getBackendUrl()}/rooms/${roomId}/settings`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body)
            });

            if (!response.ok) {
                const errorText = await response.text();
                this._view?.webview.postMessage({
                    command: 'saveRoomSettingsResult',
                    data: { error: `Failed to save settings: ${response.status} - ${errorText}` }
                });
                return;
            }

            const data = await response.json();
            this._view?.webview.postMessage({
                command: 'saveRoomSettingsResult',
                data: data
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            this._view?.webview.postMessage({
                command: 'saveRoomSettingsResult',
                data: { error: `Cannot connect to backend: ${msg}` }
            });
        }
    }

    // ----- SSO handlers ---------------------------------------------------

    /**
     * Fetch enabled SSO providers from backend and send to WebView.
     * Called on WebView creation; result is cached and re-sent on state changes.
     */
    private async _fetchEnabledSSOProviders(): Promise<void> {
        try {
            const backendUrl = getBackendUrl();
            const resp = await fetch(`${backendUrl}/auth/providers`);
            if (resp.ok) {
                const data = await resp.json() as { aws: boolean; google: boolean };
                this._enabledSSOProviders = data;
                this._view?.webview.postMessage({
                    command: 'ssoProvidersUpdate',
                    providers: data,
                });
            }
        } catch {
            // Backend may not be reachable — leave defaults
        }
    }

    /**
     * Handle SSO login request from WebView.
     * Dispatches to the correct provider endpoint (AWS or Google).
     */
    private async _handleSSOLogin(provider: SSOProvider = 'aws'): Promise<void> {
        try {
            const backendUrl = getBackendUrl();

            // Provider-specific start endpoint
            const startUrl = provider === 'google'
                ? `${backendUrl}/auth/google/start`
                : `${backendUrl}/auth/sso/start`;

            const response = await fetch(startUrl, { method: 'POST' });

            if (!response.ok) {
                const errorData = await response.json().catch(() => ({})) as { detail?: string };
                const detail = errorData.detail || `HTTP ${response.status}`;
                this._view?.webview.postMessage({
                    command: 'ssoLoginResult',
                    error: detail,
                });
                return;
            }

            const data = await response.json() as {
                verification_uri_complete?: string;
                verification_url?: string;
                user_code: string;
                device_code: string;
                client_id?: string;
                client_secret?: string;
                expires_in: number;
                interval: number;
            };

            // Open browser to the verification URL (field name differs by provider)
            const verifyUrl = data.verification_uri_complete || data.verification_url;
            if (verifyUrl) {
                await vscode.env.openExternal(vscode.Uri.parse(verifyUrl));
            }

            // Send pending state to WebView with user code
            this._view?.webview.postMessage({
                command: 'ssoLoginPending',
                userCode: data.user_code,
                provider,
            });

            // Start polling
            this._ssoPolling = true;
            const pollInterval = Math.max(data.interval || 5, 5) * 1000;
            const expiresAt = Date.now() + (data.expires_in || 600) * 1000;

            // Provider-specific poll endpoint and body
            const pollUrl = provider === 'google'
                ? `${backendUrl}/auth/google/poll`
                : `${backendUrl}/auth/sso/poll`;

            const pollBody = provider === 'google'
                ? { device_code: data.device_code }
                : {
                    device_code: data.device_code,
                    client_id: data.client_id,
                    client_secret: data.client_secret,
                };

            this._ssoTimerId = setInterval(async () => {
                if (!this._ssoPolling || Date.now() > expiresAt) {
                    this._handleSSOCancel();
                    if (Date.now() > expiresAt) {
                        this._view?.webview.postMessage({
                            command: 'ssoLoginResult',
                            error: 'SSO login expired. Please try again.',
                        });
                    }
                    return;
                }

                try {
                    const pollResp = await fetch(pollUrl, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(pollBody),
                    });

                    // Guard: another overlapping callback may have already
                    // processed a terminal result while this fetch was in-flight.
                    if (!this._ssoPolling) {
                        return;
                    }

                    if (!pollResp.ok) {
                        return; // Retry on next interval
                    }

                    const pollData = await pollResp.json() as {
                        status: 'pending' | 'complete' | 'expired' | 'error';
                        identity?: Record<string, unknown>;
                        error?: string;
                    };

                    if (pollData.status === 'pending') {
                        return; // Keep polling
                    }

                    // Stop polling for any terminal status
                    this._handleSSOCancel();

                    if (pollData.status === 'complete' && pollData.identity) {
                        // Store identity in globalState with timestamp and provider for 24h expiry
                        this._context.globalState.update(
                            'conductor.ssoIdentity',
                            wrapIdentity(pollData.identity, provider)
                        );
                        this._view?.webview.postMessage({
                            command: 'ssoLoginResult',
                            identity: pollData.identity,
                            provider,
                        });
                        console.log(`[Conductor] ${provider} SSO login complete:`, pollData.identity);
                    } else {
                        this._view?.webview.postMessage({
                            command: 'ssoLoginResult',
                            error: pollData.error || `SSO login ${pollData.status}`,
                        });
                    }
                } catch (pollError) {
                    console.warn('[Conductor] SSO poll error:', pollError);
                    // Don't stop polling on network errors — retry
                }
            }, pollInterval);
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error(`[Conductor] ${provider} SSO login failed:`, msg);
            this._view?.webview.postMessage({
                command: 'ssoLoginResult',
                error: `SSO login failed: ${msg}`,
            });
        }
    }

    /**
     * Cancel active SSO polling.
     */
    private _handleSSOCancel(): void {
        this._ssoPolling = false;
        if (this._ssoTimerId !== undefined) {
            clearInterval(this._ssoTimerId);
            this._ssoTimerId = undefined;
        }
    }

    /**
     * Evaluate policy for a ChangeSet.
     * Policy evaluation failures are non-fatal - we just skip auto-apply.
     */
    private async _evaluatePolicy(changeSet: ChangeSet): Promise<PolicyResult | null> {
        try {
            let response: Response;
            try {
                response = await fetch(`${getBackendUrl()}/policy/evaluate-auto-apply`, {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ change_set: changeSet })
                });
            } catch (networkError) {
                // Backend offline - policy evaluation is non-fatal, just log and continue
                console.warn('Policy evaluation skipped: backend unreachable');
                return null;
            }

            if (!response.ok) {
                console.warn(`Policy evaluation failed: ${response.status} ${response.statusText}`);
                return null;
            }

            return await response.json() as PolicyResult;
        } catch (error) {
            console.warn('Policy evaluation error:', error);
            return null;
        }
    }

    /**
     * Handle the Generate Changes command.
     * Calls the backend API and shows a diff preview.
     *
     * If no file is open, changes will be created in the workspace root.
     * If no workspace is open, prompts user to create one.
     */
    private async _handleGenerateChanges(filePath?: string): Promise<void> {
        // Get workspace root (required)
        let workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            // Prompt user to create a workspace
            const createWorkspace = await vscode.window.showWarningMessage(
                'No workspace folder open. Would you like to create a new project folder?',
                'Create Project',
                'Open Folder',
                'Cancel'
            );

            if (createWorkspace === 'Create Project') {
                // Ask for project name
                const projectName = await vscode.window.showInputBox({
                    prompt: 'Enter project name',
                    placeHolder: 'my-project',
                    validateInput: (value) => {
                        if (!value || value.trim().length === 0) {
                            return 'Project name cannot be empty';
                        }
                        // Check for invalid characters
                        if (/[<>:"/\\|?*]/.test(value)) {
                            return 'Project name contains invalid characters';
                        }
                        return null;
                    }
                });

                if (!projectName) {
                    return; // User cancelled
                }

                // Ask where to create the project
                const folderUri = await vscode.window.showOpenDialog({
                    canSelectFiles: false,
                    canSelectFolders: true,
                    canSelectMany: false,
                    openLabel: 'Select Parent Folder',
                    title: 'Select where to create the project'
                });

                if (!folderUri || folderUri.length === 0) {
                    return; // User cancelled
                }

                // Create the project folder
                const projectPath = vscode.Uri.joinPath(folderUri[0], projectName.trim());
                try {
                    await vscode.workspace.fs.createDirectory(projectPath);
                    // Open the new folder as workspace
                    await vscode.commands.executeCommand('vscode.openFolder', projectPath);
                    vscode.window.showInformationMessage(`Created and opened project: ${projectName}`);
                } catch (error) {
                    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
                    vscode.window.showErrorMessage(`Failed to create project folder: ${errorMessage}`);
                }
                return; // VS Code will reload after opening new folder
            } else if (createWorkspace === 'Open Folder') {
                // Let user open an existing folder
                await vscode.commands.executeCommand('vscode.openFolder');
                return; // VS Code will reload after opening folder
            } else {
                return; // User cancelled
            }
        }
        const workspaceRoot = workspaceFolders[0].uri.fsPath;

        // Get the active editor's file path if not provided
        // Note: activeTextEditor can be undefined when WebView has focus
        // So we also check visibleTextEditors
        let targetFilePath = filePath || vscode.window.activeTextEditor?.document.uri.fsPath;

        // If no active editor, try to get the first visible text editor
        if (!targetFilePath && vscode.window.visibleTextEditors.length > 0) {
            targetFilePath = vscode.window.visibleTextEditors[0].document.uri.fsPath;
        }

        // Get relative file path (or null if no file open)
        let relativePath: string | null = null;
        let fileContent = '';

        if (targetFilePath) {
            relativePath = targetFilePath.replace(workspaceRoot + '/', '');
            // Read file content
            try {
                const doc = await vscode.workspace.openTextDocument(targetFilePath);
                fileContent = doc.getText();
            } catch (error) {
                // File might not exist yet
            }
        }

        // Show progress
        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: 'Generating changes...',
                cancellable: false
            },
            async () => {
                try {
                    // Build request body - file_path is optional
                    const requestBody: { instruction: string; file_path?: string; file_content?: string } = {
                        instruction: 'Generate mock changes',  // TODO: Get from user input
                    };
                    if (relativePath) {
                        requestBody.file_path = relativePath;
                        requestBody.file_content = fileContent;
                    }

                    // Call the backend API
                    let response: Response;
                    try {
                        response = await fetch(`${getBackendUrl()}/generate-changes`, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json'
                            },
                            body: JSON.stringify(requestBody)
                        });
                    } catch (networkError) {
                        // Backend is offline or unreachable
                        vscode.window.showErrorMessage(
                            `🔌 Cannot connect to backend server at ${getBackendUrl()}. Please ensure the backend is running.`,
                            'How to Start Backend'
                        ).then(selection => {
                            if (selection === 'How to Start Backend') {
                                vscode.window.showInformationMessage(
                                    'Run: cd backend && uvicorn app.main:app --reload'
                                );
                            }
                        });
                        return;
                    }

                    if (!response.ok) {
                        const statusText = response.statusText || 'Unknown error';
                        if (response.status === 500) {
                            vscode.window.showErrorMessage(
                                `🔥 Backend server error (500). The agent may have encountered an internal error.`
                            );
                        } else if (response.status === 422) {
                            vscode.window.showErrorMessage(
                                `📋 Invalid request format (422). Please check the input parameters.`
                            );
                        } else {
                            vscode.window.showErrorMessage(
                                `❌ Backend API error: ${response.status} ${statusText}`
                            );
                        }
                        return;
                    }

                    // Parse and validate response
                    let data: { success?: boolean; change_set?: ChangeSet; message?: string; error?: string };
                    try {
                        data = await response.json() as typeof data;
                    } catch (parseError) {
                        vscode.window.showErrorMessage(
                            `🔧 Invalid response from backend: Failed to parse JSON response`
                        );
                        console.error('JSON parse error:', parseError);
                        return;
                    }

                    // Validate response structure
                    if (typeof data !== 'object' || data === null) {
                        vscode.window.showErrorMessage(
                            `🔧 Invalid agent output: Response is not an object`
                        );
                        return;
                    }

                    if (!data.success) {
                        const errorDetail = data.error || data.message || 'Unknown error';
                        vscode.window.showErrorMessage(
                            `❌ Agent failed to generate changes: ${errorDetail}`
                        );
                        return;
                    }

                    if (!data.change_set) {
                        vscode.window.showErrorMessage(
                            `🔧 Invalid agent output: Missing change_set in response`
                        );
                        return;
                    }

                    // Validate change_set structure
                    const validationError = this._validateChangeSet(data.change_set);
                    if (validationError) {
                        vscode.window.showErrorMessage(
                            `🔧 Invalid agent output: ${validationError}`
                        );
                        return;
                    }

                    // Evaluate auto-apply policy for this changeset
                    const policyResult = await this._evaluatePolicy(data.change_set);

                    // Initialize the change queue for sequential review
                    this._pendingChanges = [...data.change_set.changes];
                    this._currentChangeIndex = 0;
                    this._policyResult = policyResult ?? undefined;

                    // Show the first change
                    await this._showCurrentChange();

                    vscode.window.showInformationMessage(
                        `Generated ${this._pendingChanges.length} change(s). Reviewing change 1/${this._pendingChanges.length}`
                    );
                } catch (error) {
                    const errorMessage = error instanceof Error ? error.message : 'Unknown error';
                    vscode.window.showErrorMessage(`❌ Unexpected error: ${errorMessage}`);
                    console.error('Generate changes error:', error);
                }
            }
        );
    }

    /**
     * Validate a ChangeSet structure from the agent.
     * @returns Error message if invalid, null if valid
     */
    private _validateChangeSet(changeSet: ChangeSet): string | null {
        if (!changeSet.changes || !Array.isArray(changeSet.changes)) {
            return 'change_set.changes is missing or not an array';
        }

        if (changeSet.changes.length === 0) {
            return 'change_set.changes is empty';
        }

        for (let i = 0; i < changeSet.changes.length; i++) {
            const change = changeSet.changes[i];

            if (!change.id) {
                return `Change ${i + 1}: missing 'id' field`;
            }
            if (!change.file) {
                return `Change ${i + 1} [${change.id}]: missing 'file' field`;
            }
            if (!change.type) {
                return `Change ${i + 1} [${change.id}]: missing 'type' field`;
            }
            if (change.type !== 'create_file' && change.type !== 'replace_range') {
                return `Change ${i + 1} [${change.id}]: invalid type '${change.type}' (expected 'create_file' or 'replace_range')`;
            }
            if (change.type === 'replace_range') {
                if (!change.range) {
                    return `Change ${i + 1} [${change.id}]: replace_range requires 'range' field`;
                }
                if (typeof change.range.start !== 'number' || typeof change.range.end !== 'number') {
                    return `Change ${i + 1} [${change.id}]: range.start and range.end must be numbers`;
                }
                if (change.range.start > change.range.end) {
                    return `Change ${i + 1} [${change.id}]: range.start (${change.range.start}) cannot be greater than range.end (${change.range.end})`;
                }
            }
        }

        return null;
    }

    /**
     * Handle the Apply Changes command.
     * Applies only the current change in the queue, then shows the next one.
     * Each change has a UUID for tracking success/failure.
     */
    private async _handleApplyChanges(_changeSet: ChangeSet): Promise<void> {
        // Check if we have pending changes in the queue
        if (this._pendingChanges.length === 0 || this._currentChangeIndex >= this._pendingChanges.length) {
            vscode.window.showWarningMessage('No pending changes to apply');
            return;
        }

        const currentChange = this._pendingChanges[this._currentChangeIndex];
        const totalChanges = this._pendingChanges.length;
        const currentNum = this._currentChangeIndex + 1;
        const changeId = currentChange.id || 'unknown';

        // Validate the current change
        if (!currentChange.file || !currentChange.type) {
            vscode.window.showErrorMessage(`❌ Invalid change [${changeId}]: missing file or type`);
            console.error('Invalid change:', currentChange);
            return;
        }
        if (currentChange.type === 'replace_range' &&
            (!currentChange.range || typeof currentChange.range.start !== 'number' || typeof currentChange.range.end !== 'number')) {
            vscode.window.showErrorMessage(`❌ Invalid change [${changeId}]: replace_range requires valid range`);
            console.error('Invalid range in change:', currentChange);
            return;
        }

        await vscode.window.withProgress(
            {
                location: vscode.ProgressLocation.Notification,
                title: `Applying change ${currentNum}/${totalChanges}...`,
                cancellable: false
            },
            async () => {
                try {
                    const result = await getDiffPreviewService().applySingleChange(currentChange);

                    if (result.success) {
                        // Close the current diff tab
                        await this._closeDiffTabs();

                        if (result.skipped) {
                            // Change was skipped because content is identical
                            console.log(`⏭️ Skipped change [${result.changeId}]: ${currentChange.file} (no changes needed)`);
                            vscode.window.showInformationMessage(
                                `⏭️ Skipped change ${currentNum}/${totalChanges} [${result.changeId}]: content already matches`
                            );
                        } else {
                            // Log the apply operation to audit log (only for actual changes)
                            await this._logApply(currentChange, 'manual');
                            console.log(`✅ Applied change [${result.changeId}]: ${currentChange.file}`);
                        }

                        // Move to the next change
                        this._currentChangeIndex++;

                        if (this._currentChangeIndex < this._pendingChanges.length) {
                            // Show the next change
                            if (!result.skipped) {
                                vscode.window.showInformationMessage(
                                    `✅ Applied change ${currentNum}/${totalChanges} [${result.changeId}]. Showing next change...`
                                );
                            }
                            await this._showCurrentChange();
                        } else {
                            // All changes applied
                            this._clearChangeQueue();
                        }
                    } else {
                        // Categorize and show appropriate error message
                        const errorDetail = result.error || 'Unknown error';
                        let userMessage: string;
                        let actionButton: string | undefined;

                        if (errorDetail.includes('does not exist') || errorDetail.includes('ENOENT')) {
                            userMessage = `📁 File not found: ${currentChange.file}. The file may have been deleted or moved.`;
                            actionButton = 'Create File';
                        } else if (errorDetail.includes('permission') || errorDetail.includes('EACCES')) {
                            userMessage = `🔒 Permission denied: Cannot modify ${currentChange.file}. Check file permissions.`;
                        } else if (errorDetail.includes('locked') || errorDetail.includes('EBUSY')) {
                            userMessage = `🔐 File is locked: ${currentChange.file} is being used by another process.`;
                            actionButton = 'Retry';
                        } else if (errorDetail.includes('line') || errorDetail.includes('range')) {
                            userMessage = `📍 Range conflict: The file ${currentChange.file} may have been modified. Lines ${currentChange.range?.start}-${currentChange.range?.end} may no longer exist.`;
                            actionButton = 'View File';
                        } else {
                            userMessage = `❌ Failed to apply change [${result.changeId}]: ${errorDetail}`;
                        }

                        const selection = await vscode.window.showErrorMessage(userMessage, actionButton || 'OK');

                        if (selection === 'View File') {
                            const workspaceFolders = vscode.workspace.workspaceFolders;
                            if (workspaceFolders) {
                                const filePath = vscode.Uri.file(`${workspaceFolders[0].uri.fsPath}/${currentChange.file}`);
                                try {
                                    await vscode.window.showTextDocument(filePath);
                                } catch {
                                    vscode.window.showWarningMessage(`Could not open file: ${currentChange.file}`);
                                }
                            }
                        } else if (selection === 'Retry') {
                            // Retry the same change
                            await this._handleApplyChanges(_changeSet);
                            return;
                        }

                        console.error(`Apply failed [${result.changeId}]:`, errorDetail);
                    }
                } catch (error) {
                    const errorMessage = error instanceof Error ? error.message : String(error);
                    vscode.window.showErrorMessage(`❌ Unexpected error applying change [${changeId}]: ${errorMessage}`);
                    console.error('Apply error:', error);
                }
            }
        );
    }

    /**
     * Close all diff tabs that were opened for previewing changes.
     * These are tabs with the 'ai-collab-modified' scheme.
     */
    private async _closeDiffTabs(): Promise<void> {
        const tabGroups = vscode.window.tabGroups.all;
        for (const group of tabGroups) {
            for (const tab of group.tabs) {
                if (tab.input instanceof vscode.TabInputTextDiff) {
                    const modified = tab.input.modified;
                    if (modified.scheme === 'ai-collab-modified') {
                        await vscode.window.tabGroups.close(tab);
                    }
                }
            }
        }
    }

    /**
     * Show the current change in the queue.
     * Sends info to WebView and opens diff preview.
     */
    private async _showCurrentChange(): Promise<void> {
        if (this._currentChangeIndex >= this._pendingChanges.length) {
            // All changes reviewed
            this._clearChangeQueue();
            return;
        }

        const currentChange = this._pendingChanges[this._currentChangeIndex];
        const totalChanges = this._pendingChanges.length;

        // Send current change info to WebView
        if (this._view) {
            this._view.webview.postMessage({
                command: 'showCurrentChange',
                currentChange: currentChange,
                currentIndex: this._currentChangeIndex,
                totalChanges: totalChanges,
                policyResult: this._policyResult
            });
        }

        // Show diff preview for just this one change
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (workspaceFolders && workspaceFolders.length > 0) {
            const workspaceRoot = workspaceFolders[0].uri.fsPath;
            await getDiffPreviewService().showDiff(currentChange, workspaceRoot);
        }
    }

    /**
     * Clear the change queue and notify WebView that review is complete.
     * Called when all changes have been processed or the session is reset.
     */
    private _clearChangeQueue(): void {
        this._pendingChanges = [];
        this._currentChangeIndex = 0;
        this._policyResult = undefined;

        if (this._view) {
            this._view.webview.postMessage({ command: 'allChangesComplete' });
        }

        vscode.window.showInformationMessage('✅ All changes have been reviewed');
    }

    /**
     * Log an apply operation to the audit log.
     * @param change The change that was applied
     * @param mode Whether it was manual or auto apply
     */
    private async _logApply(change: FileChange, mode: 'manual' | 'auto'): Promise<void> {
        try {
            // Use the session's roomId for audit logging
            const roomId = getSessionService().getRoomId();

            // Get the current user (use machine ID or a placeholder)
            const appliedBy = vscode.env.machineId || 'unknown-user';

            const response = await fetch(`${getBackendUrl()}/audit/log-apply`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    room_id: roomId,
                    changeset: { changes: [change] },
                    applied_by: appliedBy,
                    mode: mode
                })
            });

            if (!response.ok) {
                console.warn(`Audit log failed: ${response.status}`);
            } else {
                const data = await response.json() as { message?: string };
                console.log(`📝 Audit logged [${change.id}]: ${data.message || 'success'}`);
            }
        } catch (error) {
            // Don't fail the apply if audit logging fails
            console.warn('Audit log error:', error);
        }
    }

    private _getHtmlContent(webview: vscode.Webview): string {
        // Get path to the chat.html file
        const htmlPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'chat.html');

        // Read the HTML file
        let html = fs.readFileSync(htmlPath.fsPath, 'utf8');

        // Get the URI for the CSS file that can be used in the webview
        const cssUri = webview.asWebviewUri(
            vscode.Uri.joinPath(this._extensionUri, 'media', 'tailwind.css')
        );

        // Replace the relative CSS path with the webview URI
        html = html.replace('href="tailwind.css"', `href="${cssUri}"`);

        // Build Content Security Policy that allows WebSocket connections
        // We need to allow ws: and wss: for both localhost and ngrok URLs
        const backendUrl = getSessionService().getBackendUrl();
        const wsUrl = backendUrl.replace('http', 'ws');
        const cspMeta = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'unsafe-inline'; connect-src ${backendUrl} ${wsUrl} ws://localhost:* wss://localhost:* ws://*.ngrok-free.dev wss://*.ngrok-free.dev ws://*.ngrok.io wss://*.ngrok.io;">`;

        // Inject initial permissions data (including sessionRole based on FSM state)
        const permissions = getPermissionsService().getPermissionsForWebView();
        const currentState = this._controller.getState();
        let sessionRole: 'host' | 'guest' | 'none' = 'none';
        if (currentState === ConductorState.Hosting) {
            sessionRole = 'host';
        } else if (currentState === ConductorState.Joined) {
            sessionRole = 'guest';
        }
        const permissionsWithRole = { ...permissions, sessionRole };
        const permissionsScript = `<script>window.initialPermissions = ${JSON.stringify(permissionsWithRole)};</script>`;

        // Inject session state (roomId, hostId, createdAt)
        const sessionState = getSessionService().getSessionStateForWebView();
        const sessionScript = `<script>window.initialSession = ${JSON.stringify(sessionState)};</script>`;

        // Inject current conductor FSM state so WebView can render correctly on reload
        const conductorState = this._controller.getState();
        const conductorScript = `<script>window.initialConductorState = ${JSON.stringify(conductorState)};</script>`;

        // Inject SSO identity from globalState (if previously signed in, within 24h)
        const ssoIdentity = this._getValidSSOIdentity();
        const ssoProvider = this._getStoredSSOProvider();
        // Clear expired/old-format entries from globalState
        const rawSso = this._context.globalState.get('conductor.ssoIdentity');
        if (isStale(rawSso)) {
            this._context.globalState.update('conductor.ssoIdentity', undefined);
        }
        const ssoScript = `<script>window.initialSSOIdentity = ${JSON.stringify(ssoIdentity)};window.initialSSOProvider = ${JSON.stringify(ssoProvider || null)};window.initialEnabledSSOProviders = ${JSON.stringify(this._enabledSSOProviders)};</script>`;

        html = html.replace('</head>', `${cspMeta}${permissionsScript}${sessionScript}${conductorScript}${ssoScript}</head>`);

        return html;
    }
}
