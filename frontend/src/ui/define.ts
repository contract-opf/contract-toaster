/**
 * ui/define.ts — safe custom-element registration (CTDS foundation, issue #385).
 *
 * vitest re-imports component modules across test files (each test file gets
 * its own module graph in some configurations, but jsdom's `customElements`
 * registry is shared per test-run process), so a bare
 * `customElements.define(tag, klass)` at module scope throws
 * `NotSupportedError: this name has already been used` the second time a
 * component module is evaluated. Every ct-* component registers through
 * `defineOnce` instead of the `@customElement` decorator so re-evaluation is
 * a harmless no-op both in tests and in dev/HMR.
 */
export function defineOnce(tag: string, klass: CustomElementConstructor): void {
  if (customElements.get(tag) === undefined) {
    customElements.define(tag, klass);
  }
}
