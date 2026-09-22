// Shared chart helpers (pure, unit-tested).

/** CSS transform keeping a hover tooltip inside the chart: right of the
 * crosshair near the start, flipped left once past the flip threshold. */
export const tipTransform = (cssX: number, cssW: number, threshold = 0.66): string =>
  cssX > cssW * threshold ? 'translateX(calc(-100% - 12px))' : 'translateX(12px)'
