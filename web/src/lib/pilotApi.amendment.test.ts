import { describe, expect, it } from 'vitest'
import { buildCommandBody } from './contracts'
import { sealCommandBody } from './pilotApi'

// Lands with the seal_registration amendment (docs/product/pilot-seal-command.patch):
// the page's own builder and the contract's builder produce the same body.
describe('seal_registration in trex-command/1', () => {
  it('builds the same body through buildCommandBody', () => {
    const key = '5f0c2a8e-1b3d-4c6e-9a7b-2d4e6f8a0b1c'
    const at = new Date('2026-10-22T14:00:00.250Z')
    const args = { program_id: 'eq-baseline-spy-weekly', registration_sha256: 'a'.repeat(64) }
    expect(buildCommandBody('seal_registration', args, key, at)).toEqual(
      sealCommandBody(args.program_id, args.registration_sha256, key, at),
    )
  })
})
