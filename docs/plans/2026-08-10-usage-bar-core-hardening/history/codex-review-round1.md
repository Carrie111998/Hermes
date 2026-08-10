# Codex 评审 Round 1 — VERDICT: CHANGES_REQUESTED (2026-08-10)

评审方式：`codex exec --sandbox read-only`（专用 CODEX_HOME，见 CHECKPOINT blocker 4）。

## 问题清单

1. **[高] cache identity 信任持久化 fingerprint** — `agent/usage_contract.py:110`
   `_credential_fingerprint()` 无条件优先 `secret_fingerprint`；env 凭证运行时
   token 轮换而 fingerprint 未同步时 identity 不变，可能把旧账号额度缓存展示给
   新账号。修复：identity 的 credential 分量始终基于当前 runtime_token 的域
   分隔哈希；持久化 fingerprint 不作 identity 依据。补"同一 env 引用 + 旧
   fingerprint + token 轮换"的缓存隔离测试。
2. **[中] worker 生命周期** — 6.5s 是调用方返回 deadline，不是 worker 上限；
   Kimi timeout 仍 10s；`executor.shutdown(wait=False)` 不能终止运行中线程；
   每次构建独立 executor，连续刷新可累积 late workers。修复：所有 provider
   connect/read/write/pool timeout 严格 < 全局 deadline；singleflight/in-flight
   registry + 进程级并发上限列为 M1 发布前条件；连续多轮 deadline 下活跃
   worker 数有界、旧 future 不复用且最终释放的测试。
3. **[中] 异常分类未实现** — 当前仅 `httpx.TimeoutException` 回退 stale；
   connect error 与 5xx 落通用错误分支，与 PLAN 声明不一致。修复：错误分类器
   （timeout/connect/5xx→stale；401/403→unavailable；429→尊重 Retry-After），
   有/无缓存场景分别测试。
4. **[中] statusbar 迁移粒度** — 一次性迁移从所有已有 hidden 集合删
   `context-usage`，无法区分旧默认值与用户主动隐藏；PLAN 的"用户显式隐藏集合
   不被触碰"声明不成立。修复：仅当存储值与已知旧默认集合匹配时迁移；补"旧默认
   集合迁移"+"用户自定义隐藏保留"两类测试；修正回滚声明。
5. **[中] 发布/回滚证据不足** — 缺校验和、版本/commit 对应、恢复步骤、回滚
   顺序、双向兼容验证。修复：回滚包记录 SHA-256+构建 commit+版本+兼容范围；
   Desktop/后端独立回滚顺序与 smoke test；新客户端↔旧后端双向兼容测试。

## 通过项

- contract v1 可选字段扩展确为 additive；
- 序列化测试已覆盖 access/refresh token 不外泄；
- `_profile_scoped` 与 `get_hermes_home()` 的 ContextVar 绑定方向正确；
- profile cache 隔离测试须在发布前落地（已在测试矩阵，升级为发布前条件）。
