# hermes-skill-cleaner Run Reference

## 运行

脚本自动发现 `~/.hermes/skills`（无需 `--root`）。会话数据在 `~/.hermes/sessions/*.{json,jsonl}`。

```bash
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts 2>&1
```

## 关键指标

| 指标 | 键 | 示例 |
|------|-----|------|
| 预算 | 2% of context_window | 5,440 tokens for 272k context |
| 描述字符数 | `description_chars` | 11,619 |
| 渲染行字符数 | `rendered_line_chars` | 20,352 |
| 截断字符数 | `truncated_description_chars` | 0 |
| 技能发现数 | `skills: N discovered` | 72 |
| 日志文件扫描数 | `log_files_scanned` | 158 |
| 预算使用率 | `unbudgeted_used_of_2%_budget` | 97.2% |

## 推荐清理工作流

经过验证的三阶段方法：

1. **先删除重复** — 100% body-hash 匹配的技能。保留更短路径的副本。
2. **删除真正未使用的技能** — 使用 `--deep-logs` 获取准确使用数据，然后删除 `usage=$0, reads=0, text=0` 的技能。
3. **压缩剩余的长描述** — 针对 >200 字符的描述。压缩冗余文字、删除重复触发词列表、多行折叠为单行。

每阶段后重新运行脚本以衡量进展。

## 诊断命令

```bash
SCRIPT=~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts

# 预算概览
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -A5 "## Skill Budget"

# Top 30 描述膨胀（按字符数排序）
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -B1 "chars: description=" | grep -v "^--$" | paste - - | sort -t: -k4 -n -r | head -30

# 总描述字符数 vs 截断
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -E "(description_chars|truncated)"

# 未使用技能（需要 --deep-logs）
node --experimental-strip-types $SCRIPT --deep-logs --months 6 --max-log-mb 800 2>&1 | grep "usage=\$0, reads=0, text=0"

# 有使用痕迹的技能
node --experimental-strip-types $SCRIPT --deep-logs --months 6 --max-log-mb 800 2>&1 | grep "usage=" | grep -v "usage=\$0, reads=0, text=0"

# 列出所有额外（未使用）技能
node --experimental-strip-types $SCRIPT --deep-logs --months 6 --max-log-mb 800 2>&1 | grep "extra; usage=\$0, reads=0, text=0" | awk '{print $1}' | sed 's/:$//' | sort
```

## 脚本架构说明

与原 skill-cleaner 的关键区别：

| 方面 | skill-cleaner (Codex) | hermes-skill-cleaner |
|------|----------------------|---------------------|
| 技能根目录 | Codex + Hermes + plugins + agent-scripts | 仅 Hermes + repo |
| 配置格式 | TOML (Codex config) | YAML (Hermes config) |
| 模型上下文 | `~/.codex/models_cache.json` | `config.yaml` 或 `--context-tokens` |
| 插件概念 | 有（plugin cache, disabled plugins） | 无 |
| Scope | codex, codex-plugin, agent-scripts, dropbox... | hermes, repo, extra |
| 禁用检测 | 通过 config.toml | 无（通过目录移动实现） |
| 代码行数 | ~650 行 | ~440 行 |

## 上下文窗口读取

优先级（从高到低）：

1. `--context-tokens` CLI 参数
2. `model.context_length` in the Hermes config file
3. 回退值：272,000

模型名称从 `models.default` 读取，可通过 `--model` 覆盖。

## Session Log

### 2026-06-18 — 初始版本（从 skill-cleaner 重构）

从 skill-cleaner 重构为纯 Hermes 版本：
- 移除了所有 Codex 路径和 TOML 配置逻辑
- 移除了插件 scope 概念
- 简化了 scope 为 hermes/repo/extra
- 合并了 walkFiles/walkRecentFiles 为单一 walkFiles（带可选 timeFilter）
- 重命名了 codexBudgetedSkillCost → budgetedSkillCost
- YAML 配置读取器替代了 TOML configState
- 移除了 OpenClaw/Slack/Discord 等无关触发词检测
- 保留了所有已验证的 Hermes session JSONL 扫描逻辑
- 使用 JSON.parse 逐行解析替代正则匹配（更可靠）

**首次运行结果：**
- 73 skills, 10,077 description chars, 17,399 rendered line chars
- 预算使用率：22.2% (4,556/20,480 tokens)
- 0 截断字符
- 发现 7 组嵌套重复（software-development 目录下的双重 SKILL.md）
- 30 个描述过长候选
