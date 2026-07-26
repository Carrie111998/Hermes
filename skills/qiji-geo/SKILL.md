---
name: qiji-geo
description: "奇计GEO平台自动化（网页端 + 桌面客户端）。网页端操作 geo.heikexia.cc：AI可见度诊断、诊断报告、关键词管理、爆文复刻、文章列表、账号权益查询。桌面客户端（auth helper.exe）操作16个社媒平台分发 + 8个AI认证。当用户提到奇计、GEO诊断、AI可见度、品牌诊断、geo.heikexia、auth helper、社媒分发 时加载。"
version: 2.0.0
---

# 奇计GEO平台自动化 Skill

通过 Playwright 自动化操作奇计GEO平台 (geo.heikexia.cc)。

## 仓库绑定（重要）

**本 skill 与 qiji-fork 仓库强绑定，不是通用 Hermes skill。** 正式版在 `~/clawd/qiji-fork/skills/qiji-geo/`，跟随离线安装包一起编译分发。`~/.hermes/skills/qiji-geo/` 只是开发测试用的副本，方便当前 Hermes 实例调用和调试。改 skill 时两份都要同步。

## ⚠️ 费用警告

以下操作会消耗真实费用，执行前**必须**向用户确认：

| 操作 | 费用 |
|------|------|
| 创建AI诊断任务 | 13元/次 |
| 创建AI诊断（含建议报告） | 16元/次 |
| AI写作 | 消耗点数 |
| 文章发布 | 消耗点数 |
| 爆文复刻 | 消耗点数 |

**查询类操作（查看报告、关键词、权益、文章列表）免费，可直接执行。**

⚠️ **AI 发布/社媒发布需要正余额**：ai_push 和 push 任务启动时会检查账户余额（点数）。余额不足时任务仍返回 `{code:1, task_id:"xxx"}`（看起来成功了），但执行时报"网络异常"，浏览器不弹窗。**任务启动成功 ≠ 任务执行成功**，必须查 ai_logs 确认实际状态。

## 环境要求

- Node.js >= 18
- Playwright（`npm install playwright` 或在 skill 目录 `npm install`）
- 网络可访问 GEO 管理后台（默认 geo.heikexia.cc，可自定义）

## 凭证配置（Agent 自动处理）

**用户只需做一件事：告诉 Agent 自己的 GEO 服务端网址、奇计账号、密码和授权码。** 其余全部由 Agent 自动完成。

Agent 首次执行 GEO 命令时，自动检测凭证：

1. 检查 `~/.hermes/.env` 是否有 `GEO_URL`、`GEO_USERNAME`、`GEO_PASSWORD`、`GEO_UDID`
2. 如果缺失，向用户要这些值（一次性），写入 `~/.hermes/.env` 永久保存
3. GEO_UID 不需要用户提供——Agent 自动调远程 API 获取并缓存

需要用户提供的信息：
- **GEO 服务端网址**：不同 OEM 贴牌商有不同的网址（如 geo.heikexia.cc），首次使用时必须提供
- **账号**：奇计网页端登录手机号
- **密码**：奇计网页端登录密码
- **授权码**：打开桌面客户端登录界面，上面显示的那串数字

⚠️ udid 无法自动提取（LevelDB block 压缩问题，见已知限制 #5），需用户手动提供。

## 可用命令

所有命令通过 `scripts/geo-cli.js` 执行。工作目录为 skill 根目录。

**浏览器模式（所有命令通用）：**
- 默认显示浏览器窗口（可视化模式），用户可实时观看自动化操作
- `--headless` — 切换为无头模式（后台运行，不显示窗口，速度更快）
- 环境变量 `GEO_HEADLESS=true` 也能达到同样效果

```bash
# 默认可视化模式（显示浏览器窗口）
node scripts/geo-cli.js login

# 无头模式（后台运行，速度更快）
node scripts/geo-cli.js login --headless
```

当用户说"后台运行"、"不用看"、"无头模式"时，加 `--headless` 参数。

### 1. 登录测试

```bash
node scripts/geo-cli.js login
```

输出 JSON：`{ success: true/false, url: string }`

### 2. 查看账号权益

```bash
node scripts/geo-cli.js rights
```

返回：已收录数、账号有效期、剩余点数、余额等。

### 3. 创建AI诊断

```bash
# 仅填写表单（不提交，不扣费）
node scripts/geo-cli.js diagnose --brand 华为 --keywords 手机,Mate70

# 提交（扣费13元，需用户确认）
node scripts/geo-cli.js diagnose --brand 华为 --keywords 手机,Mate70 --submit
```

参数：
- `--brand`：品牌名称（必填）
- `--keywords`：行业关键词，逗号分隔（必填）
- `--platforms`：AI平台ID，逗号分隔，空则全选
- `--suggestion`：生成优化建议（扣费16元）
- `--submit`：实际提交（不加则只填表单）

