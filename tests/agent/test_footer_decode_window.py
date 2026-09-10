"""The footer's tps denominator is the bench-style DECODE window, not wall-clock: per API
call, the span between the first and last streamed delta (TTFT and tool time excluded).
Request time is the fallback for surfaces that do not stream.
"""

import time
from types import SimpleNamespace

import pytest

from agent.stream_delivery import StreamDeliveryMixin
from agent.turn_usage import _fold_decode_window, _record_turn_ttft


def _agent():
    return SimpleNamespace(
        session_decode_seconds=0.0, session_api_seconds=0.0,
        _api_decode_started_at=None, _api_decode_last_at=None,
    )


def test_first_delta_opens_the_window_and_later_deltas_extend_it():
    a = _agent()
    StreamDeliveryMixin._note_decode_activity(a)
    opened = a._api_decode_started_at
    assert opened is not None and a._api_decode_last_at == opened

    time.sleep(0.01)
    StreamDeliveryMixin._note_decode_activity(a)
    assert a._api_decode_started_at == opened  # left edge never moves
    assert a._api_decode_last_at > opened


def test_fold_adds_the_window_and_request_time_then_resets():
    a = _agent()
    a._api_decode_started_at, a._api_decode_last_at = 100.0, 130.0

    _fold_decode_window(a, 35.0)

    assert a.session_decode_seconds == 30.0
    assert a.session_api_seconds == 35.0
    assert a._api_decode_started_at is None and a._api_decode_last_at is None


def test_single_delta_call_has_no_window_but_keeps_request_time():
    """One delta is a point, not a span — the decode window stays empty so the next call's
    first delta opens a fresh one instead of measuring from a stale stamp."""
    a = _agent()
    a._api_decode_started_at = a._api_decode_last_at = 100.0

    _fold_decode_window(a, 4.0)

    assert a.session_decode_seconds == 0.0
    assert a.session_api_seconds == 4.0
    assert a._api_decode_started_at is None


def test_windows_accumulate_across_calls():
    a = _agent()
    a._api_decode_started_at, a._api_decode_last_at = 0.0, 10.0
    _fold_decode_window(a, 12.0)
    a._api_decode_started_at, a._api_decode_last_at = 50.0, 65.0
    _fold_decode_window(a, 20.0)

    assert a.session_decode_seconds == 25.0
    assert a.session_api_seconds == 32.0


# ---------------------------------------------------------------------------
# footer ``ttft``: request issue → first streamed delta, ONE record per turn.
# ---------------------------------------------------------------------------


def _turn_agent(turn_id="t1"):
    a = _agent()
    a.session_ttft_seconds = 0.0
    a._api_request_started_mono = None
    a._turn_ttft_recorded_for = None
    a._current_turn_id = turn_id
    return a


def test_fold_reports_ttft_from_request_issue_to_first_delta():
    """``started`` IS the first delta, so ttft = first-delta − request-issue stamp."""
    a = _turn_agent()
    a._api_request_started_mono = 100.0
    a._api_decode_started_at, a._api_decode_last_at = 103.5, 130.0

    window, ttft = _fold_decode_window(a, 35.0)

    assert window == 26.5
    assert ttft == pytest.approx(3.5)
    assert a._api_request_started_mono is None  # per-call stamp is consumed


def test_fold_ttft_is_zero_without_a_delta_or_a_request_stamp():
    """No deltas (nothing streamed) or no request stamp ⇒ no TTFT, never a negative/fake one."""
    no_delta = _turn_agent()
    no_delta._api_request_started_mono = 100.0
    assert _fold_decode_window(no_delta, 5.0)[1] == 0.0

    no_stamp = _turn_agent()
    no_stamp._api_decode_started_at = no_stamp._api_decode_last_at = 100.0
    assert _fold_decode_window(no_stamp, 5.0)[1] == 0.0


def test_single_delta_call_has_ttft_even_though_the_window_is_empty():
    """One delta is a point (no decode window) but still a real time-to-first-token."""
    a = _turn_agent()
    a._api_request_started_mono = 90.0
    a._api_decode_started_at = a._api_decode_last_at = 92.5

    window, ttft = _fold_decode_window(a, 5.0)

    assert window == 0.0
    assert ttft == pytest.approx(2.5)


def test_one_ttft_recorded_per_turn():
    """A multi-call turn contributes its FIRST streaming call's TTFT only."""
    a = _turn_agent()
    _record_turn_ttft(a, 3.0)
    _record_turn_ttft(a, 4.0)  # same turn id → ignored

    assert a.session_ttft_seconds == 3.0


def test_later_call_supplies_the_ttft_when_the_first_one_streamed_nothing():
    """A first call that emits only tool calls has no deltas; the first call that DOES stream
    provides the turn's TTFT (measured from its own request, so tool time stays excluded)."""
    a = _turn_agent()
    _record_turn_ttft(a, 0.0)  # delta-less first call
    _record_turn_ttft(a, 2.0)  # first streaming call of the same turn

    assert a.session_ttft_seconds == 2.0


def test_ttft_counter_accumulates_across_turns_for_the_footer_delta():
    """The footer differentiates a session counter: one value per turn, summed."""
    a = _turn_agent("t1")
    _record_turn_ttft(a, 3.0)
    a._current_turn_id = "t2"
    _record_turn_ttft(a, 1.5)

    assert a.session_ttft_seconds == pytest.approx(4.5)
