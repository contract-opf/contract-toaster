import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// vitest.config.ts — component-test harness (issue #72).
//
// Kept as its own config (rather than merged into vite.config.ts) so the
// production build config never picks up test-only settings. Runs fully
// offline: jsdom environment, no network access, aws-amplify/auth mocked
// per-test (see src/__tests__/security-posture.test.tsx).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/setupTests.ts'],
    css: false,
    restoreMocks: true,
  },
});
