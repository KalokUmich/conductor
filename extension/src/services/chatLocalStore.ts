/**
 * Local filesystem cache for chat messages — ChatRecord v2.
 *
 * v2 normalizes user info into a `participants` map (UUID → name/role/email)
 * so messages carry only a `sender` UUID. AI messages get `aiMeta` (model,
 * tokens, thinking steps). Old v1 files are migrated transparently on read.
 *
 * Storage path: {globalStorageUri}/chat_history/{room_id}.json
 * Cap: MAX_MESSAGES_PER_ROOM most recent messages.
 *
 * @module services/chatLocalStore
 */

import * as fs from 'fs';
import * as path from 'path';

const MAX_MESSAGES_PER_ROOM = 2000;

// ── ChatRecord v2 on-disk types ──────────────────────────────

export interface Participant {
    email?: string;
    name: string;
    role: 'host' | 'engineer' | 'ai';
    status: 'active' | 'left';
    avatarColor?: number;
    identitySource?: string;
}

export interface AIMeta {
    model?: string;
    tokensIn?: number;
    tokensOut?: number;
    thinkingSteps?: unknown[];
}

/** Normalized message stored on disk (v2). */
export interface ChatRecordMessage {
    id: string;
    sender: string;             // UUID from participants map, or "system"
    type: string;
    content: string;
    ts: number;
    aiMeta?: AIMeta;
    metadata?: Record<string, unknown>;
    codeSnippet?: Record<string, unknown>;
    parentMessageId?: string;
    // File fields
    fileId?: string;
    originalFilename?: string;
    fileType?: string;
    mimeType?: string;
    sizeBytes?: number;
    downloadUrl?: string;
    caption?: string;
    // AI fields
    answer?: string;
    summary?: string;
    codePrompt?: string;
    // Stack trace / test failures (opaque)
    stackTrace?: unknown;
    testFailures?: unknown;
}

/** v2 on-disk wrapper — normalized participants + sender-based messages. */
export interface ChatRecord {
    version: 2;
    roomId: string;
    mode?: 'local' | 'online';
    participants: Record<string, Participant>;
    lastSyncTs: number;
    messageCount: number;
    messages: ChatRecordMessage[];
}

// ── Legacy v1 types (for migration) ──────────────────────────

/** v1 message shape (denormalized — carried userId, displayName, role per message). */
interface LegacyMessageLocal {
    id: string;
    type: string;
    roomId: string;
    userId: string;
    displayName: string;
    role: string;
    content: string;
    ts: number;
    aiData?: Record<string, unknown>;
    metadata?: Record<string, unknown>;
    codeSnippet?: Record<string, unknown>;
    identitySource?: string;
    parentMessageId?: string;
    thinkingSteps?: unknown[];
    [key: string]: unknown;
}

interface LegacyMessageCache {
    roomId: string;
    lastSyncTs: number;
    messageCount: number;
    messages: LegacyMessageLocal[];
}

// ── Denormalized shape (sent to WebView for backward compat) ─

export interface DenormalizedMessage {
    id: string;
    userId: string;
    displayName: string;
    role: string;
    sender: string;
    type: string;
    content: string;
    ts: number;
    aiMeta?: AIMeta;
    identitySource?: string;
    metadata?: Record<string, unknown>;
    codeSnippet?: Record<string, unknown>;
    parentMessageId?: string;
    fileId?: string;
    originalFilename?: string;
    fileType?: string;
    mimeType?: string;
    sizeBytes?: number;
    downloadUrl?: string;
    caption?: string;
    answer?: string;
    summary?: string;
    codePrompt?: string;
    stackTrace?: unknown;
    testFailures?: unknown;
    [key: string]: unknown;
}

// ── AI message type detection ────────────────────────────────

const AI_MESSAGE_TYPES = new Set([
    'ai_answer', 'ai_explanation', 'ai_summary', 'ai_code_prompt',
]);

// ── Migration: v1 → v2 ──────────────────────────────────────

