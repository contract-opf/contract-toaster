/**
 * AdminPenRules — admin authoring surface for the per-playbook pen-rules /
 * posture-override layer (issue #435), backed by
 * `POST /api/admin/playbooks/{playbook_id}/pen-rules/validate`
 * (issue #432, `backend/src/bundle_authoring.py`).
 *
 * ## Where this lives, and why it is its own tab
 *
 * `docs/frontend-design-system.md` §15.3 guessed this would sit inside the
 * Playbooks admin tab's per-version detail view. When this shipped that tab
 * did not exist, so it landed as a standalone admin section with its own
 * tab. The Playbooks tab has since landed (issue #434, `AdminPlaybooks.tsx`)
 * — but it has no per-version DETAIL view to nest inside, only a per-version
 * row in the history table, so this section stays where it is. Re-homing it
 * is a follow-up that needs that detail view first; nothing here duplicates
 * anything that tab offers.
 *
 * ## What this layer is — and, more importantly, what it is NOT
 *
 * Read `ARCHITECTURE.md`'s "Guidance-precedence model" before touching any
 * copy in this file. There are THREE separate guidance mechanisms in the
 * codebase and this file is the surface for exactly one of them:
 *
 *   1. `toaster_guidance` — per-review, ephemeral, governs review JUDGMENT.
 *      Live today. A different, unrelated authoring surface (issue #431).
 *      Nothing here touches it.
 *   2. The judged-NL Floor projected from `hard_rejections` (and the third,
 *      separate, still-unwired OPF v0.2 `floor_judge.py` Floor). Also
 *      judgment, not pen. Nothing here touches either.
 *   3. THIS layer: pen rules + a posture override, which govern
 *      REPLACEMENT-TEXT GENERATION (length bound + banned phrases applied to
 *      `proposed_replacement_text` by
 *      `scripts/replacement_text_enforcement.py::resolve_pen_rules`) — never
 *      review judgment.
 *
 * ## Zero runtime effect — the caveat this screen must never drop
 *
 * Every entry in `playbooks/registry.json` is a v1 playbook and
 * `pipeline_runner._load_playbook_bundle` only ever reads
 * `entry.playbook_path`, so every live review runs `resolve_pen_rules`'
 * v1-passthrough branch. A validated pen-rules document changes nothing
 * until a v2 bundle is actually activated — and the persist/bind/activate
 * route does not exist yet (the #432 route is validation only, no
 * persistence, no audit row). Hence the PERMANENT, NON-DISMISSABLE banner
 * this component renders unconditionally: shipping this screen without it
 * would present an inert control as a live one. Do not make that banner
 * conditional, collapsible, or dismissable.
 *
 * ## Validation split
 *
 * Cheaply-checkable things are checked here for fast feedback (JSON shape,
 * `mode` enum membership, numeric `max_chars`, numeric posture `version`,
 * a playbook id for the route path). Everything that needs the OPF document
 * — unknown `floor_ref`, stale `parent_section_digest`, non-monotonic
 * posture `version`, colliding `floor_additions` id, and the playbook-id /
 * OPF `agreement_type` cross-check — goes to the #432 route, whose
 * structured `{code, field, message}` errors are rendered against the
 * specific control that owns the field, never as one opaque failure string.
 *
 * Gated server-side: the route 403s a non-admin caller, and a 403 is the
 * sole signal to hide the panel — same posture as AdminUsers/AdminRetention/
 * AdminModel, no separate client-side "am I an admin" claim.
 */

import { useCallback, useState } from 'react';
import { authorizedFetch, friendlyErrorMessage, readErrorDetail } from './api';
import { CtBanner, CtButton, CtCard, CtField } from './ui/react';

// ---------------------------------------------------------------------------
// Types — mirror backend/src/bundle_authoring.py::validate_pen_rules_document.
// ---------------------------------------------------------------------------

export interface PenRulesValidationError {
  /** Stable slug, e.g. "unknown_floor_ref". */
  code: string;
  /** Input path the failure belongs to, e.g. "posture_override.version". */
  field: string;
  message: string;
}

export interface PenRulesValidationResult {
  playbook_id: string;
  valid: boolean;
  errors: PenRulesValidationError[];
}

