# TREX governed adaptive action model

**Version:** 0.1 — September 26, 2026, America/Denver  
**Status:** proposed architecture and integration contract, with a schema-checked synthetic plan example. Not a TREX release, an authority grant, a trading policy, or evidence of investment performance.  
**Purpose:** extend the existing TREX/ATKO master plan with explicit provenance, governance, action planning, durable execution, bounded adaptation, review and linked plotting.

## 1. Basis and boundaries

The supplied master plan and its 34-item backlog remain the overall program. This document refines their CORE, AGENT, PAPER, TEST and GUI work; it does not replace the financial engines, desk evidence database, execution owner, microphone owner or existing application shell. ATKO's primitives—object, relation, lens, instrument, proposal, change, commitment and trace—remain the interaction vocabulary. [P1–P3]

A fresh connected GitHub read returned main at `c4035f6b30f5a177c0a9b4334b682589c9d62377`, tree `7596ecefc3d436569149b615595eb6952b2cfc33`. Its merge record adds RL-3a forecast evaluation. The forecast package explicitly says calibration is **not claimed** in this version. The inspected desk entry path still refuses non-shadow execution with `execution_not_implemented`. Source landing is verified here; the recorded host gates, deployed modules, forecasts, paper account and operating receipts were not independently tested. The older master plan's statement that all forecast machinery is absent is therefore superseded only to this extent. [R1–R3]

The current request is for a system model. It does not authorize broker queries, orders, cancellations, service restarts, policy activation or protected-data access. SP-3 off-host anchoring and SP-5 supervised legacy/account ownership handoff keep their existing scope. A later implementation must inspect their actual status rather than infer it from this document. [P1]

All components and operations specified below are **proposals**, except where explicitly identified as inspected existing sources. The example plan contains no market data, actual strategy performance, current account facts, active mandate or actual execution receipts. Its policy references and operation registry are fixtures, not trustworthy runtime credentials.

## 2. The unit of work is a governed decision episode

An episode answers a question or performs a bounded transformation. Examples: compare a baseline with a variant; investigate a drawdown; execute one authorized paper decision cycle; resolve an uncertain submission; evaluate whether a model change deserves a prospective experiment.

Each episode should answer six questions from durable records:

1. What goal and exact versions were being worked on?
2. What information was actually available and used?
3. What plan and alternatives were proposed?
4. What was permitted, by whom, and under which constraints?
5. What actions and external outcomes actually occurred?
6. What result, uncertainty and next change are supported by that record?

The central loop is:

`observe → form a supported interpretation → propose a plan → validate/review → authorize scope → execute permitted actions → reconcile → measure → review → propose the next version`

The system may automate many transitions inside a delegated envelope. The transition between a proposal and consequential authority is explicit. Learning can improve action selection and planning; it cannot rewrite history or manufacture permission.

## 3. One linked domain model, five graph projections

Do not create a single untyped graph where every arrow means “related,” and do not require five separate databases. Keep typed entities and relations, then expose these projections:

| Projection | Meaning | Example relations | Excludes |
|---|---|---|---|
| Evidence/provenance graph | Where inputs, claims and outputs came from | `used`, `generated`, `derived_from`, `supports`, `contradicts`, `corrects` | An automatic claim that a source is true or an effect authorized |
| Plan/action graph | Proposed work and its prerequisites | `requires_output`, `requires_outcome`, `branches_on`, `implements_goal` | Claims that proposed steps actually ran |
| Execution graph | Attempts, effects, observations and reconciliation | `attempt_of`, `submitted_as`, `observed_as`, `reconciled_by`, `superseded_by_correction` | Equating an accepted request with a completed external effect |
| Governance graph | Responsibility, policy, grants and decisions | `requested_by`, `reviewed_by`, `authorized_by`, `bounded_by`, `revoked_by` | Permission inferred from a favorable backtest or agent rationale |
| Capability/topology graph | Which service can perform an operation under current constraints | `implements`, `runs_on`, `requires_data`, `requires_resource`, `has_contract` | Confusing tool availability with authorization to use it |

Use W3C PROV's Entity/Activity/Agent vocabulary for interchange of the provenance portion. Its past-tense relations describe recorded provenance. Planned steps need a distinct plan vocabulary; do not emit `wasGeneratedBy` for an output that has only been proposed. A record that one actor acted on behalf of another is not itself an enforceable runtime grant. [W1]

