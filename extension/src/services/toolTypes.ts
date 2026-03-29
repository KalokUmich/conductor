/**
 * Shared types for local tool execution.
 *
 * Extracted into a standalone module to avoid circular imports between
 * localToolDispatcher ↔ complexToolRunner / astToolRunner.
 *
 * @module services/toolTypes
 */

/** Standard tool result shape, matching the backend's ToolResult model. */
export interface ToolResult {
    success: boolean;
    data: unknown;
    error?: string;
    truncated?: boolean;
}
