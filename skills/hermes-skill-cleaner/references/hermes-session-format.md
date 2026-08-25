# Hermes Session JSONL Format

## 格式结构

Hermes 会话文件 (`~/.hermes/sessions/*.jsonl`) 每行是一个 JSON 对象。

### 工具调用请求 (assistant 角色)

```json
{
  "role": "assistant",
  "content": "I'll load the skill...",
  "tool_calls": [{
    "function": {
      "name": "skill_view",
      "arguments": "{\"name\": \"manim-video\"}"
    }
  }]
}
```

关键：`arguments` 字段是 **JSON 字符串**（非对象），内含转义引号 `\"`。

### 工具调用结果 (tool 角色)

```json
{
  "role": "tool",
  "name": "skill_view",
  "content": "{\"success\": true, \"name\": \"manim-video\", ...}"
}
```

关键：`content` 字段是 **JSON 字符串**（非对象），内含转义引号 `\"`。

## 与 Codex 的区别

| 方面 | Codex | Hermes |
|------|-------|--------|
| 工具请求 | `arguments` 字段 | 相同 |
| 工具结果 | `content` 字段（JSON 字符串） | 相同 |
| 转义层数 | `\\"name\\"` (单层) | `\"name\"` (单层，但 `content` 内部还有一层) |
| 行内结构 | 多行 JSON（pretty-print） | 单行 JSONL |
| skill 调用检测 | `$skill` 模式 | `skill_view`/`skill_manage` 工具名 |

## 解析策略

**错误做法（正则）：**
```javascript
// 正则匹配 content 中的转义 JSON 极不可靠
/"name":\s*"skill_view"[\s\S]*?"content":\s*"\{[\s\S]*?"name":\s*"([^"]+)"/g
```
问题：
- `[\s\S]*?` 跨行匹配会越界到其他行
- 转义层级在文件读取后是 1 层，在 JS 字符串中是 2 层，正则需精确匹配
- `content` 字段可能包含极长的 JSON（如完整的 SKILL.md 内容），导致回溯爆炸

**正确做法（JSON.parse）：**
```javascript
for (const line of text.split("\n")) {
  const trimmed = line.trim();
  if (!trimmed) continue;
  try {
    const parsed = JSON.parse(trimmed);
    if (parsed.name === "skill_view" || parsed.name === "skill_manage") {
      // 从 content 提取（工具结果）
      if (typeof parsed.content === "string") {
        const inner = JSON.parse(parsed.content);
        if (inner.name) { /* 使用 inner.name */ }
      }
      // 从 arguments 提取（工具请求）
      if (typeof parsed.arguments === "string") {
        const inner = JSON.parse(parsed.arguments);
        if (inner.name) { /* 使用 inner.name */ }
      }
    }
  } catch {}
}
```

优势：
- 逐行解析，不会越界
- JSON.parse 自动处理所有转义层级
- 容错：某行解析失败不影响其他行

## 已知问题

### 重复 skill_view 匹配

会话文件第一行 (`session_meta`) 包含完整的工具定义 JSON，其中 `skill_view` 出现在工具描述中（如 "Use skill_view() to see format examples"）。这些是工具定义的文档字符串，不是实际调用。

解决方案：检查 `parsed.role === "tool"` 或 `parsed.role === "assistant"` 来区分。

### available_skills 块中的行

Hermes 的 `available_skills` 块格式：
```
- skill-name: description (file: path/to/SKILL.md)
```

正则 `^- ([a-z][a-z0-9_.:-]{1,80}):\s` 可以匹配这些行，但会同时匹配到 Markdown 列表项（如 `- name: value`）。在 `session_meta` 行中这尤其常见。

解决方案：排除 `session_meta` 行（它们不是实际使用），或只统计非 meta 行中的匹配。
