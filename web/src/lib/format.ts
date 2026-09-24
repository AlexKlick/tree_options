// Formatting lives client-side; the only exception is the prebuilt _usd
// label strings the server sends inside payoff payloads (tooltips/aria).

const etTimeFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hourCycle: 'h23',
  hour: '2-digit',
  minute: '2-digit',
})
const etDateFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  month: 'short',
  day: 'numeric',
})
const etSecFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hourCycle: 'h23',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
})

export const etTime = (iso: string): string => etTimeFmt.format(new Date(iso))

/** Same ET wall-clock, for epoch-ms chart points. */
export const etTimeMs = (ms: number): string => etTimeFmt.format(new Date(ms))

export const etDateTime = (iso: string): string => {
  const d = new Date(iso)
  return `${etDateFmt.format(d)} ${etSecFmt.format(d)} ET`
}

export const usd = (v: number): string =>
  `$${Math.abs(Math.round(v)).toLocaleString('en-US')}`

/** Mirrors the server's `_usd`: sign, dollar, commas, no decimals. */
export const usdSigned = (v: number): string =>
  `${v >= 0 ? '+' : '-'}$${Math.abs(Math.round(v)).toLocaleString('en-US')}`

export const usd2 = (v: number): string => `$${v.toFixed(2)}`

/** Currency for LEVELS (equity, balances): minus preserved when negative,
 * no forced positive sign (that is P&L semantics - usdSigned). */
export const usdLevel = (v: number): string =>
  `${v < 0 ? '-' : ''}$${Math.abs(Math.round(v)).toLocaleString('en-US')}`

export const num2 = (v: number): string => v.toFixed(2)

/** Compact count for volume scale: 1.2M · 940K · 1,234. Trailing ".0" is
 * dropped so round millions read "2M", and only 5-digit-and-up counts
 * compact to K (a 4-digit count still reads fine with its comma). */
export const compactCount = (n: number): string => {
  const sign = n < 0 ? '-' : ''
  const abs = Math.abs(n)
  if (abs >= 1_000_000) {
    const m = (abs / 1_000_000).toFixed(1)
    return `${sign}${m.endsWith('.0') ? m.slice(0, -2) : m}M`
  }
  if (abs >= 10_000) return `${sign}${Math.round(abs / 1_000)}K`
  return sign + abs.toLocaleString('en-US')
}

/** Elapsed age in the unit read at a glance: 42s · 8m · 4h 37m · 2d 2h.
 * The one formatter for every "… ago" / "… old" label. */
export function ago(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 90) return `${s}s`
  const m = Math.round(s / 60)
  if (m < 90) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`
  const d = Math.floor(h / 24)
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`
}

export function ageSeconds(iso: string | null, now: number = Date.now()): number | null {
  if (!iso) return null
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return null
  return Math.max(0, Math.round((now - t) / 1000))
}
