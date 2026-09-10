"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os

import pytest

from gateway.runtime_footer import (
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_runtime_footer,
    resolve_footer_config,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == "~/projects/hermes"


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        fields=("model", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · ~/projects/hermes"


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert "/tmp/wd" in out


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------


def test_resolve_platform_override_wins():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model"]},
            "platforms": {
                "slack": {"runtime_footer": {"enabled": False}},
            },
        },
    }
    # Telegram picks up the global enable
    assert resolve_footer_config(user, "telegram")["enabled"] is True
    # Slack overrides to off
    assert resolve_footer_config(user, "slack")["enabled"] is False


def test_resolve_platform_can_add_fields_only():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {
                "discord": {"runtime_footer": {"fields": ["context_pct"]}},
            },
        },
    }
    tg = resolve_footer_config(user, "telegram")
    assert tg["enabled"] is True
    assert tg["fields"] == ["model", "context_pct", "cwd"]
    dc = resolve_footer_config(user, "discord")
    assert dc["enabled"] is True
    assert dc["fields"] == ["context_pct"]


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------


def test_build_footer_per_platform_off_suppresses():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {"slack": {"runtime_footer": {"enabled": False}}},
        },
    }
    out = build_footer_line(
        user_config=user,
        platform_key="slack",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""



# ---------------------------------------------------------------------------
# latency — opt-in wall-clock turn duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.999, "<1s"),
        (1.0, "1s"),
        (22.0, "22s"),
        (22.4, "22s"),
        (59.4, "59s"),
        (59.6, "1m00s"),
        (60.0, "1m00s"),
        (65.0, "1m05s"),
        (125.0, "2m05s"),
        (3600.0, "60m00s"),
    ],
)
def test_format_latency(seconds, expected):
    from gateway.runtime_footer import _format_latency

    assert _format_latency(seconds) == expected


def test_format_footer_latency_renders():
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
        fields=("latency",),
    )
    assert out == "22s"


def test_format_footer_latency_skipped_when_unmeasured():
    """A call site that doesn't measure timing leaves the field out entirely."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=None,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_skipped_when_negative():
    """A nonsensical (negative) duration is dropped rather than rendered."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=-1.0,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_zero_renders_sub_second():
    """Zero is a real measurement (a very fast turn), not missing data."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=0.0,
        fields=("latency",),
    )
    assert out == "<1s"


def test_format_footer_latency_in_field_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=68_000,
        context_length=100_000,
        cwd=str(tmp_path),
        turn_seconds=65.0,
        fields=("model", "context_pct", "latency", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · 1m05s · ~"


def test_build_footer_line_threads_turn_seconds(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = build_footer_line(
        user_config={
            "display": {
                "runtime_footer": {
                    "enabled": True,
                    "fields": ["model", "latency"],
                }
            }
        },
        platform_key="discord",
        model="gpt-5.4",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
    )
    assert out == "gpt-5.4 · 22s"


# ---------------------------------------------------------------------------
# Byte-stability: `latency` is opt-in, so the DEFAULT footer is unchanged.
#
# Upstream doctrine: a system prompt / rendered surface must be byte-stable for
# the life of a conversation.  Adding a field to _DEFAULT_FIELDS would silently
# change the footer text of every user who already enabled it.  These tests pin
# the default set and the exact default-config output strings.
# ---------------------------------------------------------------------------

_LEGACY_DEFAULT_FIELDS = ["model", "context_pct", "cwd"]


def test_latency_not_in_default_fields():
    from gateway.runtime_footer import _DEFAULT_FIELDS

    assert "latency" not in _DEFAULT_FIELDS
    assert list(_DEFAULT_FIELDS) == _LEGACY_DEFAULT_FIELDS


def test_resolve_footer_config_default_fields_exclude_latency():
    assert resolve_footer_config({}, "telegram")["fields"] == _LEGACY_DEFAULT_FIELDS
    assert resolve_footer_config(
        {"display": {"runtime_footer": {"enabled": True}}}, "discord"
    )["fields"] == _LEGACY_DEFAULT_FIELDS


@pytest.mark.parametrize(
    "model,tokens,window,cwd,expected",
    [
        ("openai/gpt-5.4", 50_247, 1_000_000, "/var/data", "gpt-5.4 · 5% · /var/data"),
        ("claude-opus-4-8", 68_000, 100_000, "/var/data", "claude-opus-4-8 · 68% · /var/data"),
        ("m", 0, None, "/var/data", "m · /var/data"),
        ("", 10, 100, "/var/data", "10% · /var/data"),
        ("m", 10, 100, "", "m · 10%"),
    ],
)
def test_default_footer_renders_byte_identically(
    monkeypatch, model, tokens, window, cwd, expected
):
    """Default-config output is byte-for-byte what it was before `latency`.

    Note `turn_seconds` IS supplied — proving that even when the caller
    measures timing, a default-configured footer does not show it.
    """
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model=model,
        context_tokens=tokens,
        context_length=window,
        cwd=cwd,
        turn_seconds=22.0,
        # fields deliberately NOT passed — exercises the default.
    )
    assert out == expected


def test_default_build_footer_line_ignores_turn_seconds(monkeypatch):
    """build_footer_line with default fields is unaffected by turn_seconds."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    common = dict(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="discord",
        model="openai/gpt-5.4",
        context_tokens=50_247,
        context_length=1_000_000,
        cwd="/var/data",
    )
    baseline = build_footer_line(**common)
    with_timing = build_footer_line(**common, turn_seconds=125.0)
    assert baseline == "gpt-5.4 · 5% · /var/data"
    assert with_timing == baseline


