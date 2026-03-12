/**
 * WorkspacePanel – 5-step VS Code WebView workspace-creation wizard.
 *
 * Presents a multi-step form inside a VS Code WebView panel that guides the
 * user through:
 *   Step 1 – Enter workspace name
 *   Step 2 – Choose template
 *   Step 3 – (Optional) Enter Git repository URL
 *   Step 4 – Review & confirm
 *   Step 5 – Creating… (progress indicator)
 *
 * The panel communicates with the extension host via the VS Code WebView
 * message-passing API.  When the user confirms creation the `onSubmit`
 * callback is called with the collected parameters.
 *
 * @module services/workspacePanel
 */

import * as vscode from 'vscode';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Callback invoked when the user completes the wizard and clicks "Create". */
export type WorkspaceSubmitCallback = (
    name: string,
    template: string,
    gitRepoUrl?: string
) => Promise<void>;

// ---------------------------------------------------------------------------
// WorkspacePanel
// ---------------------------------------------------------------------------

/**
 * Manages a VS Code WebView panel that hosts the workspace-creation wizard.
 *
 * Lifecycle:
 * - Instantiate to open the panel immediately.
 * - Call `reveal()` to bring an existing panel to the foreground.
 * - Call `dispose()` to close and clean up.
 * - Subscribe to `onDidDispose` to be notified when the user closes the panel.
 */
export class WorkspacePanel implements vscode.Disposable {
    private static readonly VIEW_TYPE = 'conductor.workspacePanel';
    private static readonly TITLE = 'Create Conductor Workspace';

    private readonly _panel: vscode.WebviewPanel;
    private readonly _disposables: vscode.Disposable[] = [];
    private readonly _onDidDisposeEmitter = new vscode.EventEmitter<void>();

    /** Fires when the panel is disposed (either by the user or programmatically). */
    readonly onDidDispose: vscode.Event<void> = this._onDidDisposeEmitter.event;

    constructor(
        private readonly _extensionUri: vscode.Uri,
        private readonly _onSubmit: WorkspaceSubmitCallback
    ) {
        this._panel = vscode.window.createWebviewPanel(
            WorkspacePanel.VIEW_TYPE,
            WorkspacePanel.TITLE,
            vscode.ViewColumn.One,
            {
                enableScripts: true,
                retainContextWhenHidden: true,
                localResourceRoots: [this._extensionUri],
            }
        );

        this._panel.webview.html = this._buildHtml();
        this._panel.onDidDispose(() => this._handleDispose(), null, this._disposables);
        this._panel.webview.onDidReceiveMessage(
            (msg) => void this._handleMessage(msg),
            null,
            this._disposables
        );
    }

    // -----------------------------------------------------------------------
    // Public API
    // -----------------------------------------------------------------------

    /** Bring the panel to the foreground if it’s already open. */
    reveal(): void {
        this._panel.reveal();
    }

    /** Close and clean up the panel. */
    dispose(): void {
        this._panel.dispose();
    }

    // -----------------------------------------------------------------------
    // Message handling
    // -----------------------------------------------------------------------

    private async _handleMessage(message: unknown): Promise<void> {
        if (!isRecord(message)) { return; }

        switch (message['type']) {
            case 'submit': {
                const name = String(message['name'] ?? '');
                const template = String(message['template'] ?? '');
                const gitRepoUrl = message['gitRepoUrl']
                    ? String(message['gitRepoUrl'])
                    : undefined;

                if (!name || !template) {
                    await this._panel.webview.postMessage({ type: 'error', message: 'Name and template are required.' });
                    return;
                }

                // Show the progress step inside the WebView.
                await this._panel.webview.postMessage({ type: 'step', step: 5 });

                try {
                    await this._onSubmit(name, template, gitRepoUrl);
                } catch (err) {
                    const msg = err instanceof Error ? err.message : String(err);
                    await this._panel.webview.postMessage({ type: 'error', message: msg });
                }
                break;
            }
            default:
                break;
        }
    }

    // -----------------------------------------------------------------------
    // Dispose
    // -----------------------------------------------------------------------

    private _handleDispose(): void {
        for (const d of this._disposables) {
            d.dispose();
        }
        this._onDidDisposeEmitter.fire();
        this._onDidDisposeEmitter.dispose();
    }

    // -----------------------------------------------------------------------
    // HTML
    // -----------------------------------------------------------------------

