"""open-runtime fork: two audit items in the engine (FA-8 answered: fork fixes).

* the hitl halt (audit E7, `tickets_source.decide_pick`): with
  `[tickets] hitl = "defer"` a hitl ticket first in build order no longer holds the
  rest of its epic's ready tickets behind it;
* DW-13: `[limits] harvest_summary_chars` / `harvest_evidence_chars` replace the
  fixed 200 / 1000 character caps on a harvested deferred finding."""

from __future__ import annotations

import pytest
from conftest import ticket_row
from test_tickets_source import CART, CHECKOUT, _views

from bmad_loop import devcontract
from bmad_loop import tickets_source as ts
from bmad_loop.policy import PolicyError, loads


def _pick(views, *, defer):
    next_view, status_view = views
    return ts.decide_pick(next_view, status_view, {}, {}, (), defer_hitl=defer)


def test_a_hitl_ticket_first_still_halts_by_default():
    decision = _pick(_views(start=(ticket_row("1.1", hitl=True), ticket_row("1.2"))), defer=False)
    assert isinstance(decision, ts.Halt) and decision.cause == ts.HALT_HITL
    assert decision.key == "1.1"


def test_defer_dispatches_the_next_ready_ticket_of_the_same_epic():
    """Ablation: return the hitl halt at once (upstream) and 1.2 never starts."""
    decision = _pick(_views(start=(ticket_row("1.1", hitl=True), ticket_row("1.2"))), defer=True)
    assert isinstance(decision, ts.Dispatch) and decision.row.ref == "1.2"
    assert ts.PickNote(ts.NOTE_SKIPPED, "1.1", "hitl") in decision.notes


def test_defer_halts_on_the_hitl_ticket_once_its_epic_has_nothing_else():
    decision = _pick(
        _views(start=(ticket_row("1.1", hitl=True), ticket_row("2.1", epic=CHECKOUT))),
        defer=True,
    )
    assert isinstance(decision, ts.Halt) and decision.cause == ts.HALT_HITL
    assert decision.key == "1.1"  # the later epic never starts ahead of it


def test_defer_halts_when_only_hitl_tickets_are_ready():
    decision = _pick(
        _views(start=(ticket_row("1.1", hitl=True), ticket_row("1.2", hitl=True))), defer=True
    )
    assert isinstance(decision, ts.Halt) and decision.key == "1.1"


def test_tickets_hitl_policy_parses_and_refuses_unknown_modes():
    assert loads("").tickets.hitl == "halt"
    assert loads('[tickets]\nhitl = "defer"\n').tickets.hitl == "defer"
    with pytest.raises(PolicyError, match=r"tickets\.hitl"):
        loads('[tickets]\nhitl = "later"\n')


def _fm(summary: str, evidence: str) -> dict:
    return {
        devcontract.DEFERRED_FIELD: [
            {"summary": summary, "evidence": evidence, "location": "src/x.rs:1", "severity": "low"}
        ]
    }


def test_harvest_caps_default_to_upstream_and_follow_the_policy():
    """DW-13 (12.2): long summaries and evidence were cut at 200 / 1000."""
    long_summary, long_evidence = "s" * 600, "e" * 3000
    (default,), _ = devcontract.parse_deferred_findings(_fm(long_summary, long_evidence))
    assert len(default.summary) <= 200 and len(default.evidence) <= 1000
    (wide,), _ = devcontract.parse_deferred_findings(
        _fm(long_summary, long_evidence), summary_limit=1000, evidence_limit=5000
    )
    assert wide.summary == long_summary and wide.evidence == long_evidence


def test_harvest_cap_keys_parse_and_refuse_zero():
    limits = loads("[limits]\nharvest_summary_chars = 600\nharvest_evidence_chars = 4000\n").limits
    assert (limits.harvest_summary_chars, limits.harvest_evidence_chars) == (600, 4000)
    assert loads("").limits.harvest_summary_chars == 200
    with pytest.raises(PolicyError, match="harvest"):
        loads("[limits]\nharvest_summary_chars = 0\n")


def test_the_cart_fixture_epic_is_the_first():
    # guards the fixture assumption the defer tests rely on
    assert CART == "epic-cart"