/**
 * Accepted `pen_rules` `mode` values. The union of two real sources, not an
 * invented list:
 *   - `playbooks/schema.json`'s `replacement_text.mode` enum
 *     (fixed | from_template | bounded_edit | none) — `resolve_pen_rules`
 *     returns a `replacement_text`-shaped dict, so that is the enum the
 *     resolved value is ultimately consumed against, and
 *     `tests/test_pen_rules_resolution.py` exercises `bounded_edit`.
 *   - `replace`, the value the shipped `playbooks/pen-rules.defaults.json`
 *     artifact carries (and which the same test file exercises).
 * The backend validates `mode` nowhere at all (`bundle.schema-v2.json`'s
 * `pen_rules` is `additionalProperties: true`, and `bind_bundle` only checks
 * `floor_ref`s), so accepting the union can never block a document the
 * backend would have accepted — it only catches typos.
 */
export const PEN_RULE_MODES = ['fixed', 'from_template', 'bounded_edit', 'none', 'replace'] as const;

// ---------------------------------------------------------------------------
// Draft state — one editable row/layer per shape in
// `playbooks/pen-rules.defaults.json`. Everything is held as a string until
// submit so a half-typed number never has to be represented as NaN.
// ---------------------------------------------------------------------------

interface PhraseDraft {
  key: number;
  phrase: string;
  floorRef: string;
}

interface LayerDraft {
  mode: string;
  maxChars: string;
  phrases: PhraseDraft[];
}

interface TopicLayerDraft extends LayerDraft {
  key: number;
  topicId: string;
}

let nextKey = 0;
function newKey(): number {
  return ++nextKey;
}

function emptyLayer(): LayerDraft {
  return { mode: '', maxChars: '', phrases: [{ key: newKey(), phrase: '', floorRef: '' }] };
}

function emptyTopic(): TopicLayerDraft {
  return { key: newKey(), topicId: '', ...emptyLayer() };
}

// ---------------------------------------------------------------------------
// Error plumbing. Every error (ours or the server's) is rendered against the
// control that owns its `field`; `slug` names that control's container. An
// error whose field we do not recognize is still shown — in the catch-all
// bucket, with its field path — rather than silently dropped.
// ---------------------------------------------------------------------------

interface RenderedError extends PenRulesValidationError {
  slug: string | null;
}

const FIELD_SLUGS: Record<string, string> = {
  playbook_id: 'playbook-id',
  opf: 'opf',
  'pen_rules.must_not_introduce[].floor_ref': 'floor-ref',
  'posture_override.system_prompt': 'posture-system-prompt',
  'posture_override.version': 'posture-version',
  'posture_override.parent_section_digest': 'posture-digest',
  floor_additions: 'floor-additions',
  'floor_additions[].id': 'floor-additions',
  previous_bundle: 'previous-bundle',
  'pen_rules.default.mode': 'default-mode',
  'pen_rules.default.max_chars': 'default-max-chars',
};

function withSlug(error: PenRulesValidationError): RenderedError {
  return { ...error, slug: FIELD_SLUGS[error.field] ?? null };
}

function messagesFor(errors: RenderedError[], slug: string): string {
  return errors
    .filter((error) => error.slug === slug)
    .map((error) => error.message)
    .join(' ');
}

// ---------------------------------------------------------------------------
// Client-side checks. Deliberately only the things that need no round trip.
// ---------------------------------------------------------------------------

function isPositiveInteger(raw: string): boolean {
  return /^[0-9]+$/.test(raw.trim()) && Number(raw.trim()) >= 1;
}

interface ParsedJson {
  value: unknown;
  error: RenderedError | null;
}

function parseJson(raw: string, field: string, slug: string, expected: 'object' | 'array'): ParsedJson {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {
      value: undefined,
      error: {
        code: 'invalid_json',
        field,
        slug,
        message: "That isn't valid JSON. Paste the document exactly as it is stored, braces and all.",
      },
    };
  }
  const shapeOk =
    expected === 'array'
      ? Array.isArray(parsed)
      : typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed);
  if (!shapeOk) {
    return {
      value: undefined,
      error: {
        code: 'invalid_json_shape',
        field,
        slug,
        message:
          expected === 'array'
            ? 'This has to be a JSON array of objects.'
            : 'This has to be a single JSON object.',
      },
    };
  }
  return { value: parsed, error: null };
}

