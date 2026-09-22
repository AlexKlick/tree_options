// vitest runs without globals, so RTL's auto-cleanup never registers;
// mount per test otherwise leaks into the next file's queries.
import { afterEach } from 'vitest'
import { cleanup } from '@testing-library/react'

afterEach(cleanup)

// jsdom has no matchMedia; useTween reads .matches (guarded with ?.,
// but the typed stub keeps tests honest).
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
}
