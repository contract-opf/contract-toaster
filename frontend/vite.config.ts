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
// Vendor chunking (issue #56). Third-party packages that every route shares,
// split out of the entry chunk so a visitor who never opens an admin panel —
// or a password-mode deployment that never signs in through Cognito at all —
// does not pay for them on first paint. The route-level half of the split is
// App.tsx's `React.lazy` boundaries.
//
// WHY THE FUNCTION FORM, not `manualChunks: { react: ['react','react-dom'],
// … }`: the object form assigns a package's ENTRY MODULE plus whatever of its
// dependency subgraph is still unclaimed, and `react`/`react-dom` resolve to
// thin ESM shims whose actual implementation lives in sibling CJS modules
// (react/cjs/react.production.min.js, …). Those sibling modules are reachable
// from `@aws-amplify/ui-react` too, so Rollup handed them to the `amplify`
// chunk and left `react` a 30-byte re-export — which made the ENTRY chunk
// statically import the 627 kB Amplify chunk on every page load, the exact
// regression this ticket exists to remove. Measured, not theorised: that was
// the first build of this change. Matching on the package DIRECTORY instead
// claims every module of a package, react's CJS internals included, and the
// `react`/`lit` arms are checked before `amplify` so a shared module goes to
// the smaller, always-needed chunk.
function packageOf(id: string): string | null {
  const marker = '/node_modules/';
  const at = id.lastIndexOf(marker);
  if (at === -1) {
    return null;
  }
  const segments = id.slice(at + marker.length).split('/');
  return segments[0].startsWith('@') ? `${segments[0]}/${segments[1] ?? ''}` : segments[0];
}

function manualChunks(id: string): string | undefined {
  const pkg = packageOf(id);
  if (!pkg) {
    return undefined;
  }
  // `scheduler` is react-dom's own runtime dependency and is useless apart
  // from it; keeping them together avoids a two-file waterfall for react-dom.
  if (pkg === 'react' || pkg === 'react-dom' || pkg === 'scheduler') {
    return 'react';
  }
  if (pkg === 'lit' || pkg.startsWith('lit-') || pkg.startsWith('@lit/')) {
    return 'lit';
  }
  // aws-amplify's transitive tree (@smithy/*, @aws-crypto/*, @aws-sdk/*,
  // amazon-cognito-identity-js, …) is reachable ONLY from the two packages
  // named in the ticket, so folding it into the same chunk keeps the lazy
  // Amplify payload one request instead of a dozen.
  if (
    pkg.startsWith('aws-amplify') ||
    pkg.startsWith('@aws-amplify/') ||
    pkg.startsWith('@aws-crypto/') ||
    pkg.startsWith('@aws-sdk/') ||
    pkg.startsWith('@smithy/') ||
    pkg.startsWith('amazon-cognito')
  ) {
    return 'amplify';
  }
  return undefined;
}

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  resolve: {
    // Password-mode (Docker Compose) builds omit the Amplify UI stylesheet
    // entirely (issue #56). main.tsx already imports it dynamically behind
    // the auth-mode branch, which keeps it out of the entry chunk on every
    // target — but Vite's CSS pipeline emits an asset for every CSS module
    // that REACHED the graph, not for every one that survived tree-shaking,
    // so on a password build the 315 kB file was still written to
    // dist/assets/ (orphaned, referenced by nothing). Aliasing the specifier
    // to an empty stand-in is what actually drops it. `process.env` rather
    // than `loadEnv` because this is the same variable the build is invoked
    // with (VITE_AUTH_MODE=password vite build) and Vite's own
    // `import.meta.env` substitution reads it from there too, so the two
    // cannot disagree.
    alias:
      process.env.VITE_AUTH_MODE?.toLowerCase() === 'password'
        ? {
            '@aws-amplify/ui-react/styles.css': fileURLToPath(
              new URL('./src/styles/amplify-styles-omitted.css', import.meta.url),
            ),
          }
        : {},
  },
  server: {
    port: 3000,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // Never inline audio, artwork or fonts as a `data:` URI. Vite inlines
    // assets under ~4KB by default, which would silently turn the small
    // toaster clips (src/assets/sounds/*.mp3) into data: URIs. sounds.ts
    // loads them with fetch(), and the deployed CSP allows `connect-src
    // 'self' <cognito> <api>` with no `data:` — so an inlined clip would be
    // CSP-blocked in production while working fine locally. Emitting real
    // files keeps every clip a same-origin request.
    //
    // Images and fonts are pinned the same way for the Orbit Diner console
    // (issue #717). Its seven material plates are all far above the size
    // threshold today, so the default would emit them anyway — but that is an
    // accident of how heavy the current artwork happens to be, not a
    // guarantee. A future re-export, a thinner variant plate or a small
    // decorative asset dropping under the threshold would become a data: URI
    // and the same CSP would block it. Stating the rule is what makes the
    // production behaviour independent of the byte count. Other asset types
    // keep the default behaviour.
    assetsInlineLimit: (filePath: string) =>
      /\.(mp3|ogg|wav|m4a|webp|png|jpe?g|avif|gif|woff2?|ttf|otf)$/i.test(
        filePath,
      )
        ? false
        : undefined,
    // `manualChunks` (defined above) is declared inside BOTH branches of the
    // spread below rather than beside it: the two branches each own the whole
    // `rollupOptions` key, so a sibling `rollupOptions: { output: … }` here
    // would be overwritten by the spread and the mode-conditional `input` map
    // would silently lose its vendor chunking.
    ...(mode === 'production'
      ? {
          rollupOptions: {
            output: { manualChunks },
          },
        }
      : {
          rollupOptions: {
            input: {
              main: fileURLToPath(new URL('./index.html', import.meta.url)),
              gallery: fileURLToPath(new URL('./gallery.html', import.meta.url)),
            },
            output: { manualChunks },
          },
        }),
  },
}));
