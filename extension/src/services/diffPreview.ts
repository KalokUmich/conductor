/**
 * Diff Preview Service for AI-Generated Code Changes
 *
 * This module provides functionality for:
 * - Displaying side-by-side diff previews of AI-generated changes
 * - Applying changes to the workspace using VS Code's WorkspaceEdit API
 * - Handling both new file creation and existing file modification
 *
 * The service uses VS Code's built-in diff viewer for previews,
 * which shows original content on the left and modified content on the right.
 *
 * @module services/diffPreview
 */
import * as vscode from 'vscode';

/**
 * Represents a single file change from the AI agent.
 *
 * This interface matches the FileChange schema from the backend.
 */
export interface FileChange {
    /** Unique identifier (UUID) for tracking this change. */
    id: string;
    /** Relative path to the file within the workspace. */
    file: string;
    /** Type of change: replace lines or create new file. */
    type: 'replace_range' | 'create_file';
    /** Line range to replace (1-based, inclusive). Only for replace_range. */
    range?: {
        start: number;
        end: number;
    };
    /** New content to insert or create. */
    content?: string;
    /** Original content being replaced (for reference). */
    original_content?: string;
}

/**
 * Represents a collection of file changes from the AI agent.
 *
 * A ChangeSet groups related changes that should be reviewed together.
 */
export interface ChangeSet {
    /** List of file changes (1-10 files typically). */
    changes: FileChange[];
    /** Human-readable summary of what the changes accomplish. */
    summary: string;
}

/**
 * Singleton service for showing diff previews and applying changes.
 *
 * This service manages the diff viewing experience by:
 * - Creating in-memory documents for preview
 * - Using VS Code's built-in diff command
 * - Applying changes via WorkspaceEdit for undo support
 */
export class DiffPreviewService {
    /** Singleton instance. */
    private static instance: DiffPreviewService;

    /** Private constructor for singleton pattern. */
    private constructor() {}

    /**
     * Get the singleton instance.
     */
    public static getInstance(): DiffPreviewService {
        if (!DiffPreviewService.instance) {
            DiffPreviewService.instance = new DiffPreviewService();
        }
        return DiffPreviewService.instance;
    }

    /**
     * Show a diff preview for a file change.
     * 
     * @param change The file change to preview
     * @param workspaceRoot The workspace root path
     */
    public async showDiff(change: FileChange, workspaceRoot: string): Promise<void> {
        const filePath = vscode.Uri.file(`${workspaceRoot}/${change.file}`);
        
        if (change.type === 'create_file') {
            await this.showCreateFileDiff(change, filePath);
        } else if (change.type === 'replace_range') {
            await this.showReplaceRangeDiff(change, filePath);
        }
    }

    /**
     * Show diff for a new file creation.
     */
    private async showCreateFileDiff(change: FileChange, filePath: vscode.Uri): Promise<void> {
        // For new files, left side is empty
        const emptyUri = vscode.Uri.parse('untitled:empty');
        
        // Create in-memory document for the new content
        const modifiedUri = filePath.with({ scheme: 'ai-collab-modified' });
        
        // Register content provider for the modified content
        const provider = new InMemoryContentProvider();
        provider.setContent(modifiedUri, change.content || '');
        
        const disposable = vscode.workspace.registerTextDocumentContentProvider(
            'ai-collab-modified',
            provider
        );

        try {
            await vscode.commands.executeCommand(
                'vscode.diff',
                emptyUri,
                modifiedUri,
                `New File: ${change.file}`
            );
        } finally {
            // Keep the provider alive for the diff view
            // It will be disposed when the diff is closed
        }
    }

    /**
     * Show diff for a replace_range operation.
     */
    private async showReplaceRangeDiff(change: FileChange, filePath: vscode.Uri): Promise<void> {
        // Read the original file content
        let originalContent = '';
        try {
            const originalDoc = await vscode.workspace.openTextDocument(filePath);
            originalContent = originalDoc.getText();
        } catch (error) {
            // File doesn't exist, treat as empty
            originalContent = '';
        }

        // Apply the change to create modified content
        const modifiedContent = this.applyReplaceRange(
            originalContent,
            change.range?.start || 1,
            change.range?.end || 1,
            change.content || ''
        );

        // Create in-memory document for the modified content
        const modifiedUri = filePath.with({ scheme: 'ai-collab-modified' });
        
        // Register content provider
        const provider = new InMemoryContentProvider();
        provider.setContent(modifiedUri, modifiedContent);
        
        vscode.workspace.registerTextDocumentContentProvider(
            'ai-collab-modified',
            provider
        );

        await vscode.commands.executeCommand(
            'vscode.diff',
            filePath,
            modifiedUri,
            `Changes: ${change.file} (lines ${change.range?.start}-${change.range?.end})`
        );
    }

