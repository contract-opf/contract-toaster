/**
 * opfIdentity — read an uploaded OPF artifact's own identity, and derive the
 * version identifier from it (issue #597).
 *
 * ## Why this exists
 *
 * The "Upload a new playbook" form used to ask the operator to TYPE a version
 * identifier. That value should be read out of the artifact, not invented at
 * the keyboard — the owner's words: *"Version identifier should be extracted
 * value not something I need to put in. If I want to make a note I'll add it
 * to note field."*
 *
 * ## The complication, and the rule that resolves it
 *
 * OPF 0.3 carries `identity.version`, but it is OPTIONAL and the real
 * playbook in production (`educational-affiliation`) does not have one:
 * `opf_version` 0.3, `identity.content_hash` present, `identity.version`
 * absent. So "just read it from the file" cannot be the whole rule.
 *
 * Owner decision, 2026-08-22 (issue #597 comment) — option (a), derive only:
 *
 *   1. `identity.version` WINS when the artifact has an opinion.
 *   2. Otherwise derive a deterministic, human-legible value from the
 *      artifact itself: the upload date plus a short `content_hash` prefix,
 *      e.g. `2026-08-20-73add98b`.
 *   3. The field is NEVER operator-editable. Option (b) — prefill and let
 *      them override — was considered and rejected by the owner. The Note
 *      field stays free text and is where operator prose belongs.
 *
 * Rejecting artifacts without `identity.version` (option c) was rejected
 * outright: it would refuse the owner's real playbook, which is the artifact
 * this product exists to run.
 *
 * ## Collision behaviour is a FEATURE
 *
 * Uploads are append-only and the server refuses a reused identifier. The
 * hash prefix means:
 *   - re-uploading CHANGED bytes derives a different identifier, so it lands
 *     as a new version rather than silently colliding;
 *   - re-uploading IDENTICAL bytes on the same day derives the SAME
 *     identifier, and the server's append-only check refuses it. That is the
 *     correct answer, not a bug to design around.
 *
 * The date is taken in UTC, and is passed in rather than read from the clock,
 * so this module is a pure function and its tests are not wall-clock-stamped
 * fixtures. (Identical bytes uploaded on DIFFERENT days do derive different
 * identifiers. That is intended: the derived value records an upload, and two
 * uploads a month apart are two facts worth telling apart.)
 *
 * ## Parsing
 *
 * Both OPF document forms are handled, exactly as `scripts/opf_load.py` +
 * `scripts/opf_html.py` handle them server-side:
 *
 *   - `.opf.html` — the single-file bundle. The canonical OPF JSON is
 *     embedded VERBATIM in `<script id="opf-canonical"
 *     type="application/json">`, with any `</` escaped to `<\/` so the JSON
 *     cannot prematurely close its own `</script>` tag. Extraction reverses
 *     that escape, exactly as `opf_html._unescape_json_from_script` does.
 *   - `.opf.json` — the bare canonical document, parsed directly.
 *
 * This is a READ, never a gate. The server re-parses the bytes it actually
 * received, recomputes the hash, and validates against the schema
 * (`opf_load._validate_doc`, `require_identity=True`); a client that guessed
 * wrong is refused there. Nothing here is authoritative for anything.
 */

/** The two `identity` fields this module cares about. */
export interface OpfIdentity {
  /** `identity.version` — optional in OPF 0.3, absent on the real playbook. */
  version?: string;
  /** `identity.content_hash` — `sha256:<64 hex>`. */
  contentHash?: string;
}

/**
 * Mirrors `opf_html._CANONICAL_BLOCK_RE`. Attribute order is fixed by the
 * writer, but both orders are accepted on read (liberal in what we extract),
 * which is what the two lookaheads buy. `[\s\S]*?` stands in for Python's
 * DOTALL so the JSON body may span lines.
 */
const CANONICAL_BLOCK =
  /<script\b(?=[^>]*\bid="opf-canonical")(?=[^>]*\btype="application\/json")[^>]*>([\s\S]*?)<\/script>/;

/**
 * Read `identity` out of an OPF artifact's text, from either document form.
 *
 * Returns `null` when the text is not parseable as an OPF document at all.
 * Returns an `OpfIdentity` with both fields absent when the document parses
 * but carries no usable identity — the caller must treat that as "cannot
 * derive", not as an empty-but-fine value.
 */
export function readOpfIdentity(text: string): OpfIdentity | null {
  const embedded = CANONICAL_BLOCK.exec(text);
  // Reverses opf_html.escape_json_for_script: `<\/` → `</`. Note this is a
  // no-op for anything we then READ: `\/` is already a legal escape for `/`
  // inside a JSON string, and `<`/`/` cannot appear outside one, so JSON.parse
  // yields the same values with or without it. It is kept because recovering
  // the canonical text byte-for-byte is the documented contract of this
  // envelope (that is what makes identity.content_hash verifiable over it),
  // and a reader that silently diverged from the writer would be a trap for
  // the next caller who does need the bytes. No test asserts it — see
  // opf-identity-597.test.ts for why one would be tautological.
  const jsonText = embedded ? embedded[1]!.replace(/<\\\//g, '</') : text;

  let doc: unknown;
  try {
    doc = JSON.parse(jsonText);
  } catch {
    return null;
  }
  if (typeof doc !== 'object' || doc === null) {
    return null;
  }

  const identity = (doc as Record<string, unknown>).identity;
  if (typeof identity !== 'object' || identity === null) {
    return {};
  }
  const record = identity as Record<string, unknown>;
  const version = typeof record.version === 'string' ? record.version.trim() : '';
  const contentHash =
    typeof record.content_hash === 'string' ? record.content_hash.trim() : '';

  return {
    ...(version === '' ? {} : { version }),
    ...(contentHash === '' ? {} : { contentHash }),
  };
}

/** `sha256:c3dfddcb…` → `c3dfddcb`. A bare digest with no algorithm prefix
 * is accepted too, since nothing here depends on the prefix being present. */
function digestPrefix(contentHash: string): string {
  const separator = contentHash.indexOf(':');
  const digest = separator === -1 ? contentHash : contentHash.slice(separator + 1);
  return digest.slice(0, 8);
}

/** `2026-08-20`, in UTC — see this module's docstring on why not local. */
function utcDate(at: Date): string {
  const year = at.getUTCFullYear().toString().padStart(4, '0');
  const month = (at.getUTCMonth() + 1).toString().padStart(2, '0');
  const day = at.getUTCDate().toString().padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/**
 * The derived version identifier, or `null` when the artifact gives us
 * nothing to derive from — no `identity.version` AND no
 * `identity.content_hash`. `null` is the caller's signal to refuse the
 * upload with an explanation, never to fall back to an operator-typed value.
 *
 * `uploadedAt` is passed in, not read from the clock, so this stays pure.
 */
export function deriveVersionIdentifier(
  identity: OpfIdentity,
  uploadedAt: Date,
): string | null {
  // 1. The artifact's own opinion wins whenever it has one.
  if (identity.version !== undefined && identity.version !== '') {
    return identity.version;
  }
  // 2. Otherwise: upload date + short content-hash prefix.
  if (identity.contentHash !== undefined && identity.contentHash !== '') {
    const prefix = digestPrefix(identity.contentHash);
    if (prefix !== '') {
      return `${utcDate(uploadedAt)}-${prefix}`;
    }
  }
  // 3. Nothing to derive from.
  return null;
}
