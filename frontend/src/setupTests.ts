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

afterEach(() => {
  cleanup();
});
