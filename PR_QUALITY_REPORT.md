# Hermes Agent PR 质量全景报告
**生成时间**: 2026-07-28 23:42 CST
**仓库**: x7peeps/hermes-agent → NousResearch/hermes-agent
**历史合并率**: 2% (1/50，唯一合并被 revert)

---

## 📊 当前 Open PR 质量分级

### 🟢 S 级 PR（8-10 分）— 精品，主动跟进

| PR # | 标题 | 作者 | 文件数 | +/− | 测试 | 评论 | 评分 | 建议 |
|------|------|------|--------|-----|------|------|------|------|
| 73445 | fix(google-chat): scope multiplex profile config | zyz619963502zyz | 2 | +140/-30 | ✅ | 1 | **8/10** | 等待 review，质量优秀 |
| 73448 | feat(gateway): handoff capability policy + delegation audit | bazgreen | 8 | +2322/-3 | ✅ | 0 | **7/10** | 代码量大但测试充分，需拆分 |
| 73275 | fix(delegation): reject unfinished subagent handoffs | plcunha | 4 | +235/-6 | ✅ | 0 | **7/10** | 有 RED proof，质量优秀 |
| 73260 | fix(dashboard): inherit launch profile in chat deep links | lgy1027 | 7 | +174/-18 | ✅ | 0 | **7/10** | 全栈修复，测试充分 |
| 73450 | fix(slack): ignore thread parent metadata replays | nahyeongjin1 | 2 | +452/-4 | ✅ | 0 | **6/10** | 测试充分，代码量偏大 |
| 73449 | fix(discord): reject empty outbound messages | zyz619963502zyz | 2 | +32/-0 | ✅ | 0 | **6/10** | 精准修复，测试充分 |
| 73267 | fix(windows): discover S4U gateways from profile PID files | Sagittarius987 | 2 | +42/-1 | ✅ | 0 | **6/10** | Windows 专项修复，测试充分 |
| 73261 | fix(mcp): reap orphaned stdio children immediately | JonthanaHanh | 1 | +17/-1 | ✅ | 0 | **5/10** | 精准修复，但需更多测试 |

### 🟡 A 级 PR（5-7 分）— 合格，等待 review

| PR # | 标题 | 作者 | 文件数 | +/− | 测试 | 评论 | 评分 | 建议 |
|------|------|------|--------|-----|------|------|------|------|
| 73264 | fix(kanban): handle missing worker exit signals | sycamoregroupltd | 9 | +492/-52 | ✅ | 1 | **5/10** | 代码量大，与 #70072 重复 |
| 73263 | fix: gate KANBAN_GUIDANCE on HERMES_KANBAN_TASK | rkfshakti | 2 | +54/-1 | ✅ | 1 | **5/10** | 与 #64186 重复，需 maintainer 决策 |

### 🔴 B/C 级 PR（0-4 分）— 低质量，建议关闭

| PR # | 标题 | 作者 | 文件数 | +/− | 测试 | 评论 | 评分 | 建议 |
|------|------|------|--------|-----|------|------|------|------|
| 73278 | fix: add usedforsecurity=False to hashlib.sha1 | alt-glitch | 1 | +1/-1 | ❌ | 1 | **2/10** | 无测试，1 行变更，建议关闭 |
| 73274 | fix(desktop): normalise artifact timestamp units | zapabob | 2 | +55/-1 | ✅ | 1 | **3/10** | 与 #42670/#42409 重复，需 maintainer 决策 |
| 73444 | feat(mcp): add AgentKey catalog entry | liuhao1024 | 1 | +38/-0 | ✅ | 0 | **4/10** | 新功能，但需更多测试覆盖 |

---

## 🎯 质量评分标准

| 维度 | 权重 | 说明 |
|------|------|------|
| **测试覆盖** | 3 分 | 有回归测试 +2，测试覆盖 RED proof +1 |
| **代码量** | 2 分 | 10-500 行 +2（过大说明 scope 不聚焦） |
| **评论/Review** | 1 分 | 有自评论或 maintainer 评论 +1 |
| **Bug 修复** | 1 分 | 修复已知 Issue +1 |
| **Labels 正确** | 2 分 | 有 type/bug +1，无 duplicate/needs-decision +1 |
| **Commit 规范** | 1 分 | 符合 Conventional Commits +1 |

---

## 📈 历史趋势

| 指标 | 当前 | 目标 |
|------|------|------|
| Open PR 数量 | ~20 | <5 |
| 合并率 | 2% | >50% |
| S 级 PR 占比 | 40% | >80% |
| 平均测试覆盖 | 2.5 个/PR | >4 个/PR |
| 平均代码量 | +180/-15 | +50/-10 |

---

## 🔧 优化策略

### 1. 精品 PR 策略（每周 2-3 个）
- ✅ 先 issue 讨论，确认需求后再提 PR
- ✅ 每个 PR 补 2-6 个 focused 测试
- ✅ 本地 `pytest -x -q` 全通过
- ✅ Commit 符合 Conventional Commits
- ✅ PR body 包含 RED proof 和测试输出

### 2. 自动化流水线优化
- ✅ Cron 任务已更新 Prompt，加入质量评分机制
- ✅ 禁用 Bot 自动回复（"working on this fix" / "shortly"）
- ✅ 批量扫描 → 质量分级 → 并行处理 → 手动兜底

### 3. 低质量 PR 处理
- 🔴 直接关闭：#73278（无测试，1 行变更）
- 🔴 建议关闭：#73274（重复，需 maintainer 决策）
- 🟡 等待决策：#73263、#73264（与现有 PR 重复）

### 4. Mac Mini 远程流水线
- ⚠️ 向日葵 MCP 连接成功，但 Keychain 弹窗阻塞 Terminal
- ⚠️ 需要手动关闭 Keychain 弹窗或提供正确密码
- ✅ Cron 任务 `c016106586ff`（Issue 自动修复）和 `df1c5e970a7c`（PR Follow-up）运行正常

---

## 📋 下一步行动

1. **立即执行**（需要你确认）：
   - [ ] 关闭 #73278（无测试，1 行变更）
   - [ ] 关闭 #73274（重复，需 maintainer 决策）
   - [ ] 补充 #73261 的测试覆盖

2. **本周执行**：
   - [ ] 对 #73450、#73449、#73448 等 S 级 PR 补充更多回归测试
   - [ ] 拆分 #73448（代码量 2322 行，scope 过大）
   - [ ] 等待 maintainer review #73445、#73275、#73260

3. **持续优化**：
   - [ ] 每周运行 Cron 任务，自动评分和分级
   - [ ] 监控合并率变化，调整策略
   - [ ] 解决 Mac Mini Keychain 弹窗问题

---

**报告生成**: Hermes PR 质量审查专家
**下次更新**: Cron 任务每 2 小时自动运行
