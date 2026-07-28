#!/usr/bin/env python3
"""
CI gate for issue #25: OOXML hygiene both directions — input part allowlist
+ literal-runs and rescan on generated redlines.

Two axes of coverage, matching the issue's TDD plan:

  AXIS A — Input part allowlist (extraction → prompt assembly)
    ARCHITECTURE.md and docs/threat-model.md must enumerate an explicit
    ALLOWLIST of OOXML parts whose text reaches extraction / prompt assembly.
    The following parts must be named as EXCLUDED (stripped or untrusted-only):
      - Document properties (core, app, custom)
      - Headers and footers
      - Textboxes and shape text
      - Image alt text
      - SmartArt / chart XML
      - Content-control placeholders
    And the upload FILENAME must be stated as never reaching prompts.

  AXIS B — Output-side scan: generated redlines held to the same hostile-file bar
    docs/output-contract.md and/or docs/threat-model.md must specify that:
      - Model-generated text (proposed_replacement_text, footnote rationales)
        is inserted as literal text runs only — no field codes, hyperlinks,
        content controls, or XML metacharacters.
      - The generated .docx output is subjected to the same
        external-relationship / embedded-object / field-code scan as
        uploaded inputs before being written to the outputs bucket.

  AXIS C — Admin-XSS sink update
    The Admin UI stored-XSS threat in docs/threat-model.md must enumerate
    document properties and upload filenames as XSS sinks alongside section
    titles; the existing text names "section titles" but not properties or
    filenames.

Exit codes: 0 = pass, 1 = fail
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ARCHITECTURE_PATH = REPO_ROOT / "ARCHITECTURE.md"
THREAT_MODEL_PATH = REPO_ROOT / "docs" / "threat-model.md"
OUTPUT_CONTRACT_PATH = REPO_ROOT / "docs" / "output-contract.md"


def read_text(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Required file missing: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AXIS A — Input part allowlist
# ---------------------------------------------------------------------------
#
# The input normalization section in ARCHITECTURE.md must specify an explicit
# allowlist of OOXML parts whose text reaches extraction. Everything else is
# stripped or held for untrusted-display-only. Specifically:
#   - body text / main document body
#   - tables
#   - (optionally) deliberately-surfaced notes / footnotes
# And the threat-model or ARCHITECTURE.md must name the following as excluded:
#   - document properties (core/app/custom)
#   - headers/footers
#   - textboxes and shape text
#   - image alt text
#   - SmartArt / chart XML
#   - content-control placeholders
# And the filename must be stated as never reaching prompts.

# Pattern A1: ARCHITECTURE.md names an allowlist or enumerated set of allowed
# OOXML parts that reach extraction/prompt assembly.
ARCH_ALLOWLIST_PATTERN = re.compile(
    r"(?:allowlist|allow.?list|allowed\s+parts?|parts?\s+whose\s+text\s+reaches?"
    r"|enumerat.{0,60}parts?\s+(?:whose|that)\s+reach)"
    r"(?:.|\n){0,1200}"
    r"(?:body|main\s+document\s+body|document\s+body)",
    re.IGNORECASE,
)

# Pattern A2: document properties (core/app/custom) are excluded from prompts.
# At least one of the three property types must be mentioned alongside exclusion.
ARCH_PROPS_EXCLUDED_PATTERN = re.compile(
    r"(?:core|app|custom)\s+(?:document\s+)?propert"
    r"(?:.|\n){0,400}"
    r"(?:strip|exclud|not\s+reach|never\s+reach|untrusted|discard|omit|outside\s+the\s+allow)",
    re.IGNORECASE,
)

# Pattern A3: headers/footers excluded from extraction
ARCH_HEADERS_EXCLUDED_PATTERN = re.compile(
    r"(?:header|footer)"
    r"(?:.|\n){0,400}"
    r"(?:strip|exclud|not\s+reach|never\s+reach|untrusted|discard|omit|outside\s+the\s+allow)",
    re.IGNORECASE,
)

# Pattern A4: textboxes / shape text excluded
ARCH_TEXTBOX_EXCLUDED_PATTERN = re.compile(
    r"(?:textbox|text.?box|shape\s+text|drawing\s+text|shape.*text|text.*shape)"
    r"(?:.|\n){0,400}"
    r"(?:strip|exclud|not\s+reach|never\s+reach|untrusted|discard|omit|outside\s+the\s+allow)",
    re.IGNORECASE,
)

# Pattern A5: alt text / image alt text excluded
ARCH_ALTTEXT_EXCLUDED_PATTERN = re.compile(
    r"(?:alt\s+text|image\s+alt|alt.text)"
    r"(?:.|\n){0,400}"
    r"(?:strip|exclud|not\s+reach|never\s+reach|untrusted|discard|omit|outside\s+the\s+allow)",
    re.IGNORECASE,
)

# Pattern A6: filename never reaches prompts
ARCH_FILENAME_PATTERN = re.compile(
    r"(?:filename|file\s+name|upload.{0,30}name|name.{0,30}upload)"
    r"(?:.|\n){0,400}"
    r"(?:never.{0,80}prompt|not.{0,80}prompt|strip|exclud|never\s+in\s+prompt"
    r"|escaped.{0,80}render|not\s+in\s+prompt)",
    re.IGNORECASE,
)

# Alternative: threat-model or ARCHITECTURE covers these in a single "excluded parts" table/list
# Pattern A-any: any OOXML-part-allowlist description that covers core properties + headers
COMBINED_ALLOWLIST_PATTERN = re.compile(
    r"(?:allowlist|allow.?list|excluded\s+parts?|parts?\s+(?:stripped|excluded|not\s+allowed)"
    r"|outside\s+the\s+(?:extraction\s+)?allow)"
    r"(?:.|\n){0,2000}"
    r"(?:(?:core|app|custom).{0,60}propert|document\s+propert)"
    r"(?:.|\n){0,2000}"
    r"(?:header|footer)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AXIS B — Output-side scan
# ---------------------------------------------------------------------------
#
# docs/output-contract.md and/or docs/threat-model.md must specify:
#   1. Model text is inserted as literal text runs only (no field codes,
#      hyperlinks, content controls in the generated .docx).
#   2. The generated .docx is scanned with the same external-relationship /
#      field-code / embedded-object checks as uploaded inputs before being
#      written to outputs.

# Pattern B1: literal-runs-only insertion requirement
OUTPUT_LITERAL_RUNS_PATTERN = re.compile(
    r"(?:literal\s+(?:text\s+)?runs?\s+only"
    r"|insert.{0,80}literal\s+(?:text\s+)?runs?"
    r"|only\s+(?:as\s+)?literal\s+(?:text\s+)?runs?"
    r"|literal.{0,40}text.{0,40}only"
    r"|as\s+(?:plain\s+)?literal\s+text)"
    r"(?:.|\n){0,600}"
    r"(?:field|hyperlink|content.control|XML\s+metachar|metachar|embed)",
    re.IGNORECASE,
)

# Alternative pattern: may say "no field codes / hyperlinks" in the context of insertion
OUTPUT_NO_FIELD_CODES_PATTERN = re.compile(
    r"(?:proposed_replacement_text|footnote\s+rationale|model.generated\s+text"
    r"|model\s+output.{0,80}insert|insert.{0,80}model)"
    r"(?:.|\n){0,600}"
    r"(?:no\s+field\s+codes?|no\s+hyperlinks?|literal\s+(?:text\s+)?(?:runs?|only)"
    r"|must\s+not\s+(?:contain|introduce)\s+(?:field|hyperlink|content.control))",
    re.IGNORECASE,
)

# Pattern B2: output docx is rescanned / subjected to same hostile-file scan
OUTPUT_RESCAN_PATTERN = re.compile(
    r"(?:generated\s+(?:redline\s+)?\.?docx|output\s+\.?docx|redline\s+(?:output\s+)?\.?docx"
    r"|\.docx\s+(?:output|generated|redline)|output.{0,80}docx)"
    r"(?:.|\n){0,800}"
    r"(?:scan|gauntlet|external.relationship|field.code|embedded.object|hostile.file)"
    r"(?:.|\n){0,400}"
    r"(?:before\s+(?:writing|storing|emitting|moving|copying|placing)|output[s]?\s+bucket"
    r"|written\s+to\s+(?:output|storage|the\s+bucket))",
    re.IGNORECASE,
)

# Simpler rescan pattern: explicit statement that generated output is scanned
OUTPUT_SCAN_STATEMENT_PATTERN = re.compile(
    r"(?:output.{0,60}(?:scan|inspected|gauntlet|same.{0,80}(?:check|scan|gauntlet))"
    r"|rescan.{0,80}(?:output|generated|redline)"
    r"|(?:generated|output).{0,80}(?:relationship|field.code|embedded.object).{0,200}scan"
    r"|scan.{0,80}(?:generated|output).{0,80}(?:relationship|field|embed))",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AXIS C — Admin-XSS sink update
# ---------------------------------------------------------------------------
#
# The Admin UI stored-XSS threat section must enumerate document properties
# and upload filenames as injection sinks, not just section titles.

# Pattern C1: properties (doc properties) named as XSS sink in admin context
ADMIN_XSS_PROPS_PATTERN = re.compile(
    r"Admin\s+UI\s+stored.XSS"
    r"(?:.|\n){0,2000}"
    r"(?:document\s+propert|core\s+propert|app\s+propert|custom\s+propert"
    r"|propert(?:y|ies).{0,80}(?:sink|field|render|inject|influence|attacker))",
    re.IGNORECASE,
)

# Pattern C2: filename named as XSS sink in admin/reviewer context
ADMIN_XSS_FILENAME_PATTERN = re.compile(
    r"(?:Admin\s+UI\s+stored.XSS|stored.XSS|admin\s+UI)"
    r"(?:.|\n){0,2000}"
    r"(?:filename|file\s+name|upload.{0,30}name)"
    r"(?:.|\n){0,400}"
    r"(?:escaped|escape|untrusted|sink|render|inject|XSS|attacker\s+can\s+influence"
    r"|attacker.{0,40}influence|place.{0,60}payload)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------

def gate_a_input_part_allowlist(arch_text: str, threat_text: str) -> list[str]:
    """Input: OOXML part allowlist must be documented."""
    failures = []
    combined = arch_text + "\n\n" + threat_text

    # Check A-all: presence of explicit allowlist / exclusion of auxiliary parts
    # We accept a combined pattern over both files.
    has_allowlist = (
        ARCH_ALLOWLIST_PATTERN.search(combined)
        or COMBINED_ALLOWLIST_PATTERN.search(combined)
    )
    if not has_allowlist:
        failures.append(
            "  Gate A1: ARCHITECTURE.md / docs/threat-model.md does not specify an explicit\n"
            "  allowlist (or enumerated extraction-permitted parts) for OOXML input normalization.\n"
            "  Required: name the parts whose text reaches extraction (body, tables) and state\n"
            "  that everything else is stripped or untrusted-only.\n"
            f"  Missing pattern (allowlist): {ARCH_ALLOWLIST_PATTERN.pattern[:120]!r}"
        )

    # Check A2: doc properties excluded
    if not ARCH_PROPS_EXCLUDED_PATTERN.search(combined):
        failures.append(
            "  Gate A2: No doc that documents/properties (core/app/custom) are excluded\n"
            "  from extraction / prompt assembly.\n"
            "  Required: ARCHITECTURE.md or docs/threat-model.md must state that core,\n"
            "  app, and/or custom document properties are stripped or outside the extraction\n"
            "  allowlist and never reach prompt assembly.\n"
            f"  Missing pattern: {ARCH_PROPS_EXCLUDED_PATTERN.pattern[:120]!r}"
        )

    # Check A3: headers/footers excluded
    if not ARCH_HEADERS_EXCLUDED_PATTERN.search(combined):
        failures.append(
            "  Gate A3: No statement that headers/footers are excluded from extraction.\n"
            "  Required: ARCHITECTURE.md or docs/threat-model.md must state that headers\n"
            "  and footers are stripped or outside the extraction allowlist.\n"
            f"  Missing pattern: {ARCH_HEADERS_EXCLUDED_PATTERN.pattern[:120]!r}"
        )

    # Check A4: textboxes / shape text excluded
    if not ARCH_TEXTBOX_EXCLUDED_PATTERN.search(combined):
        failures.append(
            "  Gate A4: No statement that textbox / shape text is excluded from extraction.\n"
            "  Required: ARCHITECTURE.md or docs/threat-model.md must state that textbox\n"
            "  and shape text is stripped or outside the extraction allowlist.\n"
            f"  Missing pattern: {ARCH_TEXTBOX_EXCLUDED_PATTERN.pattern[:120]!r}"
        )

    # Check A5: alt text excluded
    if not ARCH_ALTTEXT_EXCLUDED_PATTERN.search(combined):
        failures.append(
            "  Gate A5: No statement that image alt text is excluded from extraction.\n"
            "  Required: ARCHITECTURE.md or docs/threat-model.md must state that image\n"
            "  alt text is stripped or outside the extraction allowlist.\n"
            f"  Missing pattern: {ARCH_ALTTEXT_EXCLUDED_PATTERN.pattern[:120]!r}"
        )

    # Check A6: filename never reaches prompts
    if not ARCH_FILENAME_PATTERN.search(combined):
        failures.append(
            "  Gate A6: No statement that the upload filename never reaches prompt assembly.\n"
            "  Required: ARCHITECTURE.md or docs/threat-model.md must state that the\n"
            "  upload filename is never included in prompts and is escaped wherever rendered\n"
            "  in the admin/reviewer UI or audit views.\n"
            f"  Missing pattern: {ARCH_FILENAME_PATTERN.pattern[:120]!r}"
        )

    return failures


def gate_b_output_rescan(output_contract_text: str, threat_text: str) -> list[str]:
    """Output: generated redlines must be rescanned and only carry literal runs."""
    failures = []
    combined = output_contract_text + "\n\n" + threat_text

    # Check B1: literal-runs-only insertion
    has_literal_runs = (
        OUTPUT_LITERAL_RUNS_PATTERN.search(combined)
        or OUTPUT_NO_FIELD_CODES_PATTERN.search(combined)
    )
    if not has_literal_runs:
        failures.append(
            "  Gate B1: docs/output-contract.md / docs/threat-model.md does not specify\n"
            "  that model-generated text is inserted as literal text runs only.\n"
            "  Required: state that proposed_replacement_text and footnote rationales\n"
            "  are inserted as literal text runs only — no field codes, hyperlinks,\n"
            "  content controls, or XML metacharacters may appear in model text.\n"
            f"  Missing patterns: {OUTPUT_LITERAL_RUNS_PATTERN.pattern[:120]!r}\n"
            f"              and: {OUTPUT_NO_FIELD_CODES_PATTERN.pattern[:120]!r}"
        )

    # Check B2: output docx is rescanned before writing to outputs
    has_rescan = (
        OUTPUT_RESCAN_PATTERN.search(combined)
        or OUTPUT_SCAN_STATEMENT_PATTERN.search(combined)
    )
    if not has_rescan:
        failures.append(
            "  Gate B2: docs/output-contract.md / docs/threat-model.md does not specify\n"
            "  that the generated .docx is subjected to the same external-relationship /\n"
            "  field-code / embedded-object scan as uploaded inputs before being written\n"
            "  to the outputs bucket.\n"
            "  Required: state that the output .docx is scanned (or 'run through the same\n"
            "  gauntlet') for hostile constructs before storage.\n"
            f"  Missing patterns: {OUTPUT_RESCAN_PATTERN.pattern[:120]!r}\n"
            f"              and: {OUTPUT_SCAN_STATEMENT_PATTERN.pattern[:120]!r}"
        )

    return failures


def gate_c_admin_xss_updated(threat_text: str) -> list[str]:
    """Admin-XSS: properties and filenames must be named as sinks."""
    failures = []

    if not ADMIN_XSS_PROPS_PATTERN.search(threat_text):
        failures.append(
            "  Gate C1: docs/threat-model.md Admin UI stored-XSS section does not enumerate\n"
            "  document properties (core/app/custom) as an XSS injection sink.\n"
            "  Required: the Admin UI stored-XSS threat description must name document\n"
            "  properties alongside section titles as fields an attacker can influence.\n"
            f"  Missing pattern: {ADMIN_XSS_PROPS_PATTERN.pattern[:120]!r}"
        )

    if not ADMIN_XSS_FILENAME_PATTERN.search(threat_text):
        failures.append(
            "  Gate C2: docs/threat-model.md Admin/reviewer UI does not enumerate the upload\n"
            "  filename as an XSS injection sink that must be escaped in the UI and audit views.\n"
            "  Required: the stored-XSS or frontend-security section must state that the upload\n"
            "  filename is escaped wherever rendered in admin/reviewer UI and audit views.\n"
            f"  Missing pattern: {ADMIN_XSS_FILENAME_PATTERN.pattern[:120]!r}"
        )

    return failures


def main() -> int:
    try:
        arch_text = read_text(ARCHITECTURE_PATH)
        threat_text = read_text(THREAT_MODEL_PATH)
        output_contract_text = read_text(OUTPUT_CONTRACT_PATH)
    except FileNotFoundError as e:
        print(f"FAIL: {e}")
        return 1

    all_failures: list[str] = []

    g_a = gate_a_input_part_allowlist(arch_text, threat_text)
    g_b = gate_b_output_rescan(output_contract_text, threat_text)
    g_c = gate_c_admin_xss_updated(threat_text)

    print("Gate A: Input OOXML part allowlist (extraction / prompt assembly)")
    if g_a:
        for f in g_a:
            print(f)
        all_failures.extend(g_a)
    else:
        print("  PASS")

    print()
    print("Gate B: Output-side scan — literal runs only + generated docx rescanned")
    if g_b:
        for f in g_b:
            print(f)
        all_failures.extend(g_b)
    else:
        print("  PASS")

    print()
    print("Gate C: Admin-XSS sink update — properties and filenames named")
    if g_c:
        for f in g_c:
            print(f)
        all_failures.extend(g_c)
    else:
        print("  PASS")

    print()
    if all_failures:
        print(
            f"FAIL: {len(all_failures)} issue(s) found. "
            "See issue #25 for the full remediation plan."
        )
        return 1
    else:
        print("PASS: all OOXML hygiene both-directions gates satisfied.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
