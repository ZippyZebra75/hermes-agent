"""rebase-command plugin — /rebase fetches upstream and rebases the fork branch.

Track upstream (``git fetch``) then replay local commits onto upstream's main
branch (``git rebase upstream/main``).

Safe defaults (aligned with the Boss's rollback-on-error rule):

- refuses to run while the working tree has uncommitted changes;
- on rebase conflict or any failure, runs ``git rebase --abort`` so the
  working tree is restored to its pre-rebase state — never left half-rebased;
- only touches the LOCAL branch; syncing the remote fork (``--force-with-lease``)
  is left to the user.
"""

from __future__ import annotations

import os
import subprocess

REPO = "/root/.hermes/hermes-agent"


def _run(args, timeout=180):
    try:
        proc = subprocess.run(
            ["git", "-C", REPO] + args,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "command timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return -1, "", str(exc)


def _build_negotiation_report(remote, branch, conflicted_files, max_files=6):
    """Per-file 'what upstream changed vs what my patches wanted', post-abort.

    merge-base is computed AFTER ``git rebase --abort`` — HEAD is restored to
    its pre-rebase state, so the divergence point is identical to the one the
    rebase used. Presents three ways out (drop / merge / re-apply) without
    auto-deciding: classifying a conflict requires knowing the local patch's
    intent, which is a human (or agent) judgment, not a mechanical one.
    """
    _, base_out, _ = _run(["merge-base", "HEAD", f"{remote}/{branch}"])
    base = base_out.strip()
    if not base:
        return "\n\n⚠️ 无法计算 merge-base，跳过协商报告。"
    files = [f for f in conflicted_files if f.strip()][:max_files]
    if not files:
        return ""
    parts = ["", "🧭 协商报告 — 上游改了什么 vs 我的 patch 要什么"]
    for f in files:
        parts += ["", f"📄 `{f}`"]
        # ① upstream's change to this file since the divergence point.
        _, ustat, _ = _run(["diff", "--stat", f"{base}..{remote}/{branch}", "--", f])
        _, ulog, _ = _run(["log", "--oneline", f"{base}..{remote}/{branch}", "--", f])
        ulines = [l.strip() for l in ulog.splitlines() if l.strip()][:4]
        ustat_lines = ustat.strip().splitlines()
        parts.append(
            "  ① 上游改动：" + (f" `{ustat_lines[-1].strip()}`" if ustat_lines else " (无 stat)")
        )
        for l in ulines:
            parts.append("     · " + l)
        # ② my local patches touching this file.
        _, mstat, _ = _run(["diff", "--stat", f"{base}..HEAD", "--", f])
        _, mlog, _ = _run(["log", "--oneline", f"{base}..HEAD", "--", f])
        mlines = [l.strip() for l in mlog.splitlines() if l.strip()][:4]
        mstat_lines = mstat.strip().splitlines()
        parts.append(
            "  ② 我的 patch：" + (f" `{mstat_lines[-1].strip()}`" if mstat_lines else " (无 stat)")
        )
        for l in mlines:
            parts.append("     · " + l)
        parts.append("  ③ 取舍：丢（上游已覆盖用例）/ 合（取交集）/ 重打（无关重构）")
    if len(conflicted_files) > max_files:
        parts.append(f"  …另有 {len(conflicted_files) - max_files} 个文件，需要完整 diff 就说一声")
    return "\n".join(parts)


def _handle_rebase(raw_args=None) -> str:
    args = (raw_args or "").strip().split()
    remote = args[0] if len(args) > 0 else "upstream"
    branch = args[1] if len(args) > 1 else "main"

    if not os.path.isdir(os.path.join(REPO, ".git")):
        return f"❌ 不是 git repo：`{REPO}`"

    # Guard 1: working tree must be clean — never rebase over uncommitted work.
    code, out, err = _run(["status", "--porcelain"])
    if code != 0:
        return f"❌ `git status` 失败：{(err or out).strip()[:500]}"
    changes = [l for l in out.splitlines() if l.strip()]
    if changes:
        preview = "\n".join("  `" + l + "`" for l in changes[:10])
        return (
            f"❌ 工作树有未提交改动，已中止（先 commit 或 stash 再跑 /rebase）：\n"
            f"{preview}"
        )

    lines = [f"🔄 /rebase — `{remote}/{branch}` @ `{REPO}`", ""]

    # Step 1: track upstream.
    # git fetch output only shows ref ranges (old..new), never a commit count —
    # record the remote ref position before fetching so we can report how many
    # commits the fetch actually brought in.
    _, before, _ = _run(["rev-parse", "--verify", "--quiet", f"{remote}/{branch}"])
    code, out, err = _run(["fetch", remote])
    if code != 0:
        return (
            "\n".join(lines)
            + f"\n❌ `git fetch {remote}` 失败：\n```\n{(err or out).strip()[:1500]}\n```"
        )
    _, after, _ = _run(["rev-parse", "--verify", "--quiet", f"{remote}/{branch}"])
    fetched_n = ""
    if after.strip():
        if before.strip() and before.strip() != after.strip():
            # Normal case: ref moved old..new — count commits in that range.
            _, n, _ = _run(["rev-list", "--count", f"{before.strip()}..{after.strip()}"])
            fetched_n = n.strip()
        elif before.strip():
            # Ref unchanged — nothing new to fetch.
            fetched_n = "0"
        else:
            # First fetch of this ref (no pre-fetch position) — everything is new.
            _, n, _ = _run(["rev-list", "--count", after.strip()])
            fetched_n = n.strip()
    summary = " ".join(l.strip() for l in out.splitlines() if l.strip())
    msg = f"✅ `git fetch {remote}` 完成"
    if fetched_n and fetched_n != "0":
        msg += f" — 抓取到 `{fetched_n}` 个 commit"
    elif fetched_n == "0":
        msg += " — 已是最新"
    if summary:
        msg += f" — {summary}"
    lines.append(msg)

    # Step 2: rebase onto remote branch.
    code, out, err = _run(["rebase", f"{remote}/{branch}"])
    if code == 0:
        _, after, _ = _run(["rev-parse", "HEAD"])
        _, ahead, _ = _run(["rev-list", "--count", f"{remote}/{branch}..HEAD"])
        _, behind, _ = _run(["rev-list", "--count", f"HEAD..{remote}/{branch}"])
        lines.append("")
        lines.append(f"✅ rebase 成功：HEAD `{after.strip()[:12]}`")
        lines.append(f"   - 领先 `{remote}/{branch}`：`{ahead.strip()}` commit(s)")
        lines.append(f"   - 落后 `{remote}/{branch}`：`{behind.strip()}` commit(s)")
        lines.append("")
        lines.append(
            "> ⚠️ 本地分支已改写，远端 fork 落后 —— 需自行确认后 "
            "`git push --force-with-lease origin local-patches` 同步。"
        )
        return "\n".join(lines)

    # Failure path: always abort so nothing is left half-applied.
    conflicts = [
        l.strip() for l in (out + err).splitlines() if "CONFLICT" in l
    ]
    # Conflicted files only exist as unmerged index entries mid-rebase —
    # capture them BEFORE abort.
    _, unmerged_out, _ = _run(["diff", "--name-only", "--diff-filter=U"])
    conflicted_files = [l.strip() for l in unmerged_out.splitlines() if l.strip()]
    _run(["rebase", "--abort"])
    msg = "\n".join(lines) + f"\n❌ `git rebase {remote}/{branch}` 失败"
    if conflicts:
        msg += "，冲突：\n```\n" + "\n".join(conflicts[:15]) + "\n```"
    else:
        msg += f"：\n```\n{(err or out).strip()[:1500]}\n```"
    msg += "\n已执行 `git rebase --abort` 回滚，工作树保持原状。"
    if conflicted_files or conflicts:
        msg += _build_negotiation_report(remote, branch, conflicted_files)
    return msg


def register(ctx) -> None:
    ctx.register_command(
        "rebase",
        handler=_handle_rebase,
        description="Fetch upstream and rebase the local-patches fork branch onto upstream/main",
        args_hint="[remote] [branch]",
    )