function layerHasContent(layer: LayerDraft): boolean {
  return (
    layer.mode.trim() !== '' ||
    layer.maxChars.trim() !== '' ||
    layer.phrases.some((row) => row.phrase.trim() !== '' || row.floorRef.trim() !== '')
  );
}

function buildLayer(layer: LayerDraft): Record<string, unknown> {
  const built: Record<string, unknown> = {};
  if (layer.mode.trim() !== '') {
    built.mode = layer.mode.trim();
  }
  if (layer.maxChars.trim() !== '') {
    built.max_chars = Number(layer.maxChars.trim());
  }
  const phrases = layer.phrases
    .filter((row) => row.phrase.trim() !== '' || row.floorRef.trim() !== '')
    .map((row) => {
      const entry: Record<string, string> = { phrase: row.phrase.trim() };
      if (row.floorRef.trim() !== '') {
        entry.floor_ref = row.floorRef.trim();
      }
      return entry;
    });
  if (phrases.length > 0) {
    built.must_not_introduce = phrases;
  }
  return built;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

function jsonFetch(path: string, init?: RequestInit): Promise<Response> {
  return authorizedFetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
}

export default function AdminPenRules(): React.ReactElement | null {
  const [playbookId, setPlaybookId] = useState('');
  const [opfText, setOpfText] = useState('');
  const [defaultLayer, setDefaultLayer] = useState<LayerDraft>(() => emptyLayer());
  const [topicLayers, setTopicLayers] = useState<TopicLayerDraft[]>([]);
  const [postureVersion, setPostureVersion] = useState('');
  const [postureSystemPrompt, setPostureSystemPrompt] = useState('');
  const [postureDigest, setPostureDigest] = useState('');
  const [floorAdditionsText, setFloorAdditionsText] = useState('');
  const [previousBundleText, setPreviousBundleText] = useState('');

  const [errors, setErrors] = useState<RenderedError[]>([]);
  const [result, setResult] = useState<PenRulesValidationResult | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [isForbidden, setIsForbidden] = useState(false);

  const updateDefaultLayer = useCallback((patch: Partial<LayerDraft>) => {
    setDefaultLayer((current) => ({ ...current, ...patch }));
  }, []);

  const updateTopic = useCallback((key: number, patch: Partial<TopicLayerDraft>) => {
    setTopicLayers((current) => current.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  }, []);

  const handleValidate = useCallback(
    async (event: React.FormEvent) => {
      event.preventDefault();
      setRequestError(null);
      setResult(null);

      // --- client-side pass (no round trip) ---------------------------------
      const local: RenderedError[] = [];

      if (playbookId.trim() === '') {
        local.push({
          code: 'missing_playbook_id',
          field: 'playbook_id',
          slug: 'playbook-id',
          message: 'Name the playbook this draft is for.',
        });
      }

      const opf = parseJson(opfText, 'opf', 'opf', 'object');
      if (opfText.trim() === '') {
        local.push({
          code: 'missing_opf',
          field: 'opf',
          slug: 'opf',
          message:
            'Paste the OPF document this draft is authored against. Its Floor invariants and posture digest are what the checks below compare to.',
        });
      } else if (opf.error) {
        local.push(opf.error);
      }

      const layerChecks: Array<{ layer: LayerDraft; modeField: string; maxField: string; modeSlug: string; maxSlug: string }> = [
        {
          layer: defaultLayer,
          modeField: 'pen_rules.default.mode',
          maxField: 'pen_rules.default.max_chars',
          modeSlug: 'default-mode',
          maxSlug: 'default-max-chars',
        },
        ...topicLayers.map((topic, index) => ({
          layer: topic,
          modeField: `pen_rules.per_topic.${topic.topicId || `#${index + 1}`}.mode`,
          maxField: `pen_rules.per_topic.${topic.topicId || `#${index + 1}`}.max_chars`,
          modeSlug: `topic-${index}-mode`,
          maxSlug: `topic-${index}-max-chars`,
        })),
      ];

      for (const check of layerChecks) {
        const mode = check.layer.mode.trim();
        if (mode !== '' && !(PEN_RULE_MODES as readonly string[]).includes(mode)) {
          local.push({
            code: 'invalid_mode',
            field: check.modeField,
            slug: check.modeSlug,
            message: `Pick one of: ${PEN_RULE_MODES.join(', ')}.`,
          });
        }
        const maxChars = check.layer.maxChars.trim();
        if (maxChars !== '' && !isPositiveInteger(maxChars)) {
          local.push({
            code: 'invalid_max_chars',
            field: check.maxField,
            slug: check.maxSlug,
            message: 'This has to be a whole number of characters, 1 or more.',
          });
        }
      }

      for (const [index, topic] of topicLayers.entries()) {
        if (topic.topicId.trim() === '' && layerHasContent(topic)) {
          local.push({
            code: 'missing_topic_id',
            field: `pen_rules.per_topic[${index}]`,
            slug: `topic-${index}-id`,
            message: 'Name the topic these rules apply to, or remove the block.',
          });
        }
      }

      const postureTouched =
        postureVersion.trim() !== '' || postureSystemPrompt.trim() !== '' || postureDigest.trim() !== '';
      if (postureTouched && !isPositiveInteger(postureVersion)) {
        local.push({
          code: 'invalid_version',
          field: 'posture_override.version',
          slug: 'posture-version',
          message: 'A posture override needs a whole version number, 1 or more.',
        });
      }

      const floorAdditions =
        floorAdditionsText.trim() === ''
          ? null
          : parseJson(floorAdditionsText, 'floor_additions', 'floor-additions', 'array');
      if (floorAdditions?.error) {
        local.push(floorAdditions.error);
      }

      const previousBundle =
        previousBundleText.trim() === ''
          ? null
          : parseJson(previousBundleText, 'previous_bundle', 'previous-bundle', 'object');
      if (previousBundle?.error) {
        local.push(previousBundle.error);
      }

      if (local.length > 0) {
        setErrors(local);
        return;
      }

      // --- server pass (#432's route) ---------------------------------------
      const body: Record<string, unknown> = { opf: opf.value };

      const penRules: Record<string, unknown> = {};
      if (layerHasContent(defaultLayer)) {
        penRules.default = buildLayer(defaultLayer);
      }
      const perTopic: Record<string, unknown> = {};
      for (const topic of topicLayers) {
        if (topic.topicId.trim() !== '') {
          perTopic[topic.topicId.trim()] = buildLayer(topic);
        }
      }
      if (Object.keys(perTopic).length > 0) {
        penRules.per_topic = perTopic;
      }
      if (Object.keys(penRules).length > 0) {
        body.pen_rules = penRules;
      }

      if (postureTouched) {
        body.posture_override = {
          version: Number(postureVersion.trim()),
          system_prompt: postureSystemPrompt,
          parent_section_digest: postureDigest.trim(),
        };
      }
      if (floorAdditions) {
        body.floor_additions = floorAdditions.value;
      }
      if (previousBundle) {
        body.previous_bundle = previousBundle.value;
      }

      setValidating(true);
      try {
        const response = await jsonFetch(
          `/api/admin/playbooks/${encodeURIComponent(playbookId.trim())}/pen-rules/validate`,
          { method: 'POST', body: JSON.stringify(body) },
        );
        if (response.status === 403) {
          setIsForbidden(true);
          return;
        }
        if (!response.ok) {
          const detail = await readErrorDetail(response);
          throw new Error(
            detail ??
              friendlyErrorMessage(
                `POST pen-rules validate returned HTTP ${response.status}`,
                "We couldn't check that draft. Please try again.",
              ),
          );
        }
        const data = (await response.json()) as PenRulesValidationResult;
        setResult(data);
        setErrors((data.errors ?? []).map(withSlug));
      } catch (err) {
        setErrors([]);
        setRequestError(
          err instanceof Error
            ? err.message
            : friendlyErrorMessage(err, "We couldn't check that draft. Please try again."),
        );
      } finally {
        setValidating(false);
      }
    },
    [
      defaultLayer,
      floorAdditionsText,
      opfText,
      playbookId,
      postureDigest,
      postureSystemPrompt,
      postureVersion,
      previousBundleText,
      topicLayers,
    ],
  );

  if (isForbidden) {
    return null;
  }

  const unattributed = errors.filter((error) => error.slug === null);

  // Phrase rows for one layer. `slugPrefix` keeps every control addressable
  // per layer (the default layer and each per-topic layer render the same
  // fields).
  const renderPhraseRows = (
    layer: LayerDraft,
    slugPrefix: string,
    onChange: (phrases: PhraseDraft[]) => void,
  ): React.ReactElement => (
    <div className="ct-stack" data-testid={`pen-rules-${slugPrefix}-phrases`}>
      {layer.phrases.map((row, index) => (
        <div className="ct-row" key={row.key}>
          <CtField label={`Phrase ${index + 1} the replacement must never introduce`}>
            <input
              data-testid={`pen-rules-${slugPrefix}-phrase-${index}`}
              type="text"
              autoComplete="off"
              value={row.phrase}
              onChange={(e) =>
                onChange(layer.phrases.map((p) => (p.key === row.key ? { ...p, phrase: e.target.value } : p)))
              }
            />
          </CtField>
          <CtField
            label={`Floor invariant id for phrase ${index + 1} (optional)`}
            hint="Ties the ban to a Floor invariant in the OPF above, which makes it sticky — a more specific layer can add rules but never drop this one."
          >
            <input
              data-testid={`pen-rules-${slugPrefix}-floor-ref-${index}`}
              type="text"
              autoComplete="off"
              value={row.floorRef}
              onChange={(e) =>
                onChange(layer.phrases.map((p) => (p.key === row.key ? { ...p, floorRef: e.target.value } : p)))
              }
            />
          </CtField>
          <CtButton
            type="button"
            variant="ghost"
            data-testid={`pen-rules-${slugPrefix}-phrase-remove-${index}`}
            onClick={() => onChange(layer.phrases.filter((p) => p.key !== row.key))}
          >
            Remove phrase
          </CtButton>
        </div>
      ))}
      <div className="ct-row">
        <CtButton
          type="button"
          variant="secondary"
          data-testid={`pen-rules-${slugPrefix}-phrase-add`}
          onClick={() => onChange([...layer.phrases, { key: newKey(), phrase: '', floorRef: '' }])}
        >
          Add a phrase
        </CtButton>
      </div>
    </div>
  );

  return (
    <section data-testid="admin-pen-rules-panel" className="ct-section ct-stack">
      <h2 className="ct-section-title">Pen rules &amp; posture override</h2>

      {/* PERMANENT liveness caveat. Never conditional, never dismissable —
          see this module's docstring and docs/frontend-design-system.md
          §15.3. Removing or gating this presents an inert control as a live
          one. */}
      <CtBanner variant="warn" data-testid="pen-rules-liveness-caveat">
        <strong>These rules are not in effect anywhere yet.</strong> They apply only to the
        next playbook version that is built and activated with them, and only to how
        replacement text is drafted — length limits and banned phrases. They do not change
        the playbook that is active right now, they do not reach any review that is running
        or already finished, and they never change how a review is judged (that is a
        separate mechanism entirely). Nothing on this screen is saved: it checks a draft and
        tells you what would be refused when the version is built.
      </CtBanner>

      {requestError && (
        <CtBanner variant="danger" data-testid="pen-rules-request-error">
          {requestError}
        </CtBanner>
      )}

      {result?.valid && (
        <CtBanner variant="ok" data-testid="pen-rules-valid">
          This draft passes every check. It still takes effect only on the next version built
          and activated with it — nothing was saved.
        </CtBanner>
      )}

      {result && !result.valid && (
        <CtBanner variant="danger" data-testid="pen-rules-invalid-summary">
          This draft would be refused. See the {errors.length === 1 ? 'problem' : `${errors.length} problems`} marked
          against the fields below.
        </CtBanner>
      )}

      <CtCard data-testid="pen-rules-form-card">
        {/* noValidate: this form's checks are OURS (and the #432 route's), and
            they are reported as field-attributed messages. Left on, the
            browser's own constraint validation intercepts submit for e.g. a
            `max_chars` below `min` and shows a native bubble instead — which
            both bypasses `handleValidate` entirely and gives the admin an
            error in a different, un-styled, un-attributed channel. */}
        <form className="ct-stack" noValidate onSubmit={handleValidate}>
          <div data-testid="pen-rules-field-playbook-id">
            <CtField
              label="Playbook"
              hint="The playbook these rules belong to. It has to match the agreement type named in the OPF document below."
              error={messagesFor(errors, 'playbook-id')}
            >
              <input
                data-testid="pen-rules-playbook-id"
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={playbookId}
                onChange={(e) => setPlaybookId(e.target.value)}
              />
            </CtField>
          </div>

          <div data-testid="pen-rules-field-opf">
            <CtField
              label="OPF document (JSON)"
              hint="Every check below compares your draft against this document — its Floor invariant ids and its current posture digest."
              error={messagesFor(errors, 'opf')}
            >
              <textarea
                data-testid="pen-rules-opf"
                rows={6}
                spellCheck={false}
                value={opfText}
                onChange={(e) => setOpfText(e.target.value)}
              />
            </CtField>
          </div>

          <h3 className="ct-section-title">Pen rules — default layer</h3>
          <p className="ct-muted">
            The fallback for every topic. A topic block below overrides these for that one
            topic.
          </p>

          <div data-testid="pen-rules-field-default-mode">
            <CtField
              label="Mode"
              hint={`One of: ${PEN_RULE_MODES.join(', ')}. Leave empty to inherit.`}
              error={messagesFor(errors, 'default-mode')}
            >
              <input
                data-testid="pen-rules-default-mode"
                type="text"
                autoComplete="off"
                spellCheck={false}
                value={defaultLayer.mode}
                onChange={(e) => updateDefaultLayer({ mode: e.target.value })}
              />
            </CtField>
          </div>

          <div data-testid="pen-rules-field-default-max-chars">
            <CtField
              label="Maximum characters"
              hint="Upper bound on a proposed replacement. Leave empty to inherit."
              error={messagesFor(errors, 'default-max-chars')}
            >
              <input
                data-testid="pen-rules-default-max-chars"
                type="number"
                min={1}
                step={1}
                autoComplete="off"
                value={defaultLayer.maxChars}
                onChange={(e) => updateDefaultLayer({ maxChars: e.target.value })}
              />
            </CtField>
          </div>

          {/* Group-scoped: the server reports an unknown floor_ref for the
              whole document, not for one row, so the error lands on the
              phrase group rather than being guessed onto a single input. */}
          <div data-testid="pen-rules-field-floor-ref">
            {messagesFor(errors, 'floor-ref') !== '' && (
              <CtBanner variant="danger">
                <strong>Floor invariant id:</strong> {messagesFor(errors, 'floor-ref')}
              </CtBanner>
            )}
            {renderPhraseRows(defaultLayer, 'default', (phrases) => updateDefaultLayer({ phrases }))}
          </div>

          <h3 className="ct-section-title">Pen rules — per topic</h3>
          {topicLayers.map((topic, index) => (
            <CtCard key={topic.key} data-testid={`pen-rules-topic-${index}`}>
              <div className="ct-stack">
                <div data-testid={`pen-rules-field-topic-${index}-id`}>
                  <CtField label={`Topic id ${index + 1}`} error={messagesFor(errors, `topic-${index}-id`)}>
                    <input
                      data-testid={`pen-rules-topic-${index}-id`}
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      value={topic.topicId}
                      onChange={(e) => updateTopic(topic.key, { topicId: e.target.value })}
                    />
                  </CtField>
                </div>
                <div data-testid={`pen-rules-field-topic-${index}-mode`}>
                  <CtField
                    label={`Mode for topic ${index + 1}`}
                    hint={`One of: ${PEN_RULE_MODES.join(', ')}. Leave empty to inherit.`}
                    error={messagesFor(errors, `topic-${index}-mode`)}
                  >
                    <input
                      data-testid={`pen-rules-topic-${index}-mode`}
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      value={topic.mode}
                      onChange={(e) => updateTopic(topic.key, { mode: e.target.value })}
                    />
                  </CtField>
                </div>
                <div data-testid={`pen-rules-field-topic-${index}-max-chars`}>
                  <CtField
                    label={`Maximum characters for topic ${index + 1}`}
                    error={messagesFor(errors, `topic-${index}-max-chars`)}
                  >
                    <input
                      data-testid={`pen-rules-topic-${index}-max-chars`}
                      type="number"
                      min={1}
                      step={1}
                      autoComplete="off"
                      value={topic.maxChars}
                      onChange={(e) => updateTopic(topic.key, { maxChars: e.target.value })}
                    />
                  </CtField>
                </div>
                {renderPhraseRows(topic, `topic-${index}`, (phrases) => updateTopic(topic.key, { phrases }))}
                <div className="ct-row">
                  <CtButton
                    type="button"
                    variant="danger"
                    data-testid={`pen-rules-topic-${index}-remove`}
                    confirm="Click again to remove"
                    onClick={() => setTopicLayers((current) => current.filter((row) => row.key !== topic.key))}
                  >
                    Remove this topic block
                  </CtButton>
                </div>
              </div>
            </CtCard>
          ))}
          <div className="ct-row">
            <CtButton
              type="button"
              variant="secondary"
              data-testid="pen-rules-topic-add"
              onClick={() => setTopicLayers((current) => [...current, emptyTopic()])}
            >
              Add a topic block
            </CtButton>
          </div>

          <h3 className="ct-section-title">Posture override</h3>
          <p className="ct-muted">
            A governed edit to the posture prose. Leave all three empty to send no override at
            all.
          </p>

          <div data-testid="pen-rules-field-posture-system-prompt">
            <CtField
              label="Posture text"
              error={messagesFor(errors, 'posture-system-prompt')}
            >
              <textarea
                data-testid="pen-rules-posture-system-prompt"
                rows={5}
                value={postureSystemPrompt}
                onChange={(e) => setPostureSystemPrompt(e.target.value)}
              />
            </CtField>
          </div>

          <div data-testid="pen-rules-field-posture-version">
            <CtField
              label="Posture version"
              hint="Has to be higher than the previous version's — versions only ever go up."
              error={messagesFor(errors, 'posture-version')}
            >
              <input
                data-testid="pen-rules-posture-version"
                type="number"
                min={1}
                step={1}
                autoComplete="off"
                value={postureVersion}
                onChange={(e) => setPostureVersion(e.target.value)}
              />
            </CtField>
          </div>

          <div data-testid="pen-rules-field-posture-digest">
            <CtField
              label="Posture digest this edit was written against"
              hint="Copy it from the OPF document above. If the document has moved on since you started, this is what catches it."
              error={messagesFor(errors, 'posture-digest')}
            >
              <input
                data-testid="pen-rules-posture-digest"
                type="text"
                autoComplete="off"
                spellCheck={false}
                className="ct-mono"
                value={postureDigest}
                onChange={(e) => setPostureDigest(e.target.value)}
              />
            </CtField>
          </div>

          <h3 className="ct-section-title">Floor additions and the previous version</h3>

          <div data-testid="pen-rules-field-floor-additions">
            <CtField
              label="Floor additions (JSON array, optional)"
              hint="Stricter-only additions. Every id has to be new — an existing Floor invariant can never be replaced or weakened."
              error={messagesFor(errors, 'floor-additions')}
            >
              <textarea
                data-testid="pen-rules-floor-additions"
                rows={4}
                spellCheck={false}
                value={floorAdditionsText}
                onChange={(e) => setFloorAdditionsText(e.target.value)}
              />
            </CtField>
          </div>

          <div data-testid="pen-rules-field-previous-bundle">
            <CtField
              label="Previously built version (JSON, optional)"
              hint="Supply it to have the posture version checked against the one it has to beat."
              error={messagesFor(errors, 'previous-bundle')}
            >
              <textarea
                data-testid="pen-rules-previous-bundle"
                rows={4}
                spellCheck={false}
                value={previousBundleText}
                onChange={(e) => setPreviousBundleText(e.target.value)}
              />
            </CtField>
          </div>

          {unattributed.length > 0 && (
            <CtBanner variant="danger" data-testid="pen-rules-error-other">
              <ul>
                {unattributed.map((error) => (
                  <li key={`${error.code}:${error.field}`}>
                    <strong>{error.field}</strong>: {error.message}
                  </li>
                ))}
              </ul>
            </CtBanner>
          )}

          <CtBanner variant="muted" data-testid="pen-rules-submit-caveat">
            Checking a draft saves nothing, activates nothing, and changes no review.
          </CtBanner>

          <div className="ct-row">
            <CtButton
              type="submit"
              variant="primary"
              data-testid="pen-rules-validate"
              disabled={validating}
              loading={validating}
            >
              {validating ? 'Checking…' : 'Check this draft'}
            </CtButton>
          </div>
        </form>
      </CtCard>
    </section>
  );
}
