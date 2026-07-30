# 后端联动改造方案

> 状态：讨论中，待确认细节
> 日期：2026-07-29
> 分支：feat/backend-integration
> 相关文档：~/clawd/qiji-backend/客户端改动清单.md、~/clawd/qiji-backend/API设计文档_v1.md

---

## 一、背景

奇计桌面端（基于 Hermes Agent）要与后端服务器联动，实现账号体系、额度管理、统一 API Key 下发。

**只改 Hermes 桌面端**，geo-client.py 控制 auth helper.exe 的部分不管。

---

## 二、确认的设计决策

### 2.1 登录方式

- **登录账号**：username（用户名），不是手机号
- **登录请求**：`POST /api/client/v1/auth/login`，请求体 `{username, password}`
- **登录不需要短信验证**，注册时才需要填手机号 + 短信验证码
- 注：之前的聊天记录里某个 AI 提到"后端用 mobile"，但 API 设计文档 L183-187 明确写的是 username，以文档为准

### 2.2 引导页和设置页

**所有桌面端用户默认行为：**
- 去掉引导页（onboarding overlay），不管什么情况都不弹
- 设置里隐藏所有能配第三方 key 的地方
- 设置里隐藏所有能显示/选择大模型的地方

**唯一例外：**
- 后端返回 `is_custom_key=1` 的用户，才显示 key 配置和模型选择

不是"根据 is_custom_key 决定是否跳过引导页"，而是默认全部隐藏，只有后端明确授权才显示。

### 2.3 登录界面位置

桌面端内建登录页。用户启动后先看到登录界面，输账号密码后才进入主界面。

### 2.4 启动流程（方案 A）

登录页与 gateway 启动并行，用户感知更快：

```
桌面端启动
  → Electron main 进程启动 gateway（后台并行）
  → 同时检查本地有没有存有效登录 token（未过 7 天保质期）
      ├ 有 token → 跳过登录页，直接进主界面
      │   └ 启动时用存的 api_key 自动配好 LLM
      └ 没 token → 显示登录页（覆盖层，比所有东西都顶层）
          ├ 用户输 username + password
          ├ 调 POST /api/client/v1/auth/login
          ├ 成功 → 存 token + api_key + is_custom_key + score
          │   └ 关闭登录页，进主界面
          └ 失败 → 显示错误，留在登录页

  → 引导页：永远不弹（从代码里移除或禁用）
  → 设置页：根据 is_custom_key 动态显示
      ├ is_custom_key=0 → 隐藏 ModelSettings、ProvidersSettings、KeysSettings
      └ is_custom_key=1 → 正常显示（跟现在一样）
```

### 2.5 api_key 与 score 的关系

**api_key = 用什么钥匙开门（谁的账户在付费 LLM）**
- is_custom_key=0 的用户没有自己的 key
- 后端沿代理链（用户→代理→贴牌→总后台）往上找第一个有 key 的，下发
- 多个用户共用同一把代理的 key
- 一个代理有多个 key 时怎么分配是后端的业务逻辑，客户端不关心

**score = 你能用多少（额度）**
- 每次调 LLM 消耗 token，后端从 score 扣
- 扣完就不能用，弹窗提示充值
- 体验用户 score 很少（如 10），正式用户由代理充值

两种 token 的区分：
- 登录 token = 通行证，有 7 天保质期，用于调后端 API 的鉴权
- LLM token = 文本计量单位，每次调 LLM 消耗，后端用 score 计量

### 2.6 用户体验流程

```
用户注册（无邀请码）
  → mode=trial, score=少量
  → 后端下发代理的 api_key
  → 客户端自动配好 LLM，能聊天
  → 每次聊天消耗 token → 后端从 score 扣
  → 扣到 0 → 弹窗"额度不足，联系代理充值"
  → 用户拿到邀请码 → 填进去激活 → 变 formal
  → 联系代理充值 → 代理给 score 充值 → 继续用
```

---

## 三、需要改的文件

### 第 1 层：认证基础设施（后面所有东西都依赖它）

| 文件 | 改什么 |
|------|--------|
| 新建 `store/auth.ts` | 管理登录状态（token、user_info、is_custom_key、score） |
| 新建登录页组件 | username + password 输入框 + 登录按钮 |
| 新建后端 API 调用层 | 调后端 `/api/client/v1/auth/login` 等 |
| 修改 `app/desktop-controller.tsx` | 未登录时显示登录页（顶层覆盖层），已登录显示主界面 |

### 第 2 层：去掉引导页 + 自动配 key

| 文件 | 改什么 |
|------|--------|
| `store/onboarding.ts` (906行) | 永远不弹引导页，自动用后端 key 配好 LLM |
| `app/settings/model-settings.tsx` (666行) | is_custom_key=0 时隐藏模型选择/Key 输入 |
| `app/settings/providers-settings.tsx` (623行) | is_custom_key=0 时隐藏整个配置入口 |
| 可能还有 `app/settings/keys-settings.tsx` | is_custom_key=0 时隐藏 |

### 第 3 层：额度显示 + Token 上报

| 文件 | 改什么 |
|------|--------|
| 主界面某处 | 显示剩余点数（score） |
| LLM 调用后 | 上报 token 用量到 `POST /api/client/v1/quota/report` |
| 额度不足时 | 弹窗提示 |

### 第 4 层：更新机制（优先级低，可后面做）

| 文件 | 改什么 |
|------|--------|
| `store/updates.ts` (597行) | git-based 检查换成 HTTP `/api/client/v1/update/check` |

---

## 四、待确认的细节问题（明天讨论）

### 4.1 后端基地址
现在硬编码 `http://8.138.58.181`。写死在代码里？还是可配置？

### 4.2 Token 存储方式
- localStorage（页面刷新不丢，但关浏览器后清除）
- Electron secureStorage / keychain（最安全，但复杂）
- 写入 Hermes 的 config.yaml（持久化，重启不丢）

### 4.3 登录页 UI 范围
只放 username + password + 登录按钮？还是还要"忘记密码"、"注册"入口等？

### 4.4 Token 过期处理
7 天过期后：自动重新登录（需要存密码）？弹登录页？refresh token（后端文档没提）？

### 4.5 体验模式（trial）vs 正式模式（formal）
体验模式 score=0 时：允许聊天但额度为 0 弹窗？还是直接不让进？

### 4.6 显示剩余点数
显示在哪？聊天界面顶部？设置页？额度不足怎么提示？

---

## 五、后端 API 接口映射

### 登录
```
POST /api/client/v1/auth/login
请求: {username, password}
响应: {code:1, data:{token, user_id, username, api_key, is_custom_key, score, mode, quota:{...}}}
```

### 获取 API Key
```
GET /api/client/v1/apikey
Authorization: Bearer <token>
响应: {code:1, data:{api_key, is_custom_key, key_source, can_customize}}
```

### 查询额度
```
GET /api/client/v1/quota
Authorization: Bearer <token>
```

### 上报 Token 用量
```
POST /api/client/v1/quota/report
Authorization: Bearer <token>
请求: {model, input_tokens, output_tokens, request_id}
响应: {code:1, data:{remaining_score}}
额度不足时: {code:0, msg:"额度不足"}
```

### 检查更新
```
GET /api/client/v1/update/check?version=1.0.0
响应: {code:1, data:{has_update, enforce, newversion, downloadurl, upgradetext}}
```

---

## 六、当前分支状态

- 分支：`feat/backend-integration`（已从 main 拉出）
- main 上最后一个 commit：`6b2fc490f docs: 更新CHANGELOG和离线打包踩坑记录`
- 尚未写任何代码，纯方案讨论阶段
