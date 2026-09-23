// Mirrors trex/discovery/shadow.py::shadow_key — "QQQ|20261016|642|657".
// Python formats strikes with {:g}; String(number) agrees for strike-sized
// values (642 -> "642", 642.5 -> "642.5").

export function scenarioKey(
  underlying: string,
  expiry: string,
  shortStrike: number,
  longStrike: number,
): string {
  const exp = expiry.replace(/-/g, '').slice(0, 8)
  return `${underlying}|${exp}|${String(shortStrike)}|${String(longStrike)}`
}
