# TREX user-control implementation, 2026-11-04

## Controlling product decisions

Owner paper, then owner manual live, then a private no-charge cohort (at most
three initial invitees). Broker accounts and paper/live environments get isolated
instances on the private host. Operational storage must not depend on the
unavailable RAID. Live and invited-user access use a dedicated private HTTPS
hostname with account-scoped sessions and passkey confirmation for activation,
risk increases and automation grants.

Optional automation covers whole shares and defined-risk options independently.
Authority lasts one session. Adaptive stops, targets and holding periods may
change only inside an outer envelope approved before entry, with risk reserved
against that full envelope. Expiry stops new decisions, not exit management.
Automatic paper research may run after operational acceptance and preregistration.
Automatic live trading requires separate strategy and execution qualification.
External portfolio context is opt-in per provider, minimized, and account scoped.

## Ordered gates

| Gate | Work and exit requirement | Current disposition |
| --- | --- | --- |
| P0 custody | Current unit/source/owner/state inventory, consistent backup, scratch restore, actual broker reconciliation and exit disposition | OPEN: local custody is inspectable; broker gateway is unavailable, so current account state and exits are unverified |
| P1 reliability | Local immutable release, evidence publication, alerts, backup restoration; no lost history disguised as empty state | Recovery candidate; actual activation needs exact-head qualification and effective-unit review |
| P2 supervised product | Qualify existing Wave 5 ideas/manual equity flow; complete policy editing, supported manual option tickets and unassisted phone/desktop lifecycle | Wave 5 source incorporated as the baseline; remaining product work not qualified |
| P3 live foundation | Separate instance identities, credentials, state, authentication and broker-held protection; capability verdict per instrument | OPEN; no live activation or new account authority |
| P4 owner paper acceptance | Ten consecutive qualifying NYSE sessions, ten completed lifecycles per kind in the streak and all seven drills | OPEN; repository tests do not count as observed broker sessions |
| P5 owner manual live | Explicit numerical capital/limits and restricted canary, then ten live sessions and ten lifecycles per supported kind | BLOCKED by P3/P4 and owner activation inputs |
| P6 invited users | Isolated OS/service identities, per-user/account access, consent, onboarding, exports and account-specific qualification | BLOCKED by earlier product gates; never grant invitees broad Family operator access |
| P7 automatic paper | Session mandates, selection/sizing and revision-bound adaptive amendments, resource enforcement and ten-session soak | BLOCKED by operational paper acceptance and frozen research registration |
| P8 automatic live | Frozen strategy/model/prompt/data and management qualification, owner restricted pilot, then per-invitee activation | BLOCKED; no automatic promotion |

P0–P6 are sequential. P7 can run after P4 alongside P5/P6. Research observations
start after recovery, with their existing owners, resource caps and registrations.
No phase inherits another phase's account or performance proof.

## Required interfaces and invariants

Retain the existing supervised owner, command queue, permits, execution ledger,
reconciliation and immutable evidence owners. Reads and commands must be bound to
the authenticated principal, configured instance and environment. All mutations
remain durable commands; client paths never choose a state root or broker account.
New live, policy, consent, session-mandate, decision-evidence and position-amendment
semantics need strict versioned contracts. Existing paper records remain readable
and must not be relabeled as live records.

Every review shows exact terms, quote age, costs, capital committed, planned risk,
stress reserve, actual capital at risk, evidence, approval expiry, protection and
adaptive bounds. A material edit invalidates the old approval. Policy changes
invalidate pending entry and automated authority. Reducing limits below current
exposure blocks additions without silently liquidating positions. Pause/disarm
retain exits; resume never revives expired or consumed authority.

Live protection must survive loss of the host and gateway. Reconcile parent,
partial-fill, stop, target, cancellation and closing orders. Do not cancel old
protection before a replacement's safe behavior is qualified. A timeout means
unknown until reconciled, never permission to resubmit. Broker stop behavior is
not a guaranteed loss ceiling. Unsupported options remain paper-only.

Models receive market data by default. Optional portfolio context excludes
identity, broker identifiers, credentials, exact cash and raw order history.
Consent revocation stops future egress and disqualifies in-flight results from
authorizing actions. Models never change their own permissions or promote a
strategy. New pilot work must have a program ID, admission reservations and full
request/time/output accounting. Preserve the initial 20 requests, 600 seconds and
64 MiB per program/day until an explicit budget revision.

## Research and acceptance

Preserve sealed QSL/forward rules, closed studies, corrections and virtual
allocations. Revise unsealed drafts under new identities before registration.
Qualify point-in-time inputs, calendar, entitlement, corporate actions and costs.
Use equal declared capital/risk, a deterministic baseline, fixed-management agent
and adaptive-management ablation. Count complete sessions, including no-trade
periods, separately from completed lifecycles and repeated replays.

For new studies require at least 60 qualified forward sessions and 20 complete
lifecycles per arm; a preregistered session-block comparison (five-session blocks,
10,000 bootstrap draws, fixed seed, Holm at 5%) must support the claimed advantage.
Adaptive management must beat the fixed-management counterpart. Include fees,
spreads, model usage, failures, missed opportunities and drawdown. The weekly
baseline needs at least roughly 20 weeks for 20 opportunities; do not shorten
sampling to meet a product date. Study failure is a valid outcome.

Qualification uses full local logs and exact source/tree/environment identity.
Retain the host RAM launcher and TREX gate lock. Run M0's complete test/mutation
registry, frontend checks and relevant portal checks once per unchanged candidate.
Test duplicate/expired/tampered commands, account swaps, partial fills, corrections,
late fees, outside positions, risk reservation races, broker protection races,
consent egress, disk-full/fsync failures, stale evidence and authority-free restore.
Authenticated browser use and observed broker sessions remain separate evidence.

No live amount is inferred from virtual funds. Initial instruments are SPY whole
shares and supported debit verticals; borrowing, short stock, naked/fractional
orders, after-hours execution, new brokers, public signup and billing are deferred.
Private hostname/TLS, broker login, numerical limits, off-host backup and monitoring
destinations, data rights and invited-user operating obligations are required
activation inputs. They are not fabricated by source configuration.