    /**
     * Apply a replace_range operation to content.
     */
    private applyReplaceRange(
        content: string,
        startLine: number,
        endLine: number,
        newContent: string
    ): string {
        const lines = content.split('\n');
        
        // Convert 1-based line numbers to 0-based indices
        const startIndex = Math.max(0, startLine - 1);
        const endIndex = Math.min(lines.length, endLine);
        
        // Replace the lines
        const newLines = newContent.split('\n');
        lines.splice(startIndex, endIndex - startIndex, ...newLines);
        
        return lines.join('\n');
    }

    /**
     * Show diff preview for a ChangeSet.
     */
    public async showChangeSetDiff(changeSet: ChangeSet): Promise<void> {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            vscode.window.showErrorMessage('No workspace folder open');
            return;
        }

        const workspaceRoot = workspaceFolders[0].uri.fsPath;

        for (const change of changeSet.changes) {
            await this.showDiff(change, workspaceRoot);
        }
    }

    /**
     * Apply a ChangeSet to the workspace files using WorkspaceEdit.
     *
     * @param changeSet The ChangeSet to apply
     * @returns Object with success status and count of applied changes
     */
    public async applyChangeSet(changeSet: ChangeSet): Promise<{ success: boolean; appliedCount: number; errors: string[] }> {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            return { success: false, appliedCount: 0, errors: ['No workspace folder open'] };
        }

        const workspaceRoot = workspaceFolders[0].uri.fsPath;
        const workspaceEdit = new vscode.WorkspaceEdit();
        const errors: string[] = [];
        let appliedCount = 0;

        for (const change of changeSet.changes) {
            try {
                const filePath = vscode.Uri.file(`${workspaceRoot}/${change.file}`);

                if (change.type === 'create_file') {
                    // Create new file with content
                    workspaceEdit.createFile(filePath, { overwrite: false, ignoreIfExists: false });
                    workspaceEdit.insert(filePath, new vscode.Position(0, 0), change.content || '');
                    appliedCount++;
                } else if (change.type === 'replace_range') {
                    // Replace range in existing file
                    // Read the file to get the end position of the last line
                    try {
                        const doc = await vscode.workspace.openTextDocument(filePath);

                        // Convert 1-based line numbers to 0-based, with bounds checking
                        const startLine = Math.max(0, (change.range?.start || 1) - 1);
                        const endLine = Math.max(0, Math.min(doc.lineCount - 1, (change.range?.end || 1) - 1));

                        const startPos = new vscode.Position(startLine, 0);
                        // End position: end of the endLine (or start of next line)
                        const endPos = endLine < doc.lineCount - 1
                            ? new vscode.Position(endLine + 1, 0)
                            : new vscode.Position(endLine, doc.lineAt(endLine).text.length);

                        const range = new vscode.Range(startPos, endPos);

                        // Ensure content ends with newline if we're replacing full lines
                        let newContent = change.content || '';
                        if (endLine < doc.lineCount - 1 && !newContent.endsWith('\n')) {
                            newContent += '\n';
                        }

                        workspaceEdit.replace(filePath, range, newContent);
                        appliedCount++;
                    } catch (error) {
                        errors.push(`Failed to read file ${change.file}: ${error}`);
                    }
                }
            } catch (error) {
                errors.push(`Failed to process change for ${change.file}: ${error}`);
            }
        }

        // Apply all edits
        const success = await vscode.workspace.applyEdit(workspaceEdit);

        if (success && appliedCount > 0) {
            // Save all modified documents
            await vscode.workspace.saveAll(false);
        }

        return { success, appliedCount, errors };
    }

    /**
     * Apply a single FileChange to the workspace using WorkspaceEdit.
     *
     * @param change The FileChange to apply
     * @returns Object with success status, change ID, skipped flag, and any error message
     */
    public async applySingleChange(change: FileChange): Promise<{
        success: boolean;
        changeId: string;
        skipped?: boolean;
        error?: string
    }> {
        const workspaceFolders = vscode.workspace.workspaceFolders;
        if (!workspaceFolders || workspaceFolders.length === 0) {
            return { success: false, changeId: change.id, error: 'No workspace folder open' };
        }

        const workspaceRoot = workspaceFolders[0].uri.fsPath;
        const workspaceEdit = new vscode.WorkspaceEdit();
        const filePath = vscode.Uri.file(`${workspaceRoot}/${change.file}`);

        try {
            if (change.type === 'create_file') {
                const newContent = change.content || '';

                // Check if file already exists
                try {
                    const existingDoc = await vscode.workspace.openTextDocument(filePath);
                    const existingContent = existingDoc.getText();

                    if (existingContent === newContent) {
                        // File exists with identical content - skip
                        console.log(`⏭️ Skipping [${change.id}]: file ${change.file} already has identical content`);
                        return { success: true, changeId: change.id, skipped: true };
                    } else {
                        // File exists with different content - replace entire file
                        console.log(`🔄 Replacing [${change.id}]: file ${change.file} exists with different content`);
                        const fullRange = new vscode.Range(
                            new vscode.Position(0, 0),
                            new vscode.Position(existingDoc.lineCount, 0)
                        );
                        workspaceEdit.replace(filePath, fullRange, newContent);
                    }
                } catch {
                    // File doesn't exist, create it
                    workspaceEdit.createFile(filePath, { overwrite: false, ignoreIfExists: false });
                    workspaceEdit.insert(filePath, new vscode.Position(0, 0), newContent);
                }
            } else if (change.type === 'replace_range') {
                // Replace range in existing file
                let doc: vscode.TextDocument;
                try {
                    doc = await vscode.workspace.openTextDocument(filePath);
                } catch (openError) {
                    const errMsg = openError instanceof Error ? openError.message : String(openError);
                    if (errMsg.includes('ENOENT') || errMsg.includes('does not exist')) {
                        return {
                            success: false,
                            changeId: change.id,
                            error: `File does not exist: ${change.file}`
                        };
                    }
                    return { success: false, changeId: change.id, error: `Cannot open file: ${errMsg}` };
                }

                // Validate range against actual file
                const requestedStart = change.range?.start || 1;
                const requestedEnd = change.range?.end || 1;

                if (requestedStart > doc.lineCount) {
                    return {
                        success: false,
                        changeId: change.id,
                        error: `Range conflict: line ${requestedStart} does not exist (file has ${doc.lineCount} lines)`
                    };
                }

                // Convert 1-based line numbers to 0-based, with bounds checking
                const startLine = Math.max(0, requestedStart - 1);
                const endLine = Math.max(0, Math.min(doc.lineCount - 1, requestedEnd - 1));

                // Warn if range was adjusted
                if (requestedEnd > doc.lineCount) {
                    console.warn(`Range adjusted: requested end line ${requestedEnd} exceeds file length ${doc.lineCount}`);
                }

                const startPos = new vscode.Position(startLine, 0);
                // End position: end of the endLine (or start of next line)
                const endPos = endLine < doc.lineCount - 1
                    ? new vscode.Position(endLine + 1, 0)
                    : new vscode.Position(endLine, doc.lineAt(endLine).text.length);

                const range = new vscode.Range(startPos, endPos);

                // Get the current content in the range
                const currentContent = doc.getText(range);

                // Ensure content ends with newline if we're replacing full lines
                let newContent = change.content || '';
                if (endLine < doc.lineCount - 1 && !newContent.endsWith('\n')) {
                    newContent += '\n';
                }

                // Skip if content is identical
                if (currentContent === newContent) {
                    console.log(`⏭️ Skipping [${change.id}]: range in ${change.file} already has identical content`);
                    return { success: true, changeId: change.id, skipped: true };
                }

                workspaceEdit.replace(filePath, range, newContent);
            }

            // Apply the edit
            const success = await vscode.workspace.applyEdit(workspaceEdit);

            if (success) {
                // Save the modified document
                await vscode.workspace.saveAll(false);
            } else {
                return {
                    success: false,
                    changeId: change.id,
                    error: 'VS Code rejected the edit. The file may be read-only or locked.'
                };
            }

            return { success, changeId: change.id };
        } catch (error) {
            const errorMessage = error instanceof Error ? error.message : String(error);

            // Provide more specific error messages
            if (errorMessage.includes('EACCES') || errorMessage.includes('permission')) {
                return { success: false, changeId: change.id, error: `Permission denied: ${change.file}` };
            }
            if (errorMessage.includes('EBUSY') || errorMessage.includes('locked')) {
                return { success: false, changeId: change.id, error: `File is locked: ${change.file}` };
            }

            return { success: false, changeId: change.id, error: errorMessage };
        }
    }
}

/**
 * In-memory content provider for showing modified content in diff view.
 */
class InMemoryContentProvider implements vscode.TextDocumentContentProvider {
    private contents = new Map<string, string>();
    private _onDidChange = new vscode.EventEmitter<vscode.Uri>();

    public onDidChange = this._onDidChange.event;

    public setContent(uri: vscode.Uri, content: string): void {
        this.contents.set(uri.toString(), content);
        this._onDidChange.fire(uri);
    }

    public provideTextDocumentContent(uri: vscode.Uri): string {
        return this.contents.get(uri.toString()) || '';
    }
}

/**
 * Get the singleton DiffPreviewService instance.
 */
export function getDiffPreviewService(): DiffPreviewService {
    return DiffPreviewService.getInstance();
}