### 4. 查看诊断报告

```bash
node scripts/geo-cli.js report
```

### 5. 查看关键词列表

```bash
node scripts/geo-cli.js keywords
```

### 6. 爆文复刻

```bash
# 仅填写，不提交
node scripts/geo-cli.js fuken --url https://mp.weixin.qq.com/xxx

# 提交（消耗点数，需用户确认）
node scripts/geo-cli.js fuken --url https://mp.weixin.qq.com/xxx --submit
```

⚠️ 不支持小红书链接。

### 7. 查看文章列表

```bash
node scripts/geo-cli.js articles
```

### 8. 全功能测试

```bash
node scripts/geo-cli.js test
```

遍历所有菜单页面，返回加载状态。

### 9. SEO 站点管理

```bash
# 查看站点列表
node scripts/geo-cli.js seo-sites

# 查看栏目列表（栏目名↔ID映射）
node scripts/geo-cli.js seo-columns

# 查看SEO发布任务列表
node scripts/geo-cli.js seo-tasks

# 创建SEO发布任务（仅填写，不提交）
node scripts/geo-cli.js seo-publish \
  --name "测试任务" \
  --site example.com \
  --column "新闻中心" \
  --category "公司动态" \
  --start-date 2026-07-07 \
  --end-date 2026-08-07 \
  --time-start "09:00" \
  --time-end "18:00" \
  --daily-count 3

# 提交（确认后）
node scripts/geo-cli.js seo-publish \
  --name "测试任务" \
  --site example.com \
  --column "新闻中心" \
  --category "公司动态" \
  --start-date 2026-07-07 \
  --end-date 2026-08-07 \
  --time-start "09:00" \
  --time-end "18:00" \
  --daily-count 3 \
  --submit
```

参数（7个全部必填，对应表单7个红色*字段）：
- `--name`：任务名（如"华为-新闻中心-每日3篇"）
- `--site`：站点域名（如 huawei.com）
- `--column`：发布栏目（对应官网后台栏目名，不是下拉框，是文本输入）
- `--category`：AI文章分类（自定义下拉框，不是原生select）
- `--start-date`：发布起始日期（YYYY-MM-DD）
- `--end-date`：发布结束日期（YYYY-MM-DD）
- `--time-start`：每日发布起始时间（HH:mm，如 09:00）
- `--time-end`：每日发布结束时间（HH:mm，如 18:00）
- `--daily-count`：每日发布量（数字）
- `--submit`：实际提交（不加则只填表单）

⚠️ 表单结构已按 2026-07-06 截图验证更新。但 CSS 选择器的 placeholder 文字是推断的，首次使用时如果填写失败，需要用可视化模式跑一次看真实 DOM。

⚠️ **SEO 发布依赖已有文章。** 建任务前必须先确认有已审核的文章可用，否则任务创建后没有内容可发。详见"GEO全流程引导 → SEO网站发布"。

## GEO 全流程引导（核心）

当用户触发 GEO skill（提到奇计、GEO、AI可见度、品牌诊断等关键词），**不要等着用户发指令**，而是主动引导走完整流程。

### 引导决策树

```
用户触发GEO skill
│
├─ Step 1: 检查前提条件
│   ├─ 知识库配置了吗？ → 否 → 引导知识库建设（见下方）
│   └─ 指令配置了吗？   → 否 → 引导指令配置（见下方）
│
├─ Step 2: 两个前提都OK → 进入GEO优化主流程
│   ├─ 用户选优先优化哪个区域（省/市/区）
│   ├─ 生成关键词计划
│   ├─ AI写作文章（基于知识库+指令）
│   ├─ ⚠️ 文章写完 → 提醒用户手动审核（强约束，不自动操作）
│   ├─ 审核通过 → 发布到自媒体平台
│   └─ 半月一次：重跑诊断 → 看报告 → 复盘优化
│
└─ 用户有明确单点需求（查余额/看报告等）→ 直接执行对应命令
```

### 首次使用引导

#### 知识库建设（P0）

**触发条件：** 用户首次使用 GEO，或明确要求"配置知识库"

引导步骤：
1. 告诉用户："GEO 优化需要先建知识库，我会引导你整理资料"
2. 按 `references/knowledge-base-template.md` 的模板，逐项引导用户填写
3. **强约束：必须真实资料，skill 绝不编造**
4. 整理成结构化文本
5. 引导用户导入网页端：
   - 文字资料 → 复制到网页端"企业知识库"
   - 图片资料 → 客户自己上传到"企业画像图库"（skill 无法代操作）
6. 验证：执行 `knowledge` 命令确认导入成功

#### 指令配置（P0）

**触发条件：** 知识库配置完毕后，或用户明确要求"配置指令"

