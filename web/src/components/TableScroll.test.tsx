import { afterEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { TableScroll } from './TableScroll'

// M8 flash review: on a 390px phone, 600-707px tables sat in 336px
// scrollers with no cue - "Max gain" read "Ma", "+$27,523" read "+$2".
const sizes = (scrollWidth: number, clientWidth: number) => {
  Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
    configurable: true,
    get: () => scrollWidth,
  })
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => clientWidth,
  })
}

describe('TableScroll', () => {
  afterEach(() => {
    delete (HTMLElement.prototype as { scrollWidth?: number }).scrollWidth
    delete (HTMLElement.prototype as { clientWidth?: number }).clientWidth
  })

  it('cues hidden columns when the table overflows', () => {
    sizes(707, 336)
    const { container } = render(
      <TableScroll>
        <table>
          <tbody>
            <tr>
              <td>x</td>
            </tr>
          </tbody>
        </table>
      </TableScroll>,
    )
    expect(screen.getByText('scroll for more columns →')).toBeTruthy()
    expect(container.querySelector('.table-scroll.has-more')).not.toBeNull()
  })

  it('stays quiet when the table fits', () => {
    sizes(600, 600)
    const { container } = render(
      <TableScroll>
        <table />
      </TableScroll>,
    )
    expect(screen.queryByText(/scroll for more/)).toBeNull()
    expect(container.querySelector('.has-more')).toBeNull()
  })
})