A `supports` edge should identify the particular claim, source scope and support type. It is not logical entailment or economic causality. A `depends_on` edge is a scheduling dependency, not a discovered causal effect in the market. Conflicting sources remain independently inspectable.

### Graph execution semantics

A concrete plan revision has an acyclic scheduling dependency graph. Repeated trading cycles, retries and monitoring are explicit bounded submachines or new child-run instances, not a scheduler back-edge that reruns a previously sent order. A new learning round creates a new plan/strategy revision with a parent reference. The broader relation graph can contain non-scheduling links and need not be globally acyclic.

Dependencies specify outcomes, not just “the process stopped.” For example, a test dependency can require `passed`; a mandate dependency requires `approved`; a reconciliation branch can accept `submission_uncertain`. `blocked`, `failed`, `declined`, `no_op` and `not_evaluable` are not interchangeable success states.

Fork/join is explicit. Baseline and challenger can run concurrently against one immutable snapshot, while the comparison waits for their result artifacts. A review can conclude that evidence is inconclusive and still validly complete its assigned review; that does not satisfy a scientific promotion criterion.

## 4. Persistent objects and identity

| Object | Owns |
|---|---|
| Goal / WorkspaceContext | The user's question, finalized source, selected objects/revisions and visible operation |
| Claim / Hypothesis | A scoped assertion, supporting and conflicting sources, uncertainty and interpretation type |
| DataSnapshot | Immutable data identity, temporal coverage, source provenance, field completeness and access classification |
| StrategyVersion / ModelVersion | Code, parameters, prompt/tool/model identities where applicable, and supported domain |
| ExperimentSpec | Hypothesis, estimand, opportunity population, controls, training/evaluation rules, stopping rules and budgets |
| PlanRevision | Proposed typed steps, dependencies, input bindings, recovery policy and required commitments |
| PlanRun / ActionAttempt | A particular execution of the plan and each retry/attempt, with input/output receipts |
| PaperMandate | Authorized account, strategies, parameter envelope, operations, risk limits, expiry and revocation policy |
| GovernanceDecision / EffectPermit | A decision about a specific action under current policy and, if allowed, a narrowly bound effect permission |
| OrderIntent / RiskReservation | What the runtime intends to submit and the capital/risk already reserved for it |
| BrokerObservation / Reconciliation | Raw external evidence, corrections and the account state derived from it |
| ResearchResult / MetricObservation | Immutable calculation result, monetary basis, denominators, uncertainty and missingness |
| ReviewPacket / AdaptationProposal | Findings about a version and an explicit proposed successor/change |
| ChartSpec / WorkspaceRevision | A view bound to exact result identities, independent of financial or authority mutation |

### Different IDs solve different problems

Keep `object_id`, `revision`, `content_sha256`, `request_id`, `plan_run_id`, `node_id`, `attempt_id`, `logical_effect_id`, broker order/execution IDs, `decision_id` and `stream_sequence` separate.

An object ID remains recognizable across versions. A content hash identifies bytes. A request ID deduplicates a caller's request within its owner/scope. A run identity binds the actual resolved dependencies. An attempt distinguishes repeated work. A logical effect ID identifies the same intended external action across retry/reconciliation; it must never be reused for changed order semantics.

A computational run key should cover canonical spec, resolved data and strategy hashes, code/release and dependency identities, calendar/valuation/cost versions, model/provider configuration, and seeds when relevant. Dates and monetary decimals need canonical wire forms. Presentation choices such as a chart color must not alter the economic run identity.

The same inputs do not guarantee a hosted LLM produces identical output on a rerun. Preserve the actual admitted model output and tool interactions. Distinguish logged-output replay, deterministic numerical replay, and a new model-generation attempt. None is a license to repeat an external trade.

### Clocks and knowledge

Store effective/market time, source publication or availability time, locally received/recorded time, decision cutoff and observed service sequence separately. A historical reconstruction based on public availability is different from proof that this process actually possessed that information then. Both can be useful; their evidence labels differ.

Use producer epochs and ordered event IDs, not wall-clock time alone, for local ordering. A composite risk/read snapshot should bind its account, quotes, policy and instrument components with coverage/freshness limits. It must not claim an atomic world-state observation if those sources were sampled independently.

## 5. The action node contract

An action node is a typed command proposal, not free-form instructions to a broadly privileged agent.

Required fields include operation and contract version; owning service role; target environment; exact object/input bindings; prerequisites and acceptable outcomes; effect class; required authority and freshness/revision guards; bounded resource and risk budgets; timeout/retry/recovery behavior; declared output types; evidence receipts; and an observable postcondition.

