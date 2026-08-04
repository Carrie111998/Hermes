---
sidebar_position: 2
title: "安装指南"
description: "在 Linux、macOS、WSL2 或通过 Termux 在 Android 上安装 Hermes Agent"
---

# 安装指南

使用一键安装程序，在两分钟内启动并运行 Hermes Agent。

## 快速安装

### Linux / macOS / WSL2

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

### Android / Termux

Hermes 现在也提供了适用于 Termux 的安装路径：

```bash
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash
```

安装程序会自动检测 Termux 并切换到经过测试的 Android 流程：
- 使用 Termux `pkg` 安装系统依赖（`git`、`python`、`nodejs`、`ripgrep`、`ffmpeg`、构建工具）
- 使用 `python -m venv` 创建虚拟环境
- 自动导出 `ANDROID_API_LEVEL` 用于 Android wheel 构建
- 使用 `pip` 安装精选的 `.[termux]` 额外包
- 默认跳过未经测试的浏览器/WhatsApp 引导

如果你想要完全明确的路径，请查看专门的 [Termux 指南](./termux.md)。

:::warning Windows
**不支持**原生 Windows。请安装 [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install) 并从那里运行 Hermes Agent。上面的安装命令在 WSL2 中有效。
:::

### 安装程序的功能

安装程序会自动处理一切 — 所有依赖项（Python、Node.js、ripgrep、ffmpeg）、仓库克隆、虚拟环境、全局 `hermes` 命令设置以及 LLM 提供者配置。到最后，你就可以开始聊天了。

### 安装完成后

重新加载你的 shell 并开始聊天：

```bash
source ~/.bashrc   # 或：source ~/.zshrc
hermes             # 开始聊天！
```

稍后要重新配置单个设置，请使用专用命令：

```bash
hermes model          # 选择你的 LLM 提供者和模型
hermes tools          # 配置启用哪些工具
hermes gateway setup  # 设置消息传递平台
hermes config set     # 设置单个配置值
hermes setup          # 或者运行完整的设置向导一次性配置所有内容
```

---

## 前置要求

唯一的前置要求是 **Git**。安装程序会自动处理其他所有内容：

- **uv**（快速的 Python 包管理器）
- **Python 3.11**（通过 uv，无需 sudo）
- **Node.js v22**（用于浏览器自动化和 WhatsApp 桥接）
- **ripgrep**（快速文件搜索）
- **ffmpeg**（TTS 的音频格式转换）

:::info
你**不需要**手动安装 Python、Node.js、ripgrep 或 ffmpeg。安装程序会检测缺少的内容并为你安装。只需确保 `git` 可用（`git --version`）。
:::

:::tip Nix 用户
如果你使用 Nix（在 NixOS、macOS 或 Linux 上），有一个专用的设置路径，包括 Nix flake、声明式 NixOS 模块和可选的容器模式。查看 **[Nix & NixOS 设置](./nix-setup.md)** 指南。
:::

---

## 手动/开发者安装

如果你想克隆仓库并从源代码安装 — 用于贡献、从特定分支运行，或完全控制虚拟环境 — 请参阅贡献指南中的 [开发设置](../developer-guide/contributing.md#development-setup) 部分。

---

## 故障排除

| 问题 | 解决方案 |
|---------|----------|
| `hermes: command not found` | 重新加载你的 shell（`source ~/.bashrc`）或检查 PATH |
| `API key not set` | 运行 `hermes model` 配置你的提供者，或运行 `hermes config set OPENROUTER_API_KEY your_key` |
| 更新后缺少配置 | 运行 `hermes config check` 然后运行 `hermes config migrate` |

要进行更多诊断，运行 `hermes doctor` — 它会准确告诉你缺少什么以及如何修复它。

