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
import * as path from 'path';
import * as vsls from 'vsls/vscode';
import { execSync } from 'child_process';

import { checkBackendHealth } from './services/backendHealthCheck';
import { diagnoseBackendConnection } from './services/connectionDiagnostics';
import { ConductorController } from './services/conductorController';
import {
    ConductorState,
    ConductorEvent,
    ConductorStateMachine,
} from './services/conductorStateMachine';
import { ChangeSet, FileChange, getDiffPreviewService } from './services/diffPreview';
import { getPermissionsService } from './services/permissions';
import { getSessionService } from './services/session';
import { wrapIdentity, getValidIdentity, getStoredProvider, isStale, SSOProvider } from './services/ssoIdentityCache';
import { detectWorkspaceLanguages, clearLanguageCache } from './services/languageDetector';
import { clearProjectMetadataCache } from './services/projectMetadataCollector';
import { parseStackTrace, resolveFramePaths } from './services/stackTraceParser';
import { scanWorkspaceTodos, updateWorkspaceTodoInFile, UpdateTodoPayload } from './services/todoScanner';
import {
    initConductorWorkspaceStorage,
    resetWorkspaceDb,
    loadWorkspaceConfig,
    WorkspaceConfig,
} from './services/workspaceStorage';
import { ConductorDb } from './services/conductorDb';
import { runExplainPipeline } from './services/explainWithContextPipeline';
import { indexWorkspace, reindexSingleFile, cancelCurrentIndex } from './services/workspaceIndexer';
import { RagClient, RagFileChange } from './services/ragClient';
import { ConductorFileSystemProvider } from './services/conductorFileSystemProvider';
import { WorkflowPanel } from './services/workflowPanel';

/** Output channel for logging invite links to the user. */
let outputChannel: vscode.OutputChannel;

/** SQLite DB instance for context enricher metadata (set after hosting starts). */
let conductorDb: ConductorDb | null = null;

/** Active workspace root (set alongside conductorDb). */
let conductorWsRoot: string | null = null;

/** Workspace configuration loaded from .conductor/config.json (set after hosting starts). */
let workspaceConfig: WorkspaceConfig | null = null;

/** RagClient for backend-centric codebase indexing and search. */
let ragClient: RagClient | null = null;

/** ConductorFileSystemProvider instance — registered once in activate(). */
let conductorFsProvider: ConductorFileSystemProvider | null = null;

/** GlobalState key for persisting FSM state across reloads. */
const FSM_STATE_KEY = 'conductor.fsmState';

/**
 * If the index was last built less than this many milliseconds ago
 * (and the branch hasn't changed) skip Phase 1+2 on session start.
 */
const SCAN_FRESHNESS_MS = 5 * 60 * 1000; // 5 minutes

/**
 * Get the backend server URL from configuration.
 * @returns The backend URL (e.g., "http://localhost:8000")
 */
function getBackendUrl(): string {
    return getSessionService().getBackendUrl();
}

/**
 * Return the current git branch for the given workspace root, or null if
 * the directory is not a git repo or git is not available.
 */
function _getGitBranch(wsRoot: string): string | null {
    try {
        const branch = execSync('git rev-parse --abbrev-ref HEAD', {
            cwd: wsRoot,
            timeout: 2000,
            encoding: 'utf8',
            stdio: ['ignore', 'pipe', 'ignore'],
        }).trim();
        return branch || null;
    } catch {
        return null;
    }
}

/**
 * Infer the VS Code language ID from a file path's extension.
 * Used when the snippet message doesn't carry an explicit language field.
 */
function _langFromPath(filePath: string): string {
    const ext = filePath.split('.').pop()?.toLowerCase() ?? '';
    switch (ext) {
        case 'py':                        return 'python';
        case 'ts': case 'tsx':            return 'typescript';
        case 'js': case 'jsx': case 'mjs': return 'javascript';
        case 'java':                      return 'java';
        case 'go':                        return 'go';
        case 'rs':                        return 'rust';
        case 'cs':                        return 'csharp';
        case 'cpp': case 'cc': case 'cxx': return 'cpp';
        case 'rb':                        return 'ruby';
        default:                          return 'text';
    }
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
        vscode.workspace.onDidChangeWorkspaceFolders(() => { clearLanguageCache(); clearProjectMetadataCache(); })
    );

    // Detect ngrok URL if ngrok is running (async, non-blocking)
    getSessionService().detectNgrokUrl().then(ngrokUrl => {
        if (ngrokUrl) {
            console.log(`[AI Collab] Using ngrok URL: ${ngrokUrl}`);
            vscode.window.showInformationMessage(`🌐 Ngrok detected: ${ngrokUrl}`);
        } else {
            console.log('[AI Collab] No local ngrok tunnel detected. Using configured backend URL / localhost fallback.');
        }
    });

    // ---------------------------------------------------------------
    // Conductor FSM + Controller
    // ---------------------------------------------------------------

    // Restore FSM state across workspace-folder reloads (e.g. Open Workspace
    // triggers updateWorkspaceFolders → extension host restart).  Only Hosting
    // and Joined survive a restart; transient states fall back to Idle.
    const RESTORABLE_STATES = new Set<string>([ConductorState.Hosting, ConductorState.Joined, ConductorState.ReadyToHost]);
    const persistedState = context.globalState.get<string>(FSM_STATE_KEY);
    let initialState = ConductorState.Idle;
    let wasRestored = false;

    // In development mode (F5 debug sessions), always start from Idle so the
    // login/landing page is shown instead of jumping straight into a stale session.
    const isDevelopmentMode = context.extensionMode === vscode.ExtensionMode.Development;

    if (!isDevelopmentMode && persistedState && RESTORABLE_STATES.has(persistedState)) {
        initialState = persistedState as ConductorState;
        wasRestored = true;
        console.log(`[Conductor] Restored FSM state: ${initialState}`);
    } else {
        if (isDevelopmentMode && persistedState) {
            console.log('[Conductor] Development mode — ignoring persisted FSM state, starting from Idle');
        } else {
            console.log('[Conductor] Starting FSM from Idle (fresh start)');
        }
    }

    const fsm = new ConductorStateMachine(initialState);

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

    // Post-restore health check or normal startup
    if (wasRestored) {
        // Don't call controller.start() — it throws for non-Idle/BackendDisconnected states.
        // Instead, validate the backend is still reachable.
        checkBackendHealth(getSessionService().getBackendUrl()).then(alive => {
            if (alive) {
                console.log(`[Conductor] Backend reachable, restored state ${initialState} preserved`);
            } else {
                console.warn('[Conductor] Backend unreachable after restore, transitioning to BackendDisconnected');
                try {
                    fsm.transition(ConductorEvent.BACKEND_LOST);
                } catch (e) {
                    console.warn('[Conductor] Failed to transition on restore health failure:', e);
                }
            }
        }).catch(err => {
            console.warn('[Conductor] Restore health check failed:', err);
        });
    } else {
        // Normal startup: health check → ReadyToHost or BackendDisconnected
        controller.start().then(state => {
            console.log(`[Conductor] Initial health check complete → ${state}`);
        }).catch(err => {
            console.warn('[Conductor] Health check start failed:', err);
        });
    }

    // Register the WebView provider for the sidebar chat panel
    const provider = new AICollabViewProvider(context.extensionUri, context, controller);
    context.subscriptions.push(
        vscode.window.registerWebviewViewProvider('aiCollabView', provider, {
            webviewOptions: {
                retainContextWhenHidden: true  // Keep WebView state when hidden
            }
        })
    );

    // Register conductor:// virtual file-system provider
    conductorFsProvider = new ConductorFileSystemProvider(() => getBackendUrl());
    context.subscriptions.push(
        vscode.workspace.registerFileSystemProvider('conductor', conductorFsProvider, {
            isCaseSensitive: true,
            isReadonly: false,
        })
    );

    // Register conductor:// search command (VS Code's built-in search doesn't
    // work on virtual file systems without the proposed TextSearchProvider API)
    context.subscriptions.push(
        vscode.commands.registerCommand('conductor.searchWorkspace', async () => {
            const query = await vscode.window.showInputBox({
                prompt: 'Search remote workspace',
                placeHolder: 'Enter search pattern...',
            });
            if (!query) { return; }

            // Find which conductor:// workspace is active
            const conductorFolder = vscode.workspace.workspaceFolders?.find(
                f => f.uri.scheme === 'conductor',
            );
            const roomId = conductorFolder?.uri.authority;
            if (!roomId) {
                vscode.window.showWarningMessage('No Conductor workspace is open.');
                return;
            }

            try {
                const resp = await fetch(`${getBackendUrl()}/workspace/${encodeURIComponent(roomId)}/search`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ pattern: query, max_results: 50 }),
                });
                if (!resp.ok) {
                    vscode.window.showErrorMessage('Search failed');
                    return;
                }
                const data = await resp.json() as any;
                const matches: Array<{ path: string; line: number; text: string }> = data.matches ?? [];

                if (matches.length === 0) {
                    vscode.window.showInformationMessage('No results found.');
                    return;
                }

                const picks = matches.map(m => ({
                    label: `$(file) ${m.path}:${m.line}`,
                    description: m.text.trim().slice(0, 120),
                    m,
                }));

                const selected = await vscode.window.showQuickPick(picks, {
                    placeHolder: `${matches.length} results for "${query}"`,
                    matchOnDescription: true,
                });
                if (selected) {
                    const uri = vscode.Uri.parse(`conductor://${roomId}/${selected.m.path}`);
                    const doc = await vscode.workspace.openTextDocument(uri);
                    await vscode.window.showTextDocument(doc, {
                        selection: new vscode.Range(
                            selected.m.line - 1, 0,
                            selected.m.line - 1, 0,
                        ),
                    });
                }
            } catch (e) {
                vscode.window.showErrorMessage(`Search error: ${e instanceof Error ? e.message : String(e)}`);
            }
        })
    );

    // Register command to focus the AI Collab panel
    const disposable = vscode.commands.registerCommand('ai-collab.openPanel', () => {
        vscode.commands.executeCommand('aiCollabView.focus');
    });
    context.subscriptions.push(disposable);

    // Register command to open the workflow visualization panel
    context.subscriptions.push(
        vscode.commands.registerCommand('conductor.showWorkflow', () => {
            WorkflowPanel.show(context.extensionUri, getBackendUrl());
        })
    );

    // Register command to compare local tool implementations (LSP vs grep)
    context.subscriptions.push(
        vscode.commands.registerCommand('conductor.compareTools', async () => {
            await compareLocalTools();
        })
    );

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
 * Compare local tool implementations: LSP vs grep fallback.
 * Runs both paths on the same target file and saves structured results
 * to eval/tool_lsp_results.json for comparison with tree-sitter baseline.
 */
