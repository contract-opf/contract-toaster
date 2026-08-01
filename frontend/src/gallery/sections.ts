/**
 * gallery/sections.ts — builds one gallery section per CTDS component
 * (issue #395, docs/frontend-design-system.md §9), plus the token sheet.
 *
 * Every example instance below is a REAL `ct-*` custom element created
 * with `document.createElement` and wired exactly the way a non-React
 * consumer would: Lit reactive properties (`variant`, `size`, `pad`, …)
 * are set as plain HTML attributes where the component reflects them,
 * and the hand-rolled accessors several components use instead of Lit
 * properties (`text`, `loading`, `disabled`, `label`, `title`, `tabs`, …
 * — see each component's own module docstring for why: `@lit/react`
 * assigns those synchronously and this repo's tests read them
 * immediately, so the components mutate real DOM eagerly rather than
 * through Lit's async `render()` cycle) are set as JS properties. This
 * doubles as the "no-React smoke test" the ticket calls for: if any
 * component's public surface silently required React/`ui/react.ts` to
 * work, the elements built here would render broken.
 *
 * Every usage SNIPPET shown alongside a section, however, documents the
 * React consumption path (`ui/react.ts`) — that is how application code
 * actually uses these components (§5.4: "React code never writes `<ct-*>`
 * tags directly"); the vanilla construction in this file is gallery-only
 * plumbing, not the recommended pattern, so it isn't what's echoed back to
 * the reader.
 */
import { el, row, exampleGroup, codeSnippet, createEl, componentSection } from './dom';
import type { CtChip } from '../ui/components/ct-chip';
import type { CtButton } from '../ui/components/ct-button';
import type { CtIconButton } from '../ui/components/ct-icon-button';
import type { CtCard } from '../ui/components/ct-card';
import type { CtBanner } from '../ui/components/ct-banner';
import type { CtTabBar, CtTabDef } from '../ui/components/ct-tab-bar';
import type { CtAppShell } from '../ui/components/ct-app-shell';
import type { CtField } from '../ui/components/ct-field';
import type { CtToolbar } from '../ui/components/ct-toolbar';
import type { CtFileDrop } from '../ui/components/ct-file-drop';
import type { CtProgress } from '../ui/components/ct-progress';

export interface NavItem {
  id: string;
  label: string;
}

export const NAV_ITEMS: NavItem[] = [
  { id: 'ct-chip', label: 'ct-chip' },
  { id: 'ct-button', label: 'ct-button' },
  { id: 'ct-icon-button', label: 'ct-icon-button' },
  { id: 'ct-card', label: 'ct-card' },
  { id: 'ct-banner', label: 'ct-banner' },
  { id: 'ct-tab-bar', label: 'ct-tab-bar' },
  { id: 'ct-app-shell', label: 'ct-app-shell' },
  { id: 'ct-field', label: 'ct-field' },
  { id: 'ct-table', label: 'ct-table' },
  { id: 'ct-toolbar', label: 'ct-toolbar' },
  { id: 'ct-file-drop', label: 'ct-file-drop' },
  { id: 'ct-progress', label: 'ct-progress' },
  { id: 'tokens', label: 'Design tokens' },
];

const VARIANTS = ['ok', 'warn', 'danger', 'info', 'muted'] as const;

