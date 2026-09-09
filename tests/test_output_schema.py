#!/usr/bin/env python3
"""
Output-contract schema gate — issue #4, pointed at output-schema-v2.json
(issue #376: v2 is a clean-break successor to v1 adding an optional
issues[].source_quote field; v2's Issue shape is a strict superset of v1's,
so this file's checks and fixtures hold for both).

Four checks (all must pass; exit 1 on any failure):

0. RESOLVER SELF-CHECK: `_active_output_schema_path` — the `ast` reader that
   answers "which output contract is ACTIVE?" without importing the module —
   is exercised against sources whose correct answer is known, including the
   rebinding forms it must REFUSE rather than answer with a stale value
   (issue #641). Check 1 is only as trustworthy as this resolver.

1. SUBSET CHECK: every field in output_format.every_issue_includes is a top-level
   property of the ACTIVE output contract (primary_review_pass.OUTPUT_SCHEMA_PATH),
   not of a hard-coded generation of it (issue #636 scope 3).

2. BUNDLE-COMPOSITION CHECK: playbooks/schema.json requires release.output_contract_hash
   (or the active playbook's release block carries output_contract_hash).  Fails
   today because neither the playbook schema nor the EIAA release block has that
   field.

3. VALIDATOR UNIT TESTS: a set of fixture model-responses (valid and invalid) is
   tested against the output schema.  Fails today because the schema does not exist.
   Includes v2-specific fixtures covering issues[].source_quote both present and
   absent (issue #376 acceptance criterion).
"""

import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# The v2 artifact, still the subject of this file's VALIDATOR UNIT TESTS: those
# fixtures pin the superseded contract on purpose (issue #376) and must keep
# reading it.
OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v2.json"

PRIMARY_REVIEW_PASS_SRC = REPO_ROOT / "scripts" / "primary_review_pass.py"


