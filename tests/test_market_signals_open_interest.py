from __future__ import annotations

from model.market_signals import OpenInterestSnapshot, compute_open_interest_change


def test_open_interest_rel_change_from_zero_activation() -> None:
    prev = OpenInterestSnapshot(event_id="e1", market_id="m1", value=0.0)
    curr = OpenInterestSnapshot(event_id="e1", market_id="m1", value=10.0)
    change = compute_open_interest_change(prev=prev, curr=curr)
    assert change.abs_change == 10.0
    assert change.rel_change == 1.0


def test_open_interest_rel_change_zero_to_zero() -> None:
    prev = OpenInterestSnapshot(event_id="e1", market_id="m1", value=0.0)
    curr = OpenInterestSnapshot(event_id="e1", market_id="m1", value=0.0)
    change = compute_open_interest_change(prev=prev, curr=curr)
    assert change.abs_change == 0.0
    assert change.rel_change == 0.0


def test_open_interest_rel_change_standard_case() -> None:
    prev = OpenInterestSnapshot(event_id="e1", market_id="m1", value=100.0)
    curr = OpenInterestSnapshot(event_id="e1", market_id="m1", value=110.0)
    change = compute_open_interest_change(prev=prev, curr=curr)
    assert change.abs_change == 10.0
    assert change.rel_change == 0.1

