import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// https://vitejs.dev/config/
export default defineConfig({
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
  },
});
