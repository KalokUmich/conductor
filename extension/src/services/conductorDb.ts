/**
 * @deprecated Use backend RAG (ragClient.ts) instead. Metadata storage is now
 * handled server-side by the FAISS-based RAG pipeline (2.2).
 *
 * SQLite-based metadata storage for the Conductor context enricher.
 *
 * Uses `better-sqlite3` for synchronous, WAL-mode access to a local
 * `cache.db` inside the `.conductor/` workspace directory.
 *
 * @module services/conductorDb
 */

import Database from 'better-sqlite3';

// ---------------------------------------------------------------------------
// Row types
// ---------------------------------------------------------------------------

/** Metadata for a tracked workspace file. */
export interface FileMeta {
    path: string;
    mtime: number;
    size: number;
    lang: string;
    sha1: string;
    last_indexed_at: number | null;
}

/** A code symbol extracted from a file. */
export interface SymbolRow {
    id: string;
    path: string;
    name: string;
    kind: string;
    start_line: number;
    end_line: number;
    signature: string;
}

/**
 * A cloud-generated embedding vector stored alongside the symbol it describes.
 *
 * The `sha1` and `model` pair acts as a cache key: if neither changes, the
 * stored vector remains valid and no re-embedding is needed.
 */
export interface SymbolVectorRow {
    symbol_id: string;
    /** Dimensionality of the vector (number of float32 elements). */
    dim: number;
    /** Raw Float32Array bytes (little-endian IEEE 754). */
    vector: Buffer;
    /** Embedding model ID used to produce this vector, e.g. "cohere.embed-v4". */
    model: string;
    /** SHA-1 of the source text that was embedded (symbol signature). */
    sha1: string;
}

// ---------------------------------------------------------------------------
// Schema SQL
// ---------------------------------------------------------------------------

const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS files (
    path            TEXT PRIMARY KEY,
    mtime           REAL NOT NULL,
    size            INTEGER NOT NULL,
    lang            TEXT NOT NULL DEFAULT '',
    sha1            TEXT NOT NULL DEFAULT '',
    last_indexed_at REAL
);

