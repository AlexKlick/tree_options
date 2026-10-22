// Portfolio reader (Wave 3 lane G): GET /api/portfolio (`trex-portfolio/1`,
// Wave 2), relative to the app's mount. Money stays integer: cents and micros
// are formatted with integer arithmetic only, and an unknown amount is never
// printed as $0.00 (the page shows the block's reason instead).

import { fetchJson } from './api'
import { relativePath } from './activityApi'
import { API_PATHS, type Holding, type PortfolioView } from './contracts'

export const getPortfolio = (): Promise<PortfolioView> => fetchJson(relativePath(API_PATHS.portfolio))

function dollars(cents: number): string {
  const sign = cents < 0 ? '-' : ''
  const abs = Math.abs(cents)
  const whole = Math.floor(abs / 100)
  const rest = abs % 100
  return `${sign}$${whole.toLocaleString('en-US')}.${String(rest).padStart(2, '0')}`
}

/** `2500000` -> `$25,000.00`; `-134301` -> `-$1,343.01`. */
export function centsText(cents: number): string {
  if (!Number.isSafeInteger(cents)) return 'not a whole number of cents'
  return dollars(cents)
}

/** Micros (1e-6 dollars) to the cent, half away from zero. */
export function microsText(micros: number): string {
  if (!Number.isSafeInteger(micros)) return 'not a whole number of micros'
  const cents = Math.floor((Math.abs(micros) + 5_000) / 10_000)
  return dollars(micros < 0 ? -cents : cents)
}

export const SEC_TYPE_LABELS: Record<Holding['sec_type'], string> = { OPT: 'Option', STK: 'Stock' }

export const MISMATCH_LABELS: Record<string, string> = {
  unexplained: "At the broker, not in TREX's books",
  missing: "In TREX's books, not at the broker",
  quantity_mismatch: 'Quantities differ',
}

/** A holding's quantity split by attribution: lots with an intent are
 * TREX's; lots without one are outside TREX (`Attribution.intent_id` null). */
export function holdingSplit(holding: Holding): { trex: number; outside: number } {
  let trex = 0
  let outside = 0
  for (const lot of holding.lots) {
    if (lot.attribution.intent_id === null) outside += lot.quantity
    else trex += lot.quantity
  }
  return { trex, outside }
}

/** The symbol of a contract id from the holdings or the outside positions. */
export function symbolFor(view: PortfolioView, conId: number): string | null {
  const held = view.holdings.rows?.find((row) => row.con_id === conId)
  if (held) return held.symbol
  const outside = view.outside_positions.rows?.find((row) => row.con_id === conId)
  return outside ? outside.symbol : null
}