The trusted server operation registry determines effect class and ownership. A model cannot label `paper.submit` as `read`, choose a less privileged label, supply `approved=true`, or rename a command to avoid enforcement. Progressive tool discovery exposes only applicable contracts, but discovery itself is not the security boundary.

### Effect classes

- **Read:** authorized inspection of already accessible records.
- **Research write:** bounded local artifacts, drafts, analysis or approved tests.
- **Protected disclosure:** accessing an unopened evaluation set, sensitive source or external export. A filesystem read may have irreversible scientific/privacy consequences.
- **Authority change:** grant, revise, revoke or migrate a mandate; separate accountable owner.
- **Execution-state change:** reserve risk, claim a managed position or record an execution decision.
- **Paper external effect:** submit, replace, cancel, exercise or another broker-account mutation; separately supported operation contracts.

The bundled JSON Schema models the subset used by its example; protected disclosure remains part of the full implementation contract and is deliberately absent from the sample workflow. Do not infer permission from the absence of that node type in the example.

### Plan compilation and validation

Resolve natural language to a draft using selected object revisions. Then resolve registered operations and bind inputs; type-check the graph and dependencies; verify actual data capability; resolve costs and resources; identify required grants; and compile a revision-bound plan summary.

Reject cycles in the scheduling graph, missing inputs, unsupported operations, mismatched evidence scopes and unspecified consequential parameters. Do not silently fill an unknown monetary value with zero. Missing policy thresholds are blockers for the relevant effect, not invitations for the agent to invent them.

An operator may inspect and revise the draft graph. Moving its nodes visually changes only layout. Changing a parameter, dependency or target creates a semantic revision with an impact diff. The graph editor never writes directly to the broker runtime.

## 6. Governance is an executable boundary, not a badge

Use two levels of authority:

**A mandate** delegates an explicit scope for a bounded period: paper account/environment, principals, strategy/algorithm versions, instrument universe, configurable parameter space, capital/risk/order-rate constraints, protective policy, permitted changes, and expiry/revocation semantics.

**An effect permit** binds one current command to that mandate and to current runtime state. It includes logical effect/intent digest, mandate and policy revisions, owner epoch, account revision, reservation, relevant quote/contract snapshots, issue/expiry time, decision reasons and single-use state.

Routine in-scope actions need not ask the human again. A mandate may permit a registered adaptive selector or parameter rule within an approved envelope. That does not authorize unbounded new strategies, new instruments, increased risk, changing the validation rule or switching accounts.

A policy service should return `allow`, `deny`, `needs_review`, or `indeterminate`, with reason codes and obligations. A reviewed plan is not a permanent order permit. Re-evaluate dynamic preconditions at dispatch.

An illustrative necessary permission predicate is:

`active grant AND matching account/environment AND allowed operation AND bound strategy/intent revision AND current exclusive owner AND sufficiently fresh data/account state AND feasible atomic reservation AND unrevoked policy epoch AND satisfied required reviews`

It is not sufficient as a textual expression. The implementation must enforce it at the component that possesses broker send capability. OPA decision logs demonstrate a useful record shape—policy decision, input and decision identity, with explicit sensitive-field masking—but adopting OPA is optional. Ordinary telemetry that can drop events is not the authoritative effect ledger. [W2]

### Revocation and existing positions

Before dispatch, revocation prevents new exposure. After submission, revocation cannot make a fill disappear. It initiates only the cancellation/reduction behavior already authorized by the mandate and records any remaining uncertain exposure.

Mandate expiry must say what happens to open positions and working orders. Stop admission without abandoning protection. A surviving protective mandate or explicit handoff is required; do not keep risk controls alive by informally extending an expired trading grant.

A new strategy version does not automatically become the exit owner for positions opened by the old version. Position-level strategy/exit-policy attribution remains stable unless a governed migration is approved. Switching the champion pointer is not a portfolio migration.

## 7. Adaptive behavior at three speeds

| Level | Examples | Default control |
|---|---|---|
| Fast operational adaptation | Skip on stale quotes; retry idempotent reads; route eligible computation to available hardware; reprice within an approved rule; tighten admission | Deterministic rules and preauthorized bounds, with recorded triggers; no new authority |
| Bounded policy adaptation | Choose among approved strategy versions; change allowed allocations or model state under a registered algorithm | Only inside a specifically approved AdaptationPolicy and shared risk budget; otherwise a proposal |
| Slow strategy/model improvement | New features, prompts, models, exit rules, training methods or expanded universe | New version and evaluation, review, then a separate scope decision; no silent hot patch |

