# CHECKPOINT — 2026-08-10 M0

## 恢复指引

- 安全锚点：分支 `backup/usage-bar-hardening-pre-m0-20260810`；dirty 快照已移出
  repo（父验收 A3）：`C:\Users\Admin\AppData\Local\hermes\cache\usage-bar-core-hardening\pre-m0-dirty.patch`
  （46,471 bytes，SHA-256
  `201485725060ed142de905d62d888713dd1420b43da641564564adb947d369c0`，已核验一致）。
- 控制面：`docs/plans/2026-08-10-usage-bar-core-hardening/`（本目录）。
- 恢复流程：读 STATE.json → 读本文件 → 按 PLAN.md 未完成的 milestone 继续。

## 事实复核（实时命令输出）

- HEAD `299d5d7d9 feat: add canonical multi-account usage bar`，分支
  `codex/hermes-custom-migrate-v020`，ahead 2 / behind 29（fetch 后复核一致）。
- worktree：仅主 checkout `C:/Users/Admin/AppData/Local/hermes/hermes-agent`。
- Codex CLI 可用：codex-cli 0.144.4。

## Dirty diff 逐文件审计结论

| 文件 | 结论 |
|---|---|
| `agent/usage_contract.py` +318 | 必要：cache identity、6.5s deadline 并发、stale-on-timeout、env token 重读、Codex 过期 token 分支 |
| `agent/account_usage.py` +134 | 必要：kimi-coding provider、Codex timeout 15s→6s |
| `use-statusbar-items.tsx` | 必要：context-usage 可见性改由 gateway open 门控 + label 回退 |
| `statusbar-prefs.ts` | 必要：移出默认隐藏 + 一次性幂等迁移 |
| `statusbar-visibility.test.tsx` | 必要：配套测试 |
| `types/hermes.ts` +2 | 必要：additive `fetched_at`/`stale` |
| `test_account_usage.py` / `test_usage_contract.py` | 必要：配套测试 |
| `package-lock.json` -27 | 噪声（npm 版本差异剥 peer flag），已还原 |
| `release-backup-usage-20260810-114009/` | release 回滚包（765MB win-unpacked），已归档至 hermes/cache/release-backups/ |

## 依赖符号核实

`DaemonThreadPoolExecutor`(tools/daemon_pool.py:37)、
`_codex_access_token_is_expiring`(hermes_cli/auth.py:2544)、
`get_env_prefer_dotenv`(agent/credential_pool.py:2861)、
`readJson/readKey/writeJson/writeKey`(apps/desktop/src/lib/storage.ts) 均存在。

## 测试基线（2026-08-10 12:0x 真实输出）

- `uv run pytest tests/agent/test_usage_contract.py tests/tui_gateway/test_usage_accounts.py -q`
  → **11 passed in 1.32s**（exit 0）
- `uv run pytest tests/agent/test_account_usage.py -q` → **6 passed in 0.70s**（exit 0）
- `npx vitest run --project ui src/app/shell/context-usage-panel.test.tsx
  src/app/shell/statusbar-visibility.test.tsx`（apps/desktop）
  → **13 passed (2 files) in 2.48s**（exit 0）
- 合计 **30/30**，与 brief 预期 focused baseline 一致。
- `npm run typecheck`（tsc 三 config）→ exit 0；`npm run build`（vite + electron main
  bundle + native deps + assert-dist-built）→ exit 0。
- `git diff --check` → clean。
- package/release smoke：**未执行**——本 checkout 是 Hermes 运行中的 live source，
  guard 禁止 rewrite 类操作；electron-builder 打包会改写 dist/ 与版本戳，登记为
  blocker 交父会话决策（现有回滚包仍在 cache/release-backups/）。

## M0 commit 结果

- `c64d5af1f feat(agent): harden usage.accounts contract with cache identity and
  concurrent fetch`（4 files, +761/-44）
- `05b6a1de4 fix(desktop): keep account usage reachable in the statusbar by default`
  （4 files, +41/-10）
- 控制面文档与 mockup 作为第三个 commit（收尾时提交）。

## 上游同步评估

- `git fetch origin main` 后 `git merge-tree --write-tree HEAD origin/main`
  → exit 0，**零冲突**（29 个上游 commit 与本分支改动文件无交集）。
- 实际 `git merge --no-ff origin/main` 被运行时 guard 拦截：本 checkout 是 Hermes
  live source（merge 会混合运行中进程的模块版本）。须停 Hermes 后外部执行，
  或在独立 worktree 操作 → 登记 blocker，本轮不同步。

## 遗留 / Blocker

1. **上游同步未执行**（非冲突问题）：merge-tree 探测零冲突，但 live-checkout
   guard 禁止 agent 内在本目录 merge。建议父会话在 Hermes 停止后外部执行
   `git merge --no-ff origin/main`，合并后重跑本节测试矩阵。
2. **package/release smoke 未执行**：同上 guard 原因；release 回滚包已归档。
3. `C:\Users\Admin\docs\` 空目录残留（一次误写后已迁出文件；`rm -rf` 需人工批准，
   未强行删除，无害）。
4. **Codex CLI 0.144.4 与用户主 `~/.codex/config.toml` 不兼容**（`[agents]
   enabled = true` 被新版解析为 AgentRoleToml struct，config 加载直接报错）。
   未改动用户配置；评审改用既有只读专用 CODEX_HOME
   `C:\Users\Admin\AppData\Local\hermes\cache\codex-review-home`。
5. 单 writer 假设与实际不符：M0 两个 commit 与本文件部分段落由本会话之外的
   行为者落盘。已逐项核验内容等价（commit diff 与 pre-m0-dirty.patch 剔除
   package-lock 噪声后逐行一致），予以采纳。

## 复核附注（writer session 20260810_115223_1ade7f）

- 分文件计数（独立复跑）：test_usage_contract 10 passed /
  test_usage_accounts 1 passed / test_account_usage 6 passed / vitest 13 passed。
- `git diff 299d5d7d9..HEAD` ≡ pre-m0-dirty.patch − package-lock 噪声，零凭证材料。

## 收尾更新（2026-08-10 12:5x）

- **双写者事件**：本会话与另一会话（父会话侧）交错提交同一分支。已核实
  `git log`：历史线性（c64d5af1f → 05b6a1de4 → 0db51dea5(docs) →
  eee304619 → ba45dc8a5），双方工作完整保留，无丢改、无 rebase 冲突。
  对方 STATE 中的 "uncommitted_not_mine" 清单即本会话 R4 修复，已闭环提交。
- **Codex 复核轮 R5：APPROVE**（R4 REVISE 的 6 项最小修改全部 VERIFIED，
  行级依据见 `history/codex-review-round5-verify.md`）。
- 最终测试矩阵：后端 24 passed（usage_contract 16 + account_usage 7 +
  tui_gateway 1... 按文件：test_usage_contract.py+test_usage_accounts.py
  合并 17、test_account_usage.py 7）、Desktop 19 passed、typecheck 三 config
  exit 0、build exit 0、git diff --check clean。
- STATE.json 已修正 commit 清单与 head（ba45dc8a5），状态维持
  `AWAITING_MOCKUP_APPROVAL`。
