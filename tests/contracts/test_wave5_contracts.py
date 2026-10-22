"""Wave 5 additive contracts: the ``compose_entry`` manual ticket and
``trex-idea-list/1``.

Oracles independent of the implementation where it matters: canonical
bytes and body hashes are re-derived with stdlib ``json``/``hashlib``; the
earlier ``compose_entry`` body hash is computed from a literal document; the
new wire strings are literals.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from tree_options.trex.contracts import Blocker
from tree_options.trex.contracts import commands as cmd
from tree_options.trex.contracts import ideas as ideas_contract
from tree_options.trex.contracts.canonical import model_document

REPO = Path(__file__).resolve().parents[2]
WAVE5 = Path(__file__).parent / "fixtures" / "wave5"
UUID = "3b0d6c1e-8f2a-4c5d-9e7f-1a2b3c4d5e6f"


def load(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((WAVE5 / name).read_text())
    return loaded


def stdlib_canonical(document: Any) -> bytes:
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def compose_doc(args: dict[str, Any]) -> dict[str, Any]:
    doc = load("command_compose_entry_manual.json")
    doc["args"] = args
    return doc


def ticket(**changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(load("command_compose_entry_manual.json")["args"]["manual_equity_ticket"])
    out.update(changes)
    return out


def manual_args(**changes: Any) -> dict[str, Any]:
    args = copy.deepcopy(load("command_compose_entry_manual.json")["args"])
    args.update(changes)
    return args


# ---------------------------------------------------------------- fixtures

ROUND_TRIPS = [
    ("command_compose_entry_manual.json", cmd.parse_command, "trex-command/1"),
    ("idea_list.json", ideas_contract.IdeaList.model_validate, "trex-idea-list/1"),
    ("idea_list_unavailable.json", ideas_contract.IdeaList.model_validate, "trex-idea-list/1"),
]


@pytest.mark.parametrize(("name", "parse", "schema"), ROUND_TRIPS, ids=[r[0] for r in ROUND_TRIPS])
def test_fixture_round_trips(name: str, parse: Any, schema: str) -> None:
    document = load(name)
    model = parse(document)
    assert model_document(model) == document
    assert parse(model_document(model)) == model
    assert document["schema"] == schema


def test_every_wave5_fixture_is_covered() -> None:
    assert {p.name for p in WAVE5.glob("*.json")} == {r[0] for r in ROUND_TRIPS}


# ---------------------------------------------------------------- compose_entry amendment


def test_an_earlier_compose_entry_keeps_its_bytes_and_body_hash() -> None:
    """No ticket: no ``manual_equity_ticket`` key, so the body hash is what
    it was before the amendment (stdlib oracle over a literal document)."""
    args = {"program_id": "desk-miner", "candidate_id": "opt-qqq-001", "asset_kind": "option"}
    command = cmd.parse_command(compose_doc(args))
    document = model_document(command)
    assert document["args"] == {**args, "sleeve_ref": None}
    assert "manual_equity_ticket" not in json.dumps(document)
    reduced = {**document, "issued_by": {"sub": "owner", "ingress": "family"}}
    oracle = hashlib.sha256(stdlib_canonical(reduced)).hexdigest()
    assert cmd.command_body_sha256(command) == oracle
    # an explicit null ticket is the same command as an absent one
    with_null = cmd.parse_command(compose_doc({**args, "manual_equity_ticket": None}))
    assert cmd.command_body_sha256(with_null) == oracle


def test_the_ticket_is_hashed_into_the_body() -> None:
    base = cmd.parse_command(load("command_compose_entry_manual.json"))
    for field, value in (
        ("quantity", 3),
        ("stop_price_cents", 66400),
        ("target_price_cents", None),
        ("exit_deadline_session", "2026-10-26"),
        ("limit_policy", "mid"),
        ("limit_policy", {"limit_cents": 67100}),
        ("symbol", "QQQ"),
    ):
        changed = cmd.parse_command(
            compose_doc(manual_args(manual_equity_ticket=ticket(**{field: value})))
        )
        assert cmd.command_body_sha256(changed) != cmd.command_body_sha256(base), field


def test_manual_intent_id_is_path_safe_and_derived_from_the_uuid() -> None:
    assert cmd.manual_intent_id(f"manual:{UUID}") == f"manual-{UUID}"
    for bad in (UUID, f"manual:{UUID.upper()}", "manual:not-a-uuid", f"manual:{UUID}x"):
        with pytest.raises(ValueError):
            cmd.manual_intent_id(bad)


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"manual_equity_ticket": None}, id="manual-prefix-without-ticket"),
        pytest.param({"asset_kind": "option"}, id="ticket-on-an-option"),
        pytest.param({"candidate_id": "eq-spy-001"}, id="ticket-without-manual-prefix"),
        pytest.param({"candidate_id": f"manual:{UUID.upper()}"}, id="upper-case-uuid"),
        pytest.param({"candidate_id": "manual:x"}, id="not-a-uuid"),
        pytest.param({"manual_equity_ticket": {**ticket(), "extra": 1}}, id="extra-ticket-field"),
    ],
)
def test_compose_args_refuse_a_misbound_ticket(args: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cmd.parse_command(compose_doc(manual_args(**args)))


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"quantity": 0}, id="zero-shares"),
        pytest.param({"quantity": "2"}, id="string-quantity"),
        pytest.param({"quantity": 2.0}, id="float-quantity"),
        pytest.param({"quantity": True}, id="bool-quantity"),
        pytest.param({"stop_price_cents": 0}, id="zero-stop"),
        pytest.param({"stop_price_cents": 665.0}, id="float-stop"),
        pytest.param({"target_price_cents": 66500}, id="target-at-stop"),
        pytest.param({"symbol": "spy"}, id="lower-case-symbol"),
        pytest.param({"limit_policy": "market"}, id="unknown-policy"),
        pytest.param({"limit_policy": {"limit_cents": 66500}}, id="limit-at-stop"),
        pytest.param({"limit_policy": {"limit_cents": 68000}}, id="limit-at-target"),
        pytest.param({"limit_policy": {"limit_cents": "67100"}}, id="string-limit"),
        pytest.param({"exit_deadline_session": "soon"}, id="not-a-date"),
    ],
)
def test_a_malformed_ticket_refuses(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cmd.ManualEquityTicket.model_validate(ticket(**changes))


def test_a_well_formed_ticket_without_target_or_with_an_explicit_limit() -> None:
    no_target = cmd.ManualEquityTicket.model_validate(ticket(target_price_cents=None))
    assert no_target.target_price_cents is None
    explicit = cmd.ManualEquityTicket.model_validate(ticket(limit_policy={"limit_cents": 67100}))
    assert explicit.limit_policy == cmd.ManualLimit(limit_cents=67100)
    omitted = {k: v for k, v in ticket().items() if k != "target_price_cents"}
    assert cmd.ManualEquityTicket.model_validate(omitted).target_price_cents is None


# ---------------------------------------------------------------- trex-idea-list/1


def _idea() -> dict[str, Any]:
    return copy.deepcopy(load("idea_list.json")["drafts"]["items"][1])  # the option idea


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param(
            {
                "equity": {
                    "quantity": 1,
                    "limit_cents": 1,
                    "stop_price_cents": 1,
                    "target_price_cents": None,
                    "exit_deadline_session": "2026-10-23",
                }
            },
            id="both-structures",
        ),
        pytest.param({"option": None}, id="no-structure"),
        pytest.param({"proposal_id": "a" * 64}, id="proposal-id-without-status"),
        pytest.param({"proposal_status": "pending"}, id="status-without-proposal-id"),
        pytest.param({"status": "proposed"}, id="proposed-without-a-live-proposal"),
        pytest.param({"status": "expired"}, id="expired-with-time-left"),
        pytest.param({"seconds_to_expiry": 0}, id="available-without-time"),
        pytest.param(
            {
                "compose_args": {
                    "program_id": "desk-miner",
                    "candidate_id": "other",
                    "asset_kind": "option",
                }
            },
            id="compose-args-elsewhere",
        ),
        pytest.param({"hints": ["not_a_reason"]}, id="hint-outside-the-vocabulary"),
        pytest.param({"max_loss_cents": 0}, id="zero-max-loss"),
    ],
)
def test_an_idea_refuses_an_inconsistent_shape(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ideas_contract.Idea.model_validate({**_idea(), **changes})


def test_an_idea_with_a_consumed_proposal_is_used() -> None:
    used = {**_idea(), "proposal_status": "consumed", "proposal_id": "a" * 64}
    with pytest.raises(ValidationError):
        ideas_contract.Idea.model_validate(used)
    assert ideas_contract.Idea.model_validate({**used, "status": "used"}).status == "used"


def test_the_drafts_block_follows_the_null_rule() -> None:
    known = load("idea_list.json")["drafts"]
    down = load("idea_list_unavailable.json")["drafts"]
    for doc, field, value in (
        (known, "items", None),
        (known, "unavailable_reason", "drafts_dir_unreadable"),
        (down, "items", []),
        (down, "unavailable_reason", None),
    ):
        with pytest.raises(ValidationError):
            ideas_contract.DraftIdeas.model_validate({**doc, field: value})


def test_the_manual_window_is_set_exactly_on_a_session() -> None:
    terms = load("idea_list.json")["manual_equity"]
    with pytest.raises(ValidationError):
        ideas_contract.ManualEquityTerms.model_validate({**terms, "window_status": "no_session"})
    with pytest.raises(ValidationError):
        ideas_contract.ManualEquityTerms.model_validate({**terms, "window_opens_at": None})
    off = load("idea_list_unavailable.json")["manual_equity"]
    with pytest.raises(ValidationError):
        ideas_contract.ManualEquityTerms.model_validate({**off, "window_status": "open"})


def test_execution_is_literally_disabled() -> None:
    for value in (True, 0, "false"):
        with pytest.raises(ValidationError):
            ideas_contract.IdeaList.model_validate(
                {**load("idea_list.json"), "execution_enabled": value}
            )


def test_constants() -> None:
    assert ideas_contract.IDEAS_PATH == "/api/ideas"
    assert ideas_contract.IDEA_LIST_SCHEMA == "trex-idea-list/1"
    assert cmd.MANUAL_CANDIDATE_PREFIX == ideas_contract.MANUAL_CANDIDATE_PREFIX == "manual:"
    assert cmd.MANUAL_STRATEGY_VERSION == "owner-manual/1"
    assert ideas_contract.MANUAL_PROGRAM_ID == "owner-manual"
    assert ideas_contract.LIMIT_POLICIES == ("ask", "mid", "limit_cents")


# ---------------------------------------------------------------- vocabulary


W5_BLOCKERS = {"manual_ticket_invalid", "drafts_dir_unreadable", "draft_unreadable"}


def test_the_wave5_blockers_are_members_named_by_their_values() -> None:
    for code in sorted(W5_BLOCKERS):
        member = Blocker(code)
        assert member.name == code.upper() and member == code
        assert json.dumps({"reason": member}) == json.dumps({"reason": code})


def test_the_producers_spell_the_members() -> None:
    from tree_options.trex import desk_entries

    assert desk_entries.MANUAL_TICKET_INVALID == Blocker.MANUAL_TICKET_INVALID
    view = (REPO / "src/tree_options/trex_web/entry_ideas_view.py").read_text()
    assert "Blocker.DRAFTS_DIR_UNREADABLE" in view and "Blocker.DRAFT_UNREADABLE" in view


# ---------------------------------------------------------------- guards

NEW_PYTHON = [
    REPO / "src/tree_options/trex/contracts/ideas.py",
    REPO / "src/tree_options/trex/contracts/commands.py",
    REPO / "src/tree_options/trex_web/entry_ideas_view.py",
    REPO / "src/tree_options/trex/desk_entries.py",
]
NEW_TEXT = [
    *NEW_PYTHON,
    Path(__file__),
    REPO / "tests/unit/test_trex_web_entry_ideas.py",
    REPO / "tests/unit/test_desk_manual_ticket.py",
    REPO / "web/src/lib/contracts.ts",
    REPO / "web/src/lib/contracts.wave5.test.ts",
    *sorted(WAVE5.glob("*.json")),
]


@pytest.mark.parametrize("path", NEW_PYTHON, ids=lambda p: p.name)
def test_no_timedelta_weekday_or_toordinal_outside_time(path: Path) -> None:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            assert node.id != "timedelta", f"{path.name}:{node.lineno}"
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("timedelta", "weekday", "toordinal"), (
                f"{path.name}:{node.lineno}"
            )
        if isinstance(node, ast.ImportFrom):
            assert all(a.name != "timedelta" for a in node.names), f"{path.name}:{node.lineno}"


@pytest.mark.parametrize("path", NEW_TEXT, ids=lambda p: p.name)
def test_no_en_or_em_dashes(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for dash in (chr(0x2013), chr(0x2014)):  # en dash, em dash
        assert dash not in text, f"{path.name} contains U+{ord(dash):04X}"


def test_the_ideas_contract_imports_no_broker_or_web_framework() -> None:
    probe = (
        "import sys, tree_options.trex.contracts.ideas;"
        " sys.exit(1 if 'ib_async' in sys.modules or 'fastapi' in sys.modules else 0)"
    )
    done = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        capture_output=True,
        env={"PYTHONPATH": str(REPO / "src"), "PATH": "/usr/bin:/bin"},
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()[-2000:]