Presentation adaptation is a separate dimension. The app may offer a relevant chart or inspector; that must not increase work/effect permissions or move a control under the pointer. [P3]

A useful formal model is:

`I_t = admissible information available by decision t`

`proposed_action_t = policy(strategy_version_t, model_state_t, I_t, portfolio_state_t)`

`permitted_actions_t = current_governance_and_risk_envelope(...)`

`executed_action_t = validated proposal if permitted, otherwise explicit no-action/blocked outcome`

`candidate_version_(k+1) = learner(frozen matured evaluation records through cutoff k)`

The learner may create a new version. Promotion is a different event governed by the AdaptationPolicy and current authority. A drift detector is a diagnostic with possible false alarms and uncertainty, not proof that the latest alternative is better.

### AdaptationPolicy requirements

Declare the adjustable parameters and limits; available actions including no-trade; learning/selection algorithm; model-state versioning; allowed input vintages; label maturity; update cadence; turnover and change-rate constraints; cooldown/hysteresis rules; minimum evidence and uncertainty method; failure/freeze conditions; fallback policy; and authority required for a change.

Do not install invented global numeric risk limits through this document. Each paper experiment must supply its reviewed thresholds, units, scope and handling of unknown values. The initial default for new strategy revisions is proposal-only. A later mandate can explicitly delegate specified changes after their behavior has been evaluated.

### Evaluate the adaptive process itself

Do not retrospectively splice the winning strategy from each period into a purported historical adaptive track record. Replay the selector, model-state updates, information availability, label delay, capital allocation and switching costs as they would have occurred. Keep a frozen baseline and retain every attempted variant, failed fit, blocked opportunity and excluded observation.

The study protocol must account for overlapping observations, search multiplicity and repeated inspection. Use a declared dependence-aware and sequential-evaluation design where required; changing thresholds after inspecting outcomes is an amendment, not original preregistration. No fixed observation count automatically establishes statistical support.

Define utility beyond gross P&L: net returns, downside/exposure, turnover, execution quality, incomplete valuations, operational errors and research cost. Do not reward the system for generating more trades, passing more gates, persuading the user or suppressing adverse evidence.

A model's probability estimate requires its own target/horizon/evaluation evidence. A language model's verbal confidence is not a portfolio probability. The current forecast package's `calibration_status=not_claimed` must survive its incorporation into the graph and GUI. [R2]

## 8. Concrete plan: comparison to prospective paper learning

`examples/research-to-paper.plan.json` contains an 18-node proposal, all unexecuted:

| Node | Proposed action | Owner |
|---|---|---|
| N01 | Resolve pinned authorized input snapshot | Data service |
| N02 | Check coverage, economic capability and timing | Data service |
| N03 | Replay frozen baseline | Research worker |
| N04 | Replay challenger on the same supported inputs | Research worker |
| N05 | Build paired comparison | Research worker |
| N06 | Publish comparison ChartSpec | Projection service |
| N07 | Run deterministic broker-fault suite | Test worker |
| N08 | Review methodology and evidence limits | Method reviewer |
| N09 | Assemble prospective paper-test readiness | Review service |
| N10 | Propose bounded paper mandate | Planning/learning agent |
| N11 | Approve or decline exact mandate | Accountable operator |
| N12 | Instantiate one authorized decision cycle | Campaign owner |
| N13 | Recheck scope and reserve current risk | Risk service |
| N14 | Submit through exclusive paper gateway | Broker gateway |
| N15 | Reconcile observation or uncertain submission | Reconciler |
| N16 | Publish observed paper-result projection | Projection service |
| N17 | Review expected versus observed outcomes | Review service |
| N18 | Propose a new version without activation | Learning agent |

N03 and N04 can run in parallel; N06 does not wait for paper authorization. N09 can accept an operationally valid *exploratory* experiment even when economic advantage is unproven, but must retain that scientific disposition. Declining N11 stops the paper branch without deleting the research results.

N14 does not become complete because a socket write returned. An ambiguous send leads to N15, preserving the reservation. A no-signal decision cycle is separately recorded as no action; missing data is blocked, not no-signal. A real campaign instantiates new bounded cycles with new logical effects and reconciles each. It never repeatedly traverses N14 with an old effect ID.

The example is an ontology/scheduling fixture, not an instruction to run any of these actions. It supplies no broker configuration or active grant.

