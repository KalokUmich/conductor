/**
 * XML prompt assembler for Conductor.
 *
 * Converts a set of ranked code snippets and a user question into a
 * well-formed XML string ready for submission to an LLM.
 *
 * Output schema
 * -------------
 * ```xml
 * <context>
 *   <file path="src/app.ts" role="current"><![CDATA[...]]></file>
 *   <file path="src/utils.ts" role="definition"><![CDATA[...]]></file>
 *   <file path="src/types.ts" role="related"><![CDATA[...]]></file>
 * </context>
 * <question><![CDATA[...]]></question>
 * ```
 *
 * Constraints
 * -----------
 * - **Max 80 000 characters total** — snippets are trimmed in reverse-priority
 *   order (related files first, then definition, finally current file) until
 *   the assembled XML fits within the budget.
 * - **Stable deterministic ordering** — current file is always first, then
 *   definition, then related files in the order supplied by the caller.
 * - **CDATA wrapping** — all code content is wrapped in `<![CDATA[…]]>` so
 *   indentation, angle brackets, and special characters are preserved verbatim.
 *   A `]]>` sequence inside content is escaped as `]]]]><![CDATA[>`.
 * - **File path attributes** — every `<file>` element carries a `path` attribute
 *   and a `role` attribute (`current` | `definition` | `related`).
 * - **No external dependencies** — pure string manipulation, no DOM.
 *
 * @module services/xmlPromptAssembler
 */

// ---------------------------------------------------------------------------
// Public constants
// ---------------------------------------------------------------------------

/** Hard character budget for the entire assembled XML string. */
export const MAX_TOTAL_CHARS = 80_000;

/** Overhead characters reserved for XML tags and question wrapper. */
const TAG_OVERHEAD = 512;

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export type SnippetRole = 'current' | 'definition' | 'related';

export interface FileSnippet {
    /** Workspace-relative path shown in the `path` attribute. */
    path: string;
    /** Raw code content — will be CDATA-wrapped. */
    content: string;
    /** Semantic role of this snippet. */
    role: SnippetRole;
}

export interface AssemblerInput {
    /** Snippet from the file containing the cursor (role = "current"). */
    currentFile: FileSnippet;
    /** Snippet from the LSP definition file (role = "definition"; optional). */
    definition?: FileSnippet;
    /** Snippets from related files in ranked order (role = "related"). */
    relatedFiles: FileSnippet[];
    /** The user's question or instruction. */
    question: string;
}

export interface AssembleResult {
    /** The assembled XML string ready for the LLM. */
    xml: string;
    /** Whether any snippets were trimmed to fit within MAX_TOTAL_CHARS. */
    wasTrimmed: boolean;
    /** Number of characters in the final XML. */
    charCount: number;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Assemble the XML prompt from ranked context snippets and a user question.
 *
 * Snippets are incorporated in a fixed order:
 *   1. `currentFile`   (always present)
 *   2. `definition`    (if provided)
 *   3. `relatedFiles`  (in supplied order)
 *
 * If the assembled string would exceed `MAX_TOTAL_CHARS`, content is trimmed
 * starting from the lowest-priority items (related files in reverse order,
 * then definition, finally current file).  Trimming truncates the content
 * string and appends a `\n… [truncated]` marker inside the CDATA.
 *
 * @param input    All context snippets and the user question.
 * @param maxChars Character budget (default: MAX_TOTAL_CHARS).
 */
export function assembleXmlPrompt(
    input: AssemblerInput,
    maxChars: number = MAX_TOTAL_CHARS,
): AssembleResult {
    // ---- Build the ordered list of snippets (stable, deterministic) ----------
    const snippets: FileSnippet[] = [];
    snippets.push(input.currentFile);
    if (input.definition) snippets.push(input.definition);
    for (const r of input.relatedFiles) snippets.push(r);

    // ---- Iteratively trim until the assembled XML fits ----------------------
    let wasTrimmed = false;
    // Keep a mutable copy of content lengths; we never mutate the originals.
    const contents = snippets.map(s => s.content);

    for (let attempt = 0; attempt < snippets.length + 1; attempt++) {
        const xml = _assemble(snippets, contents, input.question);
        if (xml.length <= maxChars) {
            return { xml, wasTrimmed, charCount: xml.length };
        }
        wasTrimmed = true;

        // Find the lowest-priority snippet that still has content to trim.
        // Priority (trimming order): related (last → first), definition, current.
        const trimIdx = _nextTrimTarget(contents, snippets);
        if (trimIdx === -1) break; // nothing left to trim

        const excess   = xml.length - maxChars + TAG_OVERHEAD;
        const trimmed  = _trimContent(contents[trimIdx], excess);
        contents[trimIdx] = trimmed;
    }

    // Last-resort: return whatever fits in the budget (edge case for huge questions).
    const xml = _assemble(snippets, contents, input.question).slice(0, maxChars);
    return { xml, wasTrimmed: true, charCount: xml.length };
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

/** Escape `]]>` inside CDATA content so the closing delimiter is never present. */
function _escapeCdata(text: string): string {
    return text.replace(/]]>/g, ']]]]><![CDATA[>');
}

/** Wrap text in a CDATA section. */
function _cdata(text: string): string {
    return `<![CDATA[${_escapeCdata(text)}]]>`;
}

/** Escape a string for use in an XML attribute value (double-quoted). */
function _attr(value: string): string {
    return value
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

/** Render a single `<file>` element. */
function _fileElement(snippet: FileSnippet, content: string): string {
    const path = _attr(snippet.path);
    const role = _attr(snippet.role);
    return `  <file path="${path}" role="${role}">${_cdata(content)}</file>`;
}

/** Assemble the full XML string from current content values. */
function _assemble(
    snippets: FileSnippet[],
    contents: string[],
    question: string,
): string {
    const fileElems = snippets
        .map((s, i) => _fileElement(s, contents[i]))
        .join('\n');

    return `<context>\n${fileElems}\n</context>\n<question>${_cdata(question)}</question>`;
}

/**
 * Return the index of the next snippet to trim, in priority order:
 *   related files last-to-first, then definition, then current.
 * Returns -1 if all contents are already empty or just the truncation marker.
 */
function _nextTrimTarget(contents: string[], snippets: FileSnippet[]): number {
    // Build a trim-priority ordering: related (reversed) → definition → current.
    const order: number[] = [];
    for (let i = snippets.length - 1; i >= 0; i--) {
        if (snippets[i].role === 'related') order.push(i);
    }
    for (let i = 0; i < snippets.length; i++) {
        if (snippets[i].role === 'definition') { order.push(i); break; }
    }
    order.push(0); // current file is always index 0

    for (const idx of order) {
        if (contents[idx].length > 0) return idx;
    }
    return -1;
}

/**
 * Trim `content` by approximately `excess` characters, appending a marker.
 * The result always ends with `\n… [truncated]` to signal truncation.
 */
function _trimContent(content: string, excess: number): string {
    const MARKER = '\n… [truncated]';
    const targetLen = Math.max(0, content.length - excess - MARKER.length);
    if (targetLen === 0) return '';
    // Trim at the last newline before targetLen for a clean cut.
    const slice = content.slice(0, targetLen);
    const lastNl = slice.lastIndexOf('\n');
    const clean = lastNl > 0 ? slice.slice(0, lastNl) : slice;
    return clean + MARKER;
}