function migrateV1toV2(legacy: LegacyMessageCache): ChatRecord {
    const participants: Record<string, Participant> = {};

    // Build participants from unique userId values across all messages
    for (const msg of legacy.messages) {
        if (!msg.userId || msg.userId === 'system') continue;
        if (participants[msg.userId]) continue;

        const isAI = AI_MESSAGE_TYPES.has(msg.type) || msg.role === 'ai';
        participants[msg.userId] = {
            name: msg.displayName || 'Unknown',
            role: isAI ? 'ai' : (msg.role === 'host' ? 'host' : 'engineer'),
            status: 'active',
            identitySource: msg.identitySource,
        };
    }

    // Convert messages: sender = userId, move thinkingSteps into aiMeta
    const messages: ChatRecordMessage[] = legacy.messages.map((msg) => {
        const rec: ChatRecordMessage = {
            id: msg.id,
            sender: msg.userId || 'system',
            type: msg.type,
            content: msg.content,
            ts: msg.ts,
        };

        // AI metadata
        if (AI_MESSAGE_TYPES.has(msg.type)) {
            const aiMeta: AIMeta = {};
            // Parse model name from userId "AI-{model}" pattern
            if (msg.userId?.startsWith('AI-')) {
                aiMeta.model = msg.userId.slice(3);
            }
            if (msg.thinkingSteps && Array.isArray(msg.thinkingSteps) && msg.thinkingSteps.length > 0) {
                aiMeta.thinkingSteps = msg.thinkingSteps;
            }
            if (aiMeta.model || aiMeta.thinkingSteps) {
                rec.aiMeta = aiMeta;
            }
        }

        // Carry forward optional fields
        if (msg.metadata) rec.metadata = msg.metadata;
        if (msg.codeSnippet) rec.codeSnippet = msg.codeSnippet;
        if (msg.parentMessageId) rec.parentMessageId = msg.parentMessageId;
        if (msg.fileId) rec.fileId = msg.fileId as string;
        if (msg.originalFilename) rec.originalFilename = msg.originalFilename as string;
        if (msg.fileType) rec.fileType = msg.fileType as string;
        if (msg.mimeType) rec.mimeType = msg.mimeType as string;
        if (msg.sizeBytes) rec.sizeBytes = msg.sizeBytes as number;
        if (msg.downloadUrl) rec.downloadUrl = msg.downloadUrl as string;
        if (msg.caption) rec.caption = msg.caption as string;
        if (msg.answer) rec.answer = msg.answer as string;
        if (msg.summary) rec.summary = msg.summary as string;
        if (msg.codePrompt || msg.code_prompt) rec.codePrompt = (msg.codePrompt || msg.code_prompt) as string;
        if (msg.stackTrace) rec.stackTrace = msg.stackTrace;
        if (msg.testFailures) rec.testFailures = msg.testFailures;

        return rec;
    });

    return {
        version: 2,
        roomId: legacy.roomId,
        participants,
        lastSyncTs: legacy.lastSyncTs,
        messageCount: legacy.messageCount,
        messages,
    };
}

// ── Denormalization: ChatRecord → flat messages for WebView ──

export function denormalizeMessages(record: ChatRecord): DenormalizedMessage[] {
    return record.messages.map((msg) => {
        const participant = record.participants[msg.sender];
        return {
            ...msg,
            userId: msg.sender,
            displayName: participant?.name || 'Unknown',
            role: participant?.role || 'engineer',
            sender: msg.sender,
            identitySource: participant?.identitySource,
        };
    });
}

// ── Store class ──────────────────────────────────────────────

export class ChatLocalStore {
    private readonly baseDir: string;

    constructor(globalStoragePath: string) {
        this.baseDir = path.join(globalStoragePath, 'chat_history');
        if (!fs.existsSync(this.baseDir)) {
            fs.mkdirSync(this.baseDir, { recursive: true });
        }
    }

    private filePath(roomId: string): string {
        const safe = roomId.replace(/[^a-zA-Z0-9_-]/g, '_');
        return path.join(this.baseDir, `${safe}.json`);
    }

