/**
 * opf-identity-597.test.ts — the version-identifier derivation rule
 * (issue #597, `src/opfIdentity.ts`).
 *
 * Owner decision 2026-08-22, option (a): `identity.version` wins when the
 * artifact carries one; otherwise derive `<upload date>-<content_hash
 * prefix>`. The field is never operator-editable, so this derivation is the
 * ONLY source of the value — which is why the rule is pinned here as a pure
 * function rather than only through the component.
 *
 * No corpus text anywhere: the fixtures below are hand-built minimal OPF
 * shapes and a synthetic hash, per the ticket's own instruction.
 */
import { describe, expect, it } from 'vitest';
import { deriveVersionIdentifier, readOpfIdentity } from '../opfIdentity';

const HASH = 'sha256:73add98b1f2e3d4c5b6a70819283746556677889aabbccddeeff00112233445';
const UPLOADED = new Date(Date.UTC(2026, 7, 20, 13, 45, 0)); // 2026-08-20 UTC

/** The single-file `.opf.html` envelope, exactly as scripts/opf_html.py
 * writes it: canonical JSON embedded verbatim, `</` escaped to `<\/`. */
function wrapAsHtml(json: string): string {
  return [
    '<!doctype html><html><head><title>OPF playbook bundle</title></head><body>',
    '<script id="opf-canonical" type="application/json">',
    json.replace(/<\//g, '<\\/'),
    '</script>',
    '</body></html>',
  ].join('\n');
}

describe('readOpfIdentity', () => {
  it('reads identity out of a bare .opf.json document', () => {
    const text = JSON.stringify({
      opf_version: '0.3',
      identity: { id: 'synthetic-sample', version: '1.0.0', content_hash: HASH },
    });
    expect(readOpfIdentity(text)).toEqual({ version: '1.0.0', contentHash: HASH });
  });

  it('reads identity out of the embedded canonical block of an .opf.html bundle', () => {
    const json = JSON.stringify({
      opf_version: '0.3',
      identity: { id: 'synthetic-sample', content_hash: HASH },
    });
    expect(readOpfIdentity(wrapAsHtml(json))).toEqual({ contentHash: HASH });
  });

  it('does not let a </script> substring inside the JSON end the block early', () => {
    // The writer escapes `</` to `<\/` precisely so this cannot happen. If
    // extraction stopped at the FIRST `</script>` it saw, the recovered text
    // would be a truncated fragment and identity would be unreadable.
    //
    // Deliberately NOT asserted here: that the reader reverses the `<\/`
    // escape. It does (to mirror opf_html._unescape_json_from_script byte for
    // byte), but `\/` is already a legal escape for `/` inside a JSON string,
    // and `<` and `/` cannot appear outside one — so dropping the reversal
    // changes no parsed value, and a test claiming to guard it would pass
    // either way. Mutation-checked: removing the `.replace` leaves this file
    // fully green, which is why no assertion here pretends otherwise.
    const json = JSON.stringify({
      opf_version: '0.3',
      note: 'contains </script> in prose',
      identity: { content_hash: HASH },
    });
    const html = wrapAsHtml(json);
    expect(html).toContain('<\\/script>');
    expect(readOpfIdentity(html)).toEqual({ contentHash: HASH });
  });

  it('returns null for text that is not an OPF document at all', () => {
    expect(readOpfIdentity('not json')).toBeNull();
    expect(readOpfIdentity('<html><body>no canonical block</body></html>')).toBeNull();
  });

  it('returns an empty identity — not null — for a document that parses but carries none', () => {
    // The caller must be able to tell "unreadable file" from "readable file
    // with nothing to derive from"; both refuse the upload, but only one is
    // a parse failure.
    expect(readOpfIdentity(JSON.stringify({ opf_version: '0.3' }))).toEqual({});
  });

  it('treats a blank or non-string version as absent rather than as a value', () => {
    const text = JSON.stringify({
      identity: { version: '   ', content_hash: HASH },
    });
    expect(readOpfIdentity(text)).toEqual({ contentHash: HASH });
  });
});

describe('deriveVersionIdentifier', () => {
  it("uses the artifact's own identity.version verbatim when it has one", () => {
    expect(deriveVersionIdentifier({ version: '1.0.0', contentHash: HASH }, UPLOADED)).toBe(
      '1.0.0',
    );
  });

  it('derives <upload date>-<hash prefix> when identity.version is absent', () => {
    // The real production playbook (educational-affiliation) is exactly this
    // shape: opf_version 0.3, content_hash present, version absent.
    expect(deriveVersionIdentifier({ contentHash: HASH }, UPLOADED)).toBe(
      '2026-08-20-73add98b',
    );
  });

  it('derives the SAME identifier twice for byte-identical content', () => {
    // Which the server's append-only check then refuses on the second
    // upload — that is the feature, not a collision to design away.
    const first = deriveVersionIdentifier({ contentHash: HASH }, UPLOADED);
    const second = deriveVersionIdentifier({ contentHash: HASH }, UPLOADED);
    expect(second).toBe(first);
  });

  it('derives a DIFFERENT identifier for content that differs', () => {
    const other = `sha256:${'ff00'.repeat(16)}`;
    expect(deriveVersionIdentifier({ contentHash: other }, UPLOADED)).not.toBe(
      deriveVersionIdentifier({ contentHash: HASH }, UPLOADED),
    );
  });

  it('takes the date in UTC, so the identifier does not depend on the operator timezone', () => {
    // 2026-08-20T23:30Z is already 2026-08-21 in Sydney and still
    // 2026-08-20 in New York. A local-date rule would derive two different
    // identifiers for the same bytes depending on who uploaded them.
    const lateUtc = new Date(Date.UTC(2026, 7, 20, 23, 30, 0));
    expect(deriveVersionIdentifier({ contentHash: HASH }, lateUtc)).toBe(
      '2026-08-20-73add98b',
    );
  });

  it('returns null when there is nothing to derive from', () => {
    // Never a silent fallback to a typed value: the caller must refuse.
    expect(deriveVersionIdentifier({}, UPLOADED)).toBeNull();
  });
});