## 9. Durable execution and exactly what a receipt means

Keep three concerns distinct:

**Plan revision:** immutable proposed work.  
**Run/attempt:** which work began, stopped, succeeded, failed or needs reconciliation.  
**Effect/account state:** what may have happened outside the process and what is currently known about exposure.

Persist intent, applicable decision, reservation and an outbox record in the execution owner's local transaction before sending. The outbox enables recovery; it does not make broker submission atomic with SQLite. A dispatcher may send only after obtaining current permits and fences. Unknown outcomes retain possible exposure and lead to reconciliation rather than automatic resubmission.

A logical effect transitions through `prepared`, `submission_attempted`, `acknowledged` or `submission_uncertain`, partial/terminal observations, and reconciliation. Cancellation has its own request and observation states. `cancel_requested` is not `cancelled`; `filled` is not necessarily fees-final or account-reconciled. Define quantity reconciliation, price coverage and commission completeness independently.

Store account, client, order, permanent order and execution identifiers as separate typed fields. IBKR documents distinct partial execution IDs and execution-correction relationships; a local logical effect therefore needs mappings to the relevant external identities rather than treating one numeric order ID as the entire history. [W3]

Retries of pure calculation can reuse immutable outputs by complete execution identity. Idempotent broker reads can retry with pacing. Non-idempotent or ambiguously acknowledged effects require reconciliation. Changed command payload creates a distinct logical effect and fresh permit; no same-key/different-content overwrite.

A filled order cannot be undone by rolling back the workflow database. A compensating order is a new financial effect with its own risk, costs and authorization. Restoring software to an older release also does not roll back broker reality.

### Ownership fencing must reach the send boundary

A database lease alone does not stop a paused old process that still owns a direct broker connection. The exclusive gateway must enforce current epochs/permits and prevent stale owners from bypassing it; no planner or ordinary research worker may retain broker send credentials. Exact credential/process/network isolation belongs in deployment acceptance.

Keep protective actions independent of the LLM, browser and research worker availability. Reserve resources and priority for reconciler/exit handling. Broker outages can prevent actual protection; record this as an operational limitation and provide a separate alert/operator path.

## 10. Provenance that remains useful after adaptation

Record actual inputs used, source hashes/locators, temporal availability, transformations, strategy/model/prompt/tool-contract versions, relevant parameters/seeds, model outputs admitted to the workflow, calculated results, review decisions and broker observations. Store a concise user-facing rationale and cited alternatives; auditability does not depend on obtaining or exposing hidden model chain-of-thought.

Treat news, documents, retrieved snippets, model outputs and remembered summaries as evidence or proposals—not instructions that can change the tool registry or authority. Protected evaluation data must be fenced from direct tools, retrieval indexes, prompts, embeddings and caches, not merely hidden in a UI tab.

A hash proves byte identity relative to an expected hash; it does not prove market truth, trustworthy authorship or economic merit. Authenticity and custody require trusted collection/identity, appropriate signatures or attestations, storage controls and external anchors under the chosen threat model. The graph should expose source disagreement, unknown origin and unverifiable retention rather than assigning a universal confidence score.

### Change-impact propagation

When a source, policy or code artifact changes, follow explicit dependency links to affected derived claims and pending decisions. Preserve original results and manifests. For current use, mark them superseded, stale or needing re-evaluation as appropriate. Do not erase an unfavorable historical outcome because a corrected dataset later arrives.

A later broker correction can produce a revised outcome and a revised plot, while leaving the original decision-time information snapshot intact. Retain both what was known at the time and the corrected account truth. A changed risk policy invalidates pending permits; a chart zoom does not invalidate a research run. Cached calculations may be reusable if all relevant inputs match, but expired execution permits never become reusable because their old plan still hashes identically.

Maintain immutable artifacts plus rebuildable read models. Store large datasets/quotes outside the graph with content-addressed blocks and row/offset references. Per-tick graph-node expansion is unnecessary; capture decision-relevant snapshots and exact source locators while retaining required underlying records.

## 11. Review as a first-class activity

A ReviewPacket binds a subject revision and records separate dispositions:

| Review axis | Evaluates |
|---|---|
| Engineering | Independent accounting oracles, schema conformance, replay, fault/recovery and UI/data consistency |
| Scientific | Estimand, sample/cohort, uncertainty, holdout access, search history, model and simulator limitations |
| Operational | Account/environment binding, exclusive ownership, freshness, reconciliation and protective capability |
| Authority | Whether the accountable principal actually delegated the specific scope |

