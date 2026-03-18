/**
 * WorkflowPanel -- WebView panel for visualizing agent workflow graphs.
 *
 * Opens a standalone panel (beside the active editor) that fetches and renders
 * workflow graphs from the backend `GET /api/workflows/{name}/graph` endpoint.
 * The graph is rendered with pure HTML/CSS/SVG in a sandboxed WebView.
 *
 * Communication:
 * - WebView sends `{command: 'getConfig'}` on load.
 * - Extension replies with `{command: 'config', backendUrl: '...'}`.
 *
 * @module services/workflowPanel
 */

import * as vscode from 'vscode';
import * as fs from 'fs';

// ---------------------------------------------------------------------------
// WorkflowPanel
// ---------------------------------------------------------------------------

/**
 * Manages a VS Code WebView panel that visualizes agent workflow graphs.
 *
 * Lifecycle:
 * - Call `WorkflowPanel.show()` to open (or reveal) the panel.
 * - The panel disposes automatically when the user closes the tab.
 */
export class WorkflowPanel implements vscode.Disposable {
    private static readonly VIEW_TYPE = 'conductorWorkflow';
    private static readonly TITLE = 'Conductor Workflows';

    /** Singleton instance — only one workflow panel at a time. */
    public static currentPanel: WorkflowPanel | undefined;

    private readonly _panel: vscode.WebviewPanel;
    private readonly _extensionUri: vscode.Uri;
    private readonly _backendUrl: string;
    private readonly _disposables: vscode.Disposable[] = [];

    // -----------------------------------------------------------------------
    // Public static API
    // -----------------------------------------------------------------------

    /**
     * Open the workflow panel (or reveal it if already open).
     *
     * @param extensionUri  Extension root URI (for loading media/ resources)
     * @param backendUrl    Backend base URL (e.g. "http://localhost:8000")
     */
    public static show(extensionUri: vscode.Uri, backendUrl: string): void {
        if (WorkflowPanel.currentPanel) {
            WorkflowPanel.currentPanel._panel.reveal();
            return;
        }

        const panel = vscode.window.createWebviewPanel(
            WorkflowPanel.VIEW_TYPE,
            WorkflowPanel.TITLE,
            vscode.ViewColumn.Beside,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'media')],
            },
        );

        WorkflowPanel.currentPanel = new WorkflowPanel(panel, extensionUri, backendUrl);
    }

    // -----------------------------------------------------------------------
    // Constructor (private — use `show()`)
    // -----------------------------------------------------------------------

    private constructor(
        panel: vscode.WebviewPanel,
        extensionUri: vscode.Uri,
        backendUrl: string,
    ) {
        this._panel = panel;
        this._extensionUri = extensionUri;
        this._backendUrl = backendUrl;

        // Load HTML content
        this._panel.webview.html = this._getHtmlContent();

        // Handle messages from the WebView
        this._panel.webview.onDidReceiveMessage(
            (msg) => this._handleMessage(msg),
            null,
            this._disposables,
        );

        // Clean up on dispose
        this._panel.onDidDispose(() => this._handleDispose(), null, this._disposables);
    }

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /** Close and clean up the panel. */
    dispose(): void {
        this._panel.dispose();
    }

    // -----------------------------------------------------------------------
    // Message handling
    // -----------------------------------------------------------------------

    private _handleMessage(message: unknown): void {
        if (!isRecord(message)) { return; }

        switch (message['command']) {
            case 'getConfig':
                // Reply with backend URL so the WebView can fetch workflow data
                this._panel.webview.postMessage({
                    command: 'config',
                    backendUrl: this._backendUrl,
                });
                return;
            default:
                break;
        }
    }

    // -----------------------------------------------------------------------
    // Dispose
    // -----------------------------------------------------------------------

    private _handleDispose(): void {
        WorkflowPanel.currentPanel = undefined;
        for (const d of this._disposables) {
            d.dispose();
        }
    }

    // -----------------------------------------------------------------------
    // HTML content
    // -----------------------------------------------------------------------

    /**
     * Read workflow.html from the media/ directory and inject CSP + resource URIs.
     */
    private _getHtmlContent(): string {
        const htmlPath = vscode.Uri.joinPath(this._extensionUri, 'media', 'workflow.html');
        let html = fs.readFileSync(htmlPath.fsPath, 'utf8');

        // Build Content Security Policy that allows fetch to backend
        const webview = this._panel.webview;
        const backendUrl = this._backendUrl;
        const cspMeta = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'unsafe-inline'; connect-src ${backendUrl};">`;

        // Inject CSP into <head>
        html = html.replace('<head>', `<head>\n  ${cspMeta}`);

        return html;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Type guard: is the value a plain object (Record)? */
function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
