# Codex 评审 Round 3 — VERDICT: CHANGES_REQUESTED (2026-08-10)

## 遗留问题（均为高）

1. **R2#1 四态状态机仍有竞态**："entry settle → 移除"未规定 compare-and-remove；
   旧 orphan future 晚完成可能误删替代 entry。修订：settle 仅在 registry 仍指向
   该 entry（identity/generation 匹配）时移除；orphan settle 不写缓存、不触碰
   registry；补"旧 future 在替代登记后完成，新 entry 仍可 join"交错测试。
2. **R2#2 发布阻塞清单不完整**：可靠性 1–4、profile 隔离、制品校验、恢复演练
   之外，遗漏 credential 域分隔+轮换隔离测试、worker 有界性/late-worker 测试、
   双向兼容测试（后者被描述为滚动发布前提却不在阻塞清单，内部矛盾）。修订：
   建立唯一权威的 8 项发布门禁清单，全部阻塞、无例外。

## 已通过

R2#3（实现基线描述）妥善回应。

## Round 3 对应的 PLAN 修订

- 状态机 settle 分支改为 compare-and-remove（generation 匹配才移除）。
- 测试矩阵新增 singleflight 竞态交错测试。
- 新增"发布门禁清单（唯一权威）"8 项，含全部测试/制品/演练门禁。