A successful engineering suite cannot replace a scientific evaluation. Scientific promise cannot replace operational readiness. A completed review is not automatically an approval. Tests not run remain not run; statistical indeterminacy remains indeterminate.

Agents may draft reviews, identify counterexamples and cross-check calculations. Independent deterministic checks remain separate from prose review. Multiple agents repeating the same source or error are not independent evidence; role/model diversity is not a statistical sample-size multiplier.

Post-execution review should separate signal failure, allocation effects, execution-model mismatch, data failure, accounting revision and runtime defects. Numerical attribution needs a declared calculation; causal explanations require a supported identification design. “Why did this lose?” may yield accounting decomposition plus competing hypotheses, rather than an invented single cause.

Paper observations remain simulator observations. IBKR describes top-of-book fill simulation, limited combo trading and simulated complex orders. Good paper outcomes are useful for integration and prospective experimental evidence but do not establish equivalent real-market fills. [W4]

## 12. Plotting is a read model of the same graph

Every series and point binds to an immutable result and metric definition. It must carry evidence origin (synthetic, historical model, proxy, broker-paper), currency and capital basis, temporal convention, coverage/missingness and transform chain. Keep validation/study status independent of evidence origin and authorization.

A point locator should resolve `result_id + result_revision + series_id + row/metric locator`, then link to source rows and producing activities. A chart label or agent-written title cannot upgrade a proxy to an observed paper result.

| Plot/lens | Question | Additional bindings |
|---|---|---|
| Dollar NAV and flow-adjusted performance | What happened to wealth versus investment performance? | Account/ledger epoch, flows, price/fee coverage |
| Baseline difference and drawdown | Where did strategies diverge, and what decline was experienced? | Shared time/capital/cost basis and actual support |
| Plan versus actual timeline | Which actions were proposed, permitted, attempted and observed? | Node, attempt, decision, effect and broker receipt IDs |
| Execution diagnostics | Did fills/costs differ from the stated execution model? | Timestamped reference quotes, order sides, fill/fee coverage and assumptions |
| Adaptation history | When and why did the active policy/state change? | Effective strategy/model versions, approved envelope, trigger and review refs |
| Forecast evaluation | How did a declared model/horizon perform? | Forecast origin, target, interval semantics, cohort, metric version and calibration status |
| Evidence/dependency view | Which facts or permissions support this result/action? | Typed relations and scoped provenance; no implied market causality |

Use synchronized timelines and shared cursors. Independent gap compression across compared series must not misalign dates. Downsample for display only; calculate metrics on retained full-resolution observations and preserve gaps/extrema. Different evidence scopes can be overlaid with clear labels, never silently pooled into one win rate.

Forecast fans must label pointwise versus pathwise intervals, forecast target, measure and origin. Conditional shock paths without a probability model are not prediction intervals. Current RL-3 grid quantile scores must retain that exact metric name rather than be relabeled CRPS, and the package's unclaimed calibration state must stay visible. [R2]

For comparison or model changes, show a pinned parent plus difference. For executed effects, show requested/acknowledged/filled/reconciled distinctly; a dotted future path must never masquerade as a realized continuation.

## 13. ATKO cockpit integration

The graph is the shared semantic model, not an instruction to display all of it as a force-directed hairball. Follow ATKO's task-preserving abstraction. [P3]

**Persistent rail:** environment/account alias, active mandate/strategy epoch, entry state, owner/reconciliation freshness, critical data freshness and non-LLM intervention controls. Service-level `execution_enabled=false` does not mean the whole account has no other active owner.

**Focal workspace:** choose a chart, order/position, comparison, plan stage or experiment according to the user's question. Keep accepted focal objects and revisions stable.

**Situated plan view:** collapsed stages with the current action and blocked dependencies. Expand to see inputs, guards, owner, expected outputs, attempts and actual receipts. A Plan/Actual switch compares intended structure with observed execution without changing either.

**Inspector:** evidence, exact assumptions, review findings, proposed scope changes and actual authority decisions. Selecting a chart point or graph node opens the same identified object here. Keep unresolved exceptions visible even when routine progress is compressed.

**Interaction strip:** finalized voice/text and concise job state. Reuse existing microphone ownership. Voice and direct controls compile to the same command proposal; interim transcripts cannot authorize effects. Preserve the referent selected when the utterance was admitted, even if selection changes while an agent works.

Example episode:

- “Compare this version against the baseline.” The workspace binds both revisions and shows the supported result or exact data blockers.
- “Why did it do worse here?” Selecting an interval opens the relevant observations, calculation and competing explanations.
- “Try more conservative sizing.” The agent proposes an explicit allowed change; the baseline remains pinned. A missing numerical interpretation is resolved visibly, not silently applied to a broker account.
- “Run a prospective paper test within this mandate.” The compiler checks that the requested experiment fits an actual mandate, or presents the exact missing approval.
- “Pause new entries.” The deterministic control records the request and current effect state independently of the conversational agent.
- “Show what changed since yesterday.” The view compares accepted workspace, policy epochs, actual receipts and unresolved questions—not a rewritten summary.

Generated controls are declarative compositions of approved renderers and registered operations. Permissions remain outside generated content. A graph rearrangement, chart zoom or display preference never grants a domain mutation. On mobile use one focal representation and an inspector sheet, with keyboard/touch alternatives and no hover-, drag- or voice-only critical operation. [P3]

AG-UI provides snapshot and delta event forms that can transport the shared view state. Use a TREX adapter with authoritative producer, sequence, revision and account-scope checks; do not let arbitrary agent patches overwrite broker or policy-owned state. Reconnect establishes a consistent projection watermark and then a supported continuation. [W5]

## 14. Storage and implementation seams

Use existing stores as authoritative sources for their current domains. The graph index is a projection over research artifacts, governance decisions and execution receipts, not a second accounting ledger. Indexed relational entity/edge records plus content-addressed artifacts are an adequate initial representation; a graph database is not a prerequisite.

Proposed additions should be small adapters/modules around the current TREX packages:

| Seam | Integration contract |
|---|---|
| `research/` | Expose immutable comparison, scenario and forecast run/result manifests as typed graph artifacts; retain current numerical semantics |
| `desk/` | Read existing candidate/proxy evidence and explicit blockers; do not convert historic preview records into executable specs |
| Existing/future broker owner | Emit intent/decision/effect/observation receipts; accept only governed typed requests, never model-written book files |
| Proposed action-graph compiler/runner | Versioned plans, operation contracts, input binding, bounded dependency scheduling and recovery |
| Proposed governance service | Mandates, decision receipts, revocation epochs and effect-permit checks at the gateway |
| Existing web/ATKO integration | Graph and chart projections over shared IDs/revisions; scoped actions through the same server contracts |
| Host orchestration | Existing resource/quota admission, locked environments and local gate authority; independent protection/reconciliation priority |

Do not select an additional workflow engine, policy service or graph database merely to rename these boundaries. First prove the contracts using the current stack. Replace a component only against explicit requirements and compatibility/recovery evidence.

Use durable per-run scheduling with explicit resource leases. Do not make an interactive agent's lifetime or a session-local timer the owner of an experiment. If a worker is currently only single-process safe, enforce that deployment constraint until cross-process claiming/fencing is implemented.

Network/user-facing commands take typed IDs, not arbitrary server paths or shell strings. Authenticate every graph, artifact and stream scope. Sensitive payloads stay behind access-controlled references; retain necessary hashes/locators and masked decision metadata. General observability logs may be sampled; effect/authorization/ledger records may not silently disappear under sampling.

## 15. Incremental delivery mapped to the existing backlog

`integration-map.json` maps these proposed slices to the prior 34 task IDs; they are refinements, not claims of newly created GitHub issues.

| Slice | Deliverable | Non-negotiable acceptance |
|---|---|---|
| GRAPH-01 | Typed objects, action-plan compiler and revision model | Invalid bindings/effect types/revisions rejected; plan, run, attempt and effect distinct |
| GRAPH-02 | Provenance index and dependency impact | One nonempty result point resolves to correct immutable source/operation records; old results retained |
| GRAPH-03 | Mandate and permit boundary | Forged, expired, stale and wrong-account requests refused at actual effect egress |
| GRAPH-04 | Durable runner and broker fault seam | Crash/uncertain acknowledgment/restart/race tests; reservations and effects reconcile |
| GRAPH-05 | Adaptive policy evaluation and reviews | Frozen baseline, complete variant history, full selector replay and explicit version-change authority |
| GRAPH-06 | Linked plan/actual/chart/review workspace | Voice/direct edits share referents; permissions not inferred from presentation; reconnect preserves state |
| GRAPH-07 | Supervised paper cycle and repeated campaign | Explicit operator grant/ownership handoff and broker-to-ledger evidence; no waiver of SP-3/SP-5 |