async function compareLocalTools(): Promise<void> {
    const folders = vscode.workspace.workspaceFolders;
    if (!folders) {
        vscode.window.showErrorMessage('No workspace open');
        return;
    }
    const workspace = folders[0].uri.fsPath;
    const path = require('path');
    const fs = require('fs');
    const { execFile } = require('child_process');
    const { promisify } = require('util');
    const execFileAsync = promisify(execFile);

    // Auto-discover a suitable test file from the current workspace
    // Prefer the active editor, then find any Python/TypeScript file with classes
    let TARGET_FILE = '';
    let TARGET_SYMBOL = '';
    let TARGET_FUNCTION = '';

    const activeEditor = vscode.window.activeTextEditor;
    if (activeEditor && activeEditor.document.uri.fsPath.startsWith(workspace)) {
        TARGET_FILE = path.relative(workspace, activeEditor.document.uri.fsPath);
    }

    if (!TARGET_FILE) {
        // Find any .py or .ts file with some substance
        try {
            const pyFiles = await vscode.workspace.findFiles('**/*.py', '**/node_modules/**', 5);
            const tsFiles = await vscode.workspace.findFiles('**/*.ts', '**/node_modules/**', 5);
            const candidates = [...pyFiles, ...tsFiles];
            for (const f of candidates) {
                const content = fs.readFileSync(f.fsPath, 'utf-8');
                if (content.includes('class ') && content.split('\n').length > 20) {
                    TARGET_FILE = path.relative(workspace, f.fsPath);
                    break;
                }
            }
        } catch {}
    }

    if (!TARGET_FILE) {
        vscode.window.showErrorMessage('No suitable file found for comparison. Open a Python or TypeScript file first.');
        return;
    }

    // Read the file to discover a class and function name for testing
    try {
        const content = fs.readFileSync(path.resolve(workspace, TARGET_FILE), 'utf-8');
        const classMatch = content.match(/^class\s+(\w+)/m);
        if (classMatch) { TARGET_SYMBOL = classMatch[1]; }
        const funcMatch = content.match(/^(?:async\s+)?(?:def|function)\s+(\w+)/m);
        if (funcMatch) { TARGET_FUNCTION = funcMatch[1]; }
        // Fallback: use any top-level def/function
        if (!TARGET_FUNCTION) {
            const anyFunc = content.match(/(?:def|function)\s+(\w+)/);
            if (anyFunc) { TARGET_FUNCTION = anyFunc[1]; }
        }
    } catch {}

    const output = vscode.window.createOutputChannel('Conductor Tool Comparison');
    output.show();
    output.appendLine('=== Tool Comparison: LSP vs grep ===\n');
    output.appendLine(`Target file:     ${TARGET_FILE}`);
    output.appendLine(`Target symbol:   ${TARGET_SYMBOL || '(none found)'}`);
    output.appendLine(`Target function: ${TARGET_FUNCTION || '(none found)'}\n`);

    const toUri = (f: string) => vscode.Uri.file(path.resolve(workspace, f));
    const toRelative = (fsPath: string) => path.relative(workspace, fsPath);

    const results: Record<string, any> = {};

    // Helper: run grep
    const runGrep = async (args: string[]): Promise<string> => {
        try {
            const r = await execFileAsync('grep', args, { cwd: workspace, maxBuffer: 5 * 1024 * 1024 });
            return r.stdout || '';
        } catch (e: any) {
            return e.stdout || '';
        }
    };

    // ---- file_outline ----
    output.appendLine('--- file_outline ---');
    const lspOutline: any = { count: 0, names: [] as string[], lines: [] as number[] };
    const grepOutline: any = { count: 0, names: [] as string[], lines: [] as number[] };

    try {
        const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider', toUri(TARGET_FILE),
        );
        if (symbols) {
            // Only keep structural symbols (Class, Function, Method, etc.)
            const STRUCTURAL = new Set([
                vscode.SymbolKind.Class, vscode.SymbolKind.Function,
                vscode.SymbolKind.Method, vscode.SymbolKind.Constructor,
                vscode.SymbolKind.Interface, vscode.SymbolKind.Enum,
                vscode.SymbolKind.Struct, vscode.SymbolKind.Module,
                vscode.SymbolKind.Namespace,
            ]);
            const flatten = (syms: vscode.DocumentSymbol[], parent?: string): any[] => {
                const result: any[] = [];
                for (const s of syms) {
                    if (STRUCTURAL.has(s.kind)) {
                        result.push({ name: s.name, kind: vscode.SymbolKind[s.kind], line: s.range.start.line + 1 });
                    }
                    if (s.children) result.push(...flatten(s.children, s.name));
                }
                return result;
            };
            const flat = flatten(symbols);
            lspOutline.count = flat.length;
            lspOutline.names = flat.map(s => s.name);
            lspOutline.lines = flat.map(s => s.line);
            output.appendLine(`  LSP:  ${flat.length} symbols: ${flat.map(s => s.name).join(', ')}`);
        }
    } catch (e) {
        output.appendLine(`  LSP:  FAILED - ${e}`);
    }

    const grepStdout = await runGrep([
        '-n', '-E', '(^\\s*(def |async def |function |class |interface |type |const |export |struct ))',
        path.resolve(workspace, TARGET_FILE),
    ]);
    const grepLines = grepStdout.trim().split('\n').filter(Boolean);
    grepOutline.count = grepLines.length;
    grepOutline.names = grepLines.map((l: string) => l.split(':').slice(1).join(':').trim().split(/[\s(]/)[1] || '');
    grepOutline.lines = grepLines.map((l: string) => parseInt(l.split(':')[0]) || 0);
    output.appendLine(`  grep: ${grepLines.length} symbols`);

    results['file_outline'] = { lsp: lspOutline, grep: grepOutline };

    // ---- find_symbol ----
    output.appendLine('\n--- find_symbol ---');
    const lspFind: any = { count: 0, lines: [] as number[] };
    const grepFind: any = { count: 0, lines: [] as number[] };

    try {
        const wsSymbols = await vscode.commands.executeCommand<vscode.SymbolInformation[]>(
            'vscode.executeWorkspaceSymbolProvider', TARGET_SYMBOL,
        );
        if (wsSymbols) {
            const filtered = wsSymbols.filter(s => s.location.uri.fsPath.startsWith(workspace));
            lspFind.count = filtered.length;
            lspFind.lines = filtered.map(s => s.location.range.start.line + 1);
            output.appendLine(`  LSP:  ${filtered.length} matches`);
            for (const s of filtered.slice(0, 5)) {
                output.appendLine(`    ${vscode.SymbolKind[s.kind]} ${s.name} @ ${toRelative(s.location.uri.fsPath)}:${s.location.range.start.line + 1}`);
            }
        }
    } catch (e) {
        output.appendLine(`  LSP:  FAILED - ${e}`);
    }

    const grepFindStdout = await runGrep([
        '-rn', '-E', `(def |function |class |interface |struct )${TARGET_SYMBOL}`, '.',
    ]);
    const grepFindLines = grepFindStdout.trim().split('\n').filter(Boolean);
    grepFind.count = grepFindLines.length;
    grepFind.lines = grepFindLines.map((l: string) => parseInt(l.split(':')[1]) || 0);
    output.appendLine(`  grep: ${grepFindLines.length} matches`);

    results['find_symbol'] = { lsp: lspFind, grep: grepFind };

    // ---- expand_symbol ----
    output.appendLine('\n--- expand_symbol ---');
    const lspExpand: any = { start_line: 0, end_line: 0, has_body: false };

    try {
        const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider', toUri(TARGET_FILE),
        );
        if (symbols) {
            const flatten = (syms: vscode.DocumentSymbol[]): any[] => {
                const result: any[] = [];
                for (const s of syms) {
                    result.push(s);
                    if (s.children) result.push(...flatten(s.children));
                }
                return result;
            };
            const match = flatten(symbols).find(s => s.name === TARGET_SYMBOL);
            if (match) {
                lspExpand.start_line = match.range.start.line + 1;
                lspExpand.end_line = match.range.end.line + 1;
                lspExpand.has_body = true;
                output.appendLine(`  LSP:  L${lspExpand.start_line}-${lspExpand.end_line}`);
            }
        }
    } catch (e) {
        output.appendLine(`  LSP:  FAILED - ${e}`);
    }

    results['expand_symbol'] = { lsp: lspExpand, grep: {} };

    // ---- compressed_view ----
    output.appendLine('\n--- compressed_view ---');
    const lspCV: any = { has_ToolExecutor: false, has_LocalToolExecutor: false, has_RemoteToolExecutor: false, has_call_info: false };

    try {
        const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider', toUri(TARGET_FILE),
        );
        if (symbols) {
            const flatten = (syms: vscode.DocumentSymbol[]): string[] => {
                const r: string[] = [];
                for (const s of syms) {
                    r.push(s.name);
                    if (s.children) r.push(...flatten(s.children));
                }
                return r;
            };
            const names = flatten(symbols);
            lspCV.has_ToolExecutor = names.includes('ToolExecutor');
            lspCV.has_LocalToolExecutor = names.includes('LocalToolExecutor');
            lspCV.has_RemoteToolExecutor = names.includes('RemoteToolExecutor');
            lspCV.has_call_info = false; // LSP doesn't provide call info in outline
            output.appendLine(`  LSP:  classes=[${names.filter(n => /Executor/.test(n)).join(', ')}]`);
        }
    } catch (e) {
        output.appendLine(`  LSP:  FAILED - ${e}`);
    }

    results['compressed_view'] = { lsp: lspCV, grep: {} };

    // ---- get_callers ----
    output.appendLine('\n--- get_callers ---');
    const lspCallers: any = { count: 0, files: [] as string[] };
    const grepCallers: any = { count: 0, files: [] as string[] };

    if (!TARGET_FUNCTION) {
        output.appendLine('  (skipped — no function found in target file)');
    }

    // Try LSP call hierarchy
    try {
        const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
            'vscode.executeDocumentSymbolProvider',
            toUri(TARGET_FILE),
        );
        if (symbols) {
            const flatten = (syms: vscode.DocumentSymbol[]): vscode.DocumentSymbol[] => {
                const r: vscode.DocumentSymbol[] = [];
                for (const s of syms) { r.push(s); if (s.children) r.push(...flatten(s.children)); }
                return r;
            };
            const match = flatten(symbols).find(s => s.name === TARGET_FUNCTION);
            if (match) {
                // Use selectionRange (the symbol name position), not range (full body)
                const pos = match.selectionRange
                    ? match.selectionRange.start
                    : match.range.start;
                output.appendLine(`  LSP:  preparing call hierarchy for '${TARGET_FUNCTION}' at L${pos.line + 1}:${pos.character}`);
                const items = await vscode.commands.executeCommand<vscode.CallHierarchyItem[]>(
                    'vscode.prepareCallHierarchy',
                    toUri(TARGET_FILE),
                    pos,
                );
                if (items && items.length > 0) {
                    const incoming = await vscode.commands.executeCommand<vscode.CallHierarchyIncomingCall[]>(
                        'vscode.provideIncomingCalls', items[0],
                    );
                    if (incoming) {
                        lspCallers.count = incoming.length;
                        lspCallers.files = [...new Set(incoming.map(c => toRelative(c.from.uri.fsPath)))];
                        output.appendLine(`  LSP:  ${incoming.length} callers from ${lspCallers.files.length} files`);
                    }
                }
            }
        }
    } catch (e) {
        output.appendLine(`  LSP:  FAILED - ${e}`);
    }

    const grepCallerStdout = await runGrep([
        '-rn', `${TARGET_FUNCTION}(`,
        '--include', '*.py', '.',
    ]);
    const grepCallerLines = grepCallerStdout.trim().split('\n').filter(Boolean);
    grepCallers.count = grepCallerLines.length;
    grepCallers.files = [...new Set(grepCallerLines.map((l: string) => l.split(':')[0]))];
    output.appendLine(`  grep: ${grepCallerLines.length} matches from ${grepCallers.files.length} files`);

    results['get_callers'] = { lsp: lspCallers, grep: grepCallers };

    // ---- module_summary ----
    results['module_summary'] = { lsp: { success: true }, grep: { success: true } };

    // Save results
    // Save to workspace root (works regardless of which project is open)
    const outFile = path.resolve(workspace, 'tool_lsp_results.json');
    fs.writeFileSync(outFile, JSON.stringify(results, null, 2));
    output.appendLine(`\nResults saved to ${outFile}`);
    vscode.window.showInformationMessage(`Tool comparison complete. See Output panel for results.`);
}

/**
 * Extension deactivation handler.
 *
 * Called when the extension is deactivated (VS Code closing or extension disabled).
 * Performs cleanup of any resources.
 */
