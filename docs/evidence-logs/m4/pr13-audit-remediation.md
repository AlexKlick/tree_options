# PR #13 audit remediation — exact-head source evidence

Date: 2026-08-24

Audit input: `tree_options_pr13_audit_and_next_issues-1d5a47.md`

- Audited PR head: `f95b99a3321ac1ccd78440605de9c14b12b21c20`
- Tested remediation source head: `314a5f4920340f3e16482248f2bd2a8a0860e52e`
- Tested source tree: `a10828d2ccb6aae7bc82776ced658aeb3b7bf635`
- Branch: `m4/durable-closeout-preflight-20260823`

This is a source, test, mutation, build, and clean-worktree verification record.
It is not an approval of PR #13 by the original auditor and it is not evidence
of a vendor pull, a bars capture, a model run, a G4 approval or consumption, a
browser session, a hosted check, a merge, or a deployment.

## Remediation mapped to the audit blockers

1. **Canonical run identity.** `RunIdentityCore` defines the logical identity;
   creation recomputes and validates `run_id` before any store mutation, and
   open/rebind paths validate stored identity again. Provider, capture version,
   and an explicit logical start date or run nonce participate; process
   liveness fields do not.
2. **Checkout-independent coverage identity.** The coverage-universe generator
   records a validated repository-relative logical source identifier and hashes
   the source bytes. Absolute checkout roots no longer participate in emitted
   identity.
3. **Run-state no-follow custody.** The complete store uses a shared
   component-wise lexical directory walk and dir-fd-relative final-name access.
   Regular-file/link-count, inode, directory identity, byte, and atomic-publish
   checks cover the primary state, journal, current pointer, lease owner and
   adoption lock, and heartbeat.
4. **Typed, held-input G4 authority.** `VerifiedSealedInputs` is an immutable,
   self-hashed packet built from held bytes. The preflight uses the Cboe and
   Massive typed verifiers, binds each referenced payload set, validates the
   typed calendar decision and criteria/source relationship, and joins the
   approval, consumption, current checkout/protocol, runner version, and packet
   again at the effect boundary. The runner receives the held inputs rather
   than reopening paths.

The mutation registry adds M244–M267 for these G4 checks. The full registry is
255 mutants at the tested head.

## Exact-head gate

The command was run once at the tested source head with full output captured:

```text
bash scripts/m0_gate.sh > /tmp/pr13-exact-head-gate-r3.log 2>&1
```

Observed results from that log:

- Head at start and finish:
  `314a5f4920340f3e16482248f2bd2a8a0860e52e`.
- Ruff format: 211 files already formatted.
- Ruff lint: all checks passed.
- Mypy: no issues in 110 source files.
- Pytest: passed 1,585; failed 0; skipped 0; flaky 0; 101.13 seconds.
- Mutation: KILLED 255; SURVIVED 0; INVALID 0; TIMEOUT 0;
  MUTATION_DRIFT 0; HARNESS_ERROR 0.
- Post-mutation restoration full-suite pass: true.
- Source distribution and wheel built successfully.
- Fresh-environment wheel smoke: `tree_options 0.1.0`, protocol `0.2.0`.
- The gate's exit trap confirmed the head was unchanged and the tracked tree
  was clean beyond generated `artifacts/` and `dist/` outputs.
- Exit status: 0.

The full-capture log is 46,880 bytes with SHA-256
`e90e9d5c1696b371c365e4c25518ff79e8a47d62d442e79a46c4c7e370622479`.

Two earlier attempts are non-passing evidence and are not promoted: the first
stopped at the formatting check; the second completed 1,584 tests but the
mutation harness failed before running mutants because its disposable copy
followed adversarial generated-artifact links. Commit `314a5f4` excludes only
generated `artifacts/` and `dist/` trees from that disposable copy and adds an
owning regression test. The final result above is from the post-fix head.

## Clean detached-worktree reproduction

A new detached worktree at the same source head was created outside `/tmp`, a
fresh `.venv` was synchronized with `uv sync --frozen`, and the complete test
suite was captured once in `/tmp/pr13-clean-worktree.log`.

- Status before setup: clean (`0` non-output paths).
- Pytest: passed 1,585; failed 0; skipped 0; flaky 0; 106.59 seconds.
- Head after the suite: unchanged at the tested source head.
- Status after the suite: clean (`0` non-output paths).
- Worktree removed after capture.
- Exit status: 0.

The full-capture log is 3,021 bytes with SHA-256
`1fc69cd17653334e1de86e2b289f8404d54d347e3854f8e0ffece5a0c2d3f1bf`.

## Generated output identities

These generated files are intentionally outside Git. Their identities bind the
host-retained outputs to this record:

| output | bytes | SHA-256 |
|---|---:|---|
| `artifacts/m0-mutations.json` | 131,259 | `01007df9330bf3e6401f014451bb229aa0086a1c42af5eb1ab3ef02fc6f796ab` |
| `artifacts/m0-mutations.md` | 44,143 | `34e01d30f10342a8b34ba7309049d58804f8562a1eba8be69c2feef5c43e9f66` |
| `dist/tree_options-0.1.0.tar.gz` | 986,075 | `96477f9a86c884e8568c5da28296629521b627e3268f3d359f0cdef5ca00f5c3` |
| `dist/tree_options-0.1.0-py3-none-any.whl` | 301,907 | `1fea0503d8cf9c9a78857197eab2965fdf49b914850737e4031dadc01ef5531a` |

## Authority boundary

No G4 authority ledger and no bars-authority ledger existed after validation.
The tests used fixtures and refusal paths; they did not create a real approval
or consumption record. Protocol version remains `0.2.0`. The owner decision on
protocol `0.2.1`, real capture/request work, actual G4 execution, final holdout,
remote publication, merge, and deployment remain separate, unperformed lanes.

This evidence document is a documentation-only commit after the tested source
head. Reproduce source validation from `314a5f4`, not from the later evidence
commit, unless a fresh gate is intentionally authorized.