    /**
     * Build the full HTML document for the WebView.
     *
     * The wizard state is managed entirely in the browser with vanilla JS.
     * Steps 1–4 collect data; step 5 shows a spinner while the backend is
     * called.
     */
    private _buildHtml(): string {
        return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Create Conductor Workspace</title>
  <style>
    body { font-family: var(--vscode-font-family); color: var(--vscode-foreground); background: var(--vscode-editor-background); padding: 24px; max-width: 540px; margin: 0 auto; }
    h1   { font-size: 1.4rem; margin-bottom: 1.2rem; }
    .step { display: none; }
    .step.active { display: block; }
    label { display: block; margin-bottom: 0.25rem; font-weight: 600; }
    input, select { width: 100%; box-sizing: border-box; padding: 6px 8px; margin-bottom: 1rem;
      background: var(--vscode-input-background); color: var(--vscode-input-foreground);
      border: 1px solid var(--vscode-input-border); border-radius: 3px; }
    button { padding: 6px 16px; border: none; border-radius: 3px; cursor: pointer;
      background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
    button:hover { background: var(--vscode-button-hoverBackground); }
    .row { display: flex; gap: 8px; justify-content: flex-end; }
    .error { color: var(--vscode-errorForeground); margin-bottom: 1rem; }
    .spinner { border: 3px solid var(--vscode-input-border); border-top-color: var(--vscode-button-background);
      border-radius: 50%; width: 32px; height: 32px; animation: spin 0.8s linear infinite; margin: 24px auto; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .review-item { margin-bottom: 0.5rem; }
    .review-item span { font-weight: 600; }
  </style>
</head>
<body>
  <h1>Create Conductor Workspace</h1>
  <div id="error" class="error" style="display:none"></div>

  <!-- Step 1: Name -->
  <div id="step1" class="step active">
    <label for="wsName">Workspace Name</label>
    <input id="wsName" type="text" placeholder="my-workspace" />
    <div class="row"><button onclick="goStep2()">Next &rsaquo;</button></div>
  </div>

  <!-- Step 2: Template -->
  <div id="step2" class="step">
    <label for="wsTpl">Template</label>
    <select id="wsTpl">
      <option value="python-3.11">Python 3.11</option>
      <option value="node-20">Node.js 20</option>
      <option value="go-1.22">Go 1.22</option>
      <option value="rust-1.78">Rust 1.78</option>
      <option value="blank">Blank</option>
    </select>
    <div class="row">
      <button onclick="goStep1()">&lsaquo; Back</button>
      <button onclick="goStep3()">Next &rsaquo;</button>
    </div>
  </div>

  <!-- Step 3: Git repo (optional) -->
  <div id="step3" class="step">
    <label for="wsGit">Git Repository URL <small>(optional)</small></label>
    <input id="wsGit" type="url" placeholder="https://github.com/org/repo.git" />
    <div class="row">
      <button onclick="goStep2()">&lsaquo; Back</button>
      <button onclick="goStep4()">Next &rsaquo;</button>
    </div>
  </div>

  <!-- Step 4: Review -->
  <div id="step4" class="step">
    <h2 style="font-size:1rem">Review</h2>
    <div class="review-item"><span>Name:</span> <span id="rName"></span></div>
    <div class="review-item"><span>Template:</span> <span id="rTpl"></span></div>
    <div class="review-item"><span>Git URL:</span> <span id="rGit"></span></div>
    <div class="row">
      <button onclick="goStep3()">&lsaquo; Back</button>
      <button onclick="submitForm()">Create Workspace</button>
    </div>
  </div>

  <!-- Step 5: Progress -->
  <div id="step5" class="step">
    <div class="spinner"></div>
    <p style="text-align:center">Creating workspace…</p>
  </div>

  <script>
    const vscode = acquireVsCodeApi();
    let currentStep = 1;

    function showStep(n) {
      document.querySelectorAll('.step').forEach(el => el.classList.remove('active'));
      document.getElementById('step' + n).classList.add('active');
      currentStep = n;
    }

    function hideError() {
      const el = document.getElementById('error');
      el.style.display = 'none';
      el.textContent = '';
    }

    function showError(msg) {
      const el = document.getElementById('error');
      el.textContent = msg;
      el.style.display = 'block';
    }

    function goStep1() { hideError(); showStep(1); }
    function goStep2() {
      hideError();
      const name = document.getElementById('wsName').value.trim();
      if (!name) { showError('Workspace name is required.'); return; }
      showStep(2);
    }
    function goStep3() { hideError(); showStep(3); }
    function goStep4() {
      hideError();
      document.getElementById('rName').textContent = document.getElementById('wsName').value.trim();
      document.getElementById('rTpl').textContent  = document.getElementById('wsTpl').value;
      const git = document.getElementById('wsGit').value.trim();
      document.getElementById('rGit').textContent  = git || '(none)';
      showStep(4);
    }

    function submitForm() {
      hideError();
      const name      = document.getElementById('wsName').value.trim();
      const template  = document.getElementById('wsTpl').value;
      const gitRepoUrl = document.getElementById('wsGit').value.trim() || undefined;
      vscode.postMessage({ type: 'submit', name, template, gitRepoUrl });
    }

    window.addEventListener('message', event => {
      const msg = event.data;
      if (msg.type === 'step')  { showStep(msg.step); }
      if (msg.type === 'error') { showStep(currentStep < 5 ? currentStep : 4); showError(msg.message); }
    });
  </script>
</body>
</html>`;
    }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Type guard: is the value a plain object (Record)? */
function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}
