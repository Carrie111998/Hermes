# Grok SSO → CPA Skill

将 Grok 账号凭据转换为 CLIProxyAPI（CPA）可导入的 `xai-*.json`。

> **安全提醒**：账号密码、SSO、access token 和 refresh token 都是敏感凭据。不要把真实账号写进仓库、README、Issue、日志或截图。转换完成后，工具默认删除原始凭据输入文件。

## 功能

- 支持单个或批量账号。
- 支持以下输入格式：
  - `邮箱|密码|SSO`
  - `邮箱----密码----SSO`
  - `邮箱|SSO`
  - 纯 SSO JWT
- 并发执行 OAuth Device Flow，并对失败账号自动重试。
- 每个账号只保留一条最终结果，失败重试不会造成统计重复。
- 自动检测代理/TUN 环境；代理端口通过参数指定，不硬编码。
- 自动检测 CPA 默认 auth 目录：
  - 目录存在：默认写入该目录，CPA 可热加载。
  - 目录不存在：认为 CPA 尚未安装或尚未初始化，在用户桌面创建 `grok-cpa`，将 JSON 写入该目录，并明确提醒用户稍后手动导入 CPA。
- 运行结束默认删除原始凭据文件；需要保留时显式使用 `--keep-input`。
- 自带环境预检：Python、`curl_cffi`、转换核心、输入文件和输出目录不可用时，先给出修复提示，不会静默失败。

## 在 Hermes 中安装

将本目录复制到 Hermes 的 skills 目录：

```text
%LOCALAPPDATA%\\hermes\\skills\\grok-sso-to-cpa\\
```

目录至少包含：

```text
grok-sso-to-cpa/
├── SKILL.md
├── README.md
└── scripts/
    └── batch_sso2cpa.py
```

## 使用流程

1. 用户向 agent 发送一行或多行 `邮箱|密码|SSO`。
2. Agent 将凭据写入临时输入文件，不在聊天中回显完整凭据。
3. Agent 检查 CPA auth 目录和代理/TUN 状态。
4. 若缺少 `grokRegister-cpa` 转换核心，先克隆：

   ```bash
   git clone https://github.com/Git-creat7/grokRegister-cpa.git
   ```

5. 执行 wrapper：

   ```bash
   PYTHONPATH= python scripts/batch_sso2cpa.py \
     --input accounts.txt \
     --repo ./grokRegister-cpa \
     --workers 3 \
     --retries 3 \
     --max-wait 300
   ```

   `--cpa-auth-dir` 可选。省略时自动检查：

   ```text
   ~/.cli-proxy-api/
   ```

   Windows 上通常对应：

   ```text
   C:\\Users\\<用户名>\\.cli-proxy-api\\
   ```

6. 无 TUN、需要显式代理时，指定实际端口和协议：

   ```bash
   --proxy http://127.0.0.1:7890
   # 或
   --proxy socks5://127.0.0.1:1080
   ```

   不要假设端口一定是 7890；应根据用户设备实际配置检测或询问。

7. 转换结束后检查结果和输出目录。若使用桌面 fallback，agent 必须告诉用户：CPA 未检测到，JSON 位于桌面的 `grok-cpa`，安装 CPA 后需手动导入。

## CLI 参数

| 参数 | 必需 | 说明 |
|---|---:|---|
| `--input` | 是 | 凭据输入文件；成功结束后默认删除 |
| `--cpa-auth-dir` | 否 | CPA auth-dir；显式指定时强制使用该目录 |
| `--repo` | 否 | `sso_to_auth_json.py` 所在的 `grokRegister-cpa` 目录 |
| `--proxy` | 否 | `http://` 或 `socks5://` 代理地址；留空表示直连/TUN |
| `--workers` | 否 | 并发数，默认 3，建议不超过 4 |
| `--retries` | 否 | 每账号重试次数，默认 3 |
| `--max-wait` | 否 | 等待 `auth.x.ai` 网络窗口的秒数，默认 300 |
| `--skip-preflight` | 否 | 已确认环境就绪时跳过预检 |
| `--keep-input` | 否 | 保留原始凭据文件；默认不保留 |

## 输出格式

输出是 CPA 可读取的扁平 xAI OAuth JSON，包含：

- `type: xai`
- `auth_kind: oauth`
- `email`、`sub`
- `access_token`、`refresh_token`、`id_token`
- `token_endpoint`
- `base_url: https://cli-chat-proxy.grok.com/v1`
- Grok CLI 所需 headers

成功后可使用 `refresh_token` 自动刷新短期 access token。不要将生成的 JSON 提交到 Git 仓库。

## 网络和失败处理

`auth.x.ai` 可能受网络线路、代理规则或 TLS/SNI 阻断影响。wrapper 会先做连通性窗口检测：

- 连续探测成功后立即开始批量转换。
- 连续失败时，应检查 TUN、代理协议、实际端口、节点和 DNS，而不是盲目增加并发。
- `SSO 无效` 通常表示 SSO 已过期、注销或被服务端拒绝，需要新的 SSO；重试无法修复失效凭据。
- 失败结果写入 CPA auth-dir（或桌面 fallback 目录）的 `_batch_results.jsonl`，但该文件不含 token，只记录状态和邮箱。

## 开发和验证

```bash
PYTHONPATH= python -m py_compile scripts/batch_sso2cpa.py
```

本 skill 的 wrapper 不会把密码或 SSO 写入结果日志。发布前必须检查 Git diff，确认没有真实凭据、生成的 CPA JSON、输入 txt 或本机路径中的秘密配置。

## 许可证

MIT。该 skill 仅自动化用户已授权账号的凭据转换；使用者必须遵守 Grok、CLIProxyAPI、网络服务和所在地区的适用条款。

## 相关项目

- [grokRegister-cpa](https://github.com/Git-creat7/grokRegister-cpa)：OAuth 转换核心
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)：CPA 项目
- [Hermes Agent](https://github.com/Nagizyp/hermes-agent)：skill 运行环境