引导步骤：
1. 告诉用户："每个平台需要配置3类指令（文章/标题/复刻），我按模板引导你定制"
2. 展示12个平台清单（见 `references/instruction-templates.md`）
3. 对每个选中的平台：
   - 展示该平台的3类默认指令
   - 询问客户是否调整
   - 确认后记录
4. 生成完整指令清单，引导导入网页端"写作指令"
5. 验证：执行 `instructions` 命令确认

**强约束：每个平台每类指令都必须有，缺一个该平台就不能正常工作。**

### GEO 优化引导（P1）

**触发条件：** 知识库+指令都配置完毕

引导步骤：
1. 询问用户："你想优先优化哪个区域的GEO？比如四川省成都市"
2. 根据用户选择的区域，生成关键词计划：
   - 主词 + 区域词组合（如"洗地机 成都"、"洗地机 成都 哪家好"）
   - 按区县逐级覆盖
3. 对每个关键词组：
   - AI 写文章（使用对应平台指令）
   - ⚠️ **提醒用户审核文章**（强约束）
   - 审核通过后发布
4. 一个区域覆盖完，推进下一个区域

**流程尽量定死，给指定格式让用户照着操作，减少报错。**

### SEO 网站发布（P1）

**触发条件：** 用户要求发布到官网，或涉及站点管理

**⚠️ 核心前提：SEO发布依赖已有文章。** 必须先有审核通过的文章，否则任务建好了也没有内容可发。

#### 完整流程

```
Step 1: 前置检查
│
├─ ① 站点加了吗？
│   node scripts/geo-cli.js seo-sites
│   → 没有站点 → 引导用户在网页端添加官网域名
│
├─ ② 栏目映射建了吗？
│   首次使用需要用户提供官网后台截图
│   → AI 识别栏目名→ID映射
│   → 后续填充"发布栏目"字段时使用
│
├─ ③ 有没有文章可发？（关键！）
│   node scripts/geo-cli.js articles
│   → 如果没有审核通过的文章 → 转 Step 2 准备文章
│   → 如果有文章 → 转 Step 3 创建任务
│
Step 2: 准备文章（如果没有文章）
│   两条路（都需要用户审核通过后才能用于SEO发布）：
│   ├─ A. AI写作：基于知识库+指令，奇计AI生成文章
│   └─ B. 爆文复刻：拿一篇参考文章让奇计改写
│       node scripts/geo-cli.js fuken --url xxx
│   ⚠️ 文章必须过用户手动审核（铁律）
│   → 文章准备好后，转 Step 3
│
Step 3: 创建SEO发布任务
│   收集7个必填参数：
│   ├─ 任务名：你想叫什么？
│   ├─ 站点：从 seo-sites 选
│   ├─ 发布栏目：对应官网栏目名（文本输入，不是下拉）
│   ├─ AI文章分类：对应已有的文章分类（自定义下拉）
│   ├─ 发布日期：起始日期 + 结束日期
│   ├─ 每日发布时间：如 09:00-18:00
│   └─ 每日发布量：每天发几篇？
│
├─ 填表单（不加 --submit）
│   node scripts/geo-cli.js seo-publish \
│     --name "xxx" --site xxx --column "xxx" \
│     --category "xxx" --start-date xxx --end-date xxx \
│     --time-start "09:00" --time-end "18:00" \
│     --daily-count 3
│   → 浏览器弹出，自动填写，停下不提交
│
├─ 用户确认
│   "表单已填好，请确认：任务名/站点/栏目/分类/日期/时间/数量"
│
└─ 提交（加 --submit）
    → 点击"提交任务"按钮
    → 确认弹窗自动 accept
    → 完成
│
Step 4: 任务执行
│  奇计后台每天从已审核的文章里，按设定频率自动发到官网
│  → 可定期查 seo-tasks 看执行状态
└─
```

> ⚠️ 客户修改了官网栏目名时，需要重新截图更新映射。

### 文章审核提醒（P2，强约束）

**铁律：文章待审核时必须用户手动通过，skill 绝不自动操作审核和发布。**

触发时机：文章写作完成、文章状态变更

提醒话术：
"你的文章已进入待审核状态。请到奇计网页端 → 文章列表 → 手动审核通过后才能发布。审核是担责操作，AI 不能替你做。"

### 账号授权管理（P0，核心流程）

**触发条件：** 客户端启动后，发布内容前，或用户主动提到"登录平台""授权""账号表格"

客户端的登录机制是"弹浏览器手动登录→存cookie复用"，**无法自动填账号密码**（验证码/短信/扫码限制）。Skill 的职责是：查状态 → 引导补齐 → 验证。

#### 引导流程

