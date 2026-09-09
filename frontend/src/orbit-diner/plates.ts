/**
 * plates.ts — the seven Orbit Diner material plates, resolved through Vite.
 *
 * WHY THIS FILE EXISTS (issue #717). The kit addresses its artwork by a runtime
 * `assetBase` string (`${assetBase}/toaster.webp`), which assumes an operator
 * hand-copies the folder somewhere the server exposes. That is not safe on this
 * deployment: the plates would be unfingerprinted (a stale CDN/browser cache
 * survives a redeploy) and they would live outside the build's integrity story
 * entirely. Importing each plate instead makes Vite emit it as a separate,
 * content-hashed, same-origin file under `dist/assets/` — the deployed CSP has
 * no `data:` in `connect-src`, and `vite.config.ts` pins these extensions past
 * the inline threshold so a plate can never become a `data:` URI.
 *
 * The supplier API is preserved: pass an explicit `base` and the plate is
 * addressed from that folder exactly as the kit's standalone demo does. Omit it
 * — which is what this app does — and the bundled URL is used.
 *
 * An import is not a fetch. Both toaster variants are emitted so #738 can pick
 * one at runtime; naming a URL here loads nothing and preloads nothing.
 */
import counterUrl from './artwork/counter.webp';
import dialUrl from './artwork/dial.webp';
import padUrl from './artwork/pad.webp';
import registerUrl from './artwork/register.webp';
import toastUrl from './artwork/toast.webp';
import toasterUrl from './artwork/toaster.webp';
import toasterPhoneUrl from './artwork/toaster-phone.webp';

/** Plate basenames, matching the files in `artwork/`. */
export type PlateName =
  | 'counter'
  | 'dial'
  | 'pad'
  | 'register'
  | 'toast'
  | 'toaster'
  | 'toaster-phone';

const bundled: Record<PlateName, string> = {
  counter: counterUrl,
  dial: dialUrl,
  pad: padUrl,
  register: registerUrl,
  toast: toastUrl,
  toaster: toasterUrl,
  'toaster-phone': toasterPhoneUrl,
};

/**
 * The URL for one plate. `base` overrides the bundled asset with
 * `<base>/<name>.webp`, for a host that serves the artwork folder itself.
 */
export function platePath(name: PlateName, base?: string): string {
  return base ? `${base}/${name}.webp` : bundled[name];
}
