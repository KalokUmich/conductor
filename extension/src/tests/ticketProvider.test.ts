/**
 * Unit tests for ITicketProvider and ticket key extraction.
 *
 * Run after compilation:
 *   node --test out/tests/ticketProvider.test.js
 */
import { describe, it } from 'node:test';
import * as assert from 'node:assert/strict';

import { extractTicketKeys, formatTicketTag, parseTicketTags } from '../services/ticketProvider';

// ---------------------------------------------------------------------------
// extractTicketKeys
// ---------------------------------------------------------------------------

describe('extractTicketKeys', () => {
    const pattern = /\b[A-Z][A-Z0-9]+-\d+\b/;

    it('extracts single ticket key', () => {
        assert.deepEqual(extractTicketKeys('Fix DEV-123 bug', pattern), ['DEV-123']);
    });

    it('extracts multiple ticket keys', () => {
        assert.deepEqual(extractTicketKeys('Related to DEV-123 and HELP-42', pattern), ['DEV-123', 'HELP-42']);
    });

    it('deduplicates keys', () => {
        assert.deepEqual(extractTicketKeys('DEV-123 is related to DEV-123', pattern), ['DEV-123']);
    });

    it('returns empty for no matches', () => {
        assert.deepEqual(extractTicketKeys('No ticket here', pattern), []);
    });

    it('handles edge cases', () => {
        assert.deepEqual(extractTicketKeys('', pattern), []);
        assert.deepEqual(extractTicketKeys('AB-1', pattern), ['AB-1']);
        assert.deepEqual(extractTicketKeys('a-123', pattern), []); // lowercase
    });

    it('handles multi-digit project keys', () => {
        assert.deepEqual(extractTicketKeys('CR0-456', pattern), ['CR0-456']);
    });

    it('extracts from structured tags', () => {
        assert.deepEqual(extractTicketKeys('{jira:DEV-100} Fix the bug', pattern), ['DEV-100']);
    });

    it('extracts both tags and bare keys', () => {
        const result = extractTicketKeys('{jira:DEV-100} also see HELP-42', pattern);
        assert.ok(result.includes('DEV-100'));
        assert.ok(result.includes('HELP-42'));
    });
});

// ---------------------------------------------------------------------------
// formatTicketTag
// ---------------------------------------------------------------------------

describe('formatTicketTag', () => {
    it('formats jira tag', () => {
        assert.equal(formatTicketTag('jira', 'DEV-123'), '{jira:DEV-123}');
    });

    it('formats github tag', () => {
        assert.equal(formatTicketTag('github', '#42'), '{github:#42}');
    });
});

// ---------------------------------------------------------------------------
// parseTicketTags
// ---------------------------------------------------------------------------

describe('parseTicketTags', () => {
    it('parses single tag', () => {
        const result = parseTicketTags('Fix {jira:DEV-123} bug');
        assert.equal(result.length, 1);
        assert.equal(result[0].provider, 'jira');
        assert.equal(result[0].key, 'DEV-123');
    });

    it('parses multiple tags', () => {
        const result = parseTicketTags('{jira:DEV-1} and {github:#42}');
        assert.equal(result.length, 2);
        assert.equal(result[0].provider, 'jira');
        assert.equal(result[1].provider, 'github');
    });

    it('returns empty for no tags', () => {
        assert.deepEqual(parseTicketTags('no tags here'), []);
    });
});
