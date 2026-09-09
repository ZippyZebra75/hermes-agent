"""Invariant tests for the opt-in collapsible trace (``tool_progress_details``).

Contract: tool progress + every reasoning segment (each ≤200 chars, quoted) + inter-tool-call
interim prose collect into ONE collapsed ``<details>`` block at the head of the turn-final
message; the answer itself stays plain text below it (never wrapped in ``<details>``).
While streaming the block lives in the draft frames and folds one-way once the answer starts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _make_adapter(*, supports_draft: bool = True, details_supported=True, block_ok=True):
    """BasePlatformAdapter subclass with draft + tool-details probes (no platform state)."""
    from gateway.platforms.base import BasePlatformAdapter, SendResult

    cls = type("ToolDetailsAdapter", (BasePlatformAdapter,), {"MAX_MESSAGE_LENGTH": 4096})
    cls.__abstractmethods__ = frozenset()
    adapter = cls.__new__(cls)
    adapter._typing_paused = set()
    adapter._fatal_error_message = None
    adapter.draft_calls: list[str] = []
    adapter.draft_ids: list[int] = []
    adapter.sent: list[str] = []

    adapter.tool_details_supported = lambda: details_supported
    adapter.tool_details_block_ok = lambda block: block_ok

    def _supports(chat_type=None, metadata=None):
        return bool(supports_draft) and (chat_type or "").lower() == "dm"

    adapter.supports_draft_streaming = _supports

    async def _send_draft(*, chat_id, draft_id, content, metadata=None):
        adapter.draft_calls.append(content)
        adapter.draft_ids.append(draft_id)
        return SendResult(success=True, message_id=None)

    adapter.send_draft = _send_draft

    async def _send(*, chat_id, content, reply_to=None, metadata=None):
        adapter.sent.append(content)
        return SimpleNamespace(success=True, message_id=f"msg_{len(adapter.sent)}")

    adapter.send = _send
    adapter.edit_message = AsyncMock(return_value=SimpleNamespace(success=True))
    return adapter


def _make_consumer(adapter, **cfg_kwargs) -> GatewayStreamConsumer:
    cfg = StreamConsumerConfig(
        transport="auto", chat_type="dm", edit_interval=0.01, buffer_threshold=5,
        cursor="", **cfg_kwargs)
    return GatewayStreamConsumer(adapter, "12345", cfg)


async def _run_turn(consumer, *events) -> None:
    """Feed (kind, payload) events with a short settle between them, then finish.

    kinds: "tool" -> on_tool_progress, "text" -> on_delta, "break" -> on_segment_break,
    "commentary" -> on_commentary, "reasoning" -> on_reasoning."""
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.02)
    for kind, payload in events:
        if kind == "tool":
            consumer.on_tool_progress(payload)
        elif kind == "break":
            consumer.on_segment_break()
        elif kind == "commentary":
            consumer.on_commentary(payload)
        elif kind == "reasoning":
            consumer.on_reasoning(payload)
        else:
            consumer.on_delta(payload)
        await asyncio.sleep(0.05)
    consumer.finish()
    await task


def _final(adapter) -> str:
    """The single turn-final message (collapsed trace block + plain answer)."""
    return adapter.sent[-1]


class TestToolDetailsGating:
    def test_off_by_default(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)
        consumer._use_draft_streaming = True
        assert consumer.accepts_tool_progress is False

    def test_requires_live_stream_transport(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        # No transport resolved yet -> nothing renders the block.
        assert consumer.accepts_tool_progress is False
        consumer._use_draft_streaming = True
        assert consumer.accepts_tool_progress is True

    def test_probe_must_be_literally_true(self):
        adapter = _make_adapter(details_supported=MagicMock())
        consumer = _make_consumer(adapter, tool_progress_details=True)
        consumer._use_draft_streaming = True
        assert consumer.accepts_tool_progress is False

    def test_adapter_without_probe_keeps_legacy_behaviour(self):
        adapter = _make_adapter()
        del adapter.tool_details_supported
        consumer = _make_consumer(adapter, tool_progress_details=True)
        consumer._use_draft_streaming = True
        assert consumer.accepts_tool_progress is False


class TestToolDetailsDraftIdentity:
    """A tool boundary must keep ONE live draft while the cumulative trace block is active.

    Telegram treats a NEW ``draft_id`` as a NEW live draft (core.telegram.org/api/bots/ai:
    "this will add a new live draft along with existing ones"), so bumping at every tool
    boundary made the whole trace+text reappear as a second draft message (visible re-send +
    flicker on mobile).  Without the trace block the segment is finalized as a real message
    first, which clears the old draft — there the bump stays correct.
    """

    @pytest.mark.asyncio
    async def test_tool_boundary_keeps_one_draft_id_with_details(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("text", "preamble "),
            ("tool", "terminal `ls`"),
            ("break", None),
            ("text", "answer"),
        )
        assert adapter.draft_calls, "expected streamed draft frames"
        assert len(set(adapter.draft_ids)) == 1


class TestToolDetailsDelivery:
    @pytest.mark.asyncio
    async def test_final_message_carries_collapsed_trace_then_plain_answer(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("tool", "terminal: ls -la"),
            ("text", "Hello "),
            ("text", "world!"),
        )

        assert len(adapter.sent) == 1, "one message per turn"
        final = adapter.sent[0]
        assert final.startswith("<details><summary>⚙️ 执行 · 1 次工具调用</summary>")
        assert "terminal: ls -la" in final
        assert final.rstrip().endswith("Hello world!")   # answer plain, below the block
        assert "<details open>" not in final

    @pytest.mark.asyncio
    async def test_unterminated_fence_closes_after_the_answer(self):
        """Blocks first + plain answer last => the fence close lands at the very end."""
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(consumer, ("tool", "terminal: ls"), ("text", "```python\nprint(1)"))

        final = adapter.sent[0]
        assert final.rstrip().endswith("```")
        assert final.index("</details>") < final.index("```python")

    @pytest.mark.asyncio
    async def test_text_deltas_do_not_drop_collected_tool_calls(self):
        """The native overlay is cleared by real text; the trace log must survive it."""
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("tool", "terminal: first"),
            ("text", "part 1 "),
            ("tool", "read_file: second"),
            ("text", "part 2"),
        )

        final = _final(adapter)
        assert "terminal: first" in final
        assert "read_file: second" in final
        assert "⚙️ 执行 · 2 次工具调用" in final

    @pytest.mark.asyncio
    async def test_unsafe_block_degrades_to_plain_answer(self):
        """Desktop math-in-details guard rejects the block -> plain answer only."""
        adapter = _make_adapter(block_ok=False)
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(consumer, ("tool", "terminal: ls"), ("text", "answer"))

        assert all("<details" not in frame for frame in adapter.draft_calls)
        assert adapter.sent == ["answer"]

    @pytest.mark.asyncio
    async def test_disabled_keeps_legacy_streaming(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)
        await _run_turn(consumer, ("tool", "terminal: ls"), ("text", "answer"))

        assert all("<details" not in frame for frame in adapter.draft_calls)
        assert adapter.sent == ["answer"]


class TestToolDetailsInterimText:
    """Inter-tool-call interim text (boundary preambles + commentary) folds into the SAME
    trace block as the tool breadcrumbs."""

    @pytest.mark.asyncio
    async def test_boundary_preamble_folds_into_trace(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("tool", "terminal: ls"),
            ("text", "先看一下配置文件。"),
            ("break", None),
            ("text", "配置没问题，这是答案。"),
        )

        final = _final(adapter)
        assert final.startswith("<details><summary>⚙️ 执行 · 1 次工具调用 · 1 段说明</summary>")
        assert "先看一下配置文件。" in final
        assert final.rstrip().endswith("配置没问题，这是答案。")

    @pytest.mark.asyncio
    async def test_commentary_folds_into_trace(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("commentary", "我先确认一下配置。"),
            ("tool", "terminal: ls"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert "我先确认一下配置。" in final
        assert final.startswith("<details><summary>⚙️ 执行")
        assert final.rstrip().endswith("答案。")

    @pytest.mark.asyncio
    async def test_disabled_keeps_preamble_as_its_own_message(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)
        await _run_turn(
            consumer,
            ("text", "先看一下配置文件。"),
            ("break", None),
            ("text", "答案。"),
        )

        assert all("<details" not in m for m in adapter.sent)
        assert any("先看一下配置文件。" in m for m in adapter.sent)

    @pytest.mark.asyncio
    async def test_unsafe_block_keeps_preamble_visible(self):
        """Block rejected by the adapter guard -> the preamble is delivered normally, never
        silently dropped by the fold."""
        adapter = _make_adapter(block_ok=False)
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("text", "先看一下配置文件。"),
            ("break", None),
            ("text", "答案。"),
        )

        assert all("<details" not in m for m in adapter.sent)
        assert any("先看一下配置文件。" in m for m in adapter.sent)


class TestToolDetailsDraftLiveness:
    """Bot API drafts are a ~30s ephemeral preview: the consumer must keep refreshing the
    frame during long tool runs (no text deltas), else the preview goes stale and expires."""

    @pytest.mark.asyncio
    async def test_tool_progress_alone_pushes_a_frame(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.02)
        consumer.on_tool_progress("terminal: slow build")
        await asyncio.sleep(0.1)
        consumer.finish()
        await task

        assert any("slow build" in frame for frame in adapter.draft_calls), (
            "a tool line must refresh the preview even before any text arrives")

    @pytest.mark.asyncio
    async def test_draft_keepalive_refreshes_frame(self, monkeypatch):
        import gateway.stream_consumer as sc_mod
        monkeypatch.setattr(sc_mod, "_DRAFT_KEEPALIVE_SECONDS", 0.01)
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.02)
        consumer.on_delta("hello")
        await asyncio.sleep(0.12)
        consumer.finish()
        await task

        frames = [f for f in adapter.draft_calls if "hello" in f]
        assert len(frames) >= 2, "keepalive must refresh the preview while the turn is alive"


class TestToolDetailsReasoning:
    """The thinking rides INSIDE the trace block as a blockquote: every reasoning segment is
    kept (interleaved thinking after tool calls included), each hard-capped at 200 chars."""

    @pytest.mark.asyncio
    async def test_first_reasoning_segment_renders_as_quote(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("reasoning", "先想一下"),
            ("reasoning", "这个问题的结构。"),
            ("tool", "terminal: ls"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert final.startswith("<details><summary>⚙️ 执行 · 1 次工具调用</summary>")
        assert "> 💭 先想一下这个问题的结构。" in final
        assert "段思考" not in final
        assert final.rstrip().endswith("答案。")
        assert "<details open>" not in final

    @pytest.mark.asyncio
    async def test_later_reasoning_segments_are_kept(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("reasoning", "想第一步。"),
            ("tool", "terminal: a"),
            ("reasoning", "想第二步。"),
            ("commentary", "中间说一句话。"),
            ("reasoning", "想第三步。"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert "想第一步。" in final
        assert "想第二步。" in final
        assert "想第三步。" in final
        assert final.count("💭") == 3
        assert "中间说一句话。" in final
        # Real order preserved: thought → tool → thought → prose → thought.
        assert final.index("想第一步。") < final.index("- terminal: a")
        assert final.index("- terminal: a") < final.index("想第二步。")
        assert final.index("中间说一句话。") < final.index("想第三步。")

    @pytest.mark.asyncio
    async def test_each_reasoning_segment_capped_at_200_chars(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.02)
        consumer.on_reasoning("字" * 400)          # one segment, over the cap
        consumer.on_reasoning("字" * 400)
        await asyncio.sleep(0.05)
        consumer.on_tool_progress("terminal: ls")  # segment boundary
        await asyncio.sleep(0.05)
        consumer.on_reasoning("乙" * 400)          # second segment, capped independently
        await asyncio.sleep(0.05)
        consumer.on_delta("答案。")
        await asyncio.sleep(0.05)
        consumer.finish()
        await task

        final = _final(adapter)
        assert final.count("…") == 2
        start = final.index("> 💭 ") + len("> 💭 ")
        end = final.index("…", start)
        assert final[start:end] == "字" * 200
        start2 = final.index("> 💭 ", end) + len("> 💭 ")
        end2 = final.index("…", start2)
        assert final[start2:end2] == "乙" * 200

    @pytest.mark.asyncio
    async def test_math_guard_drops_thinking_but_keeps_tool_trace(self):
        """Thinking that trips the Desktop math guard is dropped; the tool trace survives."""
        adapter = _make_adapter()
        adapter.tool_details_block_ok = lambda block: "💭" not in block
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("reasoning", "想。"),
            ("tool", "terminal: ls"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert "<details><summary>⚙️ 执行 · 1 次工具调用</summary>" in final
        assert "💭" not in final
        assert "terminal: ls" in final

    @pytest.mark.asyncio
    async def test_trace_stays_open_while_streaming_and_folds_in_the_final(self):
        """No mid-stream fold: whether the current text is the final answer is unknowable
        until the response ends, so every streaming frame keeps the block open and only the
        delivered turn-final message carries it collapsed."""
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.02)
        consumer.on_reasoning("思考中。")
        await asyncio.sleep(0.08)
        consumer.on_tool_progress("terminal: ls")
        await asyncio.sleep(0.08)
        consumer.on_delta("再排查一处风险。")        # short preamble: must NOT fold
        await asyncio.sleep(0.08)
        consumer.on_delta("答案。" * 40)            # long answer: still no mid-stream fold
        await asyncio.sleep(0.1)
        consumer.finish("再排查一处风险。" + "答案。" * 40)
        await task

        frames = [f for f in adapter.draft_calls if "<details" in f]
        assert frames and all("<details open>" in f for f in frames), (
            "the trace must stay open for the whole stream")
        assert "> 💭 思考中。" in frames[0]

        final = adapter.sent[-1]
        assert final.startswith("<details><summary>⚙️ 执行 · 1 次工具调用</summary>")
        assert "<details open>" not in final
        assert final.rstrip().endswith("答案。")

    @pytest.mark.asyncio
    async def test_reasoning_ignored_when_disabled(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)
        task = asyncio.create_task(consumer.run())
        await asyncio.sleep(0.02)
        consumer.on_reasoning("thinking text")
        await asyncio.sleep(0.05)
        consumer.on_delta("answer")
        await asyncio.sleep(0.05)
        consumer.finish()
        await task

        assert all("💭" not in content for content in adapter.sent)


    @pytest.mark.asyncio
    async def test_unbalanced_fence_in_thinking_is_closed_inside_the_block(self):
        """An orphan fence in the thinking/prose must close INSIDE the block, not after the
        answer (the whole-payload pass would otherwise append it at the very end)."""
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("reasoning", "看这段代码\n```python\nprint(1)"),   # unclosed
            ("tool", "terminal: ls"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert final.count("```") % 2 == 0
        assert final.rstrip().endswith("答案。")
        assert final.index("```python") < final.index("</details>")

    def test_math_in_answer_does_not_trip_the_details_guard(self):
        """The answer is outside <details>, so math in it is fine; math INSIDE a block
        still trips the Desktop crash guard."""
        from plugins.platforms.telegram.adapter import TelegramAdapter

        adapter = TelegramAdapter.__new__(TelegramAdapter)
        outside = ("<details><summary>⚙️ 执行</summary>\n\n- x\n\n</details>\n\n"
                   "答案含数学 $E=mc^2$ 与 $$\\frac{1}{2}$$")
        assert adapter._has_telegram_desktop_details_math_crash_shape(outside) is False
        inside = ("<details><summary>⚙️ 执行</summary>\n\n$$\\frac{1}{2}$$\n\n</details>\n\n答案")
        assert adapter._has_telegram_desktop_details_math_crash_shape(inside) is True

    @pytest.mark.asyncio
    async def test_reconcile_edit_re_decorates_with_block(self):
        """The stale-finalize reconcile replaces the whole message; it must re-apply the
        collapsed trace block, else the turn's trace vanishes."""
        from types import SimpleNamespace
        from gateway.run_turn import GatewayTurnMixin

        edited = {}

        class _Adapter:
            async def edit_message(self, *, chat_id, message_id, content, finalize):
                edited["content"] = content
                return SimpleNamespace(success=True)

        class _Sc:
            message_id = "m1"
            adapter = _Adapter()

            def _final_payload(self, text):
                return f"<details><summary>⚙️ 执行 · 2 次工具调用</summary>\n\n- x\n\n</details>\n\n{text}"

        response = {}
        await GatewayTurnMixin._run_agent_edit_streamed_message(
            SimpleNamespace(), _Sc(), SimpleNamespace(chat_id="1"), response, "answer",
            _sk="s", ok=("reconciled",), fail_result=None, fail_exc="fail")

        assert edited["content"].startswith("<details>")
        assert edited["content"].rstrip().endswith("answer")
        assert response.get("already_sent") is True