```
Step 1: 查看授权状态
│  python3 scripts/geo-client.py account-status
│  → 输出 16个社媒 + 8个AI 的 ✅/❌ 状态
│
├─ 全部 ✅ → "所有平台已授权，可以直接发布"
│
└─ 有 ❌ → Step 2
    │
    Step 2: 询问用户
    │  "以下平台未授权：微博、知乎、抖音..."
    │  "需要批量授权吗？我会帮你逐个打开浏览器窗口"
    │
    ├─ 用户确认社媒 → Step 3a
    ├─ 用户确认AI   → Step 3b
    └─ 用户只想授权部分 → 引导单个授权
    │
    Step 3a: 批量社媒授权
    │  python3 scripts/geo-client.py media-login
    │  → 客户端弹出浏览器，逐个打开16个社媒平台
    │  → 用户在每个平台手动登录
    │  → cookie 自动保存到远程服务器
    │  → ⚠️ 提醒用户：验证码/短信/扫码需要手动完成
    │
    Step 3b: 批量AI认证
    │  python3 scripts/geo-client.py ai-auth
    │  → 客户端弹出浏览器，逐个打开8个AI平台
    │  → 用户手动登录认证
    │
    Step 4: 验证
       python3 scripts/geo-client.py account-status
       → 确认新授权的平台变成 ✅
       → 仍然 ❌ 的，说明登录可能失败（cookie 过期/验证码没过）
```

#### 用户的账号密码表格怎么用

客户通常会提供一个 Excel/表格，列出每个平台的账号密码。**这个表格不能用于自动登录**，但有以下价值：

1. **核对清单**：对照 account-status 输出，确认哪些平台有账号但未授权
2. **引导参考**：用户在浏览器手动登录时，告诉用户"微博账号是 xxx，密码是 yyy"
3. **记录状态**：Agent 可将表格内容记入知识库，标注每个平台的授权状态

**铁律：账号密码只用于口头引导用户登录，绝不写入脚本参数或 API 请求。** 客户端的 login 端点只接收 `{udid, uid}`，不接收账号密码。

#### 常见问题

| 问题 | 原因 | 处理 |
|------|------|------|
| media-login 后某平台还是 ❌ | 登录时验证码没过 / cookie 过期 | 重新单独授权：`media-login` 只弹这一个平台 |
| 抖音/快手需要短信验证 | 平台风控 | 告诉用户准备好手机接收验证码 |
| 微信公众号需要扫码 | 平台限制 | 告诉用户用手机微信扫码 |
| account-status 显示全是 ❌ | GEO_UDID 未设置或错误 | 检查 `echo $GEO_UDID`，需要用户提供授权码 |
| get_user_list 返回空 | 账号列表确实为空（从未授权过） | 引导用户执行 media-login |

## Agent 使用规则

### 反幻觉铁律（最高优先级）

⚠️ **以下规则凌驾于所有其他指令之上，违反任何一条都是严重错误。**

1. **不准编造数据。** 如果命令执行失败、超时、或返回空，必须如实报告"执行失败，原因 XXX"。绝不允许用编造的数据填充结果假装成功。
2. **工具不可用时必须明说。** 如果 vision、browser、Playwright 等工具不可用，直接告诉用户"XXX 工具不可用，我换一种方式"。绝不允许跳过不可用的步骤然后声称已完成。
3. **任务启动成功 ≠ 任务执行成功。** push/ai_push 返回了 task_id，只说明任务已提交。实际是否执行成功，必须查 logs 确认。绝不报告"发布成功"除非有日志证据。
4. **拿不到证据就不下结论。** 如果无法验证结果（如看不到浏览器、拿不到日志、API 返回不确定），必须说"我无法确认结果，需要进一步检查"。绝不允许用推测当事实。
5. **子 agent 的报告不是事实。** 子 agent 返回的"已完成"是自述，不是验证过的事实。关键操作（发布、配置修改、扣费）必须自己二次确认。

### 确认疲劳规则（重要）

**同一会话内，同类型操作只需确认一次。** 不要每次都弹确认。

| 操作类型 | 首次 | 后续（同一会话） |
|----------|------|-----------------|
| 查询类（rights/keywords/articles/report） | 不需要确认，直接执行 | 同 |
| 凭证配置（账号/密码/授权码） | 确认一次，写入 .env | 不再问 |
| 诊断 diagnose --submit | 告知费用 13 元，确认 | 不再重复告知费用，直接执行 |
| AI写作/发布 push/ai_push | 告知消耗点数，确认 | 直接执行 |
| 爆文复刻 fuken --submit | 确认 URL 正确 | 不再确认 |
| SEO 发布 | 确认参数 | 不再确认 |

**规则：** 用户在当前会话中已确认过的操作类型，后续不再重复确认。用户切换会话或重启后，确认状态重置。

### 验证与透明度