# NOTE the quoted annotation: this file must import on the bare
# `/usr/bin/python3` the output-schema CI step uses, which on the maintainer's
# machine is 3.9.6 -- `str | None` in a SIGNATURE is evaluated at def time and
# is a TypeError before 3.10.
def _active_output_schema_path(source: "str | None" = None) -> Path:
    """`primary_review_pass.OUTPUT_SCHEMA_PATH`, read WITHOUT importing it.

    `source` (issue #641) overrides the module text to parse, so the RESOLVER
    SELF-CHECK below can drive this function with the binding forms it must
    refuse. The input domain of this function is "the Python source of
    `scripts/primary_review_pass.py`", so a synthetic source string is that
    real domain, not a stand-in for it. Default `None` reads the real file --
    the only way any production caller in this file reaches it.

    The SUBSET CHECK below asks "does this playbook instruct the field set the
    model is actually validated against?", so it has to read the contract in
    force rather than a hard-coded generation of it -- issue #627 flipped the
    active artifact to output-schema-v3.json (which makes `issue_key` REQUIRED
    on every Issue), and a correctly-aligned playbook failed this check purely
    because the check was pinned to v2 (issue #636 scope 3).

    It is resolved out of the SOURCE with `ast` instead of by importing the
    module, because `.github/workflows/output-schema.yml` runs this file as
    `python3 tests/test_output_schema.py` on a bare interpreter and states the
    invariant explicitly: "Only the v3 step below needs it; every other step in
    this job is stdlib-only." Importing `primary_review_pass` pulls in
    `model_client` -> `jsonschema` and fails that job with ModuleNotFoundError
    while passing locally in a venv that has the dependency -- the CI blind
    spot this repo has already been bitten by twice.

    Raises rather than falling back to a guessed path: a silent fallback would
    make this check pass against a contract that is not the live one, which is
    the exact failure mode it exists to prevent.

    COVERAGE IS ASSERTED, not assumed (issue #641). This resolver is a partial
    re-implementation of Python's own name binding: it reads only top-level
    `ast.Assign` statements with a bare `Name` target. EVERY other binding
    operation Python has -- a tuple target, an annotated or augmented
    assignment, an assignment nested inside any compound statement, an
    `import`, a `def`, a `class`, a parameter, an `except ... as`, a `global`
    or `nonlocal` declaration, a match-case capture -- is invisible to it, and
    an invisible REBINDING is worse than an unresolvable one, because the
    resolver then returns the earlier value and every check built on it passes
    against a contract that is not live. Demonstrated on this tree: adding
    `OUTPUT_SCHEMA_PATH, _UNUSED = OUTPUT_SCHEMA_V2_PATH, None` after the real
    assignment made Python resolve v2 while this file printed
    "PASS SUBSET CHECK: ... are properties of output-schema-v3.json" and
    exited 0. So `_assert_every_binding_was_seen` below refuses any source in
    which a name on the resolved chain is bound anywhere this scan did not
    look -- its enumeration of "anywhere" is taken from Python's own list of
    binding operations, not from what this resolver happens to handle.
    """
    tree = ast.parse(
        PRIMARY_REVIEW_PASS_SRC.read_text(encoding="utf-8") if source is None else source
    )
    assignments: dict[str, ast.expr] = {}
    seen_targets: set[int] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
                    seen_targets.add(id(target))

    name = "OUTPUT_SCHEMA_PATH"
    chain = [name]
    seen: set[str] = set()
    while name in assignments and isinstance(assignments[name], ast.Name):
        if name in seen:
            raise RuntimeError(f"cyclic alias resolving OUTPUT_SCHEMA_PATH at {name!r}")
        seen.add(name)
        name = assignments[name].id  # type: ignore[union-attr]
        chain.append(name)

    if name not in assignments:
        raise RuntimeError(
            f"could not resolve OUTPUT_SCHEMA_PATH in {PRIMARY_REVIEW_PASS_SRC}"
        )

    _assert_every_binding_was_seen(tree, chain, seen_targets)

    # Expected shape: REPO_ROOT / "playbooks" / "<artifact>.json"
    parts: list[str] = []
    node = assignments[name]
    while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        if not isinstance(node.right, ast.Constant) or not isinstance(node.right.value, str):
            raise RuntimeError(f"unexpected path component in OUTPUT_SCHEMA_PATH: {ast.dump(node.right)}")
        parts.append(node.right.value)
        node = node.left
    if not (isinstance(node, ast.Name) and node.id == "REPO_ROOT") or not parts:
        raise RuntimeError(
            "OUTPUT_SCHEMA_PATH is no longer a REPO_ROOT-anchored path expression; "
            "update this resolver rather than guessing."
        )
    return REPO_ROOT.joinpath(*reversed(parts))


