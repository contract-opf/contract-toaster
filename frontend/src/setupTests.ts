/**
 * setupTests.ts — vitest global setup (issue #72).
 *
 * Registers @testing-library/jest-dom's matchers (toBeInTheDocument, etc.)
 * and cleans up the jsdom document between tests so component trees from
 * one test don't leak into the next.
 */
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// ---------------------------------------------------------------------------
// `window.matchMedia`, which jsdom does not implement (issue #733).
//
// Every real browser has it; jsdom throws `window.matchMedia is not a
// function`. Our own `toaster/sounds.ts` already guards the call, but the
// vendored console reads it unguarded — a media
// query is not an optional capability in the environment it was written for —
// and once the console became the Review tab that single gap took out 274
// assertions across 31 files that have nothing to do with media queries.
//
// The fix belongs HERE, not in the component: the missing API is a property of
// the test environment, and patching it once in the harness is what keeps
// `src/orbit-diner/` a faithful copy of the supplier drop. The stub answers
// "no preference" to everything, which is the default a browser reports and
// the state the suite's assertions assume.
//
// Files that need a specific answer (`motion.test.ts`, `sounds.test.tsx`, the
// orbit-diner files) still override or delete it with `vi.stubGlobal` /
// `delete window.matchMedia`; this only supplies the floor they build on.
// Assigned rather than defined so those `delete`s keep working.
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  (window as unknown as { matchMedia: (q: string) => MediaQueryList }).matchMedia = (
    query: string
  ) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as unknown as MediaQueryList;
}

// ---------------------------------------------------------------------------
// `<dialog>`'s modal methods, which jsdom declares but does not implement
// (issue #733).
//
// Same class of gap as `matchMedia` above, and the same reasoning: the console
// puts the receipt, the record, the cover note, the disposition and the
// playbook catalog in ONE real `<dialog>`, so almost every workflow test
// opens one. `showModal`/`close` are filled in with the
// attribute toggling and the `close` event the spec defines — enough for the
// element to be open, queryable and closable, which is what the assertions are
// about. Three orbit-diner files carried private copies of this before it
// belonged to the whole suite; theirs are guarded and simply find it done.
{
  const proto = HTMLDialogElement.prototype as unknown as Record<string, unknown>;
  if (typeof proto.showModal !== 'function') {
    proto.showModal = function (this: HTMLDialogElement) {
      this.setAttribute('open', '');
    };
  }
  if (typeof proto.close !== 'function') {
    proto.close = function (this: HTMLDialogElement) {
      this.removeAttribute('open');
      this.dispatchEvent(new Event('close'));
    };
  }
}

// ---------------------------------------------------------------------------
// Pointer capture, which jsdom does not implement (issue #733).
//
// Third gap of the same kind. Any control that supports a drag calls
// `setPointerCapture` on pointerdown so the pointer keeps reporting to it once
// it leaves the element's box — the console's lever and its playbook dial both
// do — and jsdom throws "is not a function" from inside the React handler.
// Every assertion still passed (React catches it), but vitest reports the
// escaped exception and the run fails on it, which is a harness gap wearing
// the costume of a product bug. No-ops are the honest stubs: jsdom has no
// pointer to capture.
for (const name of ['setPointerCapture', 'releasePointerCapture'] as const) {
  if (typeof Element.prototype[name] !== 'function') {
    (Element.prototype as unknown as Record<string, unknown>)[name] = () => {};
  }
}
if (typeof Element.prototype.hasPointerCapture !== 'function') {
  (Element.prototype as unknown as Record<string, unknown>).hasPointerCapture = () => false;
}

afterEach(() => {
  cleanup();
});
