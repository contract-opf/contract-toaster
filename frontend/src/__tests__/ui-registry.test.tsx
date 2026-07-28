/**
 * ui-registry.test.tsx — jsdom smoke test for `ui/index.ts` (issue #395,
 * docs/frontend-design-system.md §9 acceptance criteria).
 *
 * Importing `ui/index.ts` for its side effects must register every ct-*
 * custom element with `customElements` — that's the contract the dev
 * component gallery (frontend/src/gallery/) relies on, since the gallery
 * imports `ui/index.ts` directly and builds elements with
 * `document.createElement` rather than through React/`ui/react.ts`. This
 * test is the "no-React smoke test" acceptance criterion in its cheapest
 * form: it doesn't render anything, it only asserts the registry is
 * populated, mirroring what the gallery depends on at module-load time.
 *
 * Every other `ui-*.test.tsx` file in this directory imports `ui/react.ts`
 * (which itself imports every component module and so registers them too),
 * so by the time this file's own `describe` block runs, `customElements`
 * already has every tag registered by earlier test files in the same
 * vitest worker. Importing `ui/index.ts` here — the module this test is
 * actually about — keeps the assertion meaningful (and correct) regardless
 * of test run order or isolation mode: `defineOnce` (ui/define.ts) makes a
 * second registration of an already-defined tag a harmless no-op.
 */
import { describe, expect, it } from 'vitest';
import '../ui/index.ts';

const REGISTERED_TAGS = [
  'ct-chip',
  'ct-button',
  'ct-icon-button',
  'ct-card',
  'ct-banner',
  'ct-tab-bar',
  'ct-app-shell',
  'ct-field',
  'ct-table',
  'ct-toolbar',
  'ct-file-drop',
  'ct-progress',
];

describe('ui/index.ts registry', () => {
  it.each(REGISTERED_TAGS)('registers %s', (tag) => {
    expect(customElements.get(tag)).toBeDefined();
  });
});