def _binding_sites(tree: ast.Module):
    """Yield `(name, lineno, form, name_node_id)` for EVERY construct in `tree`
    that binds a name.

    The enumeration is derived from Python's own list of binding operations
    (language reference, "Binding of names") rather than from what
    `_active_output_schema_path` happens to read -- a guard enumerated from its
    own implementation can only ever confirm what it already handles. Each
    bullet of that list maps to a branch below:

      * assignment statements, annotated (`AnnAssign`) and augmented
        (`AugAssign`) assignment, tuple/starred unpacking, `for` targets,
        `with ... as`, and the walrus operator all produce a `Name` in a
        `Store` context -- the first branch;
      * `import x`, `import x as y`, `from m import n`, `from m import n as y`
        -- `Import`/`ImportFrom`. `import a.b` binds `a`, everything else binds
        the `as` name or the imported name. `ast.alias` carries no `lineno`
        before 3.10, so the statement's line is reported;
      * `def` / `async def` and `class` bind their own name;
      * function parameters (`ast.arg`);
      * `except E as X` -- `ExceptHandler.name` is a bare string, NOT a `Name`
        node, so a `Store`-only walk misses it;
      * `global` / `nonlocal` declarations;
      * match-case capture patterns (`MatchAs`, `MatchStar`, `MatchMapping`'s
        `**rest`), on interpreters that have them -- looked up via `getattr`
        so this module still imports on 3.9, the bare `python3` the
        output-schema gate can resolve to (see the note above
        `_active_output_schema_path`).

    `name_node_id` is the `id()` of the `Name` node for the first branch (so a
    binding the resolver already read can be recognised) and `None` for every
    form the resolver cannot read at all.
    """
    match_pattern_types = tuple(
        cls
        for cls in (
            getattr(ast, "MatchAs", None),
            getattr(ast, "MatchStar", None),
            getattr(ast, "MatchMapping", None),
        )
        if cls is not None
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id, node.lineno, "an assignment target", id(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                yield alias.asname or alias.name.split(".")[0], node.lineno, "an import", None
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node.lineno, "a def", None
        elif isinstance(node, ast.ClassDef):
            yield node.name, node.lineno, "a class definition", None
        elif isinstance(node, ast.arg):
            yield node.arg, node.lineno, "a function parameter", None
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node.name, node.lineno, "an `except ... as`", None
        elif isinstance(node, ast.Global):
            for declared in node.names:
                yield declared, node.lineno, "a `global` declaration", None
        elif isinstance(node, ast.Nonlocal):
            for declared in node.names:
                yield declared, node.lineno, "a `nonlocal` declaration", None
        elif match_pattern_types and isinstance(node, match_pattern_types):
            captured = getattr(node, "name", None) or getattr(node, "rest", None)
            if captured:
                yield captured, node.lineno, "a match-case capture pattern", None


def _assert_every_binding_was_seen(
    tree: ast.Module, chain: list[str], seen_targets: set[int]
) -> None:
    """Raise unless every binding of every name on the resolved alias `chain`
    is one of the top-level plain assignments `_active_output_schema_path`
    actually read (`seen_targets`, identified by node identity).

    Walks the WHOLE tree via `_binding_sites`, whose branches are enumerated
    from Python's binding operations, so it sees every form the resolver
    cannot: tuple/starred targets, `AnnAssign`, `AugAssign`, `for` targets,
    `with ... as`, `except ... as`, `import`/`import ... as`/`from ... import`,
    `def`, `class`, parameters, `global`/`nonlocal`, match-case captures, and
    anything nested inside an `if`/`try`/`def`/`class`. Any of these on one of
    these names that the resolver did not read is a rebinding it would
    silently ignore -- answering with the earlier value where Python binds the
    later one.

    A module-scope reader is not required to model function-local shadowing
    exactly; it is required not to answer confidently when it might be wrong.
    So this errs strict: it reports ANY unseen binding of these names and asks
    a human to extend the resolver, rather than guessing which ones matter.
    """
    wanted = set(chain)
    unseen = [
        (name, lineno, form)
        for name, lineno, form, name_node_id in _binding_sites(tree)
        if name in wanted and (name_node_id is None or name_node_id not in seen_targets)
    ]
    if unseen:
        detail = ", ".join(
            f"{name} at line {lineno} ({form})" for name, lineno, form in sorted(unseen, key=lambda b: b[1])
        )
        raise RuntimeError(
            f"the parsed source binds a name on the OUTPUT_SCHEMA_PATH alias "
            f"chain in a form this resolver does not read ({detail}). It would return "
            f"the earlier value and this gate would then check a contract that is not "
            f"the live one. Extend _active_output_schema_path rather than ignoring this."
        )


ACTIVE_OUTPUT_SCHEMA_PATH = _active_output_schema_path()
PLAYBOOK_SCHEMA_PATH = REPO_ROOT / "playbooks" / "schema.json"
EIAA_PLAYBOOK_PATH = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "synthetic-generic-v1.0.0.json"

# ---------------------------------------------------------------------------
# Inline model-response fixtures (valid and invalid)
# ---------------------------------------------------------------------------

VALID_RESPONSE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": "INTERNAL-ONLY: precedent-003",
            "provenance": "detector:no-exos-indemnity",
        }
    ],
    "critic_delta": None,
}