CREATE TABLE IF NOT EXISTS symbols (
    id         TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT '',
    start_line INTEGER NOT NULL,
    end_line   INTEGER NOT NULL,
    signature  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_symbols_path ON symbols(path);

CREATE TABLE IF NOT EXISTS symbol_vectors (
    symbol_id  TEXT PRIMARY KEY,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    sha1       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lsp_defs (
    key     TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lsp_refs (
    key     TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
`;

const EXPECTED_TABLES = ['files', 'symbols', 'symbol_vectors', 'lsp_defs', 'lsp_refs', 'meta'];

// ---------------------------------------------------------------------------
// Repository class
// ---------------------------------------------------------------------------

export class ConductorDb {
    private db: Database.Database;

    // Prepared statements
    private stmtUpsertFile: Database.Statement;
    private stmtGetStale: Database.Statement;
    private stmtGetAllFiles: Database.Statement;
    private stmtDeleteSymbolsByPath: Database.Statement;
    private stmtInsertSymbol: Database.Statement;
    private stmtGetSymbolsByPath: Database.Statement;
    private stmtUpsertSymbolVector: Database.Statement;
    private stmtGetSymbolVector: Database.Statement;
    private stmtGetAllVectorsByModel: Database.Statement;
    private stmtUpsertLspDef: Database.Statement;
    private stmtGetLspDef: Database.Statement;
    private stmtUpsertLspRefs: Database.Statement;
    private stmtGetLspRefs: Database.Statement;
    private stmtSetMeta: Database.Statement;
    private stmtGetMeta: Database.Statement;
    private stmtGetSymbolsByName: Database.Statement;
    private stmtGetSymbolByPathAndName: Database.Statement;

    constructor(dbPath: string) {
        this.db = new Database(dbPath);
        this.db.pragma('journal_mode = WAL');
        this.db.exec(SCHEMA_SQL);

        // ---- Schema migrations ----

        // files.sha1 (added after initial release)
        try {
            this.db.exec("ALTER TABLE files ADD COLUMN sha1 TEXT NOT NULL DEFAULT ''");
        } catch { /* column already exists */ }

        // symbol_vectors: migrate from the old FAISS-based schema
        // (symbol_id, faiss_id INTEGER, dim) to the cloud-embedding schema
        // (symbol_id, dim, vector BLOB, model, sha1).
        this._migrateSymbolVectors();

        // ---- Prepared statements ----

        this.stmtUpsertFile = this.db.prepare(`
            INSERT OR REPLACE INTO files (path, mtime, size, lang, sha1, last_indexed_at)
            VALUES (@path, @mtime, @size, @lang, @sha1, @last_indexed_at)
        `);

        this.stmtGetStale = this.db.prepare(`
            SELECT * FROM files
            WHERE last_indexed_at IS NULL OR last_indexed_at < mtime
        `);

        this.stmtGetAllFiles = this.db.prepare('SELECT * FROM files');

        this.stmtDeleteSymbolsByPath = this.db.prepare(
            'DELETE FROM symbols WHERE path = ?',
        );

        this.stmtInsertSymbol = this.db.prepare(`
            INSERT INTO symbols (id, path, name, kind, start_line, end_line, signature)
            VALUES (@id, @path, @name, @kind, @start_line, @end_line, @signature)
        `);

        this.stmtGetSymbolsByPath = this.db.prepare(
            'SELECT * FROM symbols WHERE path = ?',
        );

        this.stmtUpsertSymbolVector = this.db.prepare(`
            INSERT OR REPLACE INTO symbol_vectors (symbol_id, dim, vector, model, sha1)
            VALUES (@symbol_id, @dim, @vector, @model, @sha1)
        `);

        this.stmtGetSymbolVector = this.db.prepare(
            'SELECT * FROM symbol_vectors WHERE symbol_id = ?',
        );

        this.stmtGetAllVectorsByModel = this.db.prepare(
            'SELECT * FROM symbol_vectors WHERE model = ?',
        );

        this.stmtUpsertLspDef = this.db.prepare(`
            INSERT OR REPLACE INTO lsp_defs (key, payload)
            VALUES (?, ?)
        `);

        this.stmtGetLspDef = this.db.prepare(
            'SELECT payload FROM lsp_defs WHERE key = ?',
        );

        this.stmtUpsertLspRefs = this.db.prepare(`
            INSERT OR REPLACE INTO lsp_refs (key, payload)
            VALUES (?, ?)
        `);

        this.stmtGetLspRefs = this.db.prepare(
            'SELECT payload FROM lsp_refs WHERE key = ?',
        );

        this.stmtSetMeta = this.db.prepare(
            'INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)',
        );

        this.stmtGetMeta = this.db.prepare(
            'SELECT value FROM meta WHERE key = ?',
        );

        this.stmtGetSymbolsByName = this.db.prepare(
            `SELECT id, path, name, kind, start_line, end_line, signature
               FROM symbols
              WHERE name = ?
              ORDER BY path
              LIMIT 10`,
        );

        this.stmtGetSymbolByPathAndName = this.db.prepare(
            `SELECT id, path, name, kind, start_line, end_line, signature
               FROM symbols
              WHERE path = ? AND name = ?
              LIMIT 1`,
        );
    }

    // -----------------------------------------------------------------------
    // Schema migration helpers
    // -----------------------------------------------------------------------

    /**
     * Detect and migrate the old FAISS-based `symbol_vectors` schema to the
     * new cloud-embedding schema.
     *
     * Old columns: `symbol_id TEXT PK, faiss_id INTEGER, dim INTEGER`
     * New columns: `symbol_id TEXT PK, dim INTEGER, vector BLOB, model TEXT, sha1 TEXT`
     *
     * Any existing FAISS vectors are discarded — they are not compatible with
     * the new cloud embedding format.
     */
    private _migrateSymbolVectors(): void {
        const cols = this.db
            .prepare('PRAGMA table_info(symbol_vectors)')
            .all() as Array<{ name: string }>;

        if (cols.length > 0 && cols.some(c => c.name === 'faiss_id')) {
            // Old FAISS schema — drop and recreate.
            this.db.exec(`
                DROP TABLE symbol_vectors;
                CREATE TABLE symbol_vectors (
                    symbol_id  TEXT PRIMARY KEY,
                    dim        INTEGER NOT NULL,
                    vector     BLOB NOT NULL,
                    model      TEXT NOT NULL DEFAULT '',
                    sha1       TEXT NOT NULL DEFAULT ''
                );
            `);
        }
    }

    // -----------------------------------------------------------------------
    // Files
    // -----------------------------------------------------------------------

    /** Insert or replace file metadata rows in a single transaction. */
    upsertFiles(files: FileMeta[]): void {
        const run = this.db.transaction((rows: FileMeta[]) => {
            for (const row of rows) {
                this.stmtUpsertFile.run(row);
            }
        });
        run(files);
    }

    /** Return files that have never been indexed or whose mtime changed. */
    getFilesNeedingReindex(): FileMeta[] {
        return this.stmtGetStale.all() as FileMeta[];
    }

    /** Return all tracked file rows. */
    getAllFiles(): FileMeta[] {
        return this.stmtGetAllFiles.all() as FileMeta[];
    }

    // -----------------------------------------------------------------------
    // Symbols
    // -----------------------------------------------------------------------

    /** Replace all symbols for a given file path (transactional). */
    replaceSymbolsForFile(path: string, symbols: SymbolRow[]): void {
        const run = this.db.transaction((p: string, rows: SymbolRow[]) => {
            this.stmtDeleteSymbolsByPath.run(p);
            for (const row of rows) {
                this.stmtInsertSymbol.run(row);
            }
        });
        run(path, symbols);
    }

    /** Return all symbols stored for a given file path. */
    getSymbolsForFile(path: string): SymbolRow[] {
        return this.stmtGetSymbolsByPath.all(path) as SymbolRow[];
    }

    // -----------------------------------------------------------------------
    // Symbol ↔ Cloud embedding vectors
    // -----------------------------------------------------------------------

    /**
     * Insert or replace a symbol's embedding vector.
     *
     * The `vector` field must contain the raw bytes of a `Float32Array`
     * (little-endian IEEE 754 floats, length = `dim`).
     */
    upsertSymbolVector(row: SymbolVectorRow): void {
        this.stmtUpsertSymbolVector.run(row);
    }

    /**
     * Retrieve the stored vector row for a symbol, or `null` if none exists.
     */
    getSymbolVector(symbolId: string): SymbolVectorRow | null {
        const row = this.stmtGetSymbolVector.get(symbolId) as SymbolVectorRow | undefined;
        return row ?? null;
    }

    /**
     * Return all vector rows stored for a given embedding model.
     *
     * Used by `VectorIndex.load()` to populate the in-memory search index.
     * Only rows whose `model` matches the requested value are returned,
     * so the caller always gets a model-homogeneous set.
     */
    getAllVectorsByModel(model: string): SymbolVectorRow[] {
        return this.stmtGetAllVectorsByModel.all(model) as SymbolVectorRow[];
    }

    /**
     * Return `true` when a symbol needs to be (re-)embedded.
     *
     * A symbol needs embedding if:
     * - no vector is stored yet, OR
     * - the stored `sha1` differs from the current content hash, OR
     * - the stored `model` differs from the currently configured model.
     *
     * This implements the "skip embedding if sha1 unchanged AND model unchanged"
     * rule from the V1 embedding scope.
     */
    needsEmbedding(symbolId: string, sha1: string, model: string): boolean {
        const row = this.getSymbolVector(symbolId);
        if (!row) return true;
        return row.sha1 !== sha1 || row.model !== model;
    }

    // -----------------------------------------------------------------------
    // LSP definition cache
    // -----------------------------------------------------------------------

    /** Cache an LSP definition lookup result. */
    cacheLspDef(key: string, payload: unknown): void {
        this.stmtUpsertLspDef.run(key, JSON.stringify(payload));
    }

    /** Retrieve a cached LSP definition, or `null` if missing. */
    getLspDef(key: string): unknown | null {
        const row = this.stmtGetLspDef.get(key) as { payload: string } | undefined;
        return row ? JSON.parse(row.payload) : null;
    }

    // -----------------------------------------------------------------------
    // LSP references cache
    // -----------------------------------------------------------------------

    /** Cache an LSP references lookup result. */
    cacheLspRefs(key: string, payload: unknown): void {
        this.stmtUpsertLspRefs.run(key, JSON.stringify(payload));
    }

    /** Retrieve cached LSP references, or `null` if missing. */
    getLspRefs(key: string): unknown | null {
        const row = this.stmtGetLspRefs.get(key) as { payload: string } | undefined;
        return row ? JSON.parse(row.payload) : null;
    }

    // -----------------------------------------------------------------------
    // Meta key-value store
    // -----------------------------------------------------------------------

    /** Persist an arbitrary metadata value (e.g. the last-indexed git branch). */
    setMeta(key: string, value: string): void {
        this.stmtSetMeta.run(key, value);
    }

    /** Retrieve a metadata value, or `null` if the key does not exist. */
    getMeta(key: string): string | null {
        const row = this.stmtGetMeta.get(key) as { value: string } | undefined;
        return row?.value ?? null;
    }

    /**
     * Find symbols whose name exactly matches `name` (case-sensitive).
     * Returns up to 10 results ordered by file path.
     * Used by the explain pipeline to locate type definitions.
     */
    getSymbolsByName(name: string): Array<{
        id: string; path: string; name: string; kind: string;
        start_line: number; end_line: number; signature: string;
    }> {
        return this.stmtGetSymbolsByName.all(name) as Array<{
            id: string; path: string; name: string; kind: string;
            start_line: number; end_line: number; signature: string;
        }>;
    }

    /**
     * Find a specific symbol within a known file path.
     * Returns the first match or `null` if not found.
     * Used when import resolution gives us the file and we need to locate
     * a specific class/function within it.
     */
    getSymbolByPathAndName(filePath: string, name: string): SymbolRow | null {
        const row = this.stmtGetSymbolByPathAndName.get(filePath, name) as SymbolRow | undefined;
        return row ?? null;
    }

    /** Return the total number of tracked files (fast COUNT query). */
    getFileCount(): number {
        const row = this.db.prepare('SELECT COUNT(*) AS cnt FROM files').get() as { cnt: number };
        return row.cnt;
    }

    // -----------------------------------------------------------------------
    // Bulk operations
    // -----------------------------------------------------------------------

    /** Delete all rows from every table including meta (single transaction). */
    clearAll(): void {
        this.db.exec(`
            DELETE FROM symbol_vectors;
            DELETE FROM symbols;
            DELETE FROM lsp_defs;
            DELETE FROM lsp_refs;
            DELETE FROM files;
            DELETE FROM meta;
        `);
    }

    // -----------------------------------------------------------------------
    // Lifecycle
    // -----------------------------------------------------------------------

    /** Verify that all expected tables exist and are readable. Throws on failure. */
    selfCheck(): void {
        for (const table of EXPECTED_TABLES) {
            this.db.prepare(`SELECT COUNT(*) AS cnt FROM ${table}`).get();
        }
    }

    /** Close the database connection. */
    close(): void {
        this.db.close();
    }
}
