import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
//
// Keyed off `mode` (function form of defineConfig — issue #395) so the dev
// component gallery (frontend/gallery.html + frontend/src/gallery/) is a
// second Rollup entry ONLY outside a production build: `npm run dev` always
// serves it at /gallery.html regardless of this input map (Vite's dev
// server resolves any .html file under root on request), but
// `rollupOptions.input` controls what a `vite build` actually EMITS, and
// the gallery must never ship (docs/frontend-design-system.md §9). The main
// entry's resolution is untouched either way — `mode === 'production'`
// (the default for `vite build`, and what `npm run build`/`build:ci` use)
// keeps the implicit single-entry (index.html) behavior; any other mode
// (e.g. `--mode development`, used by the gallery's own build-time
// verification) adds gallery.html alongside it.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  server: {
    port: 3000,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Never inline audio as a `data:` URI. Vite inlines assets under ~4KB by
    // default, which would silently turn the small toaster clips
    // (src/assets/sounds/*.mp3) into data: URIs. sounds.ts loads them with
    // fetch(), and the deployed CSP allows `connect-src 'self' <cognito>
    // <api>` with no `data:` — so an inlined clip would be CSP-blocked in
    // production while working fine locally. Emitting real files keeps every
    // clip a same-origin request. Other asset types keep the default
    // behaviour.
    assetsInlineLimit: (filePath: string) =>
      /\.(mp3|ogg|wav|m4a)$/i.test(filePath) ? false : undefined,
    ...(mode === 'production'
      ? {}
      : {
          rollupOptions: {
            input: {
              main: fileURLToPath(new URL('./index.html', import.meta.url)),
              gallery: fileURLToPath(new URL('./gallery.html', import.meta.url)),
            },
          },
        }),
  },
}));
