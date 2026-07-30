# README.md — 橘宝 MEOW persona bundle

> 橘宝（七彩虹 COLORFIRE MEOW 系列 IP）学生&职场新人全能AI创作助手 persona 包。

## 文件清单与 Hermes 加载关系

| 文件 | 用途 | Hermes 自动加载？ | 部署位置 |
|---|---|---|---|
| `SOUL.md` | 人设核心，第一人称，**自我包含**（含纪律+工作模式） | ✅ 是 | `~/.hermes/SOUL.md`（默认家） |
| `USER.md` | 用户画像模板（运行时填充） | ✅ 是 | `~/.hermes/memories/USER.md` |
| `MEMORY.md` | 长期记忆种子 + 写入规则 | ✅ 是 | `~/.hermes/memories/MEMORY.md` |
| `AGENTS.md` | 主 + 5 子 Agent 编排（人类参考） | 否（已内联进 SOUL.md） | 仓库参考，不部署 |
| `DISCIPLINE.md` | 纪律边界（人类参考） | 否（已内联进 SOUL.md） | 仓库参考，不部署 |
| `IDENTITY.md` | 身份卡（人类阅读） | 否 | 参考文档 |
| `TOOLS.md` | 工具架构参考 | 否 | 参考文档 |
| `BOOTSTRAP.md` | 首次运行引导 | 否 | 参考文档 |
| `HEARTBEAT.md` | 日/周/月巡检清单 | 否 | 参考文档 |

> **重要：** 部署目标是默认家 `~/.hermes/`，不是独立 profile。这样不带任何 `-p` 参数的 `hermes` 命令本身就是橘宝。仓库根 `AGENTS.md`（开发指南）与 `data/SOUL.md`（如存在）均不动。

## 快速部署

```bash
# 1. 写入 persona 文件到默认家
cp canonical/SOUL.md   ~/.hermes/SOUL.md
cp canonical/USER.md   ~/.hermes/memories/USER.md
cp canonical/MEMORY.md ~/.hermes/memories/MEMORY.md

# 2. 冒烟测试（Hermes CLI 安装后）
hermes
# 验证：banner 显示橘宝人设；/soul 显示 canonical SOUL.md；/memory 显示 USER+MEMORY 种子
```

## 核心 disciplinary 来源

所有禁止事项、需要确认的操作、安全边界、工具权限矩阵**已全部内联进 `SOUL.md`「纪律边界」一节**，运行时模型直接在 SOUL.md 中看到完整规则，无外部文件依赖。`DISCIPLINE.md` 与 `AGENTS.md` 仅作人类参考留存仓库。冲突时按 `SOUL.md`「决策原则」裁决。