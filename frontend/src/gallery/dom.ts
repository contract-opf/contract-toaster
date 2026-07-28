/**
 * gallery/dom.ts — small vanilla-DOM helpers for the component gallery
 * (issue #395). No React, no Lit templating: every helper here returns a
 * real Node built with `document.createElement`/`textContent`, matching
 * how the gallery itself consumes the ct-* elements (see main.ts's module
 * docstring). Nothing here ever assigns through `innerHTML` or its React
 * unsafe-HTML-prop equivalent — usage snippets go through `codeSnippet`'s
 * `textContent` assignment so they stay inert text even though they
 * contain markup-looking characters (§72 XSS posture; also exercised
 * directly by tests/test_frontend_xss_posture.py's source-posture scan).
 */

/** Creates a plain HTML element, optionally with a class, text, and children. */
export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  opts: { className?: string; text?: string } = {},
  children: (Node | string)[] = [],
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (opts.className) {
    node.className = opts.className;
  }
  if (opts.text !== undefined) {
    node.textContent = opts.text;
  }
  for (const child of children) {
    node.append(child);
  }
  return node;
}

/**
 * Creates a custom element (`ct-*`) by tag name, cast to the caller's
 * element type. A thin wrapper over `document.createElement` — the return
 * type is asserted rather than inferred because `HTMLElementTagNameMap`
 * has no entries for custom elements.
 */
export function createEl<T extends HTMLElement>(tag: string): T {
  return document.createElement(tag) as T;
}

/** A muted section eyebrow/group label above a row of examples. */
export function exampleGroup(label: string, content: HTMLElement): HTMLElement {
  return el('div', { className: 'gallery-example-group' }, [
    el('span', { className: 'gallery-example-group__label', text: label }),
    content,
  ]);
}

/** A flex row of example instances (buttons, chips, cards, …). */
export function row(children: (Node | string)[]): HTMLElement {
  return el('div', { className: 'gallery-row' }, children);
}

/**
 * An escaped usage-snippet block. `textContent` (never innerHTML) keeps the
 * literal JSX/HTML text inert — it renders as visible text, not markup.
 */
export function codeSnippet(text: string): HTMLElement {
  return el('pre', { className: 'gallery-snippet' }, [el('code', { text })]);
}

/** A component section: heading + optional description, returns the body
 * element callers append examples/snippets into. */
export function componentSection(
  id: string,
  title: string,
  description: string,
): { section: HTMLElement; body: HTMLElement } {
  const body = el('div', { className: 'gallery-section__body' });
  const section = el('section', { className: 'gallery-section' }, [
    el('h2', { text: title }),
    el('p', { className: 'gallery-section__desc', text: description }),
    body,
  ]);
  section.id = id;
  return { section, body };
}
