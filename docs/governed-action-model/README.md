# TREX governed adaptive action model — v0.1

Start with `TREX-Governed-Adaptive-Action-Model-20260926.md`.

This is a **proposal/model packet**, not a production runtime or a repository patch.
It specifies the links among provenance, plans, authority, execution, adaptive
reviews and cockpit charts. It refines the previous 34-item integration backlog.

## Contents

- Architecture/design contract with source boundaries and rollout sequence.
- `contracts/action-plan.schema.json`: schema-tested proposal-only subset.
- `contracts/domain-records.json`: field outlines for the fuller domain; **not** complete validated wire schemas.
- `examples/research-to-paper.plan.json`: 18 unexecuted nodes and 20 scheduling dependencies. All artifacts are synthetic design fixtures.
- `examples/operation-registry.fixture.json`: local test registry; never runtime authority.
- `acceptance-cases.json`: 34 **future** system acceptance scenarios, not tests run on TREX.
- `integration-map.json`: seven slices mapped to existing backlog IDs.
- `validate_model.py` and `tests/test_model.py`: 16 offline conformance tests.
- `receipts/`: actual local conformance results, source/artifact manifest and limitations.

## Run the model checks

Use an isolated environment, not the live TREX environment. Requirements used
for this packet are recorded in `requirements-model.txt`.

```sh
python -m venv .venv-model
.venv-model/bin/python -m pip install -r requirements-model.txt
.venv-model/bin/python validate_model.py
.venv-model/bin/python -m pytest tests -q -o addopts=''
```

The checker performs no network calls, imports no TREX/broker runtime, and has
no dispatch or permission-grant code. It checks fixture consistency and necessary
static boundary declarations, not real grants, live risk, market data, prices,
identity/signatures, external effects, crash recovery or scientific support.

Passing the checker never authorizes execution. No actual dataset, strategy
performance, paper mandate or broker receipt is included. Pending approval is
not a malformed plan; it remains pending until a separately implemented trusted
service records an actual decision.
