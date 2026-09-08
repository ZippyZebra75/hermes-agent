# Project skills (`.agents/skills`)

Hermes 在这个 repo 里干活时用的项目级 skill（Boss 约定 2026-09-09）。
**本目录提交进 git、随项目走**——clone/换机器/分享都会带着。

## Rules

- 项目相关知识（架构 / 约定 / 踩坑 / 本地补丁脉络 / 维护流程）→ 写成 skill 放**这里**；
  不建全局 profile skill。全局 USER.md 只放跨项目用户级规则，保持干净。
- 结构：`<category>/<skill>/SKILL.md`（frontmatter `name` 即 skill 名），
  深度内容放 `references/`、模板放 `templates/`、脚本放 `scripts/`。
- 加载条件：cwd 在此 checkout 内 + repo 已被 trust
  （`hermes --profile her skills trust`，状态在 profile config 的 `skills.trusted_project_dirs`）。
- 内容规范：procedure first、pitfall = 规则 + WHY、不写日期/PR 号叙事、同一条教训只留一条。
- **边界**：本机私有运维信息（profile 实况、token、thread/chat id、个人约定细节）**不要提交**——
  那些放 gitignore 的 `.hermes/skills/`（本机私有项目笔记）。
