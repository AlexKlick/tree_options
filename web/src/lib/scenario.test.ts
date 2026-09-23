import { describe, expect, it } from 'vitest'
import { scenarioKey } from './scenario'

describe('scenarioKey', () => {
  it('matches the python shadow_key format ({:g} strikes, yyyymmdd)', () => {
    expect(scenarioKey('QQQ', '20261016', 642, 657)).toBe('QQQ|20261016|642|657')
    expect(scenarioKey('SPY', '2026-11-20', 702.5, 712.5)).toBe('SPY|20261120|702.5|712.5')
  })
})