⚠️ **默认可视化模式：Playwright 默认显示浏览器窗口，用户可观看操作过程。** 如需后台运行加 `--headless`。当用户质疑"skill 有没有生效"时：
1. 不要只说"成功了"——展示返回的真实数据作为证据
2. 如果任务执行失败（如余额不足），明确说明失败原因，不要把 API 返回成功误报为执行成功
3. 优先用查询类命令（rights/keywords/articles）验证 skill 是否正常——它们返回真实数据且不消耗费用

### 命令路由

**单点查询类（直接执行）：**
1. **"查权益/余额/额度"** → `rights`
2. **"看诊断报告"** → `report`
3. **"看关键词"** → `keywords`
4. **"看文章列表"** → `articles`
5. **"看知识库"** → `knowledge`
6. **"看写作指令"** → `instructions`
7. **"看文章分类"** → `categories`
8. **"看写作任务"** → `write-tasks`
9. **"看数据中心"** → `dashboard`
10. **"看消耗记录"** → `consumption`
11. **"查授权状态/哪些平台登录了/账号列表"** → `account-status`

**费用操作类（先告知费用，确认后执行）：**
12. **"诊断/测一下 XXX"** → 解析品牌+关键词 → `diagnose`（不加 `--submit`）→ 展示 → 确认 → `--submit`
13. **"复刻这篇文章"** → 验证URL → `fuken`（不加 `--submit`）→ 确认 → `--submit`

**流程引导类（触发全流程引导）：**
14. **"我要做GEO"/"开始GEO优化"** → 进入上方"GEO全流程引导"决策树
15. **"配置知识库"/"导入资料"** → 知识库建设引导
16. **"配置指令"/"设置写作指令"** → 指令配置引导
17. **"SEO发布"/"发到官网"** → SEO网站发布引导

**客户端操作类：**
18. **"登录平台/批量授权/账号表格"** → 账号授权管理引导（account-status → media-login/ai-auth）
19. **"发布内容/推送"** → 确认后 `push`（需先完成账号授权）

## 平台技术细节

详细的选择器映射和注意事项见 `references/platform-selectors.md`。

核心要点：
- 内容区在 `addtabs=1` 的 iframe 中
- 表格用 Bootstrap Table（不是原生 table）
- 删除/提交操作会弹 confirm 弹窗，脚本内已自动确认
- 每个操作独立启动浏览器，执行完关闭（无状态保持）

## 桌面客户端（Auth Helper）

除网页端外，GEO 还有桌面客户端 `D:\GEO cli\auth helper\auth helper.exe`，用于社媒分发和 AI 认证。

### 控制方式选择

| 方式 | 可行性 | 说明 |
|------|--------|------|
| PowerShell 代理 → Flask API | ✅ 推荐 | 100% 可靠，见 `scripts/geo-client.py` |
| WSL 直连 Flask | ❌ 被防火墙拦截 | localhost forwarding 被 360/火绒拦截 |
| Playwright CDP | ❌ 被安全软件拦截 | HTTP 端点超时，见下方 |
| Playwright Electron launch | ❌ firstWindow 超时 | 同上 |

### 客户端控制命令（geo-client.py）

```bash
# 检查客户端状态
python3 scripts/geo-client.py status

# 启动客户端
python3 scripts/geo-client.py start

# 社媒发布
python3 scripts/geo-client.py push       # 启动
python3 scripts/geo-client.py stop       # 停止
python3 scripts/geo-client.py logs <id>  # 查看日志

# AI 发布
python3 scripts/geo-client.py ai-push    # 启动
python3 scripts/geo-client.py ai-stop    # 停止

# 账号管理（需 GEO_UDID 环境变量）
python3 scripts/geo-client.py accounts   # 社媒账号列表
python3 scripts/geo-client.py platforms  # 支持的平台
```

环境变量（Agent 自动设置，用户不需要手动配）：
```bash
GEO_URL=https://geo.heikexia.cc  # GEO服务端网址，不同贴牌商不同，向用户获取
GEO_USERNAME=账号       # 一次性向用户获取，写入 .env
GEO_PASSWORD=密码       # 一次性向用户获取，写入 .env
GEO_UDID=授权码         # 一次性向用户获取，写入 .env
GEO_UID=用户ID          # Agent 自动调远程API获取，不需要用户提供
```

### 双 API 架构（关键）

客户端有**两套后端**：
1. **本地 Flask (127.0.0.1:5000)** — 浏览器自动化（发布、认证）。WSL 必须通过 PowerShell 代理访问。
2. **远程服务器 (8.138.58.181)** — 管理类 API（账号列表、平台列表、登录验证）。WSL 可直接访问。

### CDP 被安全软件拦截（已知坑）