The first implementation should be a complete **broker-free synthetic episode**: compile a comparison plan, execute a bounded replay, persist its outputs, review its limitations, inspect a linked plot and propose a successor. It must demonstrate successful nonempty behavior and honest negative controls.

Next prove the paper effect contract against deterministic broker faults, then perform a separately authorized supervised broker-paper cycle. Only then enable repeated bounded campaigns and, later, explicitly delegated adaptive selection. Existing real-data comparison obligations remain; a synthetic vertical slice cannot close them. Existing RL-3 work should be integrated and evaluated, not recreated as if absent.

## 16. Required system tests and return contract

The companion `acceptance-cases.json` contains 34 required **future system tests** across governance, execution, evidence, accounting, adaptation, GUI, review and recovery. They include revoked/stale authority, changed plan hashes, concurrent risk reservations, ambiguous sends, partial-fill/cancel races, terminal-save failures, old-order corrections, source tampering, future-data leakage, model-selection hindsight, retained position policy, out-of-order UI events and cold restoration against a changed broker account.

These are not the tests executed for this model packet. The packet's 16 local tests check only its own schema, fixture hashes, typed dependency bindings, scheduling acyclicity and declared necessary effect-boundary conditions. Passing them does not validate identity, authority, financial arithmetic, the broker runtime, current TREX code, deployment or profitability.

The implementing agent's return must bind exact source/release/dependency and data identities; list which existing components were reused; return machine-readable plan and actual execution graphs; demonstrate a nonempty result and point-level evidence path; show reviewed mandate/permit objects with secrets redacted; provide red-to-green failure tests and raw gate receipts; demonstrate keyboard/mobile/reconnect behavior; and identify remaining data/operational/operator blockers.

Report execution state, accounting completeness, scientific support and permission separately. Do not combine them into one completion percentage. No stage is accepted because the diagram looks complete or because an LLM calls its own work successful.

## 17. Sources and validation scope

### Project sources

- **P1:** `TREX-Integrated-Agentic-Paper-System-Master-Plan-20260926.md`, supplied in this conversation; especially §§2–9. Proposed integration allocation, not independently verified host state.
- **P2:** `TREX-Integrated-System-Backlog-20260926.json`, 34 dependency-linked planning tasks. This packet preserves its IDs in the integration map.
- **P3:** `ATKO-Shared-Workspace-Design-Theory(1).md`, design theory 0.1, September 26, 2026; especially §§3–8, 12, 14–15. A proposed theory/contract, not a released trading UI or empirical proof of usability.
- **R1:** connected GitHub `AlexKlick/tree_options/branches/main`, returned head `c4035f6b30f5a177c0a9b4334b682589c9d62377`, tree `7596ecefc3d436569149b615595eb6952b2cfc33`.
- **R2:** `src/tree_options/research/forecast/__init__.py` at that head, blob `f1343bd0bd23381664bdb4248625ff6473f36ba2`; forecast evaluation map, grid score naming and explicit non-claim of calibration.
- **R3:** `src/tree_options/desk/enter.py`, source lines 1–120 at that head, blob `4ad79340406f14c6a5f08bb12569dcaa8da1c865`; preview-only behavior and unresolved execution integration.

### External primary references

These support the specific distinctions cited, not the profitability of the proposal or regulatory certification.

- **W1:** W3C, PROV-DM: The PROV Data Model, Recommendation, April 30, 2013. `https://www.w3.org/TR/prov-dm/` — entities, activities, agents and provenance relations.
- **W2:** Open Policy Agent, Decision Logs. `https://www.openpolicyagent.org/docs/management-decision-logs` — decision/input records, sensitive-field masking and logging behavior. OPA is a reference pattern, not a selected mandatory dependency.
- **W3:** Interactive Brokers TWS API, The Execution Object. `https://www.interactivebrokers.com/docs/tws-api/doc/order-management/execution-details/the-execution-object` — execution identity and correction semantics.
- **W4:** Interactive Brokers Campus, Paper Trading Account. `https://www.interactivebrokers.com/campus/glossary-terms/paper-trading-account/` — simulator/partial-fill/order-type limitations.
- **W5:** AG-UI, State Management. `https://docs.ag-ui.com/concepts/state` — snapshots/deltas for synchronization, not a TREX authorization engine.

External pages were consulted for this design on September 26, 2026. No new TREX application tests, broker queries, remote writes, deployments, financial experiments, sealed-study reads or actual permissions were performed. Only the local model artifact checks described in the receipt were run.
