# VideoAgent Skill 过滤决策记录

状态：已实现
日期：2026-06-17
更新：2026-06-29
范围：为 VideoAgent / Ultra Studio profile 收敛 Hermes skill 可见目录

## 背景

这个 fork 正在从通用 Hermes Agent 收敛成面向图片、视频生成的
VideoAgent。目标生成链路会优先使用 Atlas API，后续再加入 storyboard
等专用 workflow skill。

当前 Hermes 仓库里已经有很多 creative、media、mlops skill，但大多数
不是 VideoAgent 默认入口。它们要么绑定特定渲染栈，要么只是媒体辅助能力，
如果全部暴露给 agent，会让 skill 选择变得噪声很大。

本次目标不是删除这些 skill，而是让 VideoAgent profile 默认只看到一组
干净、可控的媒体 workflow catalog。

## 当前判断

当前仓库里没有一个真正通用的 Atlas 视频生成 skill。

最适合作为 VideoAgent 通用底座的是这三个：

| Skill | 作用 |
|---|---|
| `workflow-router` | 对图片、视频请求做 intent 分类，并选择下一步 workflow。 |
| `media-qa` | 对生成或上传的媒体做质量检查，不编造视觉观察。 |
| `prompt-repair` | 根据失败原因或 QA 结果生成 retry prompt。 |

`infographic-md-flow` 暂时保留为默认 workflow，因为它已经落地，适合
数据、KPI、流程类 motion reel。但它不是通用视频生成 skill。

2026-06-29 更新：默认目录也加入三个已落地的营销/创意 director skill。
它们不是 Hermes core runtime 能力，而是 Ultra Studio / VideoAgent 产品层的
prompt 和 campaign workflow 入口：

| Skill | 作用 |
|---|---|
| `gpt-image-2-director` | 把图片/海报/UI/信息图概念整理成 GPT Image 2.0 prompt。 |
| `marketing-studio-director` | 把广告概念整理成 Higgsfield Marketing Studio prompt。 |
| `higgsfield-content-factory` | 规划 UGC / review / unboxing / ASMR 等 campaign pipeline。 |

## 不进入默认 VideoAgent 目录的 Skill

这些 skill 仍然有用，但不应该默认出现在 VideoAgent catalog 里：

| Skill | 原因 |
|---|---|
| `comfyui` | ComfyUI 后端专用，不是 Atlas-first workflow。 |
| `hyperframes` | HTML-to-video 渲染器，不是 Atlas 媒体生成链路。 |
| `manim-video` | 代码渲染的解释动画，不是通用媒体生成。 |
| `ascii-video` | ASCII/复古视频风格，过于专用。 |
| `p5js` | creative coding / canvas 渲染，不是 Atlas workflow。 |
| `touchdesigner-mcp` | 实时视觉装置或 VJ 工作流。 |
| `blender-mcp` | 3D 桌面工具控制，适合作为可选扩展。 |
| `pixel-art` | 特定像素风格图片/视频模式，适合作为可选扩展。 |
| `baoyu-*`, `meme-generation`, `concept-diagrams` | 图片/插画/图解类 workflow，不是默认视频路径。 |
| `clip`, `llava`, `segment-anything-model` | 视觉理解或模型能力，不是生成 workflow。 |
| `youtube-content`, `gif-search`, `heartmula`, `songsee`, `whisper`, `nemo-curator` | 媒体辅助工具，不是核心 VideoAgent 路由。 |

这些能力以后可以按具体产品模式再启用，例如像素视频、3D 视频、音乐视频、
YouTube ingest、视觉理解等。

## 决策

使用“屏蔽”而不是物理删除。

理由：

- `skills.disabled` 和 `skills.platform_disabled` 已经能从 discovery 中隐藏
  skill。
- 删除内置或 optional skill 会让恢复成本变高，也会制造不必要的 upstream
  分叉。
- 这是 profile 级行为：VideoAgent profile 应该窄，通用 Hermes profile
  可以继续保留完整 catalog。