export function deactivate(): void {
    if (conductorDb) {
        conductorDb.close();
        conductorDb = null;
    }
    workspaceConfig = null;
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

    // Incremental workspace indexing (file watcher)
    /** Active VS Code FileSystemWatcher for hot index updates. */
    private _fileWatcher: vscode.FileSystemWatcher | null = null;
    /** Per-file debounce timers for file-change events. */
    private _fileSyncDebounces = new Map<string, ReturnType<typeof setTimeout>>();
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
                    // Stop all background indexing work before closing the session.
                    cancelCurrentIndex();
                    ragClient?.cancel();
                    ragClient = null;
                    if (this._ragBatchTimer) { clearTimeout(this._ragBatchTimer); this._ragBatchTimer = null; }
                    this._ragPendingChanges.clear();
                    this._stopFileWatcher();
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
                case 'diagnoseBackendConnection':
                    await this._handleDiagnoseBackendConnection(message);
                    return;
                case 'setAiModel':
                    console.log('[Conductor] Received setAiModel message from WebView:', message.modelId);
                    this._handleSetAiModel(message.modelId);
                    return;
                case 'setClassifier':
                    console.log('[Conductor] Received setClassifier message from WebView:', message.enabled, message.modelId);
                    this._handleSetClassifier(message.enabled, message.modelId);
                    return;
                case 'setExplorer':
                    console.log('[Conductor] Received setExplorer message from WebView:', message.enabled, message.modelId);
                    this._handleSetExplorer(message.enabled, message.modelId);
                    return;
                case 'setLiteLLMFallback':
                    console.log('[Conductor] Received setLiteLLMFallback message from WebView:', message.enabled);
                    this._handleSetLiteLLMFallback(message.enabled);
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
                case 'jiraCheckStatus':
                    await this._handleJiraCheckStatus();
                    return;
                case 'jiraConnect':
                    await this._handleJiraConnect();
                    return;
                case 'jiraDisconnect':
                    await this._handleJiraDisconnect();
                    return;
                case 'jiraCreateIssue':
                    await this._handleJiraCreateIssue(message);
                    return;
                case 'jiraGetIssueTypes':
                    await this._handleJiraGetIssueTypes(message.projectKey);
                    return;
                case 'jiraGetCreateMeta':
                    await this._handleJiraGetCreateMeta(message.projectKey, message.issueTypeId);
                    return;
                case 'openExternal':
                    if (message.url) {
                        vscode.env.openExternal(vscode.Uri.parse(message.url));
                    }
                    return;
                case 'shareStackTrace':
                    await this._handleShareStackTrace(message.rawText);
                    return;
                case 'shareTestOutput':
                    await this._handleShareTestOutput(message.rawText, message.framework);
                    return;
                case 'shareTestFailures':
                    await this._handleShareTestFailures();
                    return;
                case 'explainCode':
                    await this._handleExplainCode(message);
                    return;
                case 'createTodo':
                    await this._handleCreateTodo(message.roomId, message.todo);
                    return;
                case 'updateTodo':
                    await this._handleUpdateTodo(message.roomId, message.todoId, message.updates);
                    return;
                case 'loadTodos':
                    await this._handleLoadTodos(message.roomId);
                    return;
                case 'deleteTodo':
                    await this._handleDeleteTodo(message.roomId, message.todoId);
                    return;
                case 'scanWorkspaceTodos':
                    await this._handleScanWorkspaceTodos();
                    return;
                case 'updateWorkspaceTodo':
                    await this._handleUpdateWorkspaceTodo(message.payload as UpdateTodoPayload);
                    return;
                case 'loadHistory':
                    await this._handleLoadHistory(message.roomId, message.before, message.limit);
                    return;
                case 'rebuildIndex':
                    await this._handleRebuildIndex();
                    return;
                case 'fetchRemoteBranches':
                    await this._handleFetchRemoteBranches(message);
                    return;
                case 'setupWorkspaceAndIndex':
                    await this._handleSetupWorkspaceAndIndex(message);
                    return;
                case 'setupLocalWorkspace':
                    await this._handleSetupLocalWorkspace();
                    return;
                case 'tool_request':
                    // Backend requests a tool execution on local workspace
                    await this._handleLocalToolRequest(message);
                    return;
                case 'openConductorWorkspace':
                    this._handleOpenConductorWorkspace(message.roomId);
                    return;
                case 'explainCodeFromSnippet':
                    await this._handleExplainCodeFromSnippet(message);
                    return;
                case 'askAI':
                    await this._handleAskAI(message);
                    return;
                case 'showWorkflow':
                    vscode.commands.executeCommand('conductor.showWorkflow');
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

            // Initialize .conductor/ workspace storage + SQLite DB, then run
            // two-phase workspace indexing:
            //   Phase 1 (blocking ≤5s): fast file metadata scan
            //   Phase 2 (background):   symbol extraction + embedding
            const folders = vscode.workspace.workspaceFolders;
            console.log('[Conductor][StartSession] workspaceFolders:', folders?.length ?? 0,
                folders?.map(f => f.uri.fsPath));
            if (folders && folders.length > 0) {
                const wsRoot = folders[0].uri.fsPath;
                console.log('[Conductor][StartSession] Initializing workspace storage at:', wsRoot);
                initConductorWorkspaceStorage(wsRoot).then(async db => {
                    conductorDb    = db;
                    conductorWsRoot = wsRoot;

                    // Cancel any Phase 2 still running from a previous session.
                    cancelCurrentIndex();

                    // Load extension-side tuning from .conductor/config.json.
                    workspaceConfig = await loadWorkspaceConfig(wsRoot);

                    // --- Branch-aware + empty-index auto-scan logic ---
                    const currentBranch = _getGitBranch(wsRoot);
                    const indexedBranch = db.getMeta('indexed_branch');
                    const fileCount     = db.getFileCount();

                    const branchChanged = !!(currentBranch && indexedBranch && currentBranch !== indexedBranch);
                    const isEmpty       = fileCount === 0;

                    if (branchChanged) {
                        console.log(`[Conductor][StartSession] Branch changed: ${indexedBranch} → ${currentBranch} — hard-resetting index`);
                        // Hard-reset: close DB, delete files, reopen fresh.
                        conductorDb = db = await resetWorkspaceDb(wsRoot, db);
                        this._view?.webview.postMessage({
                            command: 'indexBranchChanged',
                            from: indexedBranch,
                            to: currentBranch,
                        });
                    } else if (isEmpty) {
                        console.log('[Conductor][StartSession] Empty index — running initial scan');
                    } else {
                        console.log(`[Conductor][StartSession] Incremental scan on branch=${currentBranch ?? 'unknown'} (${fileCount} files cached)`);
                    }

                    // Collect open editor files to process them first in Phase 2.
                    const priorityFiles = vscode.window.tabGroups.all
                        .flatMap(g => g.tabs)
                        .map(tab => (tab.input instanceof vscode.TabInputText) ? tab.input.uri.fsPath : null)
                        .filter((p): p is string => p !== null);

                    // [DISABLED] Start backend RAG reindex in parallel (non-blocking, best-effort).
                    // Temporarily disabled for debugging — trigger manually via Rebuild Index button.
                    // ragClient = new RagClient(getBackendUrl());
                    // this._sendWorkspaceToRag(wsRoot, roomId).catch(err => {
                    //     if (err instanceof Error && err.name === 'AbortError') return;
                    //     console.warn('[Conductor][StartSession] RAG reindex failed:', err);
                    // });

                    // Run two-phase indexing.  Phase 1 always runs (fast mtime diff).
                    // Phase 2 processes only stale files (AST-only mode).
                    const indexResult = await indexWorkspace(wsRoot, db, {
                        backendUrl:      getBackendUrl(),
                        phase1TimeoutMs: 5000,
                        priorityFiles,
                        onProgress: (p) => {
                            this._view?.webview.postMessage({ command: 'indexProgress', payload: p });
                        },
                    });

                    // Persist the current branch.
                    if (currentBranch) {
                        db.setMeta('indexed_branch', currentBranch);
                    }

                    console.log(
                        `[Conductor][StartSession] Index done: ${indexResult.filesScanned} files, ` +
                        `${indexResult.staleFilesCount} stale, ${indexResult.symbolsExtracted} symbols`,
                    );

                    // Start watching for file changes (hot incremental updates).
                    this._startFileWatcher(wsRoot);
                }).catch(err => {
                    console.error('[Conductor][StartSession] Workspace storage init FAILED:', err);
                    if (err instanceof Error && err.stack) {
                        console.error('[Conductor][StartSession] Stack:', err.stack);
                    }
                });
            } else {
                console.warn('[Conductor][StartSession] No workspace folders — skipping workspace storage init');
            }

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
                models: Array<{ id: string; provider: string; display_name: string; available: boolean; classifier: boolean; litellm: boolean }>;
                default_model: string;
                classifier_enabled: boolean;
                active_classifier: string | null;
                litellm_fallback: boolean;
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

    private async _handleDiagnoseBackendConnection(message: {
        requestId?: number;
        backendUrl?: string;
        hasConnectedBefore?: boolean;
        reconnectAttempts?: number;
        maxReconnectAttempts?: number;
    }): Promise<void> {
        const backendUrl = typeof message.backendUrl === 'string' && message.backendUrl.length > 0
            ? message.backendUrl
            : getBackendUrl();
        const reconnectAttempts = Number.isFinite(message.reconnectAttempts)
            ? Number(message.reconnectAttempts)
            : 0;
        const maxReconnectAttempts = Number.isFinite(message.maxReconnectAttempts)
            ? Math.max(1, Number(message.maxReconnectAttempts))
            : 10;
        const backendHealthy = await checkBackendHealth(backendUrl, { timeoutMs: 2500 });
        const diagnosis = diagnoseBackendConnection({
            backendUrl,
            backendHealthy,
            hasConnectedBefore: Boolean(message.hasConnectedBefore),
            reconnectAttempts,
            maxReconnectAttempts,
        });

        this._view?.webview.postMessage({
            command: 'backendConnectionDiagnosis',
            requestId: message.requestId,
            diagnosis,
        });
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
     * Handle request to set the classifier model and enable/disable state.
     */
    private async _handleSetClassifier(enabled: boolean, modelId: string | null): Promise<void> {
        try {
            console.log('[Conductor] Setting classifier:', { enabled, modelId });
            const response = await fetch(`${getBackendUrl()}/ai/classifier`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled, model_id: modelId }),
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Set classifier failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'setClassifierResult',
                    data: { error: `Failed to set classifier: ${response.status}` },
                });
                this._handleGetAiStatus();
                return;
            }

            const data = await response.json();
            console.log('[Conductor] Classifier set successfully:', data);
            this._view?.webview.postMessage({
                command: 'setClassifierResult',
                data,
            });
            this._handleGetAiStatus();
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to set classifier:', msg);
            this._view?.webview.postMessage({
                command: 'setClassifierResult',
                data: { error: `Cannot connect to backend: ${msg}` },
            });
            this._handleGetAiStatus();
        }
    }

    /**
     * Handle request to set the explorer (sub-agent) model and enable/disable state.
     */
    private async _handleSetExplorer(enabled: boolean, modelId: string | null): Promise<void> {
        try {
            console.log('[Conductor] Setting explorer:', { enabled, modelId });
            const response = await fetch(`${getBackendUrl()}/ai/explorer`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled, model_id: modelId }),
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Set explorer failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'setExplorerResult',
                    data: { error: `Failed to set explorer: ${response.status}` },
                });
                this._handleGetAiStatus();
                return;
            }

            const data = await response.json();
            console.log('[Conductor] Explorer set successfully:', data);
            this._view?.webview.postMessage({
                command: 'setExplorerResult',
                data,
            });
            this._handleGetAiStatus();
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to set explorer:', msg);
            this._view?.webview.postMessage({
                command: 'setExplorerResult',
                data: { error: `Cannot connect to backend: ${msg}` },
            });
            this._handleGetAiStatus();
        }
    }

    /**
     * Handle request to enable/disable LiteLLM fallback.
     */
    private async _handleSetLiteLLMFallback(enabled: boolean): Promise<void> {
        try {
            console.log('[Conductor] Setting LiteLLM fallback:', enabled);
            const response = await fetch(`${getBackendUrl()}/ai/litellm-fallback`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });

            if (!response.ok) {
                const errorText = await response.text();
                console.warn('[Conductor] Set LiteLLM fallback failed:', response.status, errorText);
                this._view?.webview.postMessage({
                    command: 'setLiteLLMFallbackResult',
                    data: { error: `Failed to set LiteLLM fallback: ${response.status}` },
                });
                this._handleGetAiStatus();
                return;
            }

            const data = await response.json();
            console.log('[Conductor] LiteLLM fallback set successfully:', data);
            this._view?.webview.postMessage({
                command: 'setLiteLLMFallbackResult',
                data,
            });
            this._handleGetAiStatus();
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to set LiteLLM fallback:', msg);
            this._view?.webview.postMessage({
                command: 'setLiteLLMFallbackResult',
                data: { error: `Cannot connect to backend: ${msg}` },
            });
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

    // =================================================================
    // Jira Integration Handlers
    // =================================================================

    private async _handleJiraCheckStatus(): Promise<void> {
        try {
            const resp = await fetch(`${getBackendUrl()}/api/integrations/jira/status`);
            const data = await resp.json() as { connected: boolean; site_url?: string };

            let projects: Array<{ id: string; key: string; name: string }> = [];
            if (data.connected) {
                try {
                    const projResp = await fetch(`${getBackendUrl()}/api/integrations/jira/projects`);
                    if (projResp.ok) {
                        projects = await projResp.json() as Array<{ id: string; key: string; name: string }>;
                    }
                } catch (e) {
                    console.warn('[Conductor] Failed to load Jira projects:', e);
                }
            }

            this._view?.webview.postMessage({
                command: 'jiraStatus',
                connected: data.connected,
                site_url: data.site_url || '',
                projects,
            });
        } catch (error) {
            console.error('[Conductor] Jira status check failed:', error);
            this._view?.webview.postMessage({
                command: 'jiraStatus',
                connected: false,
                site_url: '',
                projects: [],
            });
        }
    }

    private async _handleJiraConnect(): Promise<void> {
        try {
            const resp = await fetch(`${getBackendUrl()}/api/integrations/jira/authorize-url`);
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` })) as { detail?: string };
                this._view?.webview.postMessage({
                    command: 'jiraError',
                    error: err.detail || 'Failed to get authorize URL',
                });
                return;
            }
            const data = await resp.json() as { authorize_url: string; state: string };

            // Send URL to WebView (for copy fallback) and show auth state
            this._view?.webview.postMessage({
                command: 'jiraAuthRequired',
                authorizeUrl: data.authorize_url,
            });

            // Open in browser
            vscode.env.openExternal(vscode.Uri.parse(data.authorize_url));

            // Poll for connection status (the callback happens in the browser)
            let attempts = 0;
            const maxAttempts = 60; // 5 minutes with 5s interval
            const pollTimer = setInterval(async () => {
                attempts++;
                if (attempts > maxAttempts) {
                    clearInterval(pollTimer);
                    this._view?.webview.postMessage({
                        command: 'jiraError',
                        error: 'Jira connection timed out',
                    });
                    return;
                }
                try {
                    const statusResp = await fetch(`${getBackendUrl()}/api/integrations/jira/status`);
                    const status = await statusResp.json() as { connected: boolean; site_url?: string };
                    if (status.connected) {
                        clearInterval(pollTimer);
                        this._view?.webview.postMessage({
                            command: 'jiraConnected',
                            site_url: status.site_url || '',
                        });
                    }
                } catch (_e) {
                    // Keep polling
                }
            }, 5000);
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            this._view?.webview.postMessage({
                command: 'jiraError',
                error: `Failed to connect: ${msg}`,
            });
        }
    }

    private async _handleJiraDisconnect(): Promise<void> {
        try {
            await fetch(`${getBackendUrl()}/api/integrations/jira/disconnect`, { method: 'POST' });
            this._view?.webview.postMessage({ command: 'jiraDisconnected' });
        } catch (error) {
            console.error('[Conductor] Jira disconnect failed:', error);
        }
    }

    private async _handleJiraCreateIssue(message: {
        projectKey: string; summary: string; description: string; issueType: string;
        priority?: string; team?: string; components?: string[];
    }): Promise<void> {
        try {
            // Step 1: Check if we have a valid Jira connection
            const statusResp = await fetch(`${getBackendUrl()}/api/integrations/jira/status`);
            const status = await statusResp.json() as { connected: boolean };

            if (!status.connected) {
                // No valid token — get authorize URL, tell WebView, then open browser
                console.log('[Conductor] Jira not connected, triggering OAuth before create');
                const authResp = await fetch(`${getBackendUrl()}/api/integrations/jira/authorize-url`);
                if (!authResp.ok) {
                    this._view?.webview.postMessage({
                        command: 'jiraError',
                        error: 'Failed to get Jira authorize URL',
                    });
                    return;
                }
                const authData = await authResp.json() as { authorize_url: string; state: string };

                // Send URL + pending create to WebView (for copy fallback)
                this._view?.webview.postMessage({
                    command: 'jiraAuthRequired',
                    authorizeUrl: authData.authorize_url,
                    pendingCreate: {
                        projectKey: message.projectKey,
                        summary: message.summary,
                        description: message.description,
                        issueType: message.issueType,
                        priority: message.priority || '',
                        team: message.team || '',
                        components: message.components || [],
                    },
                });

                // Also try to open in browser
                vscode.env.openExternal(vscode.Uri.parse(authData.authorize_url));

                // Poll for connection
                let attempts = 0;
                const maxAttempts = 60;
                const pollTimer = setInterval(async () => {
                    attempts++;
                    if (attempts > maxAttempts) {
                        clearInterval(pollTimer);
                        this._view?.webview.postMessage({
                            command: 'jiraError',
                            error: 'Jira connection timed out',
                        });
                        return;
                    }
                    try {
                        const s = await fetch(`${getBackendUrl()}/api/integrations/jira/status`);
                        const st = await s.json() as { connected: boolean; site_url?: string };
                        if (st.connected) {
                            clearInterval(pollTimer);
                            this._view?.webview.postMessage({
                                command: 'jiraConnected',
                                site_url: st.site_url || '',
                            });
                        }
                    } catch (_e) { /* keep polling */ }
                }, 5000);
                return;
            }

            // Step 2: Connected — create the issue
            const resp = await fetch(`${getBackendUrl()}/api/integrations/jira/issues`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    project_key: message.projectKey,
                    summary: message.summary,
                    description: message.description,
                    issue_type: message.issueType,
                    priority: message.priority || '',
                    team: message.team || '',
                    components: message.components || [],
                }),
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` })) as { detail?: string };
                this._view?.webview.postMessage({
                    command: 'jiraError',
                    error: err.detail || `Failed to create issue (${resp.status})`,
                });
                return;
            }
            const data = await resp.json() as { key: string; browse_url: string };
            this._view?.webview.postMessage({
                command: 'jiraIssueCreated',
                key: data.key,
                browse_url: data.browse_url,
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            this._view?.webview.postMessage({
                command: 'jiraError',
                error: `Create issue failed: ${msg}`,
            });
        }
    }

    private async _handleJiraGetIssueTypes(projectKey: string): Promise<void> {
        try {
            const resp = await fetch(`${getBackendUrl()}/api/integrations/jira/issue-types?projectKey=${encodeURIComponent(projectKey)}`);
            if (resp.ok) {
                const types = await resp.json() as Array<{ id: string; name: string }>;
                this._view?.webview.postMessage({ command: 'jiraIssueTypes', types });
            }
        } catch (error) {
            console.warn('[Conductor] Failed to load issue types:', error);
        }
    }

    private async _handleJiraGetCreateMeta(projectKey: string, issueTypeId: string): Promise<void> {
        try {
            const resp = await fetch(
                `${getBackendUrl()}/api/integrations/jira/create-meta?projectKey=${encodeURIComponent(projectKey)}&issueTypeId=${encodeURIComponent(issueTypeId)}`
            );
            if (resp.ok) {
                const meta = await resp.json() as {
                    priorities: Array<{ id: string; name: string }>;
                    components: Array<{ id: string; name: string }>;
                    teams: Array<{ id: string; name: string }>;
                    team_field_key: string;
                };
                this._view?.webview.postMessage({ command: 'jiraCreateMeta', ...meta });
            }
        } catch (error) {
            console.warn('[Conductor] Failed to load create meta:', error);
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

    // -----------------------------------------------------------------------
    // Stack Trace Sharing
    // -----------------------------------------------------------------------

    /**
     * Parse a pasted stack trace, resolve file paths to workspace-relative
     * paths, and send the structured result back to the WebView for broadcast.
     */
    private async _handleShareStackTrace(rawText: string): Promise<void> {
        if (!rawText?.trim()) {
            this._view?.webview.postMessage({
                command: 'stackTraceResolved',
                error: 'Empty stack trace.',
            });
            return;
        }

        const parsed = parseStackTrace(rawText);
        await resolveFramePaths(parsed);

        this._view?.webview.postMessage({
            command: 'stackTraceResolved',
            parsed: {
                language: parsed.language,
                errorType: parsed.errorType,
                errorMessage: parsed.errorMessage,
                frames: parsed.frames,
                rawText: parsed.rawText,
            },
        });
        console.log(
            `[Conductor] Stack trace parsed: ${parsed.language} – ` +
            `${parsed.errorType}: ${parsed.frames.length} frames`,
        );
    }

    // -----------------------------------------------------------------------
    // Test Failure Sharing
    // -----------------------------------------------------------------------

    /**
     * Collect failing tests from VS Code's Test API and post them to chat.
     * Falls back gracefully if the Test API is not available.
     */
    private async _handleShareTestFailures(): Promise<void> {
        // The Test Results API (vscode.tests.testResults) was introduced in
        // VS Code 1.78. Use dynamic access to stay compatible with older hosts.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const testResults: any[] = (vscode.tests as any).testResults ?? [];

        if (!testResults || testResults.length === 0) {
            this._view?.webview.postMessage({
                command: 'testFailuresResolved',
                error: 'No test results found. Run tests first, or use "Paste Test Output" instead.',
            });
            return;
        }

        interface TestFailureItem {
            name: string;
            filePath?: string;
            lineNumber?: number;
            errorMessage: string;
            errorType: string;
        }

        const failedTests: TestFailureItem[] = [];
        const latestRun = testResults[0];

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        const collectFailed = (item: any) => {
            if (item.taskStates) {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                for (const taskState of item.taskStates as any[]) {
                    const isFailed = taskState.state === 3 /* vscode.TestResultState.Failed */
                        || String(taskState.state) === 'failed';
                    if (isFailed) {
                        const fileUri: vscode.Uri | undefined = item.uri;
                        const relativePath = fileUri
                            ? vscode.workspace.asRelativePath(fileUri) : undefined;
                        // eslint-disable-next-line @typescript-eslint/no-explicit-any
                        const messages: any[] = taskState.messages || [];
                        const errorMsg = messages.map((m: any) =>
                            typeof m.message === 'string' ? m.message
                                : (m.message?.value ?? ''),
                        ).join('\n');

                        failedTests.push({
                            name: item.label ?? '(unknown)',
                            filePath: relativePath,
                            lineNumber: item.range ? item.range.start.line + 1 : undefined,
                            errorMessage: errorMsg,
                            errorType: 'AssertionError',
                        });
                    }
                }
            }
            if (item.children) {
                // eslint-disable-next-line @typescript-eslint/no-explicit-any
                for (const child of item.children as any[]) {
                    collectFailed(child);
                }
            }
        };

        for (const item of (latestRun.results ?? []) as unknown[]) {
            collectFailed(item);
        }

        if (failedTests.length === 0) {
            this._view?.webview.postMessage({
                command: 'testFailuresResolved',
                error: 'No failing tests found in latest run.',
            });
            return;
        }

        this._view?.webview.postMessage({
            command: 'testFailuresResolved',
            testFailure: {
                framework: 'vscode',
                totalFailed: failedTests.length,
                tests: failedTests,
            },
        });
    }

    /**
     * Parse pasted test output text (pytest / Jest / Go / JUnit format)
     * and send structured test failure data to the WebView.
     */
    private async _handleShareTestOutput(
        rawText: string,
        framework: string = 'unknown',
    ): Promise<void> {
        if (!rawText?.trim()) {
            this._view?.webview.postMessage({
                command: 'testFailuresResolved',
                error: 'Empty test output.',
            });
            return;
        }

        const tests = _parseTestOutput(rawText, framework);
        this._view?.webview.postMessage({
            command: 'testFailuresResolved',
            testFailure: {
                framework,
                totalFailed: tests.length,
                tests,
                rawOutput: rawText,
            },
        });
    }

    // -----------------------------------------------------------------------
    // AI Code Explanation
    // -----------------------------------------------------------------------

    /**
     * Gather rich context around the selected code snippet and call the
     * backend /context/explain endpoint, then broadcast the explanation.
     */
    private async _handleExplainCode(message: {
        code: string;
        relativePath: string;
        startLine: number;
        endLine: number;
        language: string;
        roomId: string;
        question?: string;
    }): Promise<void> {
        const backendUrl = getBackendUrl();

        console.log('[Conductor][ExplainCode] === Starting explain code ===');
        console.log('[Conductor][ExplainCode] file:', message.relativePath,
            'lines:', message.startLine, '-', message.endLine,
            'lang:', message.language);
        console.log('[Conductor][ExplainCode] code length:', message.code.length, 'chars');
        console.log('[Conductor][ExplainCode] conductorDb:', conductorDb ? 'SET' : 'NULL');
        console.log('[Conductor][ExplainCode] workspaceConfig:', workspaceConfig ? JSON.stringify(workspaceConfig) : 'NULL');
        console.log('[Conductor][ExplainCode] backendUrl:', backendUrl);

        // Resolve the active editor URI and cursor position for LSP queries.
        // If there is no active editor we synthesise a position from the
        // 1-based line numbers in the message so the pipeline degrades gracefully.
        const editor   = vscode.window.activeTextEditor;
        const position = editor?.selection.active
            ?? new vscode.Position(Math.max(0, message.startLine - 1), 0);

        console.log('[Conductor][ExplainCode] activeEditor:', editor ? editor.document.uri.fsPath : 'NONE');
        console.log('[Conductor][ExplainCode] position:', position.line, ':', position.character);

        let fileUri: vscode.Uri | undefined = editor?.document.uri;
        if (!fileUri) {
            console.log('[Conductor][ExplainCode] No active editor, resolving via workspace folders...');
            // Fall back to resolving via workspace folders.
            for (const folder of vscode.workspace.workspaceFolders ?? []) {
                const candidate = vscode.Uri.joinPath(folder.uri, message.relativePath);
                console.log('[Conductor][ExplainCode] Trying:', candidate.fsPath);
                try {
                    await vscode.workspace.fs.stat(candidate);
                    fileUri = candidate;
                    break;
                } catch { /* try next */ }
            }
        }

        if (!fileUri) {
            console.warn('[Conductor][ExplainCode] Cannot locate file in workspace — aborting');
            this._view?.webview.postMessage({
                command: 'codeExplanationReady',
                error: 'Cannot locate file in workspace.',
            });
            return;
        }

        console.log('[Conductor][ExplainCode] Resolved fileUri:', fileUri.fsPath);

        try {
            // ---- Run the 8-stage pipeline --------------------------------
            console.log('[Conductor][ExplainCode] Launching 8-stage pipeline...');
            const result = await runExplainPipeline({
                uri:               fileUri,
                selectionPosition: position,
                relativePath:      message.relativePath,
                language:          message.language,
                code:              message.code,
                startLine:         message.startLine,
                endLine:           message.endLine,
                question:          message.question,
                backendUrl,
                workspaceId:       message.roomId,
                conductorDb,
                workspaceFolders:  [...(vscode.workspace.workspaceFolders ?? [])],
                workspaceConfig:   workspaceConfig  ?? undefined,
                onProgress:        (evt) => this._view?.webview.postMessage({ command: 'explainProgress', ...evt }),
            });

            console.log('[Conductor][ExplainCode] Pipeline returned:',
                'model=', result.model,
                'explanation=', result.explanation.length, 'chars',
                'xmlPrompt=', result.xmlPrompt.length, 'chars',
                'timings=', JSON.stringify(result.timings));

            // ---- Stage 8: render ----------------------------------------
            // Guard: if the pipeline returned an empty explanation, do NOT post
            // an empty message to chat — show an error instead.
            if (!result.explanation?.trim()) {
                console.warn('[Conductor][ExplainCode] Pipeline returned empty explanation — not posting');
                this._view?.webview.postMessage({
                    command: 'codeExplanationReady',
                    error: 'AI returned an empty explanation. Please try again.',
                });
                return;
            }

            // Post the explanation to the chat room via REST so all participants
            // receive it via the WebSocket broadcast.  The WebView MUST NOT
            // call fetch() directly — the VS Code CSP blocks external URLs.
            const aiData = JSON.stringify({
                code:         message.code,
                relativePath: message.relativePath,
                startLine:    message.startLine,
                endLine:      message.endLine,
                structured:   result.structured,
                thinking_steps: result.thinking_steps,
            });
            const postParams = new URLSearchParams({
                message_type: 'ai_explanation',
                model_name:   result.model || 'ai',
                content:      result.explanation,
                ai_data:      aiData,
            });
            const postUrl = `${backendUrl}/chat/${encodeURIComponent(message.roomId)}/ai-message?${postParams.toString()}`;
            console.log('[Conductor][ExplainCode] POSTing explanation to chat, url length:', postUrl.length);
            const postResponse = await fetch(postUrl, { method: 'POST' });
            if (!postResponse.ok) {
                console.warn('[Conductor][ExplainCode] POST to chat failed:', postResponse.status, await postResponse.text().catch(() => ''));
            } else {
                console.log('[Conductor][ExplainCode] POST to chat succeeded');
            }

            // Dismiss any loading indicator in the WebView.
            this._view?.webview.postMessage({ command: 'codeExplanationReady' });

        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor][ExplainCode] Pipeline FAILED:', msg);
            if (error instanceof Error && error.stack) {
                console.error('[Conductor][ExplainCode] Stack:', error.stack);
            }
            this._view?.webview.postMessage({
                command: 'codeExplanationReady',
                error: msg,
            });
        }
    }

    // -----------------------------------------------------------------------
    // Chat History
    // -----------------------------------------------------------------------

    /**
     * Fetch paginated chat history from the backend and relay it to the WebView.
     */
    private async _handleLoadHistory(roomId: string, before: number, limit: number): Promise<void> {
        try {
            const url = `${getBackendUrl()}/chat/${encodeURIComponent(roomId)}/history?before=${before}&limit=${limit}`;
            console.log('[Conductor] Loading history:', url);
            const response = await fetch(url);
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}`);
            }
            const data = await response.json() as { messages: unknown[]; hasMore: boolean };
            this._view?.webview.postMessage({
                command: 'historyLoaded',
                messages: data.messages,
                hasMore: data.hasMore,
            });
        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor] Failed to load history:', msg);
            this._view?.webview.postMessage({
                command: 'historyLoaded',
                messages: [],
                hasMore: false,
            });
        }
    }

    // -----------------------------------------------------------------------
    // TODO Tracking
    // -----------------------------------------------------------------------

    private async _handleCreateTodo(roomId: string, todo: Record<string, unknown>): Promise<void> {
        try {
            const response = await fetch(
                `${getBackendUrl()}/todos/${encodeURIComponent(roomId)}`,
                {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(todo),
                },
            );
            const data = await response.json();
            this._view?.webview.postMessage({ command: 'todoCreated', todo: data });
        } catch (e) {
            console.error('[Conductor] createTodo failed:', e);
        }
    }

    private async _handleUpdateTodo(
        roomId: string,
        todoId: string,
        updates: Record<string, unknown>,
    ): Promise<void> {
        try {
            const response = await fetch(
                `${getBackendUrl()}/todos/${encodeURIComponent(roomId)}/${encodeURIComponent(todoId)}`,
                {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(updates),
                },
            );
            const data = await response.json();
            this._view?.webview.postMessage({ command: 'todoUpdated', todo: data });
        } catch (e) {
            console.error('[Conductor] updateTodo failed:', e);
        }
    }

    private async _handleLoadTodos(roomId: string): Promise<void> {
        try {
            const response = await fetch(
                `${getBackendUrl()}/todos/${encodeURIComponent(roomId)}`,
            );
            const data = await response.json() as unknown[];
            this._view?.webview.postMessage({ command: 'todosLoaded', todos: data });
        } catch (e) {
            console.error('[Conductor] loadTodos failed:', e);
        }
    }

    private async _handleDeleteTodo(roomId: string, todoId: string): Promise<void> {
        try {
            await fetch(
                `${getBackendUrl()}/todos/${encodeURIComponent(roomId)}/${encodeURIComponent(todoId)}`,
                { method: 'DELETE' },
            );
            this._view?.webview.postMessage({ command: 'todoDeleted', todoId });
        } catch (e) {
            console.error('[Conductor] deleteTodo failed:', e);
        }
    }

    // -----------------------------------------------------------------------
    // Backend RAG integration
    // -----------------------------------------------------------------------

    /** Batch timer for collecting file changes before sending to RAG. */
    private _ragBatchTimer: ReturnType<typeof setTimeout> | null = null;
    /** Pending file changes to send to RAG. */
    private _ragPendingChanges: Map<string, RagFileChange> = new Map();

    /**
     * Scan workspace files and send them to the backend RAG for reindexing.
     * Called once at session start.
     */
    private async _sendWorkspaceToRag(wsRoot: string, workspaceId: string): Promise<void> {
        if (!ragClient) return;

        const SKIP = new Set([
            'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build',
            'out', 'target', '.git', '.conductor',
        ]);
        const EXTS = new Set(['.ts', '.tsx', '.js', '.jsx', '.py', '.java', '.go']);
        const MAX_FILE_SIZE = 100_000; // 100 KB

        const files: RagFileChange[] = [];
        const folders = vscode.workspace.workspaceFolders;
        if (!folders) return;

        // Use VS Code's findFiles for efficient workspace traversal
        const uris = await vscode.workspace.findFiles(
            '**/*.{ts,tsx,js,jsx,py,java,go}',
            '{**/node_modules/**,**/.venv/**,**/venv/**,**/__pycache__/**,**/dist/**,**/build/**,**/out/**,**/target/**,**/.git/**}',
        );

        for (const uri of uris) {
            const relPath = vscode.workspace.asRelativePath(uri, false);
            const firstSeg = relPath.split(/[\\/]/)[0];
            if (SKIP.has(firstSeg)) continue;

            try {
                const stat = await vscode.workspace.fs.stat(uri);
                if (stat.size > MAX_FILE_SIZE) continue;

                const doc = await vscode.workspace.openTextDocument(uri);
                files.push({
                    path: relPath,
                    content: doc.getText(),
                    action: 'upsert',
                });
            } catch {
                // Skip unreadable files
            }
        }

        if (files.length === 0) {
            console.log('[Conductor][RAG] No files found to reindex');
            return;
        }

        const totalBytes = files.reduce((sum, f) => sum + (f.content?.length ?? 0), 0);
        console.log(
            `[Conductor][RAG] Collected ${files.length} files ` +
            `(${(totalBytes / 1024).toFixed(0)} KB total, workspace=${workspaceId})`,
        );

        // Split into batches to avoid exceeding ngrok / proxy payload limits.
        const MAX_BATCH_BYTES = 1_500_000; // ~1.5 MB per request
        const batches: RagFileChange[][] = [];
        let currentBatch: RagFileChange[] = [];
        let currentSize = 0;
        for (const f of files) {
            const fSize = (f.content?.length ?? 0) + (f.path.length) + 30; // rough JSON overhead
            if (currentBatch.length > 0 && currentSize + fSize > MAX_BATCH_BYTES) {
                batches.push(currentBatch);
                currentBatch = [];
                currentSize = 0;
            }
            currentBatch.push(f);
            currentSize += fSize;
        }
        if (currentBatch.length > 0) batches.push(currentBatch);

        console.log(`[Conductor][RAG] Sending in ${batches.length} batches`);

        let totalAdded = 0;
        let totalRemoved = 0;
        for (let i = 0; i < batches.length; i++) {
            // Check if cancelled (ragClient nulled on session end).
            if (!ragClient) {
                console.log(`[Conductor][RAG] Cancelled at batch ${i + 1}/${batches.length}`);
                return;
            }
            const batch = batches[i];
            const batchBytes = batch.reduce((s, f) => s + (f.content?.length ?? 0), 0);
            console.log(
                `[Conductor][RAG] Batch ${i + 1}/${batches.length}: ` +
                `${batch.length} files (${(batchBytes / 1024).toFixed(0)} KB)`,
            );
            // First batch uses reindex (clears old data), rest use incremental index.
            const result = i === 0
                ? await ragClient.reindex(workspaceId, batch)
                : await ragClient.index(workspaceId, batch);
            totalAdded += result.chunks_added;
            totalRemoved += result.chunks_removed;
        }

        console.log(
            `[Conductor][RAG] Reindex complete: ${totalAdded} chunks added, ` +
            `${totalRemoved} removed, ${files.length} files processed`,
        );
    }

    /**
     * Queue a single file change for batched RAG indexing.
     * Changes are collected for 2 seconds before sending.
     */
    private _queueRagFileChange(relPath: string, absPath: string, wsRoot: string): void {
        if (!ragClient) return;

        const roomId = getSessionService().getRoomId();
        if (!roomId) return;

        // Read file content and queue the change
        const uri = vscode.Uri.file(absPath);
        vscode.workspace.openTextDocument(uri).then(doc => {
            this._ragPendingChanges.set(relPath, {
                path: relPath,
                content: doc.getText(),
                action: 'upsert',
            });

            // Reset the 2-second batch timer
            if (this._ragBatchTimer) clearTimeout(this._ragBatchTimer);
            this._ragBatchTimer = setTimeout(() => {
                this._flushRagBatch(roomId);
            }, 2000);
        }).then(undefined, err => {
            console.warn('[Conductor][RAG] Failed to read file for RAG:', err);
        });
    }

    /** Flush accumulated file changes to the backend RAG. */
    private _flushRagBatch(workspaceId: string): void {
        if (!ragClient || this._ragPendingChanges.size === 0) return;

        const files = Array.from(this._ragPendingChanges.values());
        this._ragPendingChanges.clear();
        this._ragBatchTimer = null;

        console.log(`[Conductor][RAG] Flushing ${files.length} file changes`);
        ragClient.index(workspaceId, files).then(result => {
            console.log(
                `[Conductor][RAG] Index update: ${result.chunks_added} added, ` +
                `${result.chunks_removed} removed`,
            );
        }).catch(err => {
            console.warn('[Conductor][RAG] Batch index failed:', err);
        });
    }

    // -----------------------------------------------------------------------
    // Incremental file watcher
    // -----------------------------------------------------------------------

    /**
     * Start a VS Code FileSystemWatcher for the workspace.
     * Each file change/create is debounced (300 ms) and triggers a single-file
     * reindex so the index stays fresh without periodic full scans.
     */
    private _startFileWatcher(wsRoot: string): void {
        this._stopFileWatcher();

        const pattern = new vscode.RelativePattern(
            wsRoot,
            '**/*.{ts,tsx,js,jsx,py,java}',
        );
        this._fileWatcher = vscode.workspace.createFileSystemWatcher(pattern);

        const schedule = (uri: vscode.Uri) => {
            if (!conductorDb) return;
            const existing = this._fileSyncDebounces.get(uri.fsPath);
            if (existing) clearTimeout(existing);
            const timer = setTimeout(() => {
                this._fileSyncDebounces.delete(uri.fsPath);
                this._reindexSingleFile(uri.fsPath, wsRoot);
            }, 300);
            this._fileSyncDebounces.set(uri.fsPath, timer);
        };

        this._fileWatcher.onDidChange(schedule);
        this._fileWatcher.onDidCreate(schedule);
        console.log('[Conductor] File watcher active for:', wsRoot);
    }

    private _stopFileWatcher(): void {
        this._fileWatcher?.dispose();
        this._fileWatcher = null;
        for (const t of this._fileSyncDebounces.values()) clearTimeout(t);
        this._fileSyncDebounces.clear();
    }

    private async _reindexSingleFile(absPath: string, wsRoot: string): Promise<void> {
        if (!conductorDb) return;
        const relPath = path.relative(wsRoot, absPath);
        // Skip paths inside ignored directories
        const firstSegment = relPath.split(/[\\/]/)[0];
        const SKIP = new Set([
            'node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build', 'out', 'target', '.git',
        ]);
        if (SKIP.has(firstSegment)) return;

        const count = await reindexSingleFile(wsRoot, absPath, conductorDb);

        console.log(`[Conductor][FileWatcher] Reindexed ${relPath} (${count} symbols)`);
        this._view?.webview.postMessage({ command: 'indexFileSynced', file: relPath, symbols: count });

        // Also queue for backend RAG indexing (batched)
        this._queueRagFileChange(relPath, absPath, wsRoot);
    }

    /**
     * Handle "Explain with AI" clicked directly on a code-snippet message.
     *
     * The code, file path, and line range are already known from the snippet,
     * so we don't need to ask the editor for a selection.  The user's optional
     * question becomes the pipeline question.
     */
    private async _handleExplainCodeFromSnippet(message: {
        code:          string;
        relativePath:  string;
        startLine:     number;
        endLine:       number;
        language?:     string;
        roomId:        string;
        question?:     string;
    }): Promise<void> {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            this._view?.webview.postMessage({
                command: 'explainCodeFromSnippetDone',
                success: false,
                error: 'No workspace folder open',
            });
            return;
        }

        const language = message.language || _langFromPath(message.relativePath);

        // Reconstruct the file URI (best-effort — falls back to first folder if file is absent)
        let fileUri = vscode.Uri.joinPath(folders[0].uri, message.relativePath);
        for (const folder of folders) {
            const candidate = vscode.Uri.joinPath(folder.uri, message.relativePath);
            try {
                await vscode.workspace.fs.stat(candidate);
                fileUri = candidate;
                break;
            } catch { /* try next folder */ }
        }

        const position = new vscode.Position(Math.max(0, message.startLine - 1), 0);
        const backendUrl = getBackendUrl();

        try {
            const result = await runExplainPipeline({
                uri:               fileUri,
                selectionPosition: position,
                relativePath:      message.relativePath,
                language,
                code:              message.code,
                startLine:         message.startLine,
                endLine:           message.endLine,
                question:          message.question ||
                    `Describe this ${language} code: what it does, its inputs and outputs, ` +
                    `the business scenario it serves, and any key dependencies or side-effects.`,
                backendUrl,
                workspaceId:       message.roomId,
                conductorDb,
                workspaceFolders:  [...folders],
                workspaceConfig:   workspaceConfig  ?? undefined,
                onProgress:        (evt) => this._view?.webview.postMessage({ command: 'explainProgress', ...evt }),
            });

            // Guard: if the pipeline returned an empty explanation, do NOT post
            // an empty message to chat — show an error instead.
            if (!result.explanation?.trim()) {
                console.warn('[Conductor][ExplainFromSnippet] Pipeline returned empty explanation — not posting');
                this._view?.webview.postMessage({
                    command: 'explainCodeFromSnippetDone',
                    success: false,
                    error: 'AI returned an empty explanation. Please try again.',
                });
                return;
            }

            // Broadcast the explanation as an AI message in the room.
            // The endpoint uses Query params (same contract as _handleExplainCode).
            const aiData = JSON.stringify({
                code:         message.code,
                relativePath: message.relativePath,
                startLine:    message.startLine,
                endLine:      message.endLine,
                language,
                model:        result.model,
                structured:   result.structured,
                thinking_steps: result.thinking_steps,
            });
            const postParams = new URLSearchParams({
                message_type: 'ai_explanation',
                model_name:   result.model || 'ai',
                content:      result.explanation,
                ai_data:      aiData,
            });
            const aiMsgUrl = `${backendUrl}/chat/${encodeURIComponent(message.roomId)}/ai-message?${postParams.toString()}`;
            const postResponse = await fetch(aiMsgUrl, { method: 'POST' });
            if (!postResponse.ok) {
                console.warn('[Conductor][ExplainFromSnippet] POST to chat failed:', postResponse.status);
            }

            this._view?.webview.postMessage({ command: 'explainCodeFromSnippetDone', success: true });
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            console.error('[Conductor][ExplainFromSnippet] Failed:', msg);
            this._view?.webview.postMessage({
                command: 'explainCodeFromSnippetDone',
                success: false,
                error:   msg,
            });
        }
    }

    // -------------------------------------------------------------------
    // Ask AI (@AI in chat)
    // -------------------------------------------------------------------

    private async _handleAskAI(message: { roomId: string; query: string }): Promise<void> {
        const backendUrl = getBackendUrl();
        const roomId = message.roomId;
        const query = message.query;

        console.log(`[Conductor][AskAI] query="${query.slice(0, 80)}" room=${roomId}`);

        try {
            // Stream from the existing /api/context/query/stream endpoint
            const streamUrl = `${backendUrl}/api/context/query/stream`;
            const abortController = new AbortController();
            const sseTimeoutMs = 10 * 60 * 1000; // 10 minutes (multi-agent code review can take 5-8 min)
            const sseTimeoutId = setTimeout(() => abortController.abort(), sseTimeoutMs);
            const response = await fetch(streamUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    room_id: roomId,
                    query: query,
                    max_iterations: 15,
                }),
                signal: abortController.signal,
            });

            if (!response.ok) {
                const body = await response.text().catch(() => '');
                throw new Error(`HTTP ${response.status}: ${body}`);
            }

            // Parse SSE events
            const reader = response.body!.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let finalAnswer = '';
            let thinkingSteps: Array<Record<string, any>> = [];

            // eslint-disable-next-line no-constant-condition
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                const parts = buffer.split('\n\n');
                buffer = parts.pop()!;

                for (const part of parts) {
                    const lines = part.split('\n');
                    let eventKind = '';
                    let eventData = '';
                    for (const line of lines) {
                        if (line.startsWith('event: '))     eventKind = line.slice(7);
                        else if (line.startsWith('data: ')) eventData = line.slice(6);
                    }
                    if (!eventKind || !eventData) continue;

                    let data: Record<string, any>;
                    try { data = JSON.parse(eventData); } catch { continue; }

                    // Forward progress to WebView
                    if (eventKind === 'start') {
                        this._view?.webview.postMessage({
                            command: 'askAIProgress',
                            phase: 'agent', kind: 'start',
                            message: 'Connecting to AI...',
                            detail: data,
                        });
                    } else if (eventKind === 'classify') {
                        const qType = (data.query_type || data.result?.best_route || '').replace(/_/g, ' ');
                        this._view?.webview.postMessage({
                            command: 'askAIProgress',
                            phase: 'agent', kind: 'classify',
                            message: qType ? `Analyzing: ${qType}` : 'Classifying query...',
                            detail: data,
                        });
                    } else if (eventKind === 'thinking') {
                        const text = (data.text as string || '').slice(0, 120);
                        this._view?.webview.postMessage({
                            command: 'askAIProgress',
                            phase: 'agent', kind: 'thinking',
                            message: text || 'Thinking...',
                            detail: data,
                        });
                    } else if (eventKind === 'tool_call') {
                        this._view?.webview.postMessage({
                            command: 'askAIProgress',
                            phase: 'agent', kind: 'tool_call',
                            message: data.tool ? `${data.tool}` : 'Working...',
                            detail: { tool: data.tool, iteration: data.iteration },
                        });
                    } else if (eventKind === 'tool_result') {
                        this._view?.webview.postMessage({
                            command: 'askAIProgress',
                            phase: 'agent', kind: 'tool_result',
                            message: `${data.tool}: ${data.summary || 'done'}`,
                            detail: { tool: data.tool, success: data.success, iteration: data.iteration },
                        });
                    } else if (eventKind === 'done') {
                        finalAnswer = data.answer || '';
                        if (data.thinking_steps) {
                            thinkingSteps = data.thinking_steps;
                        }
                    } else if (eventKind === 'error') {
                        finalAnswer = data.answer || '';
                        if (data.thinking_steps) {
                            thinkingSteps = data.thinking_steps;
                        }
                        if (data.error) {
                            console.error(`[Conductor][AskAI] Agent error: ${data.error}`);
                        }
                    }
                }
            }

            clearTimeout(sseTimeoutId);
            console.log(`[Conductor][AskAI] Stream complete — answer=${finalAnswer.length} chars`);

            // Guard: don't post empty answers
            if (!finalAnswer.trim()) {
                console.warn('[Conductor][AskAI] Agent returned empty answer — not posting');
                this._view?.webview.postMessage({ command: 'askAIDone', error: 'AI returned an empty answer. Please try again.' });
                return;
            }

            // Post the AI answer to the chat room
            let modelName = 'ai';
            try {
                const statusResp = await fetch(`${backendUrl}/ai/status`);
                if (statusResp.ok) {
                    const statusData = await statusResp.json() as Record<string, string>;
                    modelName = statusData.active_model || statusData.model_id || 'ai';
                }
            } catch { /* ignore */ }

            const aiData = JSON.stringify({ query, thinking_steps: thinkingSteps });
            const postParams = new URLSearchParams({
                message_type: 'ai_answer',
                model_name: modelName,
                content: finalAnswer,
                ai_data: aiData,
            });
            const postUrl = `${backendUrl}/chat/${encodeURIComponent(roomId)}/ai-message?${postParams.toString()}`;
            const postResponse = await fetch(postUrl, { method: 'POST' });
            if (!postResponse.ok) {
                console.warn('[Conductor][AskAI] POST to chat failed:', postResponse.status);
            }

            this._view?.webview.postMessage({ command: 'askAIDone' });

        } catch (error) {
            const msg = error instanceof Error ? error.message : String(error);
            console.error('[Conductor][AskAI] Failed:', msg);
            this._view?.webview.postMessage({
                command: 'askAIDone',
                error: msg,
            });
        }
    }

    private async _handleRebuildIndex(): Promise<void> {
        const folders = vscode.workspace.workspaceFolders;
        if (!conductorDb || !folders || folders.length === 0) {
            this._view?.webview.postMessage({
                command: 'indexRebuildComplete',
                success: false,
                error: 'No workspace or database available',
            });
            return;
        }

        const wsRoot = conductorWsRoot ?? folders[0].uri.fsPath;

        try {
            // 1. Stop all running indexing tasks.
            cancelCurrentIndex();
            this._stopFileWatcher();

            // 2. Hard-reset: close DB, delete cache.db* + vectors/, reopen fresh.
            conductorDb = await resetWorkspaceDb(wsRoot, conductorDb);
            // Also clear the repo graph index so it gets rebuilt on next agent query.
            const { clearRepoGraph } = require('./services/repoGraphBuilder');
            clearRepoGraph(wsRoot);
            console.log('[Conductor][RebuildIndex] Workspace hard-reset complete (repo graph cleared)');

            // 3. Re-index workspace from scratch (AST-only mode).
            const indexResult = await indexWorkspace(wsRoot, conductorDb, {
                backendUrl:      getBackendUrl(),
                phase1TimeoutMs: 5000,
                onProgress: (p) => {
                    this._view?.webview.postMessage({ command: 'indexProgress', payload: p });
                },
            });

            // 4. Persist current branch and restart file watcher.
            const currentBranch = _getGitBranch(wsRoot);
            if (currentBranch) {
                conductorDb.setMeta('indexed_branch', currentBranch);
            }
            this._startFileWatcher(wsRoot);

            console.log(
                `[Conductor][RebuildIndex] Done: ${indexResult.filesScanned} files, ` +
                `${indexResult.staleFilesCount} stale, ${indexResult.symbolsExtracted} symbols`,
            );

            // 6. Invalidate backend-side symbol + graph caches (best-effort).
            const roomId = getSessionService().getRoomId();
            if (roomId) {
                const backendUrl = getBackendUrl();
                fetch(`${backendUrl}/api/code-tools/cache/invalidate?room_id=${encodeURIComponent(roomId)}`, {
                    method: 'POST',
                }).then(r => {
                    if (r.ok) {
                        console.log('[Conductor][RebuildIndex] Backend caches invalidated');
                    } else {
                        console.warn('[Conductor][RebuildIndex] Backend cache invalidation failed:', r.status);
                    }
                }).catch(err => {
                    console.warn('[Conductor][RebuildIndex] Backend cache invalidation failed:', err);
                });
            }

            this._view?.webview.postMessage({
                command: 'indexRebuildComplete',
                success: true,
            });
        } catch (e) {
            const msg = e instanceof Error ? e.message : String(e);
            console.error('[Conductor][RebuildIndex] Failed:', msg);
            this._view?.webview.postMessage({
                command: 'indexRebuildComplete',
                success: false,
                error: msg,
            });
        }
    }

    private async _handleFetchRemoteBranches(message: any): Promise<void> {
        try {
            const backendUrl = getBackendUrl();
            const resp = await fetch(`${backendUrl}/api/git-workspace/branches/remote`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    repo_url: message.repoUrl,
                    credentials: message.token ? { token: message.token } : null,
                }),
            });
            const data = await resp.json() as any;
            if (!resp.ok) {
                throw new Error(data.detail || `HTTP ${resp.status}`);
            }
            this._view?.webview.postMessage({
                command: 'remoteBranchesLoaded',
                branches: data.branches,
                defaultBranch: data.default_branch,
            });
        } catch (e) {
            this._view?.webview.postMessage({
                command: 'remoteBranchesLoaded',
                branches: [],
                error: e instanceof Error ? e.message : String(e),
            });
        }
    }

    private async _handleSetupWorkspaceAndIndex(message: any): Promise<void> {
        try {
            const backendUrl = getBackendUrl();
            const roomId = getSessionService().getRoomId();

            // --- 1. Kick off clone (returns immediately) ---
            this._view?.webview.postMessage({
                command: 'setupAndIndexProgress',
                phase: 'cloning',
                detail: 'Starting clone...',
            });

            const resp = await fetch(`${backendUrl}/api/git-workspace/workspaces/setup-and-index`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    room_id: roomId,
                    repo_url: message.repoUrl,
                    source_branch: message.sourceBranch,
                    working_branch: message.workingBranch || null,
                    credentials: message.token ? { token: message.token } : null,
                    auto_index: false, // we'll index explicitly after ready
                }),
            });

            if (!resp.ok) {
                const err = await resp.text();
                throw new Error(`Setup failed (${resp.status}): ${err}`);
            }

            // --- 2. Poll for clone progress until ready ---
            const workspace = await this._pollWorkspaceReady(backendUrl, roomId);

            if (!workspace || workspace.status !== 'ready') {
                const detail = workspace?.error_detail || workspace?.status || 'unknown';
                this._view?.webview.postMessage({
                    command: 'setupAndIndexComplete',
                    success: false,
                    message: `Workspace setup failed: ${detail}`,
                    workspace,
                });
                return;
            }

            // --- 3. Trigger indexing (non-blocking) + poll progress ---
            this._view?.webview.postMessage({
                command: 'setupAndIndexProgress',
                phase: 'indexing',
                detail: 'Indexing code for search...',
                percent: 0,
            });

            // Fire off the index request (blocks on server until done)
            // Track whether the fetch itself failed (e.g. backend restart)
            let indexFetchFailed = false;
            const indexPromise = fetch(
                `${backendUrl}/api/git-workspace/workspaces/${encodeURIComponent(roomId)}/index`,
                { method: 'POST' },
            ).then(r => {
                if (!r.ok) { indexFetchFailed = true; return {}; }
                return r.json();
            }).catch(() => { indexFetchFailed = true; return {}; });

            // Poll progress while indexing runs
            const progressUrl = `${backendUrl}/api/context/context/${encodeURIComponent(roomId)}/index-progress`;
            let indexResult: any = {};
            const pollInterval = 3000; // 3 seconds
            while (true) {
                // Check if indexing finished
                const raceResult = await Promise.race([
                    indexPromise.then(r => ({ done: true as const, result: r })),
                    new Promise<{ done: false }>(r => setTimeout(() => r({ done: false }), pollInterval)),
                ]);

                if (raceResult.done) {
                    indexResult = raceResult.result;
                    break;
                }

                // Poll progress
                try {
                    const progResp = await fetch(progressUrl);
                    if (progResp.ok) {
                        const prog = await progResp.json() as any;
                        this._view?.webview.postMessage({
                            command: 'setupAndIndexProgress',
                            phase: 'indexing',
                            detail: `Indexing code for search... ${prog.indexed_files}/${prog.total_files} files (${prog.indexed_chunks} chunks)`,
                            percent: prog.percent ?? 0,
                        });
                    }
                } catch {
                    // Progress endpoint not ready yet — keep waiting
                }
            }

            // --- 4. Notify webview (don't auto-mount — it reloads the window) ---
            const indexSuccess = !indexFetchFailed && (indexResult.index_success ?? true);
            this._view?.webview.postMessage({
                command: 'setupAndIndexComplete',
                success: indexSuccess,
                filesIndexed: indexResult.files_indexed ?? 0,
                chunksIndexed: indexResult.chunks_indexed ?? 0,
                durationMs: indexResult.index_duration_ms ?? 0,
                message: indexFetchFailed
                    ? 'Backend restarted during indexing. Please re-trigger indexing.'
                    : (indexResult.message || 'Workspace ready'),
                workspace,
                roomId,
            });
        } catch (e) {
            this._view?.webview.postMessage({
                command: 'setupAndIndexComplete',
                success: false,
                message: e instanceof Error ? e.message : String(e),
            });
        }
    }

    /**
     * Handle "Use Local Workspace" — register the current VS Code workspace
     * folder with the backend so code tools can operate on it directly.
     * No git clone is performed; guests get read-only access.
     */
    private async _handleSetupLocalWorkspace(): Promise<void> {
        const folders = vscode.workspace.workspaceFolders;
        if (!folders || folders.length === 0) {
            this._view?.webview.postMessage({
                command: 'setupAndIndexComplete',
                success: false,
                message: 'No workspace folder open in VS Code.',
            });
            return;
        }

        const wsRoot = folders[0].uri.fsPath;
        const backendUrl = getBackendUrl();
        const roomId = getSessionService().getRoomId();

        try {
            this._view?.webview.postMessage({
                command: 'setupAndIndexProgress',
                phase: 'registering',
                detail: 'Registering local workspace...',
            });

            const resp = await fetch(`${backendUrl}/api/git-workspace/workspaces/local`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    room_id: roomId,
                    local_path: wsRoot,
                }),
            });

            if (!resp.ok) {
                const err = await resp.text();
                throw new Error(`Registration failed (${resp.status}): ${err}`);
            }

            this._view?.webview.postMessage({
                command: 'setupAndIndexComplete',
                success: true,
                message: `Local workspace registered: ${wsRoot}`,
                roomId,
                isLocal: true,
            });
        } catch (e) {
            this._view?.webview.postMessage({
                command: 'setupAndIndexComplete',
                success: false,
                message: e instanceof Error ? e.message : String(e),
            });
        }
    }

    /**
     * Execute a tool request from the backend on the local workspace.
     *
     * The backend sends tool_request messages via WebSocket when the room
     * uses a local workspace.  The extension executes the tool locally
     * (subprocess for grep/git/find, fs for read/write) and sends the
     * result back via the webview → WebSocket → backend.
     */
    private async _handleLocalToolRequest(message: any): Promise<void> {
        const { requestId, tool, params, workspace } = message;
        try {
            // Use the three-tier dispatcher: AST tools → Python CLI → subprocess fallback
            const { executeLocalTool: dispatchTool } = require('./services/localToolDispatcher');
            const astRunner = require('./services/astToolRunner');

            // Tier 2 AST runner: routes to the appropriate astToolRunner function
            const astRunnerFn = async (t: string, p: any, w: string, _lsp: any) => {
                const fn = (astRunner as any)[t];
                if (typeof fn !== 'function') { return null; }
                const res = await fn(w, p);
                return res;
            };

            const result = await dispatchTool(
                tool,
                params,
                workspace,
                {
                    extensionPath: this._extensionUri?.fsPath || '',
                    lspHelpers: null,
                },
                // Subprocess fallback: the original _executeLocalTool
                (t: string, p: any, w: string) => this._executeLocalTool(t, p, w),
                // AST runner
                astRunnerFn,
            );
            this._view?.webview.postMessage({
                command: 'tool_response',
                requestId,
                tool,
                success: result.success,
                data: result.data,
                error: result.error || null,
                truncated: result.truncated || false,
            });
        } catch (e) {
            this._view?.webview.postMessage({
                command: 'tool_response',
                requestId,
                tool,
                success: false,
                data: null,
                error: e instanceof Error ? e.message : String(e),
                truncated: false,
            });
        }
    }

    /**
     * Execute a code tool locally using VS Code Language Services first,
     * falling back to subprocess/filesystem when LSP is unavailable.
     */
    private async _executeLocalTool(
        tool: string, params: any, workspace: string
    ): Promise<{ success: boolean; data: any; error?: string; truncated?: boolean }> {
        const { execFile } = require('child_process');
        const { promisify } = require('util');
        const execFileAsync = promisify(execFile);
        const fs = require('fs');
        const path = require('path');

        const maxOutput = 200_000; // 200KB output cap

        // Helper: run a command and capture stdout
        const run = async (
            cmd: string, args: string[], cwd?: string, timeout = 30_000
        ): Promise<{ stdout: string; stderr: string }> => {
            try {
                const result = await execFileAsync(cmd, args, {
                    cwd: cwd || workspace,
                    maxBuffer: 10 * 1024 * 1024, // 10MB
                    timeout,
                });
                return { stdout: result.stdout || '', stderr: result.stderr || '' };
            } catch (e: any) {
                // grep returns exit code 1 for no matches — not an error
                if (e.code === 1 && e.stdout !== undefined) {
                    return { stdout: e.stdout || '', stderr: e.stderr || '' };
                }
                throw e;
            }
        };

        // ---- VS Code Language Service helpers ----

        const toUri = (filePath: string) => vscode.Uri.file(path.resolve(workspace, filePath));
        const toRelative = (fsPath: string): string =>
            path.relative(workspace, fsPath);

        /** Get Document Symbols for a file via VS Code LSP. Returns null if unavailable. */
        const getDocumentSymbols = async (filePath: string): Promise<vscode.DocumentSymbol[] | null> => {
            try {
                const symbols = await vscode.commands.executeCommand<vscode.DocumentSymbol[]>(
                    'vscode.executeDocumentSymbolProvider', toUri(filePath),
                );
                return (symbols && symbols.length > 0) ? symbols : null;
            } catch { return null; }
        };

        /** Symbol kinds that are meaningful for code navigation (matches tree-sitter output).
         *  Excludes Variable, Constant, Field, Property — these are parameters/locals
         *  that Pylance/TS LSP reports but tree-sitter's file_outline omits. */
        const OUTLINE_KINDS = new Set([
            vscode.SymbolKind.Class,
            vscode.SymbolKind.Function,
            vscode.SymbolKind.Method,
            vscode.SymbolKind.Constructor,
            vscode.SymbolKind.Interface,
            vscode.SymbolKind.Enum,
            vscode.SymbolKind.Struct,
            vscode.SymbolKind.Module,
            vscode.SymbolKind.Namespace,
        ]);

        /** Flatten a DocumentSymbol tree, keeping only structural symbols.
         *  Filters out local variables, parameters, and deeply nested symbols
         *  to match tree-sitter's file_outline output (classes + their methods). */
        const flattenSymbols = (
            symbols: vscode.DocumentSymbol[], parent?: string, depth: number = 0
        ): Array<{name: string; kind: string; line: number; endLine: number; parent?: string; detail?: string}> => {
            const result: Array<{name: string; kind: string; line: number; endLine: number; parent?: string; detail?: string}> = [];
            for (const s of symbols) {
                const kind = vscode.SymbolKind[s.kind] || 'Unknown';
                // Keep: top-level symbols (depth 0) or methods/functions inside a class (depth 1)
                // Skip: parameters, variables, and anything deeper than depth 1
                const isStructural = OUTLINE_KINDS.has(s.kind);
                const isRelevantDepth = depth <= 1;
                if (isStructural && isRelevantDepth) {
                    result.push({
                        name: s.name,
                        kind,
                        line: s.range.start.line + 1,
                        endLine: s.range.end.line + 1,
                        parent,
                        detail: s.detail || undefined,
                    });
                }
                // Only recurse into classes/modules/namespaces (not into functions)
                if (s.children && s.children.length > 0 &&
                    (s.kind === vscode.SymbolKind.Class ||
                     s.kind === vscode.SymbolKind.Module ||
                     s.kind === vscode.SymbolKind.Namespace ||
                     s.kind === vscode.SymbolKind.Enum ||
                     s.kind === vscode.SymbolKind.Interface)) {
                    result.push(...flattenSymbols(s.children, s.name, depth + 1));
                }
            }
            return result;
        };

        /** Find references via VS Code LSP. Returns null if unavailable. */
        const findReferencesLsp = async (
            filePath: string, line: number, character: number
        ): Promise<vscode.Location[] | null> => {
            try {
                const refs = await vscode.commands.executeCommand<vscode.Location[]>(
                    'vscode.executeReferenceProvider',
                    toUri(filePath), new vscode.Position(line, character),
                );
                return (refs && refs.length > 0) ? refs : null;
            } catch { return null; }
        };

        /** Get Call Hierarchy items at a position. */
        const prepareCallHierarchy = async (
            filePath: string, line: number, character: number
        ): Promise<vscode.CallHierarchyItem[] | null> => {
            try {
                const items = await vscode.commands.executeCommand<vscode.CallHierarchyItem[]>(
                    'vscode.prepareCallHierarchy',
                    toUri(filePath), new vscode.Position(line, character),
                );
                return (items && items.length > 0) ? items : null;
            } catch { return null; }
        };

        switch (tool) {
            // ---- Repo graph (lazy-built index) ----
            case 'get_repo_graph': {
                const { getOrBuildRepoGraph } = require('./services/repoGraphBuilder');
                const forceRebuild = params.force_rebuild || false;
                const graph = await getOrBuildRepoGraph(workspace, forceRebuild);
                if (!graph) {
                    return { success: true, data: { files: {}, stats: { total_files: 0, total_definitions: 0, total_references: 0 } } };
                }
                return { success: true, data: graph };
            }

            // ---- File operations ----
            case 'read_file': {
                const rawPath = params.path || params.file_path || params.file;
                if (!rawPath) { return { success: false, data: null, error: 'read_file requires a file path' }; }
                const filePath = path.resolve(workspace, rawPath);
                if (!filePath.startsWith(workspace)) {
                    return { success: false, data: null, error: 'Path traversal blocked' };
                }
                const content = fs.readFileSync(filePath, 'utf-8');
                const lines = content.split('\n');
                const start = (params.start_line || 1) - 1;
                const end = params.end_line || lines.length;
                const slice = lines.slice(start, end).join('\n');
                return {
                    success: true,
                    data: { content: slice, total_lines: lines.length, path: params.path || params.file_path },
                    truncated: slice.length > maxOutput,
                };
            }

            case 'list_files': {
                const target = params.directory || params.path || '.';
                const depth = params.max_depth || 3;
                const includeGlob = params.include_glob || '';
                const findArgs = [
                    target, '-maxdepth', String(depth),
                    '-type', 'f',
                    '-not', '-path', '*/.git/*',
                    '-not', '-path', '*/node_modules/*',
                    '-not', '-path', '*/__pycache__/*',
                ];
                if (includeGlob) {
                    findArgs.push('-name', includeGlob);
                }
                const { stdout } = await run('find', findArgs);
                const files = stdout.trim().split('\n').filter(Boolean);
                return { success: true, data: { files, count: files.length } };
            }

            case 'grep': {
                const pattern = params.pattern;
                if (!pattern) { return { success: false, data: null, error: 'grep requires a pattern' }; }
                const grepPath = params.path || '.';
                const maxResults = String(params.max_results || 50);
                const includeGlob = params.include_glob || params.include || '';

                // Prefer ripgrep (rg) — same as the Python backend
                let stdout: string;
                try {
                    const rgArgs = [
                        '-n', '--no-heading', '--with-filename',
                        '-m', maxResults,
                    ];
                    if (includeGlob) {
                        rgArgs.push('--glob', includeGlob);
                    }
                    rgArgs.push('--', pattern, grepPath);
                    const result = await run('rg', rgArgs);
                    stdout = result.stdout;
                } catch {
                    // Fallback to system grep if rg is not installed
                    const grepArgs = ['-rn'];
                    if (includeGlob) {
                        grepArgs.push('--include', includeGlob);
                    }
                    grepArgs.push('-m', maxResults, '--', pattern, grepPath);
                    const result = await run('grep', grepArgs);
                    stdout = result.stdout;
                }

                const matches = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return {
                        file: parts[0],
                        line: parseInt(parts[1]) || 0,
                        content: parts.slice(2).join(':'),
                    };
                });
                return {
                    success: true,
                    data: { matches, count: matches.length, pattern },
                    truncated: stdout.length > maxOutput,
                };
            }

            case 'find_symbol': {
                const symbol = params.name || params.symbol;
                const kindFilter = params.kind ? params.kind.toLowerCase() : '';
                // Try VS Code Workspace Symbol Provider first
                try {
                    const wsSymbols = await vscode.commands.executeCommand<vscode.SymbolInformation[]>(
                        'vscode.executeWorkspaceSymbolProvider', symbol,
                    );
                    if (wsSymbols && wsSymbols.length > 0) {
                        let matches = wsSymbols
                            .filter(s => s.location.uri.fsPath.startsWith(workspace))
                            .map(s => ({
                                file: toRelative(s.location.uri.fsPath),
                                line: s.location.range.start.line + 1,
                                content: `${vscode.SymbolKind[s.kind]} ${s.name}`,
                                kind: vscode.SymbolKind[s.kind].toLowerCase(),
                            }));
                        if (kindFilter) {
                            matches = matches.filter(m => m.kind === kindFilter);
                        }
                        matches = matches.slice(0, 50);
                        if (matches.length > 0) {
                            return { success: true, data: { matches, count: matches.length, symbol, source: 'lsp' } };
                        }
                    }
                } catch {}
                // Fallback to grep
                const args = [
                    '-rn', '--include', '*.py', '--include', '*.ts', '--include', '*.js',
                    '--include', '*.java', '--include', '*.go', '--include', '*.rs',
                    '-E', `(def |function |class |interface |type |const |var |let |struct )${symbol}`,
                    '.',
                ];
                const { stdout } = await run('grep', args);
                const matches = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return { file: parts[0], line: parseInt(parts[1]) || 0, content: parts.slice(2).join(':').trim() };
                });
                return { success: true, data: { matches, count: matches.length, symbol, source: 'grep' } };
            }

            case 'file_outline': {
                const filePath = params.path || params.file_path || params.file;
                if (!filePath) { return { success: false, data: null, error: 'file_outline requires a file path' }; }
                // Try VS Code Document Symbols first (LSP-powered, exact AST)
                const docSymbols = await getDocumentSymbols(filePath);
                if (docSymbols) {
                    const flat = flattenSymbols(docSymbols);
                    const symbols = flat.map(s => ({
                        line: s.line,
                        text: `${s.kind} ${s.name}${s.parent ? ` (in ${s.parent})` : ''}`,
                        kind: s.kind,
                        name: s.name,
                        end_line: s.endLine,
                    }));
                    return { success: true, data: { path: filePath, symbols, count: symbols.length, source: 'lsp' } };
                }
                // Fallback to grep
                const args = [
                    '-n', '-E',
                    '(^\\s*(def |async def |function |class |interface |type |const |export |struct |impl |fn |pub fn ))',
                    path.resolve(workspace, filePath),
                ];
                const { stdout } = await run('grep', args);
                const symbols = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return { line: parseInt(parts[0]) || 0, text: parts.slice(1).join(':').trim() };
                });
                return { success: true, data: { path: filePath, symbols, count: symbols.length, source: 'grep' } };
            }

            // ---- Git operations ----
            case 'git_log': {
                const maxCount = params.n || params.max_count || 20;
                const filePath = params.file || params.path || '';
                // Get commits with --stat so LLM sees which files each commit touched
                const args = ['log', `--max-count=${maxCount}`,
                    '--pretty=format:COMMIT_START%H|%an|%ad|%s', '--date=iso', '--stat=120'];
                if (params.search) { args.push(`--grep=${params.search}`); }
                if (filePath) { args.push('--', filePath); }
                const { stdout } = await run('git', args);
                // Parse interleaved format: COMMIT_START... then stat lines until next COMMIT_START
                const commits: any[] = [];
                let current: any = null;
                for (const line of stdout.split('\n')) {
                    if (line.startsWith('COMMIT_START')) {
                        if (current) { commits.push(current); }
                        const rest = line.slice('COMMIT_START'.length);
                        const [hash, author, date, ...msgParts] = rest.split('|');
                        current = { hash: hash?.slice(0, 8), author, date, message: msgParts.join('|'), stat: [] as string[], files_changed: 0 };
                    } else if (current && line.trim() && line.includes('|')) {
                        current.stat.push(line.trim());
                        current.files_changed++;
                    }
                }
                if (current) { commits.push(current); }
                // Cap stat lines per commit
                for (const c of commits) { c.stat = c.stat.slice(0, 10); }
                return { success: true, data: { commits, count: commits.length } };
            }

            case 'git_diff': {
                const ref1 = params.ref1 || params.ref || 'HEAD~1';
                const ref2 = params.ref2 || '';
                const filePath = params.path || params.file || '';
                const contextLines = params.context_lines ?? 10;
                const args = ref2 ? ['diff', ref1, ref2] : ['diff', ref1];
                args.push(`-U${contextLines}`);
                if (filePath) { args.push('--', filePath); }
                const { stdout } = await run('git', args);
                return {
                    success: true,
                    data: { diff: stdout, ref: ref1 },
                    truncated: stdout.length > maxOutput,
                };
            }

            case 'git_blame': {
                const filePath = params.file || params.path || params.file_path;
                if (!filePath) { return { success: false, data: null, error: 'git_blame requires a file path (file, path, or file_path param)' }; }
                const args = ['blame', '--line-porcelain'];
                if (params.start_line && params.end_line) {
                    args.push(`-L${params.start_line},${params.end_line}`);
                }
                args.push(filePath);
                const { stdout } = await run('git', args);
                return { success: true, data: { blame: stdout, path: filePath }, truncated: stdout.length > maxOutput };
            }

            case 'git_show': {
                const ref = params.commit || params.ref || 'HEAD';
                const showArgs = ['show', ref];
                const showFile = params.file || params.path;
                if (showFile) { showArgs.push('--', showFile); }
                const { stdout } = await run('git', showArgs);
                const lineCount = stdout.split('\n').length;
                return { success: true, data: { content: stdout, ref, lines: lineCount }, truncated: stdout.length > maxOutput };
            }

            // ---- Test operations ----
            case 'find_tests': {
                const name = params.name || '';
                const searchPath = params.path || '.';
                if (!name) { return { success: false, data: null, error: 'find_tests requires a name parameter' }; }
                // Find test files, then grep for the target name within them
                const { stdout: testFiles } = await run('find', [
                    searchPath, '-type', 'f',
                    '(', '-name', 'test_*.py', '-o', '-name', '*.test.ts', '-o',
                    '-name', '*.test.js', '-o', '-name', '*.spec.ts', '-o',
                    '-name', '*.spec.js', '-o', '-name', '*_test.go', '-o',
                    '-name', '*Test.java', ')',
                    '-not', '-path', '*/node_modules/*',
                    '-not', '-path', '*/.git/*',
                ]);
                const allTestFiles = testFiles.trim().split('\n').filter(Boolean);
                if (allTestFiles.length === 0) {
                    return { success: true, data: { matches: [], count: 0 } };
                }
                // Grep for the name in test files
                const { stdout: grepOut } = await run('grep', [
                    '-ln', name, ...allTestFiles,
                ]);
                const matchingFiles = grepOut.trim().split('\n').filter(Boolean);
                return { success: true, data: { matches: matchingFiles, count: matchingFiles.length, name } };
            }

            case 'run_test': {
                const testFile = params.test_file || params.command;
                if (!testFile) { return { success: false, data: null, error: 'run_test requires test_file' }; }
                const testName = params.test_name || '';
                const timeout = (params.timeout || 30) * 1000;
                // Auto-detect test runner from file extension
                const ext = path.extname(testFile).toLowerCase();
                let cmd: string[];
                if (ext === '.py') {
                    cmd = testName ? ['python', '-m', 'pytest', testFile, '-k', testName, '-v'] : ['python', '-m', 'pytest', testFile, '-v'];
                } else if (['.ts', '.js', '.tsx', '.jsx'].includes(ext)) {
                    cmd = testName ? ['npx', 'jest', testFile, '-t', testName] : ['npx', 'jest', testFile];
                } else if (ext === '.go') {
                    cmd = ['go', 'test', '-run', testName || '.', '-v', `./${path.dirname(testFile)}`];
                } else {
                    // Fallback: try pytest
                    cmd = ['python', '-m', 'pytest', testFile, '-v'];
                }
                const { stdout, stderr } = await run(cmd[0], cmd.slice(1), workspace, timeout);
                return { success: true, data: { stdout, stderr, command: cmd.join(' ') } };
            }

            case 'git_diff_files': {
                const ref = params.ref || 'HEAD';
                const refParts = ref.trim().split(/\s+/);
                const { stdout: numstat } = await run('git', ['diff', '--numstat', ...refParts]);
                const { stdout: namestatus } = await run('git', ['diff', '--name-status', ...refParts]);
                const files: any[] = [];
                const statLines = namestatus.trim().split('\n').filter(Boolean);
                const numLines = numstat.trim().split('\n').filter(Boolean);
                const numMap: Record<string, {added: number; deleted: number}> = {};
                for (const nl of numLines) {
                    const [a, d, ...fp] = nl.split('\t');
                    numMap[fp.join('\t')] = { added: parseInt(a) || 0, deleted: parseInt(d) || 0 };
                }
                for (const sl of statLines) {
                    const parts = sl.split('\t');
                    const status = parts[0];
                    const filePath = parts[parts.length - 1];
                    const nums = numMap[filePath] || { added: 0, deleted: 0 };
                    files.push({ file: filePath, status: status[0], added: nums.added, deleted: nums.deleted });
                }
                return { success: true, data: { files, count: files.length, ref } };
            }

            case 'ast_search': {
                // ast-grep subprocess — may not be installed on the developer's machine
                const pattern = params.pattern;
                const searchPath = params.path || '.';
                const astArgs = ['run', '-p', pattern, '--json'];
                if (params.language) { astArgs.push('-l', params.language); }
                astArgs.push(path.resolve(workspace, searchPath));
                try {
                    const { stdout } = await run('ast-grep', astArgs);
                    const parsed = JSON.parse(stdout || '[]');
                    const matches = (Array.isArray(parsed) ? parsed : []).slice(0, params.max_results || 30);
                    return { success: true, data: { matches, count: matches.length, pattern } };
                } catch {
                    return { success: false, data: null, error: 'ast-grep not installed or failed. Install with: npm i -g @ast-grep/cli' };
                }
            }

            case 'find_references': {
                const symbol = params.symbol_name || params.name;
                const searchFile = params.file || '';
                // Try VS Code LSP Reference Provider (requires knowing the symbol location)
                if (searchFile) {
                    const rawSymbols = await getDocumentSymbols(searchFile);
                    if (rawSymbols) {
                        const findSym = (syms: vscode.DocumentSymbol[]): vscode.DocumentSymbol | undefined => {
                            for (const s of syms) {
                                if (s.name === symbol) { return s; }
                                if (s.children) { const f = findSym(s.children); if (f) { return f; } }
                            }
                            return undefined;
                        };
                        const match = findSym(rawSymbols);
                        if (match) {
                            const pos = match.selectionRange ? match.selectionRange.start : match.range.start;
                            const lspRefs = await findReferencesLsp(searchFile, pos.line, pos.character);
                            if (lspRefs) {
                                const references = lspRefs.map(r => ({
                                    file: toRelative(r.uri.fsPath),
                                    line: r.range.start.line + 1,
                                    content: '', // LSP doesn't return line content
                                }));
                                return { success: true, data: { references, count: references.length, symbol, source: 'lsp' } };
                            }
                        }
                    }
                }
                // Fallback to grep
                const { stdout } = await run('grep', [
                    '-rn', '-w', symbol,
                    '--include', '*.py', '--include', '*.ts', '--include', '*.js',
                    '--include', '*.java', '--include', '*.go', '--include', '*.rs',
                    '-m', '100', searchFile || '.',
                ]);
                const matches = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return { file: parts[0], line: parseInt(parts[1]) || 0, content: parts.slice(2).join(':').trim() };
                });
                return { success: true, data: { references: matches, count: matches.length, symbol, source: 'grep' } };
            }

            case 'test_outline': {
                const filePath = params.path || params.file_path || params.file;
                if (!filePath) { return { success: false, data: null, error: 'test_outline requires a file path' }; }
                const resolved = path.resolve(workspace, filePath);
                const { stdout } = await run('grep', [
                    '-n', '-E',
                    '(def test_|it\\(|describe\\(|test\\(|@Test|func Test|#\\[test\\])',
                    resolved,
                ]);
                const tests = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return { line: parseInt(parts[0]) || 0, text: parts.slice(1).join(':').trim() };
                });
                return { success: true, data: { path: filePath, tests, count: tests.length } };
            }

            case 'trace_variable': {
                const varName = params.variable_name || params.name;
                const file = params.file || params.file_path || params.path;
                const direction = params.direction || 'forward';
                const functionName = params.function_name || '';
                if (!varName || !file) { return { success: false, data: null, error: 'trace_variable requires variable_name and file' }; }
                // Grep for the variable in the file
                const { stdout } = await run('grep', ['-n', '-w', varName, path.resolve(workspace, file)]);
                const usages = stdout.trim().split('\n').filter(Boolean).map(line => {
                    const parts = line.split(':');
                    return { line: parseInt(parts[0]) || 0, content: parts.slice(1).join(':').trim() };
                });
                // If backward, also search callers
                if (direction === 'backward') {
                    const { stdout: callerOut } = await run('grep', ['-rn', `${functionName || varName}(`, '--include', '*.py', '--include', '*.ts', '--include', '*.js', '.']);
                    const callers = callerOut.trim().split('\n').filter(Boolean).slice(0, 20).map(line => {
                        const parts = line.split(':');
                        return { file: parts[0], line: parseInt(parts[1]) || 0, content: parts.slice(2).join(':').trim() };
                    });
                    return { success: true, data: { variable: varName, file, direction, usages, callers, count: usages.length } };
                }
                return { success: true, data: { variable: varName, file, usages, count: usages.length } };
            }

            case 'compressed_view': {
                const filePath = params.file_path || params.path || params.file;
                if (!filePath) { return { success: false, data: null, error: 'compressed_view requires a file path' }; }
                const resolved = path.resolve(workspace, filePath);
                if (!resolved.startsWith(workspace)) {
                    return { success: false, data: null, error: 'Path traversal blocked' };
                }
                const content = fs.readFileSync(resolved, 'utf-8');
                const lines = content.split('\n');
                const totalLines = lines.length;

                // Try VS Code Document Symbols for precise signatures
                const focus = params.focus ? params.focus.toLowerCase() : '';
                const docSymbols = await getDocumentSymbols(filePath);
                if (docSymbols) {
                    let flat = flattenSymbols(docSymbols);
                    if (focus) {
                        flat = flat.filter(s => s.name.toLowerCase().includes(focus) || (s.parent && s.parent.toLowerCase().includes(focus)));
                    }
                    const viewLines: string[] = [`## ${filePath} (${totalLines} lines, ${flat.length} symbols)`, ''];
                    for (const s of flat) {
                        const indent = s.parent ? '  ' : '';
                        const sig = lines[s.line - 1]?.trimEnd() || s.name;
                        viewLines.push(`${indent}L${s.line}: ${s.kind} ${s.name} — ${sig}`);
                    }
                    const view = viewLines.join('\n');
                    return {
                        success: true,
                        data: { path: filePath, total_lines: totalLines, symbols_count: flat.length, view, source: 'lsp' },
                        truncated: view.length > maxOutput,
                    };
                }
                // Fallback to regex
                const symbols: any[] = [];
                lines.forEach((line: string, i: number) => {
                    if (/^\s*(def |async def |function |class |interface |type |const |export |struct |impl |fn |pub fn |@)/.test(line)) {
                        symbols.push({ line: i + 1, text: line.trimEnd() });
                    }
                });
                let filteredSymbols = symbols;
                if (focus) {
                    filteredSymbols = symbols.filter(s => s.text.toLowerCase().includes(focus));
                }
                const view = filteredSymbols.map(s => `L${s.line}: ${s.text}`).join('\n');
                return {
                    success: true,
                    data: { path: filePath, total_lines: totalLines, symbols_count: symbols.length, view, source: 'grep' },
                    truncated: view.length > maxOutput,
                };
            }

            case 'module_summary': {
                const filePath = params.file_path || params.path || params.file || params.module_path;
                if (!filePath) { return { success: false, data: null, error: 'module_summary requires a file path' }; }
                const resolved = path.resolve(workspace, filePath);

                // Check path exists before attempting read
                let stat: fs.Stats;
                try {
                    stat = fs.statSync(resolved);
                } catch {
                    return { success: false, data: null, error: `Path not found: ${filePath}` };
                }
                if (stat.isDirectory()) {
                    const entries = fs.readdirSync(resolved, { withFileTypes: true });
                    const files: string[] = [];
                    const allDefs: string[] = [];
                    const allImports: string[] = [];
                    for (const entry of entries) {
                        if (entry.isFile() && /\.(py|ts|tsx|js|jsx|java|go|rs|c|cpp|h)$/.test(entry.name)) {
                            files.push(entry.name);
                            try {
                                const fileContent = fs.readFileSync(path.join(resolved, entry.name), 'utf-8');
                                for (const line of fileContent.split('\n')) {
                                    const t = line.trim();
                                    if (/^(import |from |require\(|const .* = require|use |#include)/.test(t)) {
                                        allImports.push(t);
                                    }
                                    if (/^(def |async def |function |class |interface |type |export (default )?(function|class|const|interface|type)|pub (fn|struct|enum|trait))/.test(t)) {
                                        allDefs.push(`${entry.name}: ${t}`);
                                    }
                                }
                            } catch { /* skip unreadable files */ }
                        } else if (entry.isDirectory()) {
                            files.push(entry.name + '/');
                        }
                    }
                    return {
                        success: true,
                        data: { path: filePath, files, definitions: allDefs, imports_count: allImports.length, definitions_count: allDefs.length, source: 'directory_scan' },
                    };
                }

                const content = fs.readFileSync(resolved, 'utf-8');
                const lines = content.split('\n');
                // Extract imports (always regex — LSP doesn't expose imports as symbols)
                const imports: string[] = [];
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (/^(import |from |require\(|const .* = require|use )/.test(trimmed)) {
                        imports.push(trimmed);
                    }
                }
                // Try VS Code Document Symbols for definitions
                const docSymbols = await getDocumentSymbols(filePath);
                if (docSymbols) {
                    const flat = flattenSymbols(docSymbols).filter(s => !s.parent); // top-level only
                    const definitions = flat.map(s => `${s.kind} ${s.name}`);
                    return {
                        success: true,
                        data: { path: filePath, total_lines: lines.length, imports, definitions, imports_count: imports.length, definitions_count: definitions.length, source: 'lsp' },
                    };
                }
                // Fallback to regex
                const definitions: string[] = [];
                for (const line of lines) {
                    const trimmed = line.trim();
                    if (/^(def |async def |function |class |interface |type |export (default )?(function|class|const|interface|type))/.test(trimmed)) {
                        definitions.push(trimmed);
                    }
                }
                return {
                    success: true,
                    data: { path: filePath, total_lines: lines.length, imports, definitions, imports_count: imports.length, definitions_count: definitions.length, source: 'grep' },
                };
            }

            case 'expand_symbol': {
                const filePath = params.file_path || params.path || params.file;
                const symbolName = params.symbol_name || params.name || params.symbol;
                if (!filePath) { return { success: false, data: null, error: 'expand_symbol requires file_path' }; }
                if (!symbolName) { return { success: false, data: null, error: 'expand_symbol requires symbol_name' }; }
                const resolved = path.resolve(workspace, filePath);
                const content = fs.readFileSync(resolved, 'utf-8');
                const lines = content.split('\n');

                // Try VS Code Document Symbols for exact range
                const docSymbols = await getDocumentSymbols(filePath);
                if (docSymbols) {
                    const flat = flattenSymbols(docSymbols);
                    const match = flat.find(s => s.name === symbolName);
                    if (match) {
                        const body = lines.slice(match.line - 1, match.endLine).join('\n');
                        return {
                            success: true,
                            data: { symbol: symbolName, path: filePath, start_line: match.line, end_line: match.endLine, body, source: 'lsp' },
                            truncated: body.length > maxOutput,
                        };
                    }
                }
                // Fallback to indentation-based extraction
                let startLine = -1;
                for (let i = 0; i < lines.length; i++) {
                    if (lines[i].includes(symbolName) &&
                        /^\s*(def |async def |function |class |interface |struct |fn |pub fn |impl )/.test(lines[i])) {
                        startLine = i;
                        break;
                    }
                }
                if (startLine < 0) {
                    return { success: false, data: null, error: `Symbol '${symbolName}' not found in ${filePath}` };
                }
                const baseIndent = lines[startLine].search(/\S/);
                let endLine = startLine + 1;
                while (endLine < lines.length) {
                    const line = lines[endLine];
                    if (line.trim() === '') { endLine++; continue; }
                    const indent = line.search(/\S/);
                    if (indent <= baseIndent && line.trim() !== '') { break; }
                    endLine++;
                }
                const body = lines.slice(startLine, endLine).join('\n');
                return {
                    success: true,
                    data: { symbol: symbolName, path: filePath, start_line: startLine + 1, end_line: endLine, body, source: 'grep' },
                    truncated: body.length > maxOutput,
                };
            }

            case 'get_dependencies':
            case 'get_dependents': {
                // Parse import/require statements from the file
                const filePath = params.file_path || params.path || params.file;
                if (!filePath) { return { success: false, data: null, error: `${tool} requires file_path` }; }
                const resolved = path.resolve(workspace, filePath);
                const content = fs.readFileSync(resolved, 'utf-8');
                const deps: string[] = [];
                for (const line of content.split('\n')) {
                    const trimmed = line.trim();
                    // Python: from X import Y, import X
                    let m = trimmed.match(/^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))/);
                    if (m) { deps.push(m[1] || m[2]); continue; }
                    // JS/TS: import ... from 'X', require('X')
                    m = trimmed.match(/(?:from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))/);
                    if (m) { deps.push(m[1] || m[2]); continue; }
                    // Go: import "X"
                    m = trimmed.match(/^\s*"([^"]+)"\s*$/);
                    if (m && content.includes('import (')) { deps.push(m[1]); }
                }
                const toolName = tool;
                if (tool === 'get_dependents') {
                    // Reverse: grep for files that import this file
                    const basename = path.basename(filePath, path.extname(filePath));
                    const { stdout } = await run('grep', [
                        '-rln', basename,
                        '--include', '*.py', '--include', '*.ts', '--include', '*.js',
                        '--include', '*.go', '--include', '*.rs', '--include', '*.java',
                        '.',
                    ]);
                    const dependents = stdout.trim().split('\n').filter(Boolean);
                    return { success: true, data: { file: filePath, dependents, count: dependents.length } };
                }
                return { success: true, data: { file: filePath, dependencies: deps, count: deps.length } };
            }

            case 'get_callees':
            case 'get_callers': {
                const symbolName = params.name || params.symbol_name || params.function_name;
                const filePath = params.file || params.file_path || '.';

                // Try VS Code Call Hierarchy (LSP-powered)
                if (filePath !== '.') {
                    // Get raw DocumentSymbols (not flattened) to access selectionRange
                    const rawSymbols = await getDocumentSymbols(filePath);
                    if (rawSymbols) {
                        // Find the symbol with selectionRange for precise positioning
                        const findSymbol = (syms: vscode.DocumentSymbol[]): vscode.DocumentSymbol | undefined => {
                            for (const s of syms) {
                                if (s.name === symbolName) { return s; }
                                if (s.children) {
                                    const found = findSymbol(s.children);
                                    if (found) { return found; }
                                }
                            }
                            return undefined;
                        };
                        const match = findSymbol(rawSymbols);
                        if (match) {
                            const pos = match.selectionRange ? match.selectionRange.start : match.range.start;
                            const items = await prepareCallHierarchy(filePath, pos.line, pos.character);
                            if (items && items.length > 0) {
                                try {
                                    if (tool === 'get_callees') {
                                        const outgoing = await vscode.commands.executeCommand<vscode.CallHierarchyOutgoingCall[]>(
                                            'vscode.provideOutgoingCalls', items[0],
                                        );
                                        if (outgoing && outgoing.length > 0) {
                                            const callees = outgoing.map(c => ({
                                                name: c.to.name,
                                                file: toRelative(c.to.uri.fsPath),
                                                line: c.to.range.start.line + 1,
                                                kind: vscode.SymbolKind[c.to.kind],
                                            }));
                                            return { success: true, data: { symbol: symbolName, callees, count: callees.length, source: 'lsp' } };
                                        }
                                    } else {
                                        const incoming = await vscode.commands.executeCommand<vscode.CallHierarchyIncomingCall[]>(
                                            'vscode.provideIncomingCalls', items[0],
                                        );
                                        if (incoming && incoming.length > 0) {
                                            const callers = incoming.map(c => ({
                                                name: c.from.name,
                                                file: toRelative(c.from.uri.fsPath),
                                                line: c.from.range.start.line + 1,
                                                kind: vscode.SymbolKind[c.from.kind],
                                            }));
                                            return { success: true, data: { symbol: symbolName, callers, count: callers.length, source: 'lsp' } };
                                        }
                                    }
                                } catch {}
                            }
                        }
                    }
                }
                // Fallback to grep/regex
                if (tool === 'get_callees') {
                    const resolved = path.resolve(workspace, filePath);
                    const content = fs.readFileSync(resolved, 'utf-8');
                    const callPattern = /\b([a-zA-Z_]\w*)\s*\(/g;
                    const callees = new Set<string>();
                    let match;
                    while ((match = callPattern.exec(content)) !== null) {
                        const name = match[1];
                        if (!['if', 'for', 'while', 'return', 'print', 'len', 'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'type', 'isinstance', 'hasattr', 'getattr', 'setattr', 'super'].includes(name)) {
                            callees.add(name);
                        }
                    }
                    return { success: true, data: { symbol: symbolName, callees: Array.from(callees), count: callees.size, source: 'grep' } };
                } else {
                    const searchPath = params.path || '.';
                    const { stdout } = await run('grep', [
                        '-rn', `${symbolName}(`,
                        '--include', '*.py', '--include', '*.ts', '--include', '*.js',
                        '--include', '*.java', '--include', '*.go', '--include', '*.rs',
                        searchPath,
                    ]);
                    const callers = stdout.trim().split('\n').filter(Boolean).map(line => {
                        const parts = line.split(':');
                        return { file: parts[0], line: parseInt(parts[1]) || 0, content: parts.slice(2).join(':').trim() };
                    });
                    return { success: true, data: { symbol: symbolName, callers, count: callers.length, source: 'grep' } };
                }
            }

            case 'detect_patterns': {
                const filePath = params.file_path || params.path || params.file;
                const resolved = path.resolve(workspace, filePath || '.');
                const content = filePath ? fs.readFileSync(resolved, 'utf-8') : '';
                const categories = params.categories ? new Set((params.categories as string[]).map(c => c.toLowerCase())) : null;
                const maxResults = params.max_results || 50;

                const allPatterns: Array<{category: string; pattern: string; match: boolean}> = [
                    { category: 'webhook', pattern: 'Webhook/Callback', match: /@app\.(route|get|post|put|delete)|@router\.|app\.(get|post|put|delete)\(/.test(content) },
                    { category: 'queue', pattern: 'Queue Consumer/Producer', match: /sqs|sns|kafka|rabbitmq|celery|bull/i.test(content) },
                    { category: 'retry', pattern: 'Retry/Backoff', match: /retry|backoff|max_retries|exponential/i.test(content) },
                    { category: 'lock', pattern: 'Lock/Mutex', match: /lock|mutex|semaphore|synchronized/i.test(content) },
                    { category: 'check_then_act', pattern: 'Check-then-Act', match: /if.*exists.*\n.*create|if.*not.*found.*\n.*insert/i.test(content) },
                    { category: 'transaction', pattern: 'Transaction Boundary', match: /BEGIN|COMMIT|ROLLBACK|@Transactional|session\.begin/i.test(content) },
                    { category: 'token_lifecycle', pattern: 'Token Lifecycle', match: /token.*expir|refresh.*token|access_token/i.test(content) },
                    { category: 'side_effect_chain', pattern: 'Side-Effect Chain', match: /async def |async function |await /.test(content) },
                    { category: 'orm', pattern: 'ORM Model', match: /class .+\(.*Model\)|@Entity|@Table/.test(content) },
                    { category: 'test', pattern: 'Test File', match: /def test_|describe\(|it\(|@Test/.test(content) },
                    { category: 'error_handling', pattern: 'Error Handling', match: /try:|try\s*\{|catch\s*\(/.test(content) },
                    { category: 'todo', pattern: 'Has TODOs', match: /TODO|FIXME|HACK|XXX/.test(content) },
                ];

                let matched = allPatterns.filter(p => p.match);
                if (categories) {
                    matched = matched.filter(p => categories.has(p.category));
                }
                const patterns = matched.slice(0, maxResults).map(p => p.pattern);
                return { success: true, data: { file: filePath || '.', patterns, count: patterns.length } };
            }

            // ---- Fallback ----
            default: {
                return {
                    success: false,
                    error: `Tool '${tool}' is not supported in local mode`,
                    data: null,
                };
            }
        }
    }

    /**
     * Poll workspace status every 2s, forwarding clone progress to the
     * webview overlay.  Returns when status is 'ready' or 'error',
     * or after ~15 minutes.
     */
    private async _pollWorkspaceReady(
        backendUrl: string,
        roomId: string,
    ): Promise<any | null> {
        const maxAttempts = 450;  // 450 × 2s = ~15 min
        const intervalMs = 2000;

        for (let i = 0; i < maxAttempts; i++) {
            await new Promise(r => setTimeout(r, intervalMs));
            try {
                const r = await fetch(`${backendUrl}/api/git-workspace/workspaces/${encodeURIComponent(roomId)}`);
                if (!r.ok) { continue; }
                const info = await r.json() as any;

                // Forward clone progress to the webview
                const cp = info.clone_progress;
                if (cp) {
                    let detail = this._formatCloneProgress(cp);
                    this._view?.webview.postMessage({
                        command: 'setupAndIndexProgress',
                        phase: 'cloning',
                        detail,
                        percent: cp.percent ?? 0,
                        current: cp.current ?? 0,
                        total: cp.total ?? 0,
                    });
                } else if (info.status === 'syncing' || info.status === 'pending') {
                    // Clone started but no progress yet (connecting)
                    this._view?.webview.postMessage({
                        command: 'setupAndIndexProgress',
                        phase: 'cloning',
                        detail: 'Connecting to remote...',
                    });
                }

                if (info.status === 'ready' || info.status === 'error') {
                    return info;
                }
            } catch {
                // Network error — keep trying
            }
        }
        return null;
    }

    private _formatCloneProgress(cp: any): string {
        const phaseLabels: Record<string, string> = {
            connecting: 'Connecting to remote...',
            counting: 'Counting objects...',
            compressing: 'Compressing objects...',
            receiving: 'Downloading objects...',
            resolving: 'Resolving deltas...',
        };
        let detail = phaseLabels[cp.phase] || cp.phase;
        if (cp.bytes_received) {
            detail += ` ${cp.bytes_received}`;
        }
        if (cp.throughput) {
            detail += ` (${cp.throughput})`;
        }
        return detail;
    }

    /**
     * Add the conductor:// workspace as a folder in the current VS Code window.
     *
     * We add it to the current window rather than opening a new one so that
     * the chat panel stays visible alongside the remote workspace explorer.
     * The FileSystemProvider is already registered in this window, so there
     * is no activation-race with vscode.openFolder in a new window.
     */
    private _handleOpenConductorWorkspace(roomId: string): void {
        const conductorUri = vscode.Uri.parse(`conductor://${roomId}/`);
        console.log(`[Conductor] Adding conductor://${roomId}/ as workspace folder`);

        const folders = vscode.workspace.workspaceFolders ?? [];
        // Remove any existing conductor:// folder for this room (idempotent)
        const existingIdx = folders.findIndex(
            f => f.uri.scheme === 'conductor' && f.uri.authority === roomId,
        );
        vscode.workspace.updateWorkspaceFolders(
            existingIdx >= 0 ? existingIdx : folders.length,
            existingIdx >= 0 ? 1 : 0,
            { uri: conductorUri, name: `Remote: ${roomId.slice(0, 8)}` },
        );
    }

    private async _handleScanWorkspaceTodos(): Promise<void> {
        try {
            const todos = await scanWorkspaceTodos();
            this._view?.webview.postMessage({ command: 'workspaceTodosScanned', todos });
        } catch (e) {
            console.error('[Conductor] scanWorkspaceTodos failed:', e);
            this._view?.webview.postMessage({ command: 'workspaceTodosScanned', todos: [], error: String(e) });
        }
    }

    private async _handleUpdateWorkspaceTodo(payload: UpdateTodoPayload): Promise<void> {
        const ok = updateWorkspaceTodoInFile(payload);
        this._view?.webview.postMessage({ command: 'workspaceTodoUpdated', ok, filePath: payload.filePath });
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

        // Build Content Security Policy that allows WebSocket and fetch connections.
        // Include both localhost and all ngrok patterns explicitly so that the CSP
        // remains valid even if session.backendUrl is updated to a ngrok URL after
        // the webview is first rendered (race between detectNgrokUrl and render time).
        const backendUrl = getSessionService().getBackendUrl();
        const wsUrl = backendUrl.replace('http', 'ws');
        const cspMeta = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; connect-src ${backendUrl} ${wsUrl} http://localhost:* https://localhost:* ws://localhost:* wss://localhost:* https://*.ngrok-free.dev wss://*.ngrok-free.dev https://*.ngrok-free.app wss://*.ngrok-free.app https://*.ngrok.io wss://*.ngrok.io https://*.ngrok.app wss://*.ngrok.app;">`;

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

// ---------------------------------------------------------------------------
// Test output parser (module-level helper)
// ---------------------------------------------------------------------------

interface ParsedTestItem {
    name: string;
    filePath?: string;
    lineNumber?: number;
    errorType: string;
    errorMessage: string;
}

/**
 * Parse common test output formats (pytest, Jest, Go test) into structured
 * test failure items.
 */
function _parseTestOutput(rawText: string, framework: string): ParsedTestItem[] {
    const tests: ParsedTestItem[] = [];

    if (framework === 'pytest' || rawText.includes('FAILED') && rawText.includes('::')) {
        // pytest:  FAILED tests/test_auth.py::test_name - AssertionError: assert ...
        const pytestFailed = /FAILED\s+([\w/.]+)::(\w+)\s*(?:-\s*(.+))?/g;
        let m: RegExpExecArray | null;
        while ((m = pytestFailed.exec(rawText)) !== null) {
            const [, filePath, testName, errorPart] = m;
            const errorColon = (errorPart || '').indexOf(':');
            tests.push({
                name: testName,
                filePath,
                errorType: errorColon !== -1 ? errorPart.slice(0, errorColon).trim() : 'AssertionError',
                errorMessage: errorColon !== -1 ? errorPart.slice(errorColon + 1).trim() : errorPart?.trim() || '',
            });
        }
    }

    if (framework === 'jest' || rawText.includes('● ') || rawText.includes('FAIL ')) {
        // Jest:  ● TestSuite › test name
        const jestFailed = /●\s+(.+)/g;
        let m: RegExpExecArray | null;
        while ((m = jestFailed.exec(rawText)) !== null) {
            tests.push({
                name: m[1].trim(),
                errorType: 'Error',
                errorMessage: '',
            });
        }
    }

    if (framework === 'go' || rawText.includes('--- FAIL:')) {
        // Go:  --- FAIL: TestName (0.00s)
        const goFailed = /---\s+FAIL:\s+([\w/]+)\s+\([\d.]+s\)/g;
        let m: RegExpExecArray | null;
        while ((m = goFailed.exec(rawText)) !== null) {
            tests.push({
                name: m[1],
                errorType: 'FAIL',
                errorMessage: '',
            });
        }
    }

    // Generic fallback: look for lines with "Error:" or "FAIL"
    if (tests.length === 0) {
        const errorLine = /^.*(Error|FAIL|FAILED|Assertion).*$/gm;
        let m: RegExpExecArray | null;
        while ((m = errorLine.exec(rawText)) !== null) {
            const line = m[0].trim();
            if (line.length < 200) {
                tests.push({ name: line, errorType: 'Error', errorMessage: line });
            }
        }
    }

    return tests;
}