# Invalid: missing required top-level field 'decision'
INVALID_MISSING_DECISION = {
    "schema_version": "output-schema-v1",
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: wrong type for 'decision'
INVALID_WRONG_DECISION_TYPE = {
    "schema_version": "output-schema-v1",
    "decision": 42,
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: decision value not in enum
INVALID_DECISION_VALUE = {
    "schema_version": "output-schema-v1",
    "decision": "MAYBE",
    "confidence_state": "OK",
    "issues": [],
}

# Invalid: issue missing required field 'section_ref'
INVALID_ISSUE_MISSING_FIELD = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_title": "Indemnification",
            "counterparty_change_summary": "...",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "...",
            "proposed_replacement_text": "...",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
        }
    ],
}

# Invalid: proposed_replacement_text exceeds a reasonable character length.
# The schema should enforce max_length on proposed_replacement_text (e.g. 8000 chars).
INVALID_REPLACEMENT_TOO_LONG = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted indemnification.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "Creates new obligation.",
            "proposed_replacement_text": "X" * 9000,  # Exceeds max
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
        }
    ],
}

# v2 (issue #376): issues[].source_quote is OPTIONAL. VALID_RESPONSE above
# already covers the "omits it" half of the acceptance criterion (no
# source_quote key on its one issue). VALID_RESPONSE_WITH_SOURCE_QUOTE below
# covers the "includes it" half.
VALID_RESPONSE_WITH_SOURCE_QUOTE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": "INTERNAL-ONLY: precedent-003",
            "provenance": "detector:no-exos-indemnity",
            "source_quote": "Company shall indemnify, defend, and hold harmless Counterparty from any and all claims.",
        }
    ],
    "critic_delta": None,
}

# Invalid: wrong type for source_quote (must be a string, not e.g. an
# integer). The minimal stdlib validator below implements "type" but not
# "minLength", so this is a type-check regression guard rather than a
# minLength one -- see check_validator_fixtures' docstring.
INVALID_SOURCE_QUOTE_WRONG_TYPE = {
    "schema_version": "output-schema-v1",
    "decision": "REQUEST_CHANGE",
    "confidence_state": "OK",
    "issues": [
        {
            "section_ref": "14",
            "section_title": "Indemnification",
            "counterparty_change_summary": "Counterparty inserted a one-sided indemnification clause.",
            "decision": "REQUEST_CHANGE",
            "external_rationale_for_footnote": "This clause imposes a new indemnification obligation on the Company beyond our standard form.",
            "proposed_replacement_text": "Neither party shall indemnify the other for third-party claims.",
            "playbook_topic_id": "indemnification",
            "internal_precedent_citation": None,
            "provenance": "model",
            "source_quote": 12345,
        }
    ],
    "critic_delta": None,
}

VALIDATOR_FIXTURES = [
    {"name": "valid_response", "obj": VALID_RESPONSE, "expect_valid": True},
    {"name": "valid_response_with_source_quote", "obj": VALID_RESPONSE_WITH_SOURCE_QUOTE, "expect_valid": True},
    {"name": "missing_decision", "obj": INVALID_MISSING_DECISION, "expect_valid": False},
    {"name": "wrong_decision_type", "obj": INVALID_WRONG_DECISION_TYPE, "expect_valid": False},
    {"name": "invalid_decision_value", "obj": INVALID_DECISION_VALUE, "expect_valid": False},
    {"name": "issue_missing_field", "obj": INVALID_ISSUE_MISSING_FIELD, "expect_valid": False},
    {"name": "replacement_too_long", "obj": INVALID_REPLACEMENT_TOO_LONG, "expect_valid": False},
    {"name": "source_quote_wrong_type", "obj": INVALID_SOURCE_QUOTE_WRONG_TYPE, "expect_valid": False},
]


