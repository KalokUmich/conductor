import * as vscode from 'vscode';

/**
 * User roles for the AI Collab extension.
 */
export type Role = 'lead' | 'member';

/**
 * Features that can be permission-controlled.
 */
export type Feature = 
    | 'chat'
    | 'createSummary'
    | 'generateChanges'
    | 'autoApply';

/**
 * Permission matrix defining which roles can access which features.
 */
const PERMISSION_MATRIX: Record<Role, Set<Feature>> = {
    lead: new Set([
        'chat',
        'createSummary',
        'generateChanges',
        'autoApply'
    ]),
    member: new Set([
        'chat'
    ])
};

/**
 * Service for managing role-based permissions in the AI Collab extension.
 */
export class PermissionsService {
    private static instance: PermissionsService;

    private constructor() {}

    /**
     * Get the singleton instance of the PermissionsService.
     */
    public static getInstance(): PermissionsService {
        if (!PermissionsService.instance) {
            PermissionsService.instance = new PermissionsService();
        }
        return PermissionsService.instance;
    }

    /**
     * Get the current user role from VS Code settings.
     */
    public getRole(): Role {
        const config = vscode.workspace.getConfiguration('aiCollab');
        const role = config.get<string>('role', 'member');
        
        // Validate the role
        if (role === 'lead' || role === 'member') {
            return role;
        }
        
        // Default to member if invalid
        return 'member';
    }

    /**
     * Check if the current role has permission for a specific feature.
     */
    public hasPermission(feature: Feature): boolean {
        const role = this.getRole();
        return PERMISSION_MATRIX[role].has(feature);
    }

    /**
     * Get all features available to the current role.
     */
    public getAvailableFeatures(): Feature[] {
        const role = this.getRole();
        return Array.from(PERMISSION_MATRIX[role]);
    }

    /**
     * Check if the current user is a lead.
     */
    public isLead(): boolean {
        return this.getRole() === 'lead';
    }

    /**
     * Check if the current user is a member.
     */
    public isMember(): boolean {
        return this.getRole() === 'member';
    }

    /**
     * Get permissions data to pass to the WebView.
     * This returns a serializable object that can be sent to the frontend.
     */
    public getPermissionsForWebView(): WebViewPermissions {
        const role = this.getRole();
        return {
            role,
            canChat: this.hasPermission('chat'),
            canCreateSummary: this.hasPermission('createSummary'),
            canGenerateChanges: this.hasPermission('generateChanges'),
            canAutoApply: this.hasPermission('autoApply')
        };
    }
}

/**
 * Permissions data structure for the WebView.
 */
export interface WebViewPermissions {
    role: Role;
    canChat: boolean;
    canCreateSummary: boolean;
    canGenerateChanges: boolean;
    canAutoApply: boolean;
}

/**
 * Convenience function to get the permissions service instance.
 */
export function getPermissionsService(): PermissionsService {
    return PermissionsService.getInstance();
}