    // ------------------------------------------------------------------
    // Write
    // ------------------------------------------------------------------

    /** Save a full ChatRecord (v2) for a room. */
    async saveRecord(roomId: string, record: ChatRecord): Promise<void> {
        const trimmed = record.messages.slice(-MAX_MESSAGES_PER_ROOM);
        const out: ChatRecord = {
            ...record,
            messages: trimmed,
            messageCount: trimmed.length,
            lastSyncTs: trimmed.length > 0 ? trimmed[trimmed.length - 1].ts : 0,
        };
        const fp = this.filePath(roomId);
        await fs.promises.writeFile(fp, JSON.stringify(out), 'utf-8');
    }

    /** Overwrite the full cache for a room (backward-compat wrapper). */
    async saveMessages(roomId: string, messages: ChatRecordMessage[], participants?: Record<string, Participant>): Promise<void> {
        const existing = await this.loadRecord(roomId);
        const mergedParticipants = { ...(existing?.participants || {}), ...(participants || {}) };
        const record: ChatRecord = {
            version: 2,
            roomId,
            participants: mergedParticipants,
            lastSyncTs: 0,
            messageCount: 0,
            messages,
        };
        await this.saveRecord(roomId, record);
    }

    /** Append new messages (dedup by id, cap at MAX). */
    async appendMessages(
        roomId: string,
        newMessages: ChatRecordMessage[],
        participants?: Record<string, Participant>,
    ): Promise<void> {
        const existing = await this.loadRecord(roomId);
        const existingMsgs = existing ? existing.messages : [];
        const existingIds = new Set(existingMsgs.map(m => m.id));
        const merged = [
            ...existingMsgs,
            ...newMessages.filter(m => !existingIds.has(m.id)),
        ];
        const mergedParticipants = { ...(existing?.participants || {}), ...(participants || {}) };
        const record: ChatRecord = {
            version: 2,
            roomId,
            participants: mergedParticipants,
            lastSyncTs: 0,
            messageCount: 0,
            messages: merged,
        };
        await this.saveRecord(roomId, record);
    }

    /** Add or update a participant in a stored ChatRecord. */
    async upsertParticipant(roomId: string, uuid: string, participant: Participant): Promise<void> {
        const record = await this.loadRecord(roomId);
        if (!record) return;
        record.participants[uuid] = participant;
        await this.saveRecord(roomId, record);
    }

    // ------------------------------------------------------------------
    // Read
    // ------------------------------------------------------------------

    /** Load a ChatRecord (v2). Auto-migrates v1 on first read. */
    async loadRecord(roomId: string): Promise<ChatRecord | null> {
        const fp = this.filePath(roomId);
        try {
            if (!fs.existsSync(fp)) return null;
            const raw = await fs.promises.readFile(fp, 'utf-8');
            const data = JSON.parse(raw);
            if (!data || typeof data !== 'object') return null;

            // v2 — already in new format
            if (data.version === 2 && data.participants && Array.isArray(data.messages)) {
                return data as ChatRecord;
            }

            // v1 — legacy format, migrate
            if (Array.isArray(data.messages)) {
                const v2 = migrateV1toV2(data as LegacyMessageCache);
                // Lazy migration: re-save as v2
                await fs.promises.writeFile(fp, JSON.stringify(v2), 'utf-8');
                return v2;
            }

            return null;
        } catch {
            return null;
        }
    }

    /** Load and denormalize messages for WebView (backward compat). */
    async loadDenormalized(roomId: string): Promise<{
        messages: DenormalizedMessage[];
        participants: Record<string, Participant>;
    } | null> {
        const record = await this.loadRecord(roomId);
        if (!record) return null;
        return {
            messages: denormalizeMessages(record),
            participants: record.participants,
        };
    }

    /** Get the timestamp of the newest locally cached message. */
    async getLatestTimestamp(roomId: string): Promise<number> {
        const record = await this.loadRecord(roomId);
        return record ? record.lastSyncTs : 0;
    }