// ---------------------------------------------------------------------------
// ct-chip
// ---------------------------------------------------------------------------
export function buildChipSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-chip',
    'ct-chip',
    'Status pill. Shadow DOM leaf — the slotted label stays in light DOM (§5.2).',
  );

  function chip(variant: (typeof VARIANTS)[number], dot: boolean, label: string): CtChip {
    const c = createEl<CtChip>('ct-chip');
    c.setAttribute('variant', variant);
    if (dot) {
      c.setAttribute('dot', '');
    }
    c.textContent = label;
    return c;
  }

  body.append(
    exampleGroup(
      'Variants',
      row(VARIANTS.map((v) => chip(v, false, v))),
    ),
    exampleGroup(
      'With dot',
      row(VARIANTS.map((v) => chip(v, true, v))),
    ),
    codeSnippet(`import { CtChip } from '../ui/react';\n\n<CtChip variant="ok" dot>\n  Active\n</CtChip>`),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-button
// ---------------------------------------------------------------------------
export function buildButtonSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-button',
    'ct-button',
    'Light DOM, renders a real <button>. variant: primary|secondary|ghost|danger, size: sm|md, loading (spinner + aria-busy).',
  );

  function button(opts: {
    variant: 'primary' | 'secondary' | 'ghost' | 'danger';
    size?: 'sm' | 'md';
    text: string;
    loading?: boolean;
    disabled?: boolean;
    confirm?: string;
  }): CtButton {
    const b = createEl<CtButton>('ct-button');
    b.setAttribute('variant', opts.variant);
    b.setAttribute('size', opts.size ?? 'md');
    b.text = opts.text;
    if (opts.loading) {
      b.loading = true;
    }
    if (opts.disabled) {
      b.disabled = true;
    }
    if (opts.confirm) {
      b.confirm = opts.confirm;
    }
    return b;
  }

  const variants: Array<'primary' | 'secondary' | 'ghost' | 'danger'> = [
    'primary',
    'secondary',
    'ghost',
    'danger',
  ];

  body.append(
    exampleGroup(
      'Variants (md)',
      row(variants.map((v) => button({ variant: v, text: v }))),
    ),
    exampleGroup(
      'Sizes',
      row([
        button({ variant: 'primary', size: 'md', text: 'Medium' }),
        button({ variant: 'primary', size: 'sm', text: 'Small' }),
      ]),
    ),
    exampleGroup(
      'States',
      row([
        button({ variant: 'primary', text: 'Loading', loading: true }),
        button({ variant: 'secondary', text: 'Disabled', disabled: true }),
      ]),
    ),
    exampleGroup(
      'Confirm step (destructive) — click once to arm, again to confirm (auto-disarms after 4s)',
      row([button({ variant: 'danger', text: 'Remove', confirm: 'Click again to remove' })]),
    ),
    codeSnippet(
      `import { CtButton } from '../ui/react';\n\n<CtButton variant="primary" loading={isSubmitting}>\n  Toast it\n</CtButton>\n\n// Destructive action — arms on first click, fires on the second:\n<CtButton variant="danger" confirm="Click again to remove" onClick={remove}>\n  Remove\n</CtButton>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-icon-button
// ---------------------------------------------------------------------------
export function buildIconButtonSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-icon-button',
    'ct-icon-button',
    'Square hit-target ≥44px. `label` is required and becomes the inner button’s aria-label.',
  );

  function iconButton(opts: {
    label: string;
    text: string;
    pressed?: boolean;
    disabled?: boolean;
  }): CtIconButton {
    const b = createEl<CtIconButton>('ct-icon-button');
    b.label = opts.label;
    b.text = opts.text;
    if (opts.pressed !== undefined) {
      b.pressed = opts.pressed;
    }
    if (opts.disabled) {
      b.disabled = true;
    }
    return b;
  }

  body.append(
    exampleGroup(
      'Default / pressed / disabled',
      row([
        iconButton({ label: 'Mute sound', text: '\u{1F50A}' }),
        iconButton({ label: 'Unmute sound', text: '\u{1F507}', pressed: true }),
        iconButton({ label: 'Remove selected file', text: '×', disabled: true }),
      ]),
    ),
    codeSnippet(
      `import { CtIconButton } from '../ui/react';\n\n<CtIconButton label="Mute sound" aria-pressed={isMuted} onClick={toggleMute}>\n  \u{1F50A}\n</CtIconButton>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-card
// ---------------------------------------------------------------------------
export function buildCardSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-card',
    'ct-card',
    'Surface + border + shadow-1 + radius. Shadow DOM, all content slotted (§5.2). pad: none|md|lg.',
  );

  function card(pad: 'none' | 'md' | 'lg'): CtCard {
    const c = createEl<CtCard>('ct-card');
    c.setAttribute('pad', pad);
    c.append(
      el('h3', { text: `pad="${pad}"` }),
      el('p', { text: 'Card surface content, slotted from the light DOM.' }),
    );
    return c;
  }

  body.append(
    exampleGroup(
      'Padding',
      row([card('none'), card('md'), card('lg')]),
    ),
    codeSnippet(`import { CtCard } from '../ui/react';\n\n<CtCard pad="md">\n  <h3>Retention window</h3>\n  <p>…</p>\n</CtCard>`),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-banner
// ---------------------------------------------------------------------------
export function buildBannerSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-banner',
    'ct-banner',
    'Inline status surface. role="alert" for danger, role="status" otherwise (computed from variant).',
  );

  function banner(variant: (typeof VARIANTS)[number], text: string): CtBanner {
    const b = createEl<CtBanner>('ct-banner');
    b.setAttribute('variant', variant);
    b.textContent = text;
    return b;
  }

  const messages: Record<(typeof VARIANTS)[number], string> = {
    ok: 'Review complete — no requested changes identified by tool.',
    warn: 'Low confidence — attorney review recommended before relying on this result.',
    danger: 'Upload rejected — the file failed the format check.',
    info: 'Your document is queued for review.',
    muted: 'No reviews yet.',
  };

  body.append(
    el(
      'div',
      { className: 'gallery-example-group' },
      VARIANTS.map((v) => banner(v, messages[v])),
    ),
    codeSnippet(
      `import { CtBanner } from '../ui/react';\n\n<CtBanner variant="danger">\n  {error}\n</CtBanner>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-tab-bar (paired with a tiny live tabpanel demo — all panels stay
// mounted, toggled via `hidden`, matching App.tsx's own contract, §3.4).
// ---------------------------------------------------------------------------
export function buildTabBarSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-tab-bar',
    'ct-tab-bar',
    'ARIA tablist with roving tabindex + arrow/Home/End keyboard navigation. Emits ct-select {id}; the caller owns `active`.',
  );

  const tabs: CtTabDef[] = [
    { id: 'review', label: 'Review' },
    { id: 'users', label: 'Users' },
    { id: 'retention', label: 'Retention' },
  ];

  const tabBar = createEl<CtTabBar>('ct-tab-bar');
  tabBar.tabs = tabs;
  tabBar.active = 'review';

  const panels = new Map<string, HTMLElement>();
  const panelsWrap = el('div');
  for (const tab of tabs) {
    const panel = el(
      'section',
      { className: 'gallery-frame' },
      [el('p', { text: `Panel content for "${tab.label}".` })],
    );
    panel.id = `panel-${tab.id}`;
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', `tab-${tab.id}`);
    panel.hidden = tab.id !== 'review';
    panels.set(tab.id, panel);
    panelsWrap.append(panel);
  }

  tabBar.addEventListener('ct-select', (event) => {
    const { id } = (event as CustomEvent<{ id: string }>).detail;
    tabBar.active = id;
    for (const [panelId, panel] of panels) {
      panel.hidden = panelId !== id;
    }
  });

  body.append(
    exampleGroup('Live demo (click a tab, or use arrow keys)', el('div', {}, [tabBar, panelsWrap])),
    codeSnippet(
      `import { CtTabBar } from '../ui/react';\n\nconst tabs = [\n  { id: 'review', label: 'Review' },\n  { id: 'users', label: 'Users' },\n];\n\n<CtTabBar tabs={tabs} active={activeTab} onSelect={(e) => setActiveTab(e.detail.id)} />`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-app-shell
// ---------------------------------------------------------------------------
export function buildAppShellSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-app-shell',
    'ct-app-shell',
    'The app’s outer chrome: nameplate header, tab strip, max-width content column, mono footer. Slots: header regions via `slot="identity"`/`slot="tabs"`/`slot="footer"`, default content region for tabpanels.',
  );

  const shell = createEl<CtAppShell>('ct-app-shell');
  shell.brand = 'Contract Toaster Review Tool';

  const identity = el('div', { text: 'Signed in as jane@example.com' });
  identity.setAttribute('slot', 'identity');

  const tabsWrap = el('div');
  tabsWrap.setAttribute('slot', 'tabs');
  const miniTabs = createEl<CtTabBar>('ct-tab-bar');
  miniTabs.tabs = [{ id: 'review', label: 'Review' }];
  miniTabs.active = 'review';
  tabsWrap.append(miniTabs);

  const content = el('section', { text: 'Tabpanel content goes here.' });

  const footer = el('footer', { text: 'Version 0.4.2 (a1b2c3d4)' });
  footer.setAttribute('slot', 'footer');

  shell.append(identity, tabsWrap, content, footer);

  body.append(
    exampleGroup('Live demo (scaled to fit)', el('div', { className: 'gallery-frame' }, [shell])),
    codeSnippet(
      `import { CtAppShell, CtTabBar } from '../ui/react';\n\n<CtAppShell brand="Contract Toaster Review Tool">\n  <div slot="identity">Signed in as {userEmail}</div>\n  <div slot="tabs"><CtTabBar tabs={tabs} active={activeTab} onSelect={handleTabSelect} /></div>\n  <section id="panel-review" hidden={activeTab !== 'review'}>…</section>\n  <footer slot="footer">Version {version}</footer>\n</CtAppShell>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-field
// ---------------------------------------------------------------------------
export function buildFieldSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-field',
    'ct-field',
    'Label + slotted control + hint + error, with `for`/`aria-describedby`/`aria-invalid` wired automatically.',
  );

  function field(opts: { label: string; hint?: string; error?: string; type?: string }): CtField {
    const f = createEl<CtField>('ct-field');
    f.label = opts.label;
    if (opts.hint) {
      f.hint = opts.hint;
    }
    if (opts.error) {
      f.error = opts.error;
    }
    const input = el('input', {});
    input.setAttribute('type', opts.type ?? 'text');
    f.append(input);
    return f;
  }

  body.append(
    exampleGroup(
      'Default / with hint / with error',
      el('div', {}, [
        field({ label: 'Username' }),
        field({ label: 'OpenRouter API key', hint: 'Starts with sk-or-…', type: 'password' }),
        field({ label: 'Password', error: 'Incorrect username or password.', type: 'password' }),
      ]),
    ),
    codeSnippet(
      `import { CtField } from '../ui/react';\n\n<CtField label="Username">\n  <input type="text" value={username} onChange={handleChange} />\n</CtField>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-table
// ---------------------------------------------------------------------------
export function buildTableSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-table',
    'ct-table',
    'Styled table wrapper: sunken header row, hairline rows, hover tint, built-in horizontal scroll. The slotted <table> stays a real, fully-semantic table.',
  );

  const table = createEl<HTMLElement>('ct-table');
  const realTable = el('table');
  const thead = el('thead', {}, [
    el('tr', {}, [el('th', { text: 'Email' }), el('th', { text: 'Status' }), el('th', { text: 'Cognito sub' })]),
  ]);
  const tbody = el('tbody', {}, [
    el('tr', {}, [
      el('td', { text: 'jane@example.com' }),
      el('td', { text: 'active' }),
      (() => {
        const td = el('td', { text: 'a1b2c3d4-e5f6' });
        td.className = 'ct-table__mono';
        return td;
      })(),
    ]),
  ]);
  realTable.append(thead, tbody);
  table.append(realTable);

  const emptyTable = createEl<HTMLElement>('ct-table');
  const emptyRealTable = el('table');
  emptyRealTable.append(
    el('thead', {}, [el('tr', {}, [el('th', { text: 'Email' }), el('th', { text: 'Status' })])]),
    el('tbody', {}, [
      el('tr', {}, [
        (() => {
          const td = el('td', { text: 'No users match this filter.' });
          td.className = 'ct-table__empty';
          td.colSpan = 2;
          return td;
        })(),
      ]),
    ]),
  );
  emptyTable.append(emptyRealTable);

  body.append(
    exampleGroup('Populated', table),
    exampleGroup('Empty state (.ct-table__empty)', emptyTable),
    codeSnippet(
      `import { CtTable } from '../ui/react';\n\n<CtTable>\n  <table>\n    <thead>…</thead>\n    <tbody>…</tbody>\n  </table>\n</CtTable>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-toolbar
// ---------------------------------------------------------------------------
export function buildToolbarSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-toolbar',
    'ct-toolbar',
    'Row layout for a title plus filters/actions above a ct-table. `title` renders a real <h2> — see ct-toolbar.ts for why it never reflects the native `title` tooltip attribute.',
  );

  const toolbar = createEl<CtToolbar>('ct-toolbar');
  toolbar.title = 'Users';

  const filters = el('div', {});
  filters.setAttribute('slot', 'filters');
  const filterChip = createEl<CtChip>('ct-chip');
  filterChip.setAttribute('variant', 'info');
  filterChip.textContent = 'active only';
  filters.append(filterChip);

  const actions = el('div', {});
  actions.setAttribute('slot', 'actions');
  const actionButton = createEl<CtButton>('ct-button');
  actionButton.setAttribute('variant', 'primary');
  actionButton.setAttribute('size', 'sm');
  actionButton.text = 'Invite user';
  actions.append(actionButton);

  toolbar.append(filters, actions);

  body.append(
    exampleGroup('Title + filters + actions', el('div', { className: 'gallery-frame' }, [toolbar])),
    codeSnippet(
      `import { CtToolbar } from '../ui/react';\n\n<CtToolbar title="Users">\n  <div slot="filters"><CtChip variant="info">active only</CtChip></div>\n  <div slot="actions"><CtButton variant="primary" size="sm">Invite user</CtButton></div>\n</CtToolbar>`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-file-drop
// ---------------------------------------------------------------------------
export function buildFileDropSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-file-drop',
    'ct-file-drop',
    'Drag-and-drop + click-to-browse upload well. Emits ct-files {files}; the visible input stays the only focus stop (§5.1 note on ct-file-drop.ts).',
  );

  // Simulates a real user selection through the component's own real
  // <input type=file> (light DOM — §5.1), the same DataTransfer path a
  // browser drag-drop or file picker would produce; this is dev-gallery-only
  // demo wiring, not a public ct-file-drop API.
  const selectDemoFile = (drop: CtFileDrop, filename: string): boolean => {
    const input = drop.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) {
      return false;
    }
    const file = new File(['sample contents'], filename, {
      type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    });
    const dataTransfer = new DataTransfer();
    dataTransfer.items.add(file);
    input.files = dataTransfer.files;
    input.dispatchEvent(new Event('change'));
    return true;
  };

  const empty = createEl<CtFileDrop>('ct-file-drop');
  empty.accept = '.docx';

  const withFile = createEl<CtFileDrop>('ct-file-drop');
  withFile.accept = '.docx';
  queueMicrotask(() => {
    selectDemoFile(withFile, 'Sample Agreement.docx');
  });

  // Selected, then CLEARED — the exact state issue #423 fixed. Before
  // ct-file-drop.css's `:not([hidden])` guard this frame rendered a lingering
  // empty grey box holding a lone `×`; it must now look identical to the
  // never-touched "empty" frame on its left.
  const cleared = createEl<CtFileDrop>('ct-file-drop');
  cleared.accept = '.docx';
  queueMicrotask(() => {
    if (!selectDemoFile(cleared, 'Sample Agreement.docx')) {
      return;
    }
    cleared.querySelector<HTMLElement>('.ct-file-drop__clear')?.click();
  });

  body.append(
    exampleGroup(
      'Empty / with a file selected / selected then cleared',
      row([
        el('div', { className: 'gallery-frame' }, [empty]),
        el('div', { className: 'gallery-frame' }, [withFile]),
        el('div', { className: 'gallery-frame' }, [cleared]),
      ]),
    ),
    codeSnippet(
      `import { CtFileDrop } from '../ui/react';\n\n<CtFileDrop accept=".docx" onFiles={(e) => setFile(e.detail.files[0] ?? null)} />`,
    ),
  );

  return section;
}

// ---------------------------------------------------------------------------
// ct-progress
// ---------------------------------------------------------------------------
export function buildProgressSection(): HTMLElement {
  const { section, body } = componentSection(
    'ct-progress',
    'ct-progress',
    'Indeterminate warm shimmer bar. role="progressbar" with no aria-valuenow signals indeterminate; `label` becomes aria-valuetext.',
  );

  const bare = createEl<CtProgress>('ct-progress');

  const captioned = createEl<CtProgress>('ct-progress');
  captioned.label = 'Reviewing your document…';

  body.append(
    exampleGroup('Bare / with caption', el('div', {}, [bare, el('div', { className: 'gallery-space-row' }), captioned])),
    codeSnippet(`import { CtProgress } from '../ui/react';\n\n<CtProgress label="Reviewing your document…" />`),
  );

  return section;
}

// ---------------------------------------------------------------------------
// Tokens page (docs/frontend-design-system.md §4)
// ---------------------------------------------------------------------------
function swatch(varName: string, label?: string): HTMLElement {
  const fill = el('div', { className: 'gallery-swatch__fill' });
  fill.style.background = `var(${varName})`;
  return el('div', { className: 'gallery-swatch' }, [fill, el('div', { className: 'gallery-swatch__label', text: label ?? varName })]);
}

export function buildTokensSection(): HTMLElement {
  const { section, body } = componentSection(
    'tokens',
    'Design tokens',
    'frontend/src/styles/tokens.css is the single source of truth (docs/frontend-design-system.md §4). Every ct-* component and this gallery reference these custom properties exclusively.',
  );

  const brandTokens = [
    '--ct-accent',
    '--ct-accent-strong',
    '--ct-accent-soft',
    '--ct-accent-contrast',
    '--ct-glow',
    '--ct-toast',
    '--ct-toast-crust',
  ];
  const statusTokens = ['ok', 'warn', 'danger', 'info', 'neutral'].flatMap((s) => [
    `--ct-${s}`,
    `--ct-${s}-bg`,
    `--ct-${s}-border`,
  ]);
  const surfaceTokens = [
    '--ct-bg',
    '--ct-surface',
    '--ct-surface-raised',
    '--ct-surface-sunken',
    '--ct-border',
    '--ct-border-strong',
    '--ct-text',
    '--ct-text-muted',
  ];

  const colorSection = el('div', {}, [
    exampleGroup('Brand ramp', el('div', { className: 'gallery-swatch-grid' }, brandTokens.map((t) => swatch(t)))),
    exampleGroup('Status pairs', el('div', { className: 'gallery-swatch-grid' }, statusTokens.map((t) => swatch(t)))),
    exampleGroup('Surfaces & text', el('div', { className: 'gallery-swatch-grid' }, surfaceTokens.map((t) => swatch(t)))),
  ]);

  const spaceSteps = [1, 2, 3, 4, 5, 6, 7, 8];
  const spacingSection = exampleGroup(
    'Spacing scale',
    el(
      'div',
      {},
      spaceSteps.map((n) => {
        const bar = el('div', { className: 'gallery-space-bar' });
        bar.style.width = `var(--ct-space-${n})`;
        return el('div', { className: 'gallery-space-row' }, [
          el('span', { className: 'gallery-space-row__name', text: `--ct-space-${n}` }),
          bar,
        ]);
      }),
    ),
  );

  const radiusTokens = ['--ct-radius-sm', '--ct-radius', '--ct-radius-lg', '--ct-radius-full'];
  const radiusSection = exampleGroup(
    'Radii',
    el(
      'div',
      { className: 'gallery-radius-row' },
      radiusTokens.map((t) => {
        const box = el('div', { className: 'gallery-radius-swatch', text: t });
        box.style.borderRadius = `var(${t})`;
        return box;
      }),
    ),
  );

  const shadowTokens = ['--ct-shadow-1', '--ct-shadow-2', '--ct-shadow-3'];
  const shadowSection = exampleGroup(
    'Shadow levels',
    el(
      'div',
      { className: 'gallery-shadow-row' },
      shadowTokens.map((t) => {
        const box = el('div', { className: 'gallery-shadow-swatch', text: t });
        box.style.boxShadow = `var(${t})`;
        return box;
      }),
    ),
  );

  const typeSpecimens: Array<{ font: string; label: string; sample: string; size: string }> = [
    { font: '--ct-font-display', label: 'Display — Space Grotesk', sample: 'Contract Toaster', size: '--ct-text-2xl' },
    { font: '--ct-font-sans', label: 'Sans — Instrument Sans', sample: 'Attorney approval required.', size: '--ct-text-md' },
    { font: '--ct-font-mono', label: 'Mono — IBM Plex Mono', sample: 'rev_8f2a1c9d', size: '--ct-text-sm' },
  ];
  const typeSection = exampleGroup(
    'Typography',
    el(
      'div',
      {},
      typeSpecimens.map((spec) => {
        const sample = el('p', { text: spec.sample });
        sample.style.fontFamily = `var(${spec.font})`;
        sample.style.fontSize = `var(${spec.size})`;
        sample.style.margin = '0';
        return el('div', { className: 'gallery-type-specimen' }, [
          el('p', { className: 'gallery-type-specimen__meta', text: `${spec.label} — ${spec.font} @ ${spec.size}` }),
          sample,
        ]);
      }),
    ),
  );

  const motionSection = exampleGroup(
    'Motion (hover the box — durations/easings from tokens.css, reduced-motion honored via base.css)',
    el('div', { className: 'gallery-motion-demo' }, [
      el('div', { className: 'gallery-motion-box' }),
      el('div', { className: 'gallery-motion-row' }, [
        el('span', { text: '--ct-dur-fast 120ms' }),
        el('span', { text: '--ct-dur 200ms' }),
        el('span', { text: '--ct-dur-slow 400ms' }),
        el('span', { text: '--ct-ease-spring' }),
      ]),
    ]),
  );

  body.append(colorSection, spacingSection, radiusSection, shadowSection, typeSection, motionSection);

  return section;
}
