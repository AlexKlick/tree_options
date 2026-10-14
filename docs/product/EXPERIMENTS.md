# Experiment inventory increment

The Experiments destination (`#/experiments`) reads one versioned publication.
`GET /api/experiments` returns the index; `GET /api/experiments/{id}` returns a
record and its publication identity. Existing research, quant, workspace and
trade-floor bookmarks remain available. The complete five-destination navigation
change belongs to the next workflow increment.

The publisher reads existing challenge standings, the verified QSL ledger,
long-run digests, sealed campaign catalog, a verified ResearchWorker snapshot,
the existing frozen dataset registry, forward minute captures, and the original
virtual allocation plan. It does not submit work, create registrations, place
orders, change account bindings, or claim new strategy qualification.

Each record provides its hypothesis (or an explicit unavailable reason),
evidence class, original status, source freshness, observations with units,
limitations, next decision and available artifact hashes. Quantitative confidence
is not manufactured from lifecycle metadata or a sample floor. Modelled dollar
amounts remain Decimal strings. No record exposes these research results as an
account return. Counts missing from a source remain null with a reason.

Challenge sessions are counted from distinct dates, separately from repeated
replay entries and cumulative modeled P&L. The original and corrected v3
long-run reports are linked only when both exact recorded SHA-256 identities
match. The correction remains incomplete and post-hoc; the older accounting is
visibly superseded. Other reports without both identities retain a lineage
blocker. Closed campaign verdicts remain closed; reading does not reopen them.

Forward captures show the three-clock exact-minute count separately from the
eight-clock 15-minute-window count. Failed capture attempts remain in the
denominator. Recorded budget exhaustion, invalid captures and empty observations
have distinct labels. Historical receipts that do not distinguish provider from
entitlement failures retain that uncertainty. The sealed 60-session requirement
is unchanged; this inventory does not determine restart eligibility.

The three-program pilot panel describes existing QSL observation and two blocked
equity drafts. It grants **zero new requests, compute seconds or storage bytes**.
Those are admission caps for new work from this increment, not consumption
measurements or changes to existing QSL/challenge jobs. The equity baseline and
challenger are not registered or running: independently qualified real inputs,
a frozen challenger/holdout protocol, and enforceable job budgets are still
required. Synthetic machinery inputs and user-supplied unqualified inputs stay
research-only. The 29 virtual allocations retain their original owner and file.
Account binding and research assignment are separate; a binding is not evidence
of an assigned or profitable research program.

## Publication and installation contract

Run the publisher under host admission. It captures each artifact once and
uses supported SQLite backup for consistent evidence/runstate reads. A source
failure leaves the other sources inspectable, marks its count unavailable and
returns exit 3 after publishing the degraded index. It never fills missing
sources with empty successful portfolios. The web service reads no source DB and
has no fallback to one. Missing, corrupt, future or older-than-120-second
publications return HTTP 503 with a specific reason.

The source configuration is an operator-owned JSON file outside the release:

```json
{
  "schema": "trex-experiment-sources/1",
  "sources": {
    "challenge": "/private/desk-store/evaluations/challenge",
    "qsl": "/private/desk-state/evidence/qsl.sqlite3",
    "longrun": "/private/desk-store/evaluations/longrun",
    "longrun_correction": "/private/longrun/v3-final-accounting-20261001/digest.json",
    "campaigns": "/private/artifacts/campaign-2026-09",
    "research": "/private/research-workspace",
    "datasets": "/private/research-datasets",
    "forward": "/private/desk-forward-minutes",
    "allocations": "/private/paper-workspace/allocation-plan.json"
  }
}
```

These are placeholders, not new storage locations. Bind each to its existing
owner. An omitted source is explicitly unavailable. Never copy active research
stores into a replaceable release to make the view appear populated.

```sh
host-test .venv/bin/python -m tree_options.research.experiments \
  --config /private/experiment-sources.json \
  --out /private/desk-state/read-model/experiments.json \
  --source-release FULL_SOURCE_COMMIT
```

Set `TREX_EXPERIMENT_SNAPSHOT` in the web service to that output. The version 2
release manifest binds the source-configuration hash, publication path and
installed command. Changes to source bindings require a new seal. The supplied
publisher timer waits 45 seconds after completion; RAM admission delays can make
the view unavailable, rather than presenting old evidence as current. Atomic
publication retains the previous complete file if replacement fails.

Deploy only after the existing custody/release gates close. This increment does
not replace the running canary owner or enable broker/browser command authority.
Readiness of this inventory, the paper product, live trading and any strategy
remain separate decisions.

## Remaining Phase 2 work

- Qualify real point-in-time equity datasets with universe, corporate-action,
  calendar, cost and availability provenance. Existing registry classes cannot
  claim this qualification.
- Freeze and register the control and one challenger through the existing
  ResearchWorker; enforce request, compute and storage budgets at job admission.
- Extend capture-owner receipts where provider, entitlement and capture failures
  are currently indistinguishable, without inferring a cause from empty bars.
- Complete the Phase 0 recovery/backup gates and Phase 1 release qualification,
  then install and measure publication latency in the private environment.

This increment is a read-model and workflow foundation, not completion of the
Phase 2 exit gate or the 60-session research qualification requirement.
