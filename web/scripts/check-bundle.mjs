// Post-build guard: the family portal prefix-strips /trex/, so the SPA
// must stay ONE relative JS chunk (no dynamic-import splits resolving
// against the wrong base) and reference assets relatively.
import { readFileSync, readdirSync } from 'node:fs'

const out = new URL('../../src/tree_options/trex_web/static/', import.meta.url)
const html = readFileSync(new URL('index.html', out), 'utf8')
const scripts = [...html.matchAll(/<script[^>]*src="([^"]+)"/g)].map((m) => m[1])
const jsFiles = readdirSync(new URL('assets/', out)).filter((f) => f.endsWith('.js'))
const problems = []
if (scripts.length !== 1) problems.push(`expected 1 <script src>, found ${scripts.length}`)
if (jsFiles.length !== 1) problems.push(`expected 1 JS chunk, found ${jsFiles.join(', ')}`)
for (const src of scripts) if (!src.startsWith('./')) problems.push(`non-relative script src ${src}`)
if (problems.length) {
  console.error('bundle check FAILED:\n  ' + problems.join('\n  '))
  process.exit(1)
}
console.log(`bundle check ok: single relative chunk ${scripts[0]}`)
