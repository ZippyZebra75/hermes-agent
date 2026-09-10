"""Gateway runtime-metadata footer (model · context % · cwd), off by default to keep replies
minimal. Config: ``display.runtime_footer: {enabled: bool, fields: [model, context_pct, cwd]}``
(order shown; drop any to hide), per-platform override ``display.platforms.<p>.runtime_footer``,
toggled by ``/footer on|off``. Fields: ``model`` (vendor prefix dropped), ``context_pct`` (last-call
occupancy), ``latency`` (turn wall-clock, opt-in — NOT in the default set so an unset ``fields``
renders exactly as before), ``tps`` (decode-phase tokens/sec — opt-in like ``latency``),
``ttft`` (request issue → first streamed delta of the turn's first streaming call — opt-in like
``latency``), ``cwd`` (home-relative), ``out_tokens`` (THIS turn's output tokens — opt-in like
``latency``). ``gateway/run.py`` appends the footer to the final response only (never to
tool-progress or streaming partials); when streaming already delivered the text, it goes out as a
trailing message via ``send_trailing_footer()``.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


# Local display aliases for the footer model field: full model id (vendor
# prefix already stripped) -> compact label. Kept local to the patch stack;
# extend here when another model needs a shorter footer name.
_FOOTER_MODEL_ALIASES = {
    "deepseek-v4-flash": "DSv4flash",
}


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``), then apply
    local display aliases (full model id -> compact footer label)."""
    if not model:
        return ""
    short = model.rsplit("/", 1)[-1]
    return _FOOTER_MODEL_ALIASES.get(short, short)


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def _format_out_tokens(tokens: Optional[int]) -> str:
    """Render this turn's output tokens: ``842 tok`` / ``12.3k tok`` ("" when unknown).

    ``tokens`` is the per-turn delta (``turn_output_tokens`` = session completion tokens now
    minus the pre-turn snapshot), so the number is THIS turn's output, not the session total.
    """
    if not tokens or tokens <= 0:
        return ""
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k tok"
    return f"{tokens} tok"


def _format_ttft(seconds: Optional[float]) -> str:
    """Render time-to-first-token as ``ttft 0.83s`` / ``ttft 3s`` / ``ttft 1m05s`` ("" when unmeasured).

    TTFT here is the model's OWN start-up latency: from a request being issued to the first
    streamed delta of the turn's first call that streamed anything (see
    ``agent.turn_usage._record_turn_ttft``) — tool time and earlier delta-less calls excluded.
    Carries the ``ttft`` label because ``latency`` also renders bare seconds and the two can
    appear side by side.  Sub-10s values keep TWO decimals (a 0.3s TTFT and a 0.39s TTFT are
    different diagnoses, and this window is the one people actually tune); past 10s the digits
    stop carrying information, so it rounds to whole seconds and then to ``1m05s``.  Missing or
    nonsensical (<=0) values drop the field entirely.
    """
    if seconds is None or seconds <= 0:
        return ""
    if seconds < 10:
        return f"ttft {seconds:.2f}s"
    total = int(round(seconds))
    if total < 60:
        return f"ttft {total}s"
    m, sec = divmod(total, 60)
    return f"ttft {m}m{sec:02d}s"


def _format_tps(response_tokens: Optional[int], elapsed_ms: Optional[float]) -> str:
    """Render a ``tokens-per-second`` value as ``"NNt/s"`` or return ``""``.

    ``elapsed_ms`` is the DECODE window — the caller sums each API call's first→last streamed
    delta (bench-style decode phase, TTFT and tool time excluded).  Callers on surfaces that
    do not stream pass the request time instead.  Skipped silently when either input is
    missing or the window is too short to give a meaningful rate (denominator approaches zero
    → wild numbers like "12345t/s" on a 1ms async loop tick).  ``min_elapsed_ms`` floors at
    50ms which corresponds to ~20 sample points / second — fine granularity for the
    user-facing rate without divide-by-tiny noise.
    """
    if not response_tokens or response_tokens <= 0:
        return ""
    if elapsed_ms is None or elapsed_ms < 50:
        return ""
    tps = response_tokens / (elapsed_ms / 1000.0)
    if tps >= 100:
        return f"{int(round(tps))}t/s"
    return f"{tps:.1f}t/s"


def format_runtime_footer(*, model: Optional[str], context_tokens: int,
                          context_length: Optional[int], cwd: Optional[str] = None,
                          turn_seconds: Optional[float] = None,
                          response_tokens: Optional[int] = None,
                          elapsed_ms: Optional[float] = None,
                          ttft_seconds: Optional[float] = None,
                          fields: Iterable[str] = _DEFAULT_FIELDS) -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots.

    The ``tps`` field shows the decode-phase tokens-per-second for the just-finished turn
    (response_tokens / elapsed_ms, where ``elapsed_ms`` is the summed first→last streamed-delta
    window).  Callers without timing/usage data omit the optional kwargs and ``tps`` renders
    nothing (same skip-silently rule)."""
    def context_pct() -> str:
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{max(0, min(100, round((context_tokens / context_length) * 100)))}%"
        return ""

    renderers = {
        "model": lambda: _model_short(model),
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
        # Skipped when the caller supplied no token/timing data.
        "tps": lambda: _format_tps(response_tokens, elapsed_ms),
        # Model start-up latency for this turn (opt-in, skipped when unmeasured).
        "ttft": lambda: _format_ttft(ttft_seconds),
        # This turn's output tokens (same per-turn delta tps divides by decode time).
        "out_tokens": lambda: _format_out_tokens(response_tokens),
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def build_footer_line(*, user_config: dict[str, Any] | None, platform_key: str | None,
                      model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      response_tokens: Optional[int] = None,
                      elapsed_ms: Optional[float] = None,
                      ttft_seconds: Optional[float] = None) -> str:
    """Entry point for gateway/run.py: footer text, or "" when disabled / no data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``latency`` field. ``response_tokens`` + ``elapsed_ms`` feed the optional ``tps`` field
    (``elapsed_ms`` = the turn's summed streamed-delta window; request time on non-streaming
    surfaces). ``ttft_seconds`` is the turn's model start-up latency (request issue → first
    streamed delta); ``None``/0 skips the ``ttft`` field."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 response_tokens=response_tokens, elapsed_ms=elapsed_ms,
                                 ttft_seconds=ttft_seconds,
                                 fields=cfg.get("fields") or _DEFAULT_FIELDS)
