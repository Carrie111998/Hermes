---
name: hermes-skill-cleaner
description: "Audit Hermes skills: prompt-budget costs, duplicates, unused skills, compact descriptions."
---

# Hermes Skill Cleaner

纯 Hermes 技能审计工具。扫描 `~/.hermes/skills/` 和项目 `.agents/skills/`，分析技能预算、重复、未使用和建议压缩的描述。

## 快速运行

```bash
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts --no-logs 2>&1
```

常用变体：

```bash
# 完整报告（含使用统计）
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts

# 深度扫描（含归档会话）
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts --months 6 --max-log-mb 800 --deep-logs

# 自定义上下文窗口
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts --context-tokens 200000 --budget-percent 2 --no-logs

# 指定额外技能根目录
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts --root ~/Dropbox/skills --no-logs

# JSON 输出（程序化消费）
node --experimental-strip-types ~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts --json --no-logs
```

## 报告解读顺序

1. **Skill Budget** — 上下文窗口大小、2% 技能预算、预算使用率、被截断的描述字符数
2. **Description Candidates** — 描述过长的技能，附带压缩建议
3. **Duplicates By Name** — 同名技能在不同位置
4. **Duplicate Delete Suggestions** — 推荐删除的重复副本
5. **Duplicates By Body Hash** — 内容相同但名称不同的技能
6. **Unused Candidates** — 近期日志中无使用痕迹的技能
7. **Root Summary** — 各技能根目录的技能数量

## 诊断命令

```bash
SCRIPT=~/.hermes/skills/hermes-skill-cleaner/scripts/hermes-skill-cleaner.ts

# 预算概览
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -A5 "## Skill Budget"

# Top 30 描述膨胀技能（按字符数排序）
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -B1 "chars: description=" | grep -v "^--$" | paste - - | sort -t: -k4 -n -r | head -30

# 总描述字符数 vs 截断
node --experimental-strip-types $SCRIPT --no-logs 2>&1 | grep -E "(description_chars|truncated)"

# 未使用技能（需 --deep-logs）
node --experimental-strip-types $SCRIPT --deep-logs --months 6 --max-log-mb 800 2>&1 | grep "usage=\$0, reads=0, text=0"

# 有使用痕迹的技能
node --experimental-strip-types $SCRIPT --deep-logs --months 6 --max-log-mb 800 2>&1 | grep "usage=" | grep -v "usage=\$0, reads=0, text=0"
```

## 上下文窗口读取

脚本从以下来源读取 Hermes 的上下文窗口大小（优先级从高到低）：

1. `--context-tokens` 命令行参数
2. `model.context_length` 在 Hermes 主配置文件（`config.yaml`）中
3. 回退值：272,000 tokens

模型名称从 `models.default` 读取（或通过 `--model` 覆盖）。

## 描述压缩模式

预算公式：`ceil(description_utf8_bytes / 4)`。目标：保持在上下文窗口的 2% 以内。

**高价值压缩目标（通常 200+ 字符）：**
- `|` 多行 YAML 块 → 单行引用字符串
- 描述后的额外触发词行
- 重复的中英文翻译
- 冗余的解释性文字

**高效模式：**
1. `description: "动词短语: 做什么。触发: 何时加载, 别名。"` — 紧凑无冗余
2. 保留触发名词（产品、工具、动作），删除解释性文字
3. `|` 块只有 2-3 行 → 折叠为单行 `"..."`
4. 技能同时有 `description:` 行和下方散文时，先删散文

## 禁用技能组

Hermes 没有内置的技能禁用机制。要减少加载的技能数量，将目录移出 `~/.hermes/skills/`：

```bash
mkdir -p ~/.hermes/skills-disabled
mv ~/.hermes/skills/<category-or-name> ~/.hermes/skills-disabled/
```

恢复：
```bash
mv ~/.hermes/skills-disabled/<category-or-name> ~/.hermes/skills/
```

验证：重新运行脚本，`skills: N discovered` 数量应减少。

## 手动分类清理流程（基于相关性删除）

当需要削减 token 膨胀时，先做相关性分类再跑脚本：

### Step 1: 列出所有技能 `skills_list()`

### Step 2: 分为三档

- **🔴 从不使用** — 用户不用的工具/服务技能（Odoo、Notion、Google Workspace、Airtable、学术论文等）
- **🟡 偶尔使用** — 低频技能（React Native 调试、API 契约调试、webhook 订阅等）
- **🟢 核心工作流** — 活跃使用的技能（内容创作、视频制作、agent 委派、前端设计、开发工作流、GitHub、搜索等）

### Step 3: 确认后删除

```bash
cd ~/.hermes/skills && rm -rf skill1 skill2 ...
```

### Step 4: 验证

```bash
find ~/.hermes/skills -name "SKILL.md" -not -path "*/.archive/*" | wc -l
```

### Step 5: 报告

展示前后对比：SKILL.md 数量、磁盘使用、预估 token 节省（每个删除的技能描述在 available_skills 块中约 80-100 tokens）。

## 批量清理工作流（描述压缩）

1. **基线：** 运行脚本，记录 `description_chars`、`unbudgeted_used_of_2%_budget`、`skills: N discovered`
2. **压缩描述：** 针对 "Description Candidates" 部分。对每个：
   - `|` 多行 → 单行 `"..."`
   - 删除冗余的触发词行
   - 删除重复的中英文翻译
3. **禁用组：** 将整个目录移到 `~/.hermes/skills-disabled/`
4. **验证：** 重新运行脚本，对比基线指标
5. **报告：** 展示前后对比表

## 输出策略

- 先建议，用户要求时才编辑
- 如果要求执行清理，做小批量分组提交：描述、删除、禁用
- 不要在不确认的情况下删除或修改

## 注意事项

- **CRITICAL: 不要未经确认就删除或修改。** 用户说"先跑数据再决策"时，意味着 STOP 在报告阶段——不要进入删除/修改。遵循"讨论阶段只讨论，不动代码"规则。
- **Hermes 会话格式：** 参见 `references/hermes-session-format.md` — 记录了 JSONL 格式、转义层级、以及为什么用 `JSON.parse` 逐行解析而非正则匹配。
- **恢复：** 本地备份在 `~/GitHub-Work/hermes-backup/skills/`。如果用了 `rm -rf`（绕过回收站），可以从备份恢复。
- **符号链接技能可能不在备份中。** `design-taste-frontend` 是指向 `~/.agents/skills/` 的符号链接——备份中可能只有 `taste-frontend`（不同名称）。
- **`rm -rf` 在 macOS 上不可恢复。** `~/.hermes/skills/` 不是 git 仓库，`rm -rf` 绕过回收站。始终先确认。优先用 `mv` 到 disabled 目录作为可逆的第一步。
- **重启生效。** 技能列表在 Hermes 启动时扫描。更改在下次 `/reset` 或重启后生效。
- **`.archive/` 目录** 包含已归档技能——忽略它，不会被加载。
- **空类别目录**（无 SKILL.md）不消耗 token，但可以清理。
