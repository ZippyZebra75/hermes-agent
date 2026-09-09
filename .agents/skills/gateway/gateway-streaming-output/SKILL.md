---
name: gateway-streaming-output
description: Use when 改 gateway 流式输出（Telegram draft / trace 折叠块 / runtime footer）。三条不变量 + 踩坑。
category: gateway
tags:
  - gateway
  - streaming
  - telegram
  - draft
  - footer
  - trace
---

# Gateway 流式输出（draft / trace 折叠块 / footer）

## 何时用

改 `gateway/stream_consumer*.py`、`gateway/run_turn*.py` 的流式投递，或
`plugins/platforms/telegram/adapter.py` 的 draft/rich 路径；动 `<details>` 折叠 trace 块、
runtime footer、tool-progress 面包屑时先读本文。

## 数据流（先记住这条链）

```
run_turn_runner._progress_emit        # 工具开始时的进度行（结束/失败没有事件）
  → GatewayStreamConsumer 队列 → run() 每 tick
      _should_edit → _push_update → _send_or_edit
          transport 顺序：native → draft → edit → first-send
  → got_done: _finalize_turn → _finalize_edit / _send_fallback_final
      _final_payload()  ← 唯一拼「折叠 trace 块 + runtime footer」的地方
```

## 三条不变量

### 1. Telegram `draft_id`：同 id 原地动画，换 id 整段重新出现

`draft_id` 是草稿身份。同 id 更新 → 客户端原地淡入新字符；**换 id → 平滑动画没了**：
MTProto 文档说会追加一条新 live draft，Bot API 文档说会无动画替换——两种表现都会让
整段内容重新出现一次。

推论：**累积型内容（trace 块）整轮只能用同一个 draft_id**。任何"每个工具边界换草稿"
的 `_bump_draft_id()` 都只有在旧草稿已被真消息清掉时才成立（发真消息会自动清草稿）。
trace 块在工具边界是 fold 进块里、不发真消息的 → 那里换 id 就会整段重发。
细节与症状：`references/telegram-draft-transport.md`。

### 2. trace 块的两个门不能混用

- `_tool_details_active()` — **流式帧**能不能渲染块：要求 live stream transport
  （native 或 draft）+ 适配器探针通过。
- `_final_details_available()` — **最终消息**能不能带块：只要求 rich **SEND** 可用。

把最终的门写成 `_tool_details_active()`，draft 中途失败就会让已收集的 trace 静默消失——
最终消息本来就是 rich send，跟 draft 还活不活着无关。

### 3. footer 只有一条真路径：`_final_payload`

footer 只在 `_final_payload()`（`finalize and is_turn_final` 的 `_send_or_edit`）里拼进正文。
另外两条：未流式时追加到 `response` 末尾；流式已投递但 footer 没随行时由 gateway 尾部补发。

`footer_taken` / `footer_streamed` 必须表示「**已随成功投递的消息发出**」，不能表示「已排队」：
fallback 续发（`_send_fallback_final` / `_send_empty_fallback_final`）绕过 `_final_payload`、
不带 footer，若仍报 True，gateway 会跳过尾部补发 → footer 静默丢失。
细节：`references/runtime-footer-paths.md`。

## 踩坑（规则 + WHY）

- **帧首字段不能每 tick 变**：`<summary>` 在块首，秒级变化会让整帧非前缀，客户端只能整帧
  重渲染（草稿看着在跳）。耗时类字段只放最终消息。
- **trace 块超限要降级，不要整块丢**：降级链 = 全量 → 丢思考引用（Desktop
  math-in-details crash guard）→ 逐条丢**最旧**条目 + 省略说明。整块 `return ""` 会让用户
  彻底看不到执行过程，且 `_fold_segment_into_details()` 会把 preamble 当独立消息发出去。
- **别把 flush 屏障当 segment break**：`_Tick.is_interim = not got_done and not
  got_segment_break`，而 flush 会置 `got_segment_break=True` → flush tick 投递的是
  `_accumulated` **纯文本**（不带 trace 块）。怀疑"屏障导致 trace 重复"之前先断言
  `adapter.sent` 的实际内容。
- **判断"用户会看到什么"用 `adapter.sent` / `adapter.draft_calls`**，别读私有状态推演：
  一个"看起来显然"的重复/丢失，可能因为 `is_interim`、`_fold_break` 这类门根本不发生。
- **修之前先写红测试**：base 上红、修复后绿才算证完；只有绿过的测试才能当不变量。

## 测试落点

- `tests/gateway/test_stream_tool_details.py` — trace 块 / footer / 摘要；`_make_adapter`
  提供 `draft_calls`、`draft_ids`、`sent`，可直接断言帧内容与消息数。
- `tests/gateway/test_stream_consumer_draft.py` — draft 传输与降级。
- `tests/gateway/test_stream_final_contract.py` — 最终投递契约。

跑法：`scripts/run_tests.sh tests/gateway/test_stream_tool_details.py`（runner 是文件粒度，
`-k` 只做筛选）。
