---
name: grok-sso-to-cpa
description: 用户给 grok 账号（邮箱|密码|SSO）要转 CPA/CLIProxyAPI 的 xai JSON 入库时使用。
version: 1.2.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [grok, cpa, cliproxyapi, oauth, xai, sso]
    related_skills: []
---

# Grok SSO → CPA JSON

## When to Use

当用户提供 Grok 账号（`邮箱|密码|SSO`）并要求转成 CPA/CLIProxyAPI 的 xAI JSON、批量导入或保存时使用。

## 必须遵守的执行流程

1. 将用户输入写入临时凭据文件；不要在聊天、日志或结果中回显密码、SSO、access token 或 refresh token。
2. 读取并使用本 skill 自带的 `scripts/batch_sso2cpa.py`。
3. 先运行预检；如果 Python、curl_cffi、转换核心、输入文件或输出目录缺失，明确告诉用户缺少什么，并先修复环境。
4. 检测实际代理/TUN。不要硬编码 7890；没有 TUN 时使用用户设备实际的 `http://` 或 `socks5://` 代理和端口。
5. 默认检测 CPA auth-dir：`~/.cli-proxy-api/`，Windows 通常是 `C:\Users\<用户名>\.cli-proxy-api\`。
6. **如果默认 CPA auth-dir 已存在**，默认把 JSON 写入该目录并告知 CPA 可热加载。
7. **如果默认 CPA auth-dir 不存在**，不要为了通过检测而创建它；这表示 CPA 可能未安装或未初始化。自动在用户桌面创建 `grok-cpa`，把 JSON 写入该目录，并提醒用户安装/初始化 CPA 后手动导入这些 JSON。
8. 转换完成后验证输出文件和结果统计；可抽样 probe，但不要在聊天中打印 token。
9. 转换结束后默认删除原始凭据文件。只有用户明确要求保留时才使用 `--keep-input`。

## 环境准备

需要 Python 3.9+、`curl_cffi` 和上游转换核心：

```bash
python --version
python -c "import curl_cffi; print(curl_cffi.__version__)"
git clone https://github.com/Git-creat7/grokRegister-cpa.git
```

上游目录通过 `--repo` 指定。用户没有 CPA 时不阻止转换，但必须使用桌面 fallback 并在结果中明确说明。

## 代理策略

- TUN 已启用且可接管流量：省略 `--proxy`。
- 普通 HTTP 代理：`--proxy http://127.0.0.1:<实际端口>`。
- SOCKS5 代理：`--proxy socks5://127.0.0.1:<实际端口>`。
- 先通过 `auth.x.ai/.well-known/openid-configuration` 连续探测确认线路，再开始批量。
- 建议 `--workers 3`，不要超过 4，避免 xAI 风控、429 或 slow_down。

## 推荐命令

```bash
PYTHONPATH= python scripts/batch_sso2cpa.py \
  --input accounts.txt \
  --repo ./grokRegister-cpa \
  --workers 3 \
  --retries 3 \
  --max-wait 300
```

没有 TUN 时增加：

```bash
--proxy http://127.0.0.1:<实际端口>
```

需要强制使用指定 CPA 目录时：

```bash
--cpa-auth-dir "<CPA auth-dir>"
```

`--cpa-auth-dir` 显式指定后视为用户明确选择的目录；默认自动检测不存在才触发桌面 fallback。

## wrapper 行为

- 支持 `邮箱|密码|SSO`、`邮箱----密码----SSO`、`邮箱|SSO` 和纯 SSO。
- 账号按 SSO 去重。
- 每个账号只保存一条最终结果；重试不会重复计数。
- 默认删除 `--input` 凭据文件；`--keep-input` 可显式保留。
- 结果摘要写入输出目录的 `_batch_results.jsonl`，不写入 token。
- `--skip-preflight` 仅适用于用户已确认环境完整的情况。

## 安全

真实凭据、生成的 CPA JSON、输入 txt、refresh token 和 access token 都不得提交到 GitHub。发布前检查 `git diff` 和 `git status`。本仓库只包含 skill 文档与通用脚本。

## 相关文件

- `scripts/batch_sso2cpa.py`：批量转换 wrapper
- `README.md`：安装、使用、fallback 和安全说明
- 上游核心：[grokRegister-cpa](https://github.com/Git-creat7/grokRegister-cpa)
- CPA：[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