    /** Get the UUID of the last locally cached message (for incremental sync). */
    async getLastMessageId(roomId: string): Promise<string | null> {
        const record = await this.loadRecord(roomId);
        if (!record || record.messages.length === 0) return null;
        return record.messages[record.messages.length - 1].id;
    }

    // ------------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------------

    /** Delete local cache for a room. */
    async clearRoom(roomId: string): Promise<void> {
        const fp = this.filePath(roomId);
        try {
            if (fs.existsSync(fp)) {
                await fs.promises.unlink(fp);
            }
        } catch {
            // ignore
        }
    }

    /** List room IDs that have local caches. */
    async listRooms(): Promise<string[]> {
        try {
            const files = await fs.promises.readdir(this.baseDir);
            return files
                .filter(f => f.endsWith('.json'))
                .map(f => f.replace('.json', ''));
        } catch {
            return [];
        }
    }
}

// ── Conversion helper: raw WS message → ChatRecordMessage ───

/**
 * Convert a raw WebSocket/backend message object into a ChatRecordMessage
 * for storage. Extracts sender from userId, builds aiMeta for AI messages.
 */
export function toRecordMessage(raw: Record<string, unknown>): ChatRecordMessage {
    const type = (raw.type as string) || 'text';
    const userId = (raw.userId as string) || '';

    const rec: ChatRecordMessage = {
        id: (raw.id as string) || `msg-${Date.now()}-${Math.random()}`,
        sender: userId || 'system',
        type,
        content: (raw.content as string) || '',
        ts: (raw.ts as number) || Date.now() / 1000,
    };

    // AI metadata
    if (AI_MESSAGE_TYPES.has(type)) {
        const aiMeta: AIMeta = {};
        if (userId.startsWith('AI-')) {
            aiMeta.model = userId.slice(3);
        }
        const steps = raw.thinkingSteps as unknown[] | undefined;
        if (steps && Array.isArray(steps) && steps.length > 0) {
            aiMeta.thinkingSteps = steps;
        }
        if (aiMeta.model || aiMeta.thinkingSteps) {
            rec.aiMeta = aiMeta;
        }
    }

    // Carry forward optional fields
    if (raw.metadata) rec.metadata = raw.metadata as Record<string, unknown>;
    if (raw.codeSnippet) rec.codeSnippet = raw.codeSnippet as Record<string, unknown>;
    if (raw.parentMessageId) rec.parentMessageId = raw.parentMessageId as string;
    if (raw.fileId) rec.fileId = raw.fileId as string;
    if (raw.originalFilename) rec.originalFilename = raw.originalFilename as string;
    if (raw.fileType) rec.fileType = raw.fileType as string;
    if (raw.mimeType) rec.mimeType = raw.mimeType as string;
    if (raw.sizeBytes) rec.sizeBytes = raw.sizeBytes as number;
    if (raw.downloadUrl) rec.downloadUrl = raw.downloadUrl as string;
    if (raw.caption) rec.caption = raw.caption as string;
    if (raw.answer) rec.answer = raw.answer as string;
    if (raw.summary) rec.summary = raw.summary as string;
    if (raw.codePrompt || raw.code_prompt) rec.codePrompt = (raw.codePrompt || raw.code_prompt) as string;
    if (raw.stackTrace) rec.stackTrace = raw.stackTrace;
    if (raw.testFailures) rec.testFailures = raw.testFailures;

    return rec;
}

/**
 * Build a Participant entry from a raw WS user/message object.
 */
export function toParticipant(raw: Record<string, unknown>): Participant {
    const role = (raw.role as string) || 'engineer';
    const isAI = role === 'ai' || AI_MESSAGE_TYPES.has((raw.type as string) || '');
    return {
        name: (raw.displayName as string) || 'Unknown',
        role: isAI ? 'ai' : (role === 'host' ? 'host' : 'engineer'),
        status: 'active',
        email: (raw.ssoEmail as string) || (raw.email as string) || undefined,
        identitySource: (raw.identitySource as string) || undefined,
        avatarColor: (raw.avatarColor as number) || undefined,
    };
}
