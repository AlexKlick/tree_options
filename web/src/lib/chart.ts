// Shared chart helpers (pure, unit-tested).

/** CSS transform keeping a hover tooltip inside the chart: right of the
 * crosshair near the start, flipped left once past the flip threshold. */
export const tipTransform = (cssX: number, cssW: number, threshold = 0.66): string =>
  cssX > cssW * threshold ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)'

/** Estimated rendered width of a tabular-nums label (0.6em per glyph). */
export const labelWidth = (text: string, px = 12): number => text.length * px * 0.6

export interface ChartGeometry {
  vw: number
  vh: number
  padL: number
  padR: number
  padT: number
  padB: number
}

/** Time-series layout in CSS pixels: the viewBox equals the measured width,
 * so 12px labels render at 12px on a phone, and the left gutter fits the
 * widest y label. Height tracks width loosely, clamped to 200..320px. */
export const chartGeometry = (width: number, yLabels: string[]): ChartGeometry => {
  const vw = Math.max(240, Math.round(width))
  const widest = Math.max(0, ...yLabels.map((l) => labelWidth(l)))
  return {
    vw,
    vh: Math.round(Math.min(320, Math.max(200, vw * 0.3))),
    padL: Math.ceil(widest + 12),
    padR: 14,
    padT: 16,
    padB: 26,
  }
}

/** Last-point label: right of the dot when it fits, else flipped inside
 * (end-anchored, left of the dot) so it is never clipped at the edge. */
export const endLabel = (
  dotX: number,
  text: string,
  vw: number,
  px = 12.5,
): { x: number; anchor: 'start' | 'end' } =>
  dotX + 8 + labelWidth(text, px) <= vw - 2
    ? { x: dotX + 8, anchor: 'start' }
    : { x: dotX - 8, anchor: 'end' }