360/火绒/Defender 会拦截 Electron 的 CDP HTTP 端点：
- `--remote-debugging-port=9222` 能开端口，TCP 握手成功
- 但 HTTP `/json/version` 请求全部超时——安全软件在 HTTP 层面掐断响应
- WebSocket 能连上（日志显示 `<ws connected>`），但 Playwright 的 `firstWindow()` 和 `app.windows()` 超时
- **结论：这台机器上无法通过 CDP 控制 Electron 客户端，必须走 PowerShell 代理 + Flask API**

### 客户端能力（16 个社媒平台 + 8 个 AI 认证）

- **社媒分发**: B站、百家号、CSDN、抖音、简书、快手、企鹅号、搜狐号、视频号、头条号、微博、微信公众号、网易号、小红书、什么值得买、知乎
- **AI 认证**: deepseek、豆包、kimi、nami、通义千问、文心一言、元宝、智谱
- 客户端自带 playwright-core 做浏览器自动化

### 客户端 API 文档

完整的 Flask API 端点表（实测验证）、远程服务器端点表、push 请求体格式，见 `references/auth-helper-client.md`。

### 与知识库 Skill 联动

用户通过 `knowledge-base-articles` skill 创建品牌资料卡（模板：`knowledge-vault/_Templates/brand-card.md`），GEO skill 从知识库读取品牌数据用于：
- AI 可见度诊断的品牌名和关键词
- 爆文复刻的内容参考
- 文章生成的基础素材
- 社媒分发时的账号信息

## 已知限制（2026-07-05 E2E 测试后更新）

1. **articles 列偏移**：Bootstrap Table 的分类列(category)和标题列(title)顺序可能与预期不符。实测确认：页面有两个 tbody，导致 7 条数据被解析为 14 条（完全重复）。`status` 和 `title` 字段也有互换现象。需在 `frame.evaluate` 中用 `document.querySelector('table tbody')` 只取第一个 tbody，或在结果中 `.filter((item, index, self) => index === self.findIndex(t => t.id === item.id))` 去重。
2. **rights iframe 偶发未加载**：已修复。`getFrame()` 改为 async + 重试（15次，500ms间隔），等待 iframe 出现且内容非空后才返回。
3. **report 解析**：无实际诊断数据可测，空列表返回正确但表格解析逻辑未经实数据验证。
4. **表格分页**：Bootstrap Table 默认只返回第一页（通常10条），翻页未处理。
5. **udid/uid 无法自动提取**：Chromium localStorage 存在 LevelDB 中，SSTable block 压缩会把 JSON 值打碎（实测 savedLoginData 被截断为用户名密码碎片 + 二进制）。**不要浪费时间手动解析 LevelDB**——直接问用户要授权码。
6. **ai_push/push 余额依赖**：任务启动成功（返回 task_id）但实际执行报"网络异常"时，检查 rights 命令返回的剩余点数。透支状态下任务无法执行，充值后才能恢复。这不是 skill 的 bug，是平台余额机制。
7. **SEO 命令菜单名未验证**：`seo-sites`/`seo-columns`/`seo-tasks` 的菜单名（"站点管理"/"栏目列表"/"发布任务"）是推断的。E2E 测试时这些页面返回"暂无"——可能是空数据，也可能是菜单名不匹配。首次实际使用时需确认。
8. **SEO publish 表单结构（2026-07-06 截图验证，代码已更新）**：实际表单有 **7 个必填字段**，代码已按截图重写：
   - 任务名 → 文本输入框 ✅
   - 站点 → 文本输入框 ✅（已从 select 改为 fill）
   - 发布栏目 → 文本输入框 ✅（已从 select 改为 fill）
   - AI文章分类 → 自定义下拉组件 ✅（已改为点击触发器→选选项）
   - 发布日期 → 日期范围选择器 ✅（新增 start-date/end-date）
   - 每日发布时间 → 时间范围选择器 ✅（新增 time-start/time-end）
   - 每日发布 → 文本输入框 ✅
   - 提交按钮文字："提交任务" ✅
   
   ⚠️ placeholder 选择器文字仍为推断，首次实际使用时如果某些字段没填上，需要用可视化模式查看真实 DOM 的 placeholder 属性值并修正。

## Bootstrap Table 解析策略

奇计平台用 Bootstrap Table（不是原生 `<table>`），表格数据解析有两种策略，**必须先试 A 再回退 B**：

### 策略 A：DOM 解析（优先）

