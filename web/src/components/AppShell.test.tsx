import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { AppShell } from './AppShell'

describe('AppShell nav (portal invariant)', () => {
  it('nav links are hash-only and document-relative', () => {
    render(<AppShell title="Plans">x</AppShell>)
    const links = screen.getAllByRole('link')
    for (const a of links) {
      expect(a.getAttribute('href')?.startsWith('/')).toBe(false)
    }
    expect(screen.getByRole('link', { name: 'Discover' })).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Plans' }).getAttribute('aria-current')).toBe(
      'page',
    )
  })
})
