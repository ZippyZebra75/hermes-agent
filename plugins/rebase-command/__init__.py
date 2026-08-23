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
    code, out, err = _run(["fetch", remote])
    if code != 0:
        return (
            "\n".join(lines)
            + f"\n❌ `git fetch {remote}` 失败：\n```\n{(err or out).strip()[:1500]}\n```"
        )
    summary = " ".join(l.strip() for l in out.splitlines() if l.strip())
    lines.append(f"✅ `git fetch {remote}` 完成" + (f" — {summary}" if summary else ""))

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
    _run(["rebase", "--abort"])
    msg = "\n".join(lines) + f"\n❌ `git rebase {remote}/{branch}` 失败"
    if conflicts:
        msg += "，冲突：\n```\n" + "\n".join(conflicts[:15]) + "\n```"
    else:
        msg += f"：\n```\n{(err or out).strip()[:1500]}\n```"
    return msg + "\n已执行 `git rebase --abort` 回滚，工作树保持原状。"


def register(ctx) -> None:
    ctx.register_command(
        "rebase",
        handler=_handle_rebase,
        description="Fetch upstream and rebase the local-patches fork branch onto upstream/main",
        args_hint="[remote] [branch]",
    )