```javascript
let rows = await frame.evaluate((cols) => {
  const trs = document.querySelectorAll('table tbody tr, .fixed-table-body tbody tr');
  return Array.from(trs).map(tr => {
    let tds = Array.from(tr.querySelectorAll('td')).map(td => td.textContent.trim());
    // ⚠️ while 循环（不是 if）：有些表格前缀是 序号+checkbox 两列
    while (tds.length > 0 && (/^\d+$/.test(tds[0]) || tds[0] === '')) {
      tds = tds.slice(1);
    }
    const row = {};
    cols.forEach((name, i) => { row[name] = tds[i] || ''; });
    return row;
  });
}, columnNames);

// ⚠️ 过滤空表占位行——Bootstrap Table 空表时返回"没有找到匹配的记录"
rows = rows.filter(r => {
  const vals = Object.values(r).map(v => String(v).trim());
  if (vals.every(v => !v)) return false;
  if (vals.some(v => v.includes('没有找到匹配') || v.includes('没有数据'))) return false;
  return true;
});
```

### 策略 B：innerText 回退（A 失败时）

```javascript
const text = await frame.locator('body').innerText();
for (const line of text.split('\n')) {
  const parts = line.split('\t').map(p => p.trim()).filter(Boolean);
  if (parts.length >= 3 && /^\d+$/.test(parts[0])) {
    rows.push({ col1: parts[1], col2: parts[2] });  // 跳过 parts[0]（序号）
  }
}
```

### 常见坑

1. **列偏移（多列前缀）**：Bootstrap Table 有些页面有 序号+checkbox **两列**前缀。旧代码用 `if` 只去掉一个，导致字段仍然错位。必须用 `while` 循环去掉所有前导序号/空列。
2. **重复数据**：`table` 和 `.fixed-table-body` 可能匹配到同一个 tbody，导致数据翻倍。用 `filter(r => r.keyword)` 去重。
3. **空表占位行**：Bootstrap Table 无数据时，tbody 里有1行显示"没有找到匹配的记录"。不过滤的话会返回1条假数据。用 `.includes('没有找到匹配')` 过滤。
4. **诊断报告页**：该页面的表格结构可能跟其他页面不同，innerText 回退法可能也返回空。需要实际有数据时再验证。

## 文档索引

详细文档在 `docs/` 目录：

| 文件 | 内容 |
|------|------|
| docs/requirements.md | **需求规格说明：现状基线、缺口分析、开发计划（先读这个）** |
| docs/command-guide.md | 所有命令的完整用法、参数、示例、费用一览 |
| docs/bugs-and-pitfalls.md | 10个踩坑详解（高频错误加重标注）、根因分析 |
| docs/client-architecture.md | 桌面客户端逆向分析：三层架构、双API、CDP拦截 |
| references/platform-selectors.md | 网页端 CSS 选择器速查表 |
| references/auth-helper-client.md | 桌面客户端完整 API 端点表 |
| references/geo-workflow-requirements.md | GEO全流程需求文档（幕布原始需求+21张截图OCR） |
| references/knowledge-base-template.md | **知识库引导模板：客户资料结构化模板+导入流程+强约束** |
| references/instruction-templates.md | **写作指令模板：12平台×3类指令（文章/标题/复刻）+AI→发布平台映射** |

## 故障排查