# ---------------------------------------------------------------------------
# tps field (#26877)
# ---------------------------------------------------------------------------


class TestOutTokensField:
    """``out_tokens`` — this turn's output tokens (opt-in field)."""

    def test_renders_plain_count_below_1000(self):
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("out_tokens",), response_tokens=842,
        )
        assert out == "842 tok"

    def test_renders_k_suffix_at_or_above_1000(self):
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("out_tokens",), response_tokens=12345,
        )
        assert out == "12.3k tok"

    def test_skipped_without_usage(self):
        for value in (None, 0, -5):
            assert format_runtime_footer(
                model="gpt-5", context_tokens=0, context_length=None,
                fields=("out_tokens",), response_tokens=value,
            ) == ""


class TestTtftField:
    """``ttft`` — the turn's model start-up latency (request issue → first streamed delta)."""

    def test_renders_sub_ten_seconds_with_two_decimals(self):
        for seconds, expected in ((0.42, "ttft 0.42s"), (3.19, "ttft 3.19s"), (9.94, "ttft 9.94s")):
            out = format_runtime_footer(
                model=None, context_tokens=0, context_length=None,
                fields=("ttft",), ttft_seconds=seconds,
            )
            assert out == expected

    def test_renders_whole_seconds_at_and_above_ten(self):
        """Past 10s the decimals stop carrying information — whole seconds, then m/ss."""
        for seconds, expected in ((10.0, "ttft 10s"), (47.4, "ttft 47s"), (59.4, "ttft 59s")):
            out = format_runtime_footer(
                model=None, context_tokens=0, context_length=None,
                fields=("ttft",), ttft_seconds=seconds,
            )
            assert out == expected

    def test_renders_minute_scale(self):
        for seconds, expected in ((59.6, "ttft 1m00s"), (125.0, "ttft 2m05s")):
            out = format_runtime_footer(
                model=None, context_tokens=0, context_length=None,
                fields=("ttft",), ttft_seconds=seconds,
            )
            assert out == expected

    def test_skipped_when_unmeasured(self):
        """None (no data) and <=0 (nothing streamed this turn) both drop the field."""
        for value in (None, 0.0, 0, -1.0):
            assert format_runtime_footer(
                model="gpt-5", context_tokens=0, context_length=None,
                fields=("ttft",), ttft_seconds=value,
            ) == ""

    def test_omitted_when_not_in_fields_list(self):
        baseline = format_runtime_footer(
            model="openai/gpt-5.4", context_tokens=512, context_length=2048,
            cwd="", fields=("model", "context_pct"),
        )
        with_data = format_runtime_footer(
            model="openai/gpt-5.4", context_tokens=512, context_length=2048,
            cwd="", fields=("model", "context_pct"), ttft_seconds=1.5,
        )
        assert baseline == with_data
        assert "ttft" not in with_data

    def test_joins_with_other_fields_in_order(self):
        out = format_runtime_footer(
            model="openai/gpt-5.4", context_tokens=512, context_length=2048,
            cwd="", fields=("model", "context_pct", "ttft", "tps"),
            response_tokens=80, elapsed_ms=4000.0, ttft_seconds=1.26,
        )
        assert out == "gpt-5.4 · 25% · ttft 1.26s · 20.0t/s"

    def test_not_in_default_fields(self):
        """Opt-in like latency: an unset ``fields`` must not change existing footers."""
        from gateway.runtime_footer import _DEFAULT_FIELDS

        assert "ttft" not in _DEFAULT_FIELDS

    def test_build_footer_line_threads_kwargs_through(self):
        out = build_footer_line(
            user_config={
                "display": {
                    "runtime_footer": {
                        "enabled": True,
                        "fields": ["model", "ttft"],
                    }
                }
            },
            platform_key="telegram",
            model="openai/gpt-5.4",
            context_tokens=0, context_length=None,
            cwd="",
            ttft_seconds=2.5,
        )
        assert out == "gpt-5.4 · ttft 2.50s"

    def test_build_footer_line_old_callers_unaffected(self):
        out = build_footer_line(
            user_config={
                "display": {
                    "runtime_footer": {
                        "enabled": True,
                        "fields": ["model", "context_pct"],
                    }
                }
            },
            platform_key="telegram",
            model="openai/gpt-5.4",
            context_tokens=100, context_length=400,
            cwd="",
        )
        assert "ttft" not in out