# ---------------------------------------------------------------------------
# Minimal JSON-Schema validator (stdlib only — no jsonschema package).
# Supports: type, required, properties, enum, maxLength, items, $ref (local).
# ---------------------------------------------------------------------------

class ValidationError(Exception):
    pass


def _resolve_ref(ref: str, root_schema: dict) -> dict:
    """Resolve a $ref like '#/definitions/Issue' within root_schema."""
    if not ref.startswith("#/"):
        raise ValidationError(f"Only local $ref supported, got: {ref!r}")
    parts = ref.lstrip("#/").split("/")
    node = root_schema
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            raise ValidationError(f"$ref {ref!r} could not be resolved")
        node = node[part]
    return node


def _validate(obj, schema: dict, root_schema: dict, path: str = "") -> list:
    """Return list of error strings (empty = valid)."""
    errors = []

    # Resolve $ref
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root_schema)
        return _validate(obj, resolved, root_schema, path)

    # type check
    schema_type = schema.get("type")
    if schema_type:
        type_map = {
            "object": dict,
            "array": list,
            "string": str,
            "integer": int,
            "number": (int, float),
            "boolean": bool,
            "null": type(None),
        }
        expected_types = schema_type if isinstance(schema_type, list) else [schema_type]
        python_types = tuple(type_map[t] for t in expected_types if t in type_map)
        if python_types and not isinstance(obj, python_types):
            errors.append(f"{path}: expected type {schema_type!r}, got {type(obj).__name__!r}")
            return errors  # further checks would be type-unsafe

    # enum check
    if "enum" in schema:
        if obj not in schema["enum"]:
            errors.append(f"{path}: {obj!r} not in enum {schema['enum']!r}")

    # maxLength check (strings)
    if "maxLength" in schema and isinstance(obj, str):
        if len(obj) > schema["maxLength"]:
            errors.append(
                f"{path}: string length {len(obj)} exceeds maxLength {schema['maxLength']}"
            )

    # required + properties (objects)
    if isinstance(obj, dict):
        for req in schema.get("required", []):
            if req not in obj:
                errors.append(f"{path}: missing required property {req!r}")
        for prop, prop_schema in schema.get("properties", {}).items():
            if prop in obj:
                errors.extend(_validate(obj[prop], prop_schema, root_schema, f"{path}.{prop}"))

    # items (arrays)
    if isinstance(obj, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(obj):
                errors.extend(_validate(item, items_schema, root_schema, f"{path}[{i}]"))

    return errors


def validate(obj, schema: dict) -> list:
    return _validate(obj, schema, schema, "$")


# ---------------------------------------------------------------------------
# Check 0: resolver self-check (issue #641)
# ---------------------------------------------------------------------------

# Each case is (label, source, expectation). `None` means "must resolve to
# playbooks/output-schema-v3.json"; a string means "must raise, and the message
# must contain this".
#
# The REBIND cases are the ones that matter: before this check existed, each of
# them made Python resolve v2 (or a shadowing object) while `check_subset`
# printed "...are properties of output-schema-v3.json" and the gate exited 0.
# They are ordinary Python that a developer flipping the contract could
# plausibly write, which is exactly why "the resolver did not crash" was never
# evidence.
#
# They are enumerated from PYTHON'S binding operations, not from the forms
# `_assert_every_binding_was_seen` already handles -- a table written from the
# implementation can only ever confirm what already works. So the rebind cases
# below deliberately span the whole list: a `Store`-producing form (tuple
# target, nested-in-`if`, `AnnAssign`, `for` target, `global`+assign), the
# `import` forms and the `def`/`class` forms (none of which produce a `Store`
# `Name`, and all of which answered with the stale earlier value until this
# round), an `except ... as` (whose bound name is a bare string on the handler,
# invisible to a `Store`-only walk), and a match-case capture on the
# interpreters that have one. `from model_output_schema import
# OUTPUT_SCHEMA_PATH` is not hypothetical: scripts/model_output_schema.py
# defines its own `OUTPUT_SCHEMA_PATH`, so consolidating on it is a plausible
# next edit to scripts/primary_review_pass.py.
_RESOLVER_SELF_CHECK_CASES = (
    (
        "the shipped shape",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n',
        None,
    ),
    (
        "an alias chain",
        'REPO_ROOT = 1\n_V3 = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "_MID = _V3\nOUTPUT_SCHEMA_PATH = _MID\n",
        None,
    ),
    (
        "a tuple-target rebind",
        'REPO_ROOT = 1\n_V2 = REPO_ROOT / "playbooks" / "output-schema-v2.json"\n'
        'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "OUTPUT_SCHEMA_PATH, _OTHER = _V2, None\n",
        "in a form this resolver does not read",
    ),
    (
        "a rebind nested in an if",
        'REPO_ROOT = 1\n_V2 = REPO_ROOT / "playbooks" / "output-schema-v2.json"\n'
        'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "if True:\n    OUTPUT_SCHEMA_PATH = _V2\n",
        "in a form this resolver does not read",
    ),
    (
        "an annotated rebind",
        'REPO_ROOT = 1\n_V2 = REPO_ROOT / "playbooks" / "output-schema-v2.json"\n'
        'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "OUTPUT_SCHEMA_PATH: object = _V2\n",
        "in a form this resolver does not read",
    ),
    (
        "a rebind of a name further up the alias chain",
        'REPO_ROOT = 1\n_V3 = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "OUTPUT_SCHEMA_PATH = _V3\nfor _V3 in []:\n    pass\n",
        "in a form this resolver does not read",
    ),
    (
        "a rebind through `global` inside a function",
        'REPO_ROOT = 1\n_V2 = REPO_ROOT / "playbooks" / "output-schema-v2.json"\n'
        'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "def _flip():\n    global OUTPUT_SCHEMA_PATH\n    OUTPUT_SCHEMA_PATH = _V2\n",
        "in a form this resolver does not read",
    ),
    (
        "a `from ... import` rebind",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "from model_output_schema import OUTPUT_SCHEMA_PATH\n",
        "in a form this resolver does not read",
    ),
    (
        "an `import ... as` rebind",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "import model_output_schema as OUTPUT_SCHEMA_PATH\n",
        "in a form this resolver does not read",
    ),
    (
        "a `from ... import` rebind of a name further up the alias chain",
        'REPO_ROOT = 1\n_V3 = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "OUTPUT_SCHEMA_PATH = _V3\nfrom model_output_schema import _V3\n",
        "in a form this resolver does not read",
    ),
    (
        "a `def` that shadows the constant",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "def OUTPUT_SCHEMA_PATH():\n    pass\n",
        "in a form this resolver does not read",
    ),
    (
        "a `class` that shadows the constant",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "class OUTPUT_SCHEMA_PATH:\n    pass\n",
        "in a form this resolver does not read",
    ),
    (
        "an `except ... as` rebind",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
        "try:\n    pass\nexcept Exception as OUTPUT_SCHEMA_PATH:\n    pass\n",
        "in a form this resolver does not read",
    ),
    (
        "a renamed constant",
        'REPO_ROOT = 1\nACTIVE_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n',
        "could not resolve OUTPUT_SCHEMA_PATH",
    ),
    (
        "a non-literal path expression",
        "REPO_ROOT = 1\nimport os\n"
        'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / os.environ["X"]\n',
        "unexpected path component",
    ),
    (
        "a path that is no longer REPO_ROOT-anchored",
        'REPO_ROOT = 1\nOUTPUT_SCHEMA_PATH = SOMEWHERE_ELSE / "output-schema-v3.json"\n',
        "no longer a REPO_ROOT-anchored path expression",
    ),
    (
        "a cyclic alias",
        "OUTPUT_SCHEMA_PATH = _A\n_A = OUTPUT_SCHEMA_PATH\n",
        "cyclic alias",
    ),
)

if sys.version_info >= (3, 10):
    # A bare name in a `case` pattern is a CAPTURE pattern -- it binds. This
    # case is appended rather than written into the table above because `match`
    # is a SyntaxError on 3.9, and this file must still parse and run on the
    # bare `python3` the output-schema CI step uses (3.9.6 on the maintainer's
    # machine).
    _RESOLVER_SELF_CHECK_CASES += (
        (
            "a match-case capture rebind",
            'REPO_ROOT = 1\n_V2 = REPO_ROOT / "playbooks" / "output-schema-v2.json"\n'
            'OUTPUT_SCHEMA_PATH = REPO_ROOT / "playbooks" / "output-schema-v3.json"\n'
            "match _V2:\n    case OUTPUT_SCHEMA_PATH:\n        pass\n",
            "in a form this resolver does not read",
        ),
    )


def check_resolver_self_check(failures: list) -> None:
    """`_active_output_schema_path` is a partial re-implementation of Python's
    own name binding, so it is checked against sources whose correct answer is
    known -- including the rebinding forms it must REFUSE rather than silently
    answer with a stale value (issue #641). Those forms are enumerated from
    Python's binding operations, so the table can discover a gap in
    `_binding_sites` instead of merely restating what it already handles.

    Without this, "the gate is green" only ever proved the resolver did not
    crash on one file. It never proved the path it returned was the one Python
    computes -- and a wrong path here makes the SUBSET CHECK below validate the
    playbook against a contract that is not live, while printing PASS.

    Stays stdlib-only (`ast` only), so it runs in the same bare-interpreter CI
    step as the rest of this file. Its complement -- resolved value ==
    `primary_review_pass.OUTPUT_SCHEMA_PATH` as the interpreter itself computes
    it -- needs the import and therefore lives in `tests/test_v3_flip_627.py`.
    """
    label = "RESOLVER SELF-CHECK"
    before = len(failures)
    for case_label, source, expected_error in _RESOLVER_SELF_CHECK_CASES:
        try:
            resolved = _active_output_schema_path(source=source)
        except RuntimeError as exc:
            if expected_error is None:
                failures.append(
                    f"{label}: {case_label} must resolve, but the resolver refused it: {exc}"
                )
            elif expected_error not in str(exc):
                failures.append(
                    f"{label}: {case_label} was refused for the wrong reason -- expected a "
                    f"message containing {expected_error!r}, got: {exc}"
                )
            continue
        if expected_error is not None:
            failures.append(
                f"{label}: {case_label} must be REFUSED -- the resolver cannot see that "
                f"binding, so it silently answered {resolved.name!r} where Python would not."
            )
        elif resolved != REPO_ROOT / "playbooks" / "output-schema-v3.json":
            failures.append(
                f"{label}: {case_label} resolved to {resolved}, expected "
                f"{REPO_ROOT / 'playbooks' / 'output-schema-v3.json'}"
            )
    if len(failures) == before:
        print(
            f"  PASS {label}: all {len(_RESOLVER_SELF_CHECK_CASES)} resolver cases behaved as "
            f"specified ({sum(1 for _l, _s, e in _RESOLVER_SELF_CHECK_CASES if e)} refused)."
        )


# ---------------------------------------------------------------------------
# Check 1: subset check
# ---------------------------------------------------------------------------

def check_subset(failures: list) -> None:
    """every_issue_includes ⊆ properties of the ACTIVE output contract's issues[]."""
    label = "SUBSET CHECK"
    schema_name = ACTIVE_OUTPUT_SCHEMA_PATH.name

    if not ACTIVE_OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {schema_name} does not exist. "
            f"Author playbooks/{schema_name} to fix."
        )
        return

    with open(ACTIVE_OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)

    with open(EIAA_PLAYBOOK_PATH) as f:
        playbook = json.load(f)

    every_issue_includes = playbook.get("output_format", {}).get("every_issue_includes", [])
    if not every_issue_includes:
        failures.append(f"{label}: output_format.every_issue_includes is empty or missing in playbook.")
        return

    # Navigate to the issue object schema, resolving $ref if needed.
    # Expected path: output-schema-v2.json -> properties.issues.items -> (resolve $ref) -> properties
    try:
        items_schema = output_schema["properties"]["issues"]["items"]
        # Resolve $ref if present (e.g. "#/definitions/Issue")
        if "$ref" in items_schema:
            ref = items_schema["$ref"]
            parts = ref.lstrip("#/").split("/")
            node = output_schema
            for part in parts:
                node = node[part]
            items_schema = node
        issue_props = items_schema["properties"]
    except (KeyError, TypeError) as e:
        failures.append(
            f"{label}: could not navigate {schema_name} to "
            f"properties.issues.items.properties (with $ref resolution): {e}"
        )
        return

    missing = [field for field in every_issue_includes if field not in issue_props]
    if missing:
        failures.append(
            f"{label}: fields in every_issue_includes not found in {schema_name} "
            f"issue properties: {missing}"
        )
    else:
        print(
            f"  PASS {label}: all {len(every_issue_includes)} every_issue_includes fields "
            f"are properties of {schema_name} issues[]."
        )