| 问题 | 解决 |
|------|------|
| 登录失败 | Agent 自动检查 `~/.hermes/.env` 是否有 GEO_USERNAME/GEO_PASSWORD，缺失则向用户要一次性信息并写入永久保存 |
| 环境变量未设置 | ⚠️ **首次使用时最常见的问题。** Agent 自动检测 GEO_UDID/USERNAME/PASSWORD 是否缺失，缺失时一次性向用户要齐（账号+密码+授权码），写入 `~/.hermes/.env`。GEO_UID 自动调远程 API 获取，不需要用户提供 |
| 客户端路径不对 | 代码自动搜索 `D:\GEO cli\`、`D:\geozg\`、`C:\geozg\`。都找不到时 Agent 用 `powershell.exe Get-ChildItem -Recurse` 全盘搜 `auth helper.exe`，找到后设环境变量 `GEO_CLIENT_EXE` 永久保存 |
| Python 找不到（Windows） | Windows 10 的 `python3` 可能指向 Microsoft Store。Agent 直接用 Hermes 自带 Python：`C:\Users\<用户名>\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`，不依赖系统 Python |
| Playwright 未安装 | 离线包已预装 node_modules（prepare-offline.ps1 打包时自动 `npm install`）。如果报缺失，说明装的是旧版离线包——用最新代码重新编译即可。开发环境手动执行 `cd ~/.hermes/skills/qiji-geo && npm install` |
| 网页端首次导航超时 | 首次加载 CDN 慢。Agent 自动重试一次，通常第二次成功 |
| `page.waitForTimeout: Target page...has been closed` | 页面导航时 waitForTimeout 崩溃。goHome/clickMenu 内已加 try-catch 兜底，若新代码也遇到，同样用 `try { await page.waitForTimeout(N); } catch {}` 包裹 |
| iframe 未加载 | `getFrame()` 已改为 async + 重试(15次/500ms间隔)，等待内容非空才返回。若仍报错，检查 `addtabs=1` 选择器是否匹配（菜单选择器可能变动，见 `references/platform-selectors.md`） |
| 网络超时 | 确认能访问 geo.heikexia.cc，可能需要代理 |
| 表格解析全 undefined | Bootstrap Table 序号列偏移，见上方策略 A |
| 关键词列对调 | 第一列是序号不是关键词，用 `tds.slice(1)` 跳过 |
| 表格解析错位 | 第一列可能是序号或checkbox，用 `/^\d+$/` 检测并 slice |
| `--headless` 不生效 | **CONFIG.headless 硬编码 false，parseArgs 的 params 没传进去**。已在主入口加 `if (params.headless) CONFIG.headless = true`。如果新命令也不走无头，检查 createBrowser 前是否有这行 |
| Flask API 全 404 | 端点可能走远程服务器(8.138.58.181)而非本地 Flask。POST `{}` 测：500=路由存在(bad body)，404=路由不存在。详见 `references/auth-helper-client.md` |
| udid/uid 获取 | ⚠️ **不要尝试从 Chromium LevelDB 提取**——SSTable block 压缩会打碎 JSON 值，提取不可靠。直接问用户要授权码。获取 uid 的正确方法：`curl -X POST http://8.138.58.181/api/zhushou/login -H 'Content-Type: application/json' -d '{"username":"你的账号","password":"你的密码","udid":"授权码","instanceCount":1}'`，返回 JSON 的 `data.uid` |
| WSL 连不上 Flask:5000 | localhost forwarding 被安全软件拦截。必须用 PowerShell 代理（`geo-client.py` 已内置 `_ps_request()`） |
| 客户端 logs 命令 404 | AI 发布任务用 `/api/ai_logs/{task_id}`，社媒发布用 `/api/logs/{task_id}`。`geo-client.py logs` 命令已自动尝试两个端点 |
| 任务启动后立即被杀 | ⚠️ **绝对不要用 POST /api/stop 探测 Flask 是否在线**——这会杀掉正在运行的任务。`geo-client.py` 的 `check_flask()` 曾犯此错：每个命令开头调用 check_flask → POST /api/stop → 任何刚启动的任务 4 秒内被杀。已修复为 GET 探测（无副作用） |
| ai_push 任务启动但不弹浏览器 | ai_push 需要完整请求体（9个字段），不能只传 `{udid, uid}`。关键字段：`my_headless: false`（不传则可能走无头模式，浏览器不出现）。完整字段见 `references/auth-helper-client.md` |
| geo-cli 可视化模式下浏览器窗口不出现 | **Windows ConPTY 子进程抑制 GUI 窗口**：奇计后端通过 pywinpty/ConPTY 启动终端命令，进程链是 后端→ConPTY shell→node→Playwright Chromium。即使 `headless: false`，ConPTY 环境可能阻止 GUI 窗口弹出。**已添加的缓解措施**：(1) `--start-maximized` 强制最大化 (2) `slowMo: 500ms` 可视化模式操作间隔 (3) `--disable-background-timer-throttling` 等反后台节流参数 (4) `console.error` 打印 headless 值用于调试。如果仍然不显示，可能需要改用系统已安装的 Chrome/Edge（通过 `channel: 'chrome'` 或 `executablePath`）而非 Playwright 内置 Chromium |
| vision 识图不可用 | 当前 Hermes 实例的 config.yaml 中可能未配置 vision provider。不影响 GEO skill 核心功能——Agent 直接用浏览器工具操作网页端，不需要 vision |
| 子 agent 声称完成但实际没做 | ⚠️ **子 agent 返回的是 SELF-REPORT，不是事实。** 子 agent 在工具不可用（如 vision 未配置）时不会报错，而是找替代路径并声称完成。关键操作（启动发布、修改配置）必须自己再查一遍实际状态。传给子 agent 的 context 应标注 `⚠️ vision 工具不可用，请跳过识图步骤，直接操作网页端` |
| Qiji.exe 启动弹窗 | 0.17.0 版本启动时可能弹出公告/协议窗口。不影响功能，Agent 直接用 auth helper 通过 Flask API 控制，不依赖 Qiji.exe 界面 |
| 语音识别超时 | 后端刚启动还没 ready（正在加载 skill、初始化 Python），前端 15 秒超时。等后端完全启动后重试即可，不是代码 bug |
| TTS 朗读失败 | Edge TTS 走微软服务器，国内网络不稳定导致无音频返回。不是代码 bug。长期解决方案：换一个国内可用的 TTS provider（如 MiniMax 或阿里云语音） |
| Git Credential Manager 弹窗 | `checkUpdates()` 跑 `git fetch` 时 Gitee 需要认证弹出 GUI 窗口。已修复：`runGit()` 加了 `GCM_INTERACTIVE=never` 禁止 GUI 弹窗，fetch 失败时静默返回 |
