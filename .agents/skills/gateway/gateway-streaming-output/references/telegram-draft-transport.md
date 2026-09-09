# Telegram draft 传输（`sendMessageDraft` / `sendRichMessageDraft`）

## 机制

- Bot API 9.5+ 的私聊流式预览：`send_draft(chat_id, draft_id, content)`。**只有 private/dm
  可用**（`TelegramAdapter.supports_draft_streaming` 按 `chat_type` 判断），群/频道回落 edit。
- 草稿**没有 message_id**，是临时预览（~30s TTL），靠 `_maybe_keepalive_draft()` 每
  `_DRAFT_KEEPALIVE_SECONDS` 重发同一帧续命。
- **发一条真消息会清掉当前草稿**——这是草稿生命周期里唯一"收尾"手段。
- `draft_id` 是草稿身份，语义（两处官方文档口径略有差异，结论一致：换 id 就没有平滑动画）：
  - MTProto（core.telegram.org/api/bots/ai）：同 `random_id` → 原地更新、淡入新增字符；
    换 `random_id` → **在聊天里追加一条新的 live draft（旧的还在）**。
  - Bot API / aiogram：同 `draft_id` → 变更被动画；**否则草稿被无动画替换**。
  - 共同点：换 id ⇒ 客户端不是平滑更新，而是把整段内容重新呈现一次。

## 为什么代码里会在工具边界换 id

`stream_consumer._reset_segment_state()` / `_fold_segment_into_details()` 里的 `_bump_draft_id()`
来自这个设计：**每个工具调用前的文本块先 finalize 成一条真消息，下一段再开一条新草稿动画**，
避免"跨工具调用的文本泄漏到下一段预览"（openclaw #32535 那类问题）。

成立的前提是：**这一段确实被当成真消息发出去了**。真消息一发出，旧草稿自动消失，换 id 才安全。

## 累积内容会破坏这个前提（踩过的坑）

折叠 trace 块（`tool_progress_details`）是**累积**的：工具边界把这段文本 fold 进 `<details>`，
**不发真消息**。此时再 `_bump_draft_id()`：

- 旧草稿没人清（没有真消息），
- 新 id 的帧又带着同一份 trace + 正文，
- 结果：**每次调用工具，整段内容重新出现一次，手机端闪屏**。

修法：`_tool_details_active()` 为真时**不换 id**，整轮共用一个草稿、就地动画更新；最终消息
（真 send，带折叠块）发出去时草稿自然被清。

回归测试：`tests/gateway/test_stream_tool_details.py::TestToolDetailsDraftIdentity`
（断言一次工具边界后 `draft_ids` 仍只有一个值）。

## 排查清单

1. `adapter.draft_ids` 在一次 turn 里是否只有一个值？多了就是换了 id。
2. 换 id 的那条路径，之前是否真的发过真消息？（`adapter.sent` 里有对应文本吗）
3. `supports_draft_streaming` 是否因 chat_type 不是 dm 而回落 edit —— 回落时
   `_tool_details_active()` 为 False，折叠块根本不会进帧（见 SKILL.md 不变量 2）。
