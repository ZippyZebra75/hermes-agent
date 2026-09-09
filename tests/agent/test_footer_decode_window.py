"""The footer's tps denominator is the bench-style DECODE window, not wall-clock: per API
call, the span between the first and last streamed delta (TTFT and tool time excluded).
Request time is the fallback for surfaces that do not stream.
"""

import time
from types import SimpleNamespace

from agent.stream_delivery import StreamDeliveryMixin
from agent.turn_usage import _fold_decode_window


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
