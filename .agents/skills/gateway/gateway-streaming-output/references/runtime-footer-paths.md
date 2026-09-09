# Runtime footer 的三条投递路径

`display.runtime_footer.fields` 渲染出一行元信息（model / context_pct / latency / tps / cwd），
由 `gateway/runtime_footer.py` 生成。它必须**每轮只出现一次**，且**不能丢**。

## 谁负责发

| # | 路径 | 触发条件 | 代码 |
|---|---|---|---|
| A | 随最终消息 | 流式 turn-final | `stream_consumer.set_footer()` → `_final_payload()` 拼进正文 |
| B | 追加到 `response` | 未流式（`not already_sent`） | `run_turn` 里 `response = f"{response}\n\n{footer}"` |
| C | 尾部补发 | 流式已投递、但 footer 没随行 | `run_turn._hmwa_deliver_turn_response` 里的 trailing send |

A 是唯一"真路径"；B/C 是兜底。`stream_consumer._record_turn_final_payload()` 会剥掉正文末尾的
footer 再记录，否则 gateway 的对账（`delivered_final_matches`）会认为投递不匹配而重发一遍。

## 标志语义（这里踩过坑）

- `consumer.footer_taken` → `run_turn` 把它写进结果字典的 `footer_streamed`。
- 语义必须是「**footer 已随某条成功投递的消息发出**」，**不能**是「已排队」。
  因为 A 之外的最终投递路径（`_send_fallback_final`、`_send_empty_fallback_final`）**绕过
  `_final_payload`、不带 footer**：
  - 若它们投递成功（`_already_sent=True`）却把 `footer_streamed` 报成 True，
  - gateway 就会跳过 C（尾部补发）→ **footer 静默消失，日志里什么都没有**。
- 修法：这些绕过路径置 `_footer_missed=True`，`footer_taken` 返回
  `bool(self._footer_line) and not self._footer_missed`。这样：
  - 它们投递成功 → `footer_taken=False` → C 补发一次；
  - 它们没投递成功 → 走 B 把 footer 拼进 `response` → 也只出现一次（B 和 C 互斥）。

## 改动规则

- 任何新的"最终消息出口"要么带上 `_footer_line`，要么显式标记 `_footer_missed`——
  否则 footer 会静默丢失。
- 别在流式帧里放 footer：帧会被反复重发/替换，footer 会跟着抖。

## 测试

`tests/gateway/test_stream_tool_details.py::TestToolDetailsFooter`：
正常路径断言 footer 在最终消息末尾且 `footer_taken is True`；
降级路径断言 fallback 续发的消息**不含** footer 且 `footer_taken is False`。