class TestToolDetailsFooter:
    """The runtime footer rides the turn-final message instead of a trailing send."""

    @pytest.mark.asyncio
    async def test_footer_appended_to_final_message(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        consumer.set_footer("`dsv4flash` · 12% · 3.4s")
        await _run_turn(consumer, ("tool", "terminal: ls"), ("text", "答案。"))

        assert len(adapter.sent) == 1
        final = adapter.sent[0]
        assert final.rstrip().endswith("`dsv4flash` · 12% · 3.4s")
        assert consumer.footer_taken is True

    @pytest.mark.asyncio
    async def test_footer_excluded_from_delivery_record(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        consumer.set_footer("FOOTER")
        await _run_turn(consumer, ("tool", "terminal: ls"), ("text", "answer"))

        assert consumer._delivered_final_text == "answer"
        assert consumer.delivered_final_matches("answer") is True

    @pytest.mark.asyncio
    async def test_footer_rides_plain_message_when_details_off(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)          # details block off
        consumer.set_footer("FOOTER")
        await _run_turn(consumer, ("text", "answer"))

        assert adapter.sent == ["answer\n\nFOOTER"]
        assert consumer.footer_taken is True


class TestToolDetailsReconcile:
    """The gateway reconciles the recorded final against ``final_response``.  Draft frames
    carry the block, so the recorded payload must strip it or the reconcile fires."""

    @pytest.mark.asyncio
    async def test_recorded_final_excludes_injected_block(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(consumer, ("reasoning", "想。"), ("tool", "terminal: ls"), ("text", "answer"))

        assert consumer._delivered_final_text == "answer"
        assert consumer.delivered_final_matches("answer") is True
        assert consumer.delivered_final_matches("something else") is False

    def test_strip_drops_leading_blocks_only(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter)
        assert consumer._strip_tool_details_block(
            "<details><summary>💭 思考</summary>\n\n想。\n\n</details>\n\n"
            "<details><summary>⚙️ 执行 · 1 次工具调用</summary>\n\n- a\n\n</details>\n\nanswer") == "answer"
        # A user-authored block inside the answer is content, not decoration.
        inline = "answer with <details>inline</details> text"
        assert consumer._strip_tool_details_block(inline) == inline
        assert consumer._strip_tool_details_block("") == ""


class TestFooterMonospace:
    """Local patch: the runtime footer renders as an inline code span (mono) on the
    turn-final message, not as body text."""

    def _line(self, monkeypatch, result, *, platform=None):
        import gateway.run as gateway_run
        from gateway.config import Platform
        from gateway.run_turn import GatewayTurnMixin

        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
            "display": {"runtime_footer": {"enabled": True, "fields": ["model", "context_pct"]}}})
        monkeypatch.setattr(gateway_run, "_terminal_scope_cwd", lambda *a, **k: "")
        return GatewayTurnMixin._hmwa_runtime_footer_line(
            SimpleNamespace(), result,
            SimpleNamespace(platform=platform or Platform.TELEGRAM), 3.0)

    def test_footer_line_is_wrapped_in_inline_code(self, monkeypatch):
        line = self._line(monkeypatch, {
            "model": "deepseek/deepseek-v4-flash", "last_prompt_tokens": 42, "context_length": 100})

        assert line == "`DSv4flash · 42%`"
        assert line.startswith("`") and line.endswith("`")

    def test_disabled_footer_stays_empty(self, monkeypatch):
        import gateway.run as gateway_run
        from gateway.config import Platform
        from gateway.run_turn import GatewayTurnMixin

        monkeypatch.setattr(gateway_run, "_load_gateway_config",
                            lambda: {"display": {"runtime_footer": {"enabled": False}}})
        monkeypatch.setattr(gateway_run, "_terminal_scope_cwd", lambda *a, **k: "")

        line = GatewayTurnMixin._hmwa_runtime_footer_line(
            SimpleNamespace(), {"model": "gpt-5.4", "last_prompt_tokens": 1, "context_length": 10},
            SimpleNamespace(platform=Platform.TELEGRAM), 1.0)

        assert line == ""


class TestFooterNotSentTwice:
    """The footer rides the streamed turn-final message, so the delivery path must be told —
    the runner's inner result dict never reaches it (live bug: two footers in the chat)."""

    def _mark(self, consumer):
        from gateway.run_turn import GatewayTurnMixin

        turn_ctx = SimpleNamespace(
            stream_consumer_holder=[consumer],
            source=SimpleNamespace(chat_id="1"), session_key="s")
        self_ = SimpleNamespace(_run_agent_stream_confirmed_final_delivery=lambda *a, **k: True)
        response = {"final_response": "answer", "failed": False}
        return response, GatewayTurnMixin._run_agent_mark_streamed_delivery(self_, response, turn_ctx)

    @pytest.mark.asyncio
    async def test_streamed_footer_is_recorded_on_the_result(self):
        response, coro = self._mark(SimpleNamespace(footer_taken=True))
        await coro

        assert response["footer_streamed"] is True
        assert response["already_sent"] is True

    @pytest.mark.asyncio
    async def test_no_footer_queued_leaves_the_flag_false(self):
        response, coro = self._mark(SimpleNamespace(footer_taken=False))
        await coro

        assert response["footer_streamed"] is False

    @pytest.mark.asyncio
    async def test_streamed_footer_is_not_sent_trailing(self):
        from gateway.run_turn import GatewayTurnMixin

        adapter = SimpleNamespace(send=AsyncMock())
        self_ = SimpleNamespace(
            _adapter_for_source=lambda source: adapter,
            _should_send_voice_reply=lambda *a, **k: False,
            _deliver_media_from_response=AsyncMock(),
            _event_thread_metadata=lambda event, source: None,
        )
        out = await GatewayTurnMixin._hmwa_deliver_turn_response(
            self_, SimpleNamespace(), SimpleNamespace(chat_id="1"), SimpleNamespace(session_id="s"),
            "sk", 1, {"already_sent": True, "footer_streamed": True}, [], "answer", "`f`", False)

        assert out is None
        assert adapter.send.await_count == 0

    @pytest.mark.asyncio
    async def test_unstreamed_footer_still_sent_trailing(self):
        from gateway.run_turn import GatewayTurnMixin

        adapter = SimpleNamespace(send=AsyncMock())
        self_ = SimpleNamespace(
            _adapter_for_source=lambda source: adapter,
            _should_send_voice_reply=lambda *a, **k: False,
            _deliver_media_from_response=AsyncMock(),
            _event_thread_metadata=lambda event, source: None,
        )
        await GatewayTurnMixin._hmwa_deliver_turn_response(
            self_, SimpleNamespace(), SimpleNamespace(chat_id="1"), SimpleNamespace(session_id="s"),
            "sk", 1, {"already_sent": True}, [], "answer", "`f`", False)

        assert adapter.send.await_count == 1
        assert adapter.send.await_args.args[1] == "`f`"


class TestToolBreadcrumbOneLine:
    """A tool bullet must be ONE line.  Terminal progress arrives as a fenced block; a bullet
    carrying a fence/newline breaks the list item and the italic header swallows the next
    line (live format bug in the collapsed trace)."""

    def test_fenced_terminal_breadcrumb_flattens_to_one_line(self):
        from gateway.stream_consumer import GatewayStreamConsumer

        text = ('*💻 terminal*\n```\ncd /root/.hermes/hermes-agent && grep -n "write=" '
                'evals/postmortem/forensics/logcalls.py | head -25\n```')
        line = GatewayStreamConsumer._breadcrumb_line(text)

        assert "\n" not in line and "```" not in line
        assert line.startswith("*💻 terminal* `") and line.endswith("`")

    def test_single_line_breadcrumb_is_unchanged(self):
        from gateway.stream_consumer import GatewayStreamConsumer

        assert (GatewayStreamConsumer._breadcrumb_line("*🔧 Editing /root/x.py*")
                == "*🔧 Editing /root/x.py*")

    def test_backticks_in_command_cannot_close_the_code_span(self):
        from gateway.stream_consumer import GatewayStreamConsumer

        line = GatewayStreamConsumer._breadcrumb_line("*💻 terminal*\n```\necho `date`\n```")

        assert line.count("`") == 2
        assert "echo 'date'" in line

    @pytest.mark.asyncio
    async def test_terminal_breadcrumb_stays_one_line_in_the_block(self):
        adapter = _make_adapter()
        consumer = _make_consumer(adapter, tool_progress_details=True)
        await _run_turn(
            consumer,
            ("tool", "*💻 terminal*\n```\ncd /root/x && ls\n```"),
            ("text", "答案。"),
        )

        final = _final(adapter)
        assert "- *💻 terminal* `cd /root/x && ls`" in final
        assert "```\ncd /root/x && ls" not in final


class TestStreamFooterCarriesUsage:
    """The streamed footer is built BEFORE the runner's usage-merged return dict exists, so its
    source must merge the post-run usage itself — live bug: the sealed message showed only
    ``model · 3s · ~`` (no context %, no tps).  The tps denominator is the bench-style decode
    window (first→last streamed delta), with request time as the non-streaming fallback."""

    def _line(self, monkeypatch, result, usage, seconds=3.4):
        import gateway.run as gateway_run
        from gateway.config import Platform
        from gateway.run_turn import GatewayTurnMixin
        from gateway.run_turn_runner import TurnRunner

        monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {
            "display": {"runtime_footer": {"enabled": True,
                                           "fields": ["model", "context_pct", "tps"]}}})
        monkeypatch.setattr(gateway_run, "_terminal_scope_cwd", lambda *a, **k: "")
        return GatewayTurnMixin._hmwa_runtime_footer_line(
            SimpleNamespace(), TurnRunner._footer_source(result, usage),
            SimpleNamespace(platform=Platform.TELEGRAM), seconds)

    def test_footer_source_merges_post_run_usage(self, monkeypatch):
        # Shape of the agent result the footer is built from mid-run: model + prompt tokens only.
        result = {"model": "gpt-5.4", "last_prompt_tokens": 40_000}
        usage = {"last_prompt_tokens": 40_000, "context_length": 100_000, "turn_output_tokens": 500,
                 "turn_decode_seconds": 2.5, "turn_api_seconds": 3.4}

        assert self._line(monkeypatch, result, usage) == "`gpt-5.4 · 40% · 200t/s`"

    def test_tps_uses_request_time_when_nothing_streamed(self, monkeypatch):
        result = {"model": "gpt-5.4", "last_prompt_tokens": 40_000}
        usage = {"last_prompt_tokens": 40_000, "context_length": 100_000, "turn_output_tokens": 500,
                 "turn_api_seconds": 2.5}

        assert self._line(monkeypatch, result, usage) == "`gpt-5.4 · 40% · 200t/s`"

    def test_tps_hidden_without_any_timing(self, monkeypatch):
        result = {"model": "gpt-5.4", "last_prompt_tokens": 40_000}
        usage = {"last_prompt_tokens": 40_000, "context_length": 100_000, "turn_output_tokens": 500}

        assert self._line(monkeypatch, result, usage) == "`gpt-5.4 · 40%`"