# ---------------------------------------------------------------------------
# Check 2: bundle-composition check
# ---------------------------------------------------------------------------

def check_bundle_composition(failures: list) -> None:
    """release block in schema.json should require output_contract_hash."""
    label = "BUNDLE-COMPOSITION CHECK"

    with open(PLAYBOOK_SCHEMA_PATH) as f:
        schema = json.load(f)

    # The release block's required fields live at:
    # properties.playbook.properties.release.required
    try:
        release_required = (
            schema["properties"]["playbook"]["properties"]["release"]["required"]
        )
    except (KeyError, TypeError) as e:
        failures.append(
            f"{label}: could not navigate schema.json to "
            f"properties.playbook.properties.release.required: {e}"
        )
        return

    if "output_contract_hash" not in release_required:
        failures.append(
            f"{label}: 'output_contract_hash' is not in the release block's required "
            f"fields in playbooks/schema.json. Current required: {release_required}"
        )
    else:
        print(
            f"  PASS {label}: 'output_contract_hash' is required in the release bundle."
        )


# ---------------------------------------------------------------------------
# Check 3: validator unit tests
# ---------------------------------------------------------------------------

def check_validator_fixtures(failures: list) -> None:
    """Run valid/invalid model-response fixtures against output-schema-v2.json."""
    label = "VALIDATOR UNIT TESTS"

    if not OUTPUT_SCHEMA_PATH.exists():
        failures.append(
            f"{label}: {OUTPUT_SCHEMA_PATH.name} does not exist — cannot run fixture tests. "
            "Author playbooks/output-schema-v2.json to fix."
        )
        return

    with open(OUTPUT_SCHEMA_PATH) as f:
        output_schema = json.load(f)

    for fixture in VALIDATOR_FIXTURES:
        name = fixture["name"]
        obj = fixture["obj"]
        expect_valid = fixture["expect_valid"]

        errs = validate(obj, output_schema)
        is_valid = len(errs) == 0

        if is_valid == expect_valid:
            status = "valid" if is_valid else "invalid"
            print(f"  PASS {label}: fixture '{name}' correctly classified as {status}.")
        else:
            if expect_valid:
                failures.append(
                    f"{label}: fixture '{name}' expected VALID but got errors: {errs}"
                )
            else:
                failures.append(
                    f"{label}: fixture '{name}' expected INVALID but passed validation."
                )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Output-contract schema gate (issue #4)\n")
    failures = []

    check_resolver_self_check(failures)
    check_subset(failures)
    check_bundle_composition(failures)
    check_validator_fixtures(failures)

    print()
    if failures:
        print(f"FAIL: {len(failures)} check(s) failed:\n")
        for f in failures:
            print(f"  - {f}\n")
        return 1

    print("PASS: all output-contract schema checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