class TestTpsField:
    def test_tps_renders_decimal_below_100(self):
        # 50 tokens in 2 seconds = 25 t/s — under 100, decimal format.
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=50,
            elapsed_ms=2000.0,
        )
        assert out == "25.0t/s"

    def test_tps_renders_integer_at_or_above_100(self):
        # 500 tokens in 2 seconds = 250 t/s — integer when ≥100.
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=500,
            elapsed_ms=2000.0,
        )
        assert out == "250t/s"

    def test_tps_skipped_when_response_tokens_zero(self):
        out = format_runtime_footer(
            model="gpt-5", context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=0,
            elapsed_ms=2000.0,
        )
        assert out == ""

    def test_tps_skipped_when_response_tokens_none(self):
        out = format_runtime_footer(
            model="gpt-5", context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=None,
            elapsed_ms=2000.0,
        )
        assert out == ""

    def test_tps_skipped_when_elapsed_too_small(self):
        # <50ms elapsed → degenerate denominator; skip to avoid wild numbers.
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=10,
            elapsed_ms=5.0,
        )
        assert out == ""

    def test_tps_skipped_when_elapsed_none(self):
        out = format_runtime_footer(
            model=None, context_tokens=0, context_length=None,
            fields=("tps",),
            response_tokens=50,
            elapsed_ms=None,
        )
        assert out == ""

    def test_tps_joins_with_other_fields(self):
        out = format_runtime_footer(
            model="openai/gpt-5.4",
            context_tokens=512, context_length=2048,
            cwd="",
            fields=("model", "context_pct", "tps"),
            response_tokens=80,
            elapsed_ms=4000.0,
        )
        # gpt-5.4 · 25% · 20.0t/s
        assert "gpt-5.4" in out
        assert "25%" in out
        assert "20.0t/s" in out
        assert out.count(" · ") == 2

    def test_tps_omitted_when_not_in_fields_list(self):
        baseline = format_runtime_footer(
            model="openai/gpt-5.4",
            context_tokens=512, context_length=2048,
            cwd="",
            fields=("model", "context_pct"),
        )
        with_data = format_runtime_footer(
            model="openai/gpt-5.4",
            context_tokens=512, context_length=2048,
            cwd="",
            fields=("model", "context_pct"),
            response_tokens=80,
            elapsed_ms=4000.0,
        )
        assert baseline == with_data
        assert "t/s" not in with_data

    def test_build_footer_line_threads_kwargs_through(self):
        out = build_footer_line(
            user_config={
                "display": {
                    "runtime_footer": {
                        "enabled": True,
                        "fields": ["model", "tps"],
                    }
                }
            },
            platform_key="telegram",
            model="openai/gpt-5.4",
            context_tokens=0, context_length=None,
            cwd="",
            response_tokens=60,
            elapsed_ms=3000.0,
        )
        assert "gpt-5.4" in out
        # 60 / 3 = 20 t/s
        assert "20.0t/s" in out

    def test_build_footer_line_old_callers_unaffected(self):
        out = build_footer_line(
            user_config={
                "display": {
                    "runtime_footer": {
                        "enabled": True,
                        "fields": ["model", "context_pct"],
                    }
                }
            },
            platform_key="cli",
            model="openai/gpt-5.4",
            context_tokens=100, context_length=400,
            cwd="",
        )
        assert "gpt-5.4" in out
        assert "t/s" not in out
