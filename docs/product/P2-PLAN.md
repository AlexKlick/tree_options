# P2: complete the supervised product (plan, 2026-11-04)

Roadmap row: [CONTROL-ROADMAP-20261104.md](CONTROL-ROADMAP-20261104.md), P2.
Exit gate: a user can prepare, review, approve, follow and close a paper trade
from phone or desktop without terminal assistance, and every refusal gives an
understandable reason and a next action.

Source: a gpt-6.1-sol design pass over 7ffcce6
(`~/.local/state/trex-implementation-20261104/review/codex-p2-design.md`),
spot-checked against the code and adjusted where noted. Paper only. P3 owns
live, invitees, passkeys and the dedicated origin.

## Where the journey stands (7ffcce6)

| Step | State |
| --- | --- |
| Open the cockpit, five pages, phone navigation | done |
| Readiness and gateway login | partial: Today has five checks; the screen password still needs a terminal |
| Confirm account and environment | missing: Settings shows account ids, nothing confirms configured against observed |
| Inspect and edit policy | missing: Settings is read-only by design ("the owner changes these on the host") |
| Prepare from an idea; manual SPY ticket | done (Wave 5); approval now re-checks exit marketability |
| Manual debit-vertical ticket | missing: manual tickets are equity-only |
| Review exact terms | partial: risk collapses to "max loss" and "notional"; the equity max loss is the larger of the planned stop loss and the gap reserve, not a ceiling |
| Approve or reject | done |
| Follow entry, fills, protection | partial: contract rows and command history, no per-trade lifecycle |
| Close | close-all (`flatten`) only; no close of one selected trade |
| Know it is finished | partial: acceptance distinguishes flat, reconciled and commissions, but no trade view shows it |

## Lanes

**A. Setup, identity and policy.**
- Server-owned instance view: configured paper account against the observed account, with observation age.
- Versioned capital policy (`trex-capital-policy/1`) edited through `update_policy`:
  - compare-and-swap on the reviewed revision;
  - a change invalidates pending entry authority;
  - limits lowered below current exposure block additions and never liquidate.
- `check_command_path` proves a phone command reached the owner.
- Mutants P2A-M*.

**B. Qualify Wave 5 and add a manual debit vertical.**
- `ManualDebitVerticalTicket`: one package, derived legs, debit limit, and exits in basis points before expiry.
- It produces the existing supervised effect and keeps the canary screening, the flat-book rule and the session cap.
- Review `/2` separates capital committed, planned stop loss, stress reserve, capital at risk and costs.
- An edit is a new proposal that atomically retires the unsent predecessor.
- Deferred item (a) lands here: quote-independent refusals move before any broker preparation.

**C. Follow a trade and close it.**
- `trex-trade-view/1` links proposal, order, fills, exit, broker-flat, reconciliation and fees.
- Selected close (`close_position`) closes the entire remaining position. It is guarded by ownership, cancel-confirm and no resubmission after an unknown outcome.
- `owner_close` counts toward acceptance only with its bound command and complete lifecycle evidence.
- A structured refusal action (reason, next step, retry policy) applies on every surface.

**D. Recovery handback and integrated acceptance.**
- Normal-mode release with exact SSD write grants (replacing the RAID path in `96-narrow-writes.conf`).
- Drop `AF_UNIX` from the web in both modes. The private `/tmp` stays as ephemeral scratch.
- Recovery suppresses the mutation controls in the UI too.
- Three separate receipts:
  1. offline (fakes, FakeHost, browser at 390x844 and 1280x800);
  2. host handback;
  3. live paper product acceptance: a SPY lifecycle and a vertical lifecycle from phone and desktop, at least two sessions under the three-entry cap.
- Deferred item (b), restored pending intents: they stay as they are, because they are inert without permits. A no-send regression test pins that.

**Order.** A's interfaces freeze first. After A, B and C proceed in separate worktrees. D's offline work runs alongside. Integrate B, then C, then D, and qualify one frozen head: the full M0, the web check and the browser at both widths. Only one integrator edits the shared files: `contracts/{commands,proposals,vocabulary}.py`, `web/src/lib/contracts.ts`, `scripts/mutate.py` and `desk_entries.py`.

## Adjustments to the design pass (review)

1. **Keep the command `/2` instance binding out of P2.** The design binds `instance_id`, environment and account into every command (`trex-command/2`) now. That rewrites about 20 consumers, for a P2 that has one paper instance. P2 ships the read-only instance view and policy. Command context binding moves to P3, where several instances first exist. (Owner decision 6.)
2. **Selected close is lower priority than the trade view.** The canary's same-underlying flat-book rule keeps at most one option structure per underlying, so close-all already closes "the trade" in most paper states. Lane C's lifecycle view and refusal actions are the exit-gate work. Selected close can follow in the same lane once the view exists.

## Owner decisions

1. Account selection: **a) one configured paper instance with explicit identity confirmation** (recommended) · b) several provisioned paper instances.
2. Starting policy: **a) import the verified paper profile, show every value, owner confirms** (recommended) · b) enter a new policy in the cockpit.
3. Selected-position close in P2: **a) close the whole remaining trade, keep close-all** (recommended) · b) close-all only.
4. Broker reads before a quote-dependent refusal: **a) yes, after pure validation, bounded and stated** (recommended) · b) require an already fresh quote.
5. Do owner closes count toward paper acceptance: **a) yes, as `owner_close` with complete evidence** (recommended) · b) shown, not counted.
6. Command instance context: **a) P3** (recommended, see adjustment 1) · b) P2 as designed.