- 以后新增 storyboard、product、UGC、app teaser、cinematic 等 workflow
  时，只需要把新 skill 名加入 allowlist，不需要移动或删除旧 skill。

## 已实现的 Allowlist

代码位置：`hermes_cli/ultra_studio_skills.py`

核心 VideoAgent allowlist：

```python
VIDEO_AGENT_CORE_SKILL_ALLOWLIST = (
    "workflow-router",
    "media-qa",
    "prompt-repair",
)
```

默认 VideoAgent allowlist：

```python
DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST = (
    "workflow-router",
    "media-qa",
    "prompt-repair",
    "infographic-md-flow",
    "gpt-image-2-director",
    "marketing-studio-director",
    "higgsfield-content-factory",
)
```

`DEFAULT_ULTRA_STUDIO_SKILL_ALLOWLIST` 保留为兼容 alias，避免影响已有
Ultra Studio helper 和测试。

代码中同时保留三段可读常量：

- `VIDEO_AGENT_CORE_SKILL_ALLOWLIST`：`workflow-router`、`media-qa`、
  `prompt-repair`。
- `VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST`：当前为 `infographic-md-flow`。
- `VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST`：当前为三个 marketing director skill。

## 如何应用屏蔽

代码位置：`hermes_cli/skills_config.py`

对当前 profile 应用 VideoAgent preset：

```bash
hermes skills video-agent
```

对指定 profile 应用：

```bash
hermes -p videoagent skills video-agent
```

只保留路由、QA、修复三件套：

```bash
hermes -p videoagent skills video-agent --core-only
```

只对某个平台写入 `skills.platform_disabled`：

```bash
hermes -p videoagent skills video-agent --platform cli
```

这个命令会把所有不在 allowlist 里的已安装 skill 写入 disabled list。
它不会删除任何 skill 文件。

安全边界：

- `--platform` 只接受 `global` 或 Hermes 已注册平台名，例如 `cli`、
  `api_server`；拼写错误会被 argparse 拒绝，不写入无效配置。
- discovery 失败时命令 fail closed，并且不会保存空 disabled list。这样不会
  因为一次 skill 扫描异常把 profile 变成“全部 skill 可见”。

## 后续加入 Storyboard / Atlas Workflow

通用 Atlas storyboard 或媒体生成 workflow 应该作为新的 skill 加入，而不是
复用 `comfyui`、`manim-video`、`ascii-video` 这类特定渲染栈 skill。

期望链路：

```text
workflow-router
  -> storyboard / atlas-video-flow / atlas-image-flow
  -> media job tool
  -> media-qa
  -> prompt-repair when needed
```

新增 workflow skill 后，把它的 frontmatter `name` 加到：

- `VIDEO_AGENT_WORKFLOW_SKILL_ALLOWLIST`，或
- `VIDEO_AGENT_MARKETING_SKILL_ALLOWLIST`，或
- 其它新的按产品模式命名的 allowlist 常量，再组合进
  `DEFAULT_VIDEO_AGENT_SKILL_ALLOWLIST`

除非产品明确支持对应模式，否则不要把 `comfyui`、`manim-video`、
`ascii-video`、`p5js`、`hyperframes` 放回默认 allowlist。

## 验证

本次实现后跑过的检查：

```bash
pytest -o addopts='' tests/hermes_cli/test_ultra_studio_skills.py -q
pytest -o addopts='' tests/hermes_cli/test_ultra_studio_skills.py tests/hermes_cli/test_subcommands_batch.py -q
python3 -m py_compile hermes_cli/ultra_studio_skills.py hermes_cli/skills_config.py hermes_cli/subcommands/skills.py tests/hermes_cli/test_ultra_studio_skills.py
git diff --check -- hermes_cli/ultra_studio_skills.py hermes_cli/skills_config.py hermes_cli/subcommands/skills.py tests/hermes_cli/test_ultra_studio_skills.py
```

本地运行 pytest 时需要 `-o addopts=''`，因为当前环境没有安装
`pytest-timeout`，而仓库默认 pytest 配置里包含 `--timeout` 参数。
