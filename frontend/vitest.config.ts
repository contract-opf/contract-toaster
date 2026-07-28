import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// vitest.config.ts — component-test harness (issue #72; `resolve.conditions`
// added for issue #385).
//
// Kept as its own config (rather than merged into vite.config.ts) so the
// production build config never picks up test-only settings. Runs fully
// offline: jsdom environment, no network access, aws-amplify/auth mocked
// per-test (see src/__tests__/security-posture.test.tsx).
export default defineConfig({
  plugins: [react()],
  resolve: {
    // Vite/Vitest resolve npm "exports" conditions using Node's SSR
    // condition set by default, even under the jsdom test environment.
    // `@lit/react`'s "node" export omits the client-side property-binding
    // effect entirely (it assumes SSR hydration via `@lit/ssr-react`
    // instead) — under the default conditions its `createComponent`
    // wrapper silently no-ops on every prop, so `<CtChip variant="danger">`
    // would render but never reflect `variant` onto the host. Forcing the
    // "browser" condition here (test-only; vite.config.ts is untouched)
    // makes Vitest resolve the same browser build real users get.
    conditions: ['browser'],
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
    css: false,
    restoreMocks: true,
  },
});
