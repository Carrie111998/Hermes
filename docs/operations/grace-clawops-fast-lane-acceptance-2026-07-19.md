# Grace / ClawOps 快速分流驗收報告

日期：2026-07-19（Asia/Taipei）

## 2026-07-21 交接強化

- `clawops_delegate` 已改為 Grace 實際產生的標準巢狀 Loop Contract；處理端在過渡期仍相容既有扁平排程呼叫。
- Live Grace policy version 3 會固定注入新建的 system prompt，舊版快取提示詞會自動失效重建。
- `openclaw_delegate` 明確限定為診斷 dry-run，Grace／Telegram／Feishu／cron 不得將它當作執行降級路徑。
- 只有取得 `execution_task_id` 與 `grace_review_task_id`，Grace 才能回報已成功指派。
- 本次不會重送或發布先前的望遠鏡刊登；live 驗證維持無外部副作用。
- Live nested-contract smoke：execution `t_e289324e` 由 `clawops-ops / gpt-5.5` 完成，dependent review `t_32a5d1c0` 之後才由 `default / gpt-5.6-terra` 執行，結果 `review_outcome=accepted`、`external_side_effects=none`。
- 精確 Topic memory namespace promotion 因現有 memory tool 不支援指定 namespace 而標為 `pending`；系統沒有退而寫入全域或錯誤 namespace。

### 2026-07-21 10:49 空合約派送修正

- 實際失敗證據不是 handler 遺失參數；`state.db` 的原始 model tool call 為 `clawops_delegate` 加上 `arguments: {}`。
- 根因是 plugin registry 要求完整 function schema（包含 `parameters`），但先前只註冊裸 JSON 參數 schema。schema sanitizer 因此補出空的 `parameters.properties`，模型實際看不到任何欄位。
- `CLAWOPS_DELEGATE_SCHEMA` 與診斷用 `OPENCLAW_DELEGATE_SCHEMA` 已改為完整 function schema；最終 model-visible `parameters` 現在含 12 個欄位與 11 個必填欄位。
- 回歸測試 59 項通過。另以 `gpt-5.6-terra` 做隔離模型端 smoke test，實際 tool call 成功產生完整非空巢狀合約並回傳 `status=queued`。該測試使用 `/tmp` 暫存 Kanban DB，未交給 live dispatcher、未通知 Telegram、未操作 Facebook。

### 2026-07-21 兩階段回覆

- Telegram token streaming 維持關閉，避免 partial edit 與 final send 重複。
- Telegram 的制式計時 `fast_ack` 已關閉；它不再用「收到、處理中」占用第一則回覆。
- 執行任務的第一階段改在 Grace 完成理解並編譯 Loop Contract 後觸發。Gateway 從實際送交的合約產生詳細覆述，包括 Grace 的理解、目標、交付內容、可做／不可做範圍、驗證方式、完成標準與授權界線。
- 覆述刻意不讀取 audit-only 的 `original_request`，並限制項目數、單項與總長度；因此呈現的是 Grace 消化後的指令，而不是轉貼 KJ 原話。
- 第一階段只表示「準備送交」，不宣稱已排隊。第二階段必須等 `clawops_delegate` 回傳 `execution_task_id` 與 `grace_review_task_id` 後才回報任務建立。
- policy version 升級為 5，ClawOps skill 升級為 1.3.0；Grace 不再額外輸出重複的制式 receipt 或 handoff preamble。
- 合約格式、長度限制、原始文字隔離、授權界線、fast ack、stream consumer、ClawOps schema 與 Grace execution boundary 相關測試共 147 項通過。

### 2026-07-21 非 ClawOps 工具任務補強

- 實際回合「請回報目前的刊登進度」用了 `skill_view`、`read_file`、`search_files` 等查核工具，但沒有呼叫 `clawops_delegate`，因此舊觸發條件沒有第一階段訊息。
- 第一階段範圍已擴大至所有需要工具、來源查核或背景工作的請求；只有立即可答、完全不需工具的問答維持單階段。
- version 6 曾加入 raw-request fail-safe；13:49 的 Caffe OTTIMO 任務證明它在第一個 `skill_view` 就搶先回覆並截斷重述 KJ 原話，反而抑制了稍後完成的 canonical Loop Contract confirmation。此設計已撤回。
- version 7 不再啟用 `work_confirmation`。ClawOps 任務的 skill、memory、file、browser 等前置工具不會占用第一階段；只有 `clawops_delegate` 的完整合約可以觸發 handoff confirmation。
- 非 ClawOps 的工具查核必須由 Grace 在模型 response 中自行說明理解、證據與驗證標準；gateway 不得從 raw request 複製、截斷或假裝已理解。
- 回歸測試共 147 項通過，並包含 `original_request` sentinel 不得出現在合約確認中的檢查。

## 驗收結論

四階段調整已完成並通過驗收：

1. Grace 的預設模型由 `gpt-5.5` 切換為 `gpt-5.6-terra`，並設定 `reasoning_effort: none`，作為快速對話與初步判斷入口。
2. 所有自然語言先進入 Grace；Grace 理解完成後才決定直接回答或編譯 Loop Contract，系統不再於 Grace 之前以關鍵字改寫成 `/clawops`。
3. 六個 ClawOps 執行 Profile 全部維持 `gpt-5.5`，沒有變更。
4. Kanban Dispatcher 輪詢由 60 秒縮短為 10 秒；ClawOps 通知補齊「已排隊、已啟動、執行中、完成」。

## 第一階段：即時盤點

調整前的 `~/.hermes/config.yaml`：

- Provider：`openai-codex`
- Grace 預設模型：`gpt-5.5`
- Dispatcher：60 秒
- 六個 ClawOps Profile：全部為 `gpt-5.5`

當日既有 GPT-5.5 紀錄按內容長度分組：短內容平均約 8.4 秒、中等內容約 12.3 秒、大型內容約 24.3 秒；另有 130 至 141 秒的大型任務。這表示體感緩慢不只來自模型，內容長度、工具呼叫、迭代次數與 Dispatcher 等待也會疊加。

## 第二階段：Luna / Terra 受控 A/B

測試條件：同一台機器、同一組 `openai-codex` 認證、同一份 Hermes 設定、相同四組短提示，Luna 與 Terra 交錯各跑 20 次。

| 模型 | 次數 | 平均 | P50 | P95 | 最大值 |
|---|---:|---:|---:|---:|---:|
| `gpt-5.6-luna` | 20 | 8.396 秒 | 7.888 秒 | 11.801 秒 | 14.149 秒 |
| `gpt-5.6-terra` | 20 | 7.948 秒 | 7.824 秒 | 9.074 秒 | 9.465 秒 |

Terra 的平均值快約 0.45 秒，P95 快約 2.73 秒，尾端延遲也較穩定，因此 Grace 採用 Terra。官方模型說明亦將 Terra 定位為智慧、成本與速度的平衡選項，而 Luna 偏向高吞吐、低成本工作：

- https://developers.openai.com/api/docs/models/gpt-5.6-terra
- https://developers.openai.com/api/docs/models/gpt-5.6-luna

## 第三階段：模型與路由分工

即時 Grace 設定：

```yaml
model:
  default: gpt-5.6-terra
agent:
  reasoning_effort: none
```

ClawOps 執行層維持：

- `clawops-browser`: `gpt-5.5`
- `clawops-content`: `gpt-5.5`
- `clawops-dev`: `gpt-5.5`
- `clawops-ops`: `gpt-5.5`
- `clawops-research`: `gpt-5.5`
- `clawops-review`: `gpt-5.5`

原先的關鍵字 pre-dispatch 路由已撤回。事故驗證顯示，它會把釐清問題中的「執行」誤判為新任務，也會因缺少 Topic/project 而回退到 `hub_ops`。目前規則為：

- 所有自然語言先進 Grace；問題、說明、比較、建議與釐清由 Grace 直接回答。
- 真正的執行需求，由 Grace 理解後呼叫 `clawops_delegate`，不得把 KJ 原話當成 worker 指令。
- 委派前必須通過 Topic/project 精確綁定及完整 Loop Contract 驗證。
- Loop Contract 包含 trigger、goal、deliverables、non-goals、scope、verification、evidence、acceptance、stop rules、iteration/runtime budget 與 memory namespace。
- 系統同時建立 GPT-5.5 執行卡與相依的 Grace/Terra 驗收卡；執行卡完成只代表「待驗收」。

## 第四階段：Dispatcher 與即時進度

即時設定：

```yaml
kanban:
  dispatch_interval_seconds: 10
openclaw_bridge:
  url: http://127.0.0.1:18789

toolsets:
  - hermes-cli
  - browser-cdp
  - openclaw
```

Gateway 重啟後的驗證：

- LaunchAgent：`ai.hermes.gateway`
- 最終新 PID：`49363`
- Gateway state：`running`
- Telegram：`connected`
- `clawops_delegate`：已註冊在 `openclaw` toolset，Grace tool surface 可見
- Dispatcher log：`embedded in gateway (interval=10.0s)`

## 第一版即時 Smoke Test（已被後續架構取代）

實際任務：`t_0aac250f`

- Assignee：`clawops-ops`
- 事件：`created` → `claimed` → `spawned(pid=30471)` → `heartbeat` → `completed`
- 完成摘要：只讀取 `/Users/kj/.hermes/profiles/clawops-ops/config.yaml`，確認 `model.default = gpt-5.5`，未修改任何檔案或服務。
- Telegram 訂閱游標走到 completed event 後自動移除，證明進度通知鏈路完成。

## Loop Contract 修正版驗收

- Topic registry：`telegram/-1003938559457/thread/270` 精確綁定 `ingrids.app` / `ingrids_marketing`。
- 未登記 Topic：拒絕且不建立 DB 任務。
- 完整合約：建立一張 `clawops-* / gpt-5.5` execution card，以及一張 parent-dependent `default / gpt-5.6-terra` Grace review card。
- 原文隔離：execution card 明示原始用語只供 audit，worker 只能執行 compiled contract，禁止跨 Topic/global history 推測。
- 最終修正版不把原始用語放入 execution/review prompt；只留下 SHA-256 audit fingerprint 與「原文僅存在 Grace session history」的位置說明。
- Loop：execution 與 review 都啟用 `goal_mode`，受 max iterations 與 runtime 控制；驗收失敗時 review 必須 block 並輸出精確 correction contract，不得誤報完成。
- Topic 270 session：保留 132 則訊息，把 model 從 `gpt-5.5` 修正為 `gpt-5.6-terra`，清除舊 system prompt 以便下一回合重建；DB 備份為 `/Users/kj/.hermes/state.db.bak-grace-loop-20260719`。
- Live execution card `t_7244f294`：`clawops-content`，session `20260719_183012_ea0b36`，`state.db` 驗證 model=`gpt-5.5`；只讀證據為 `/Users/kj/.hermes/profiles/clawops-content/config.yaml` 的 `model.default: gpt-5.5`。
- Live Grace review card `t_0e6e9652`：parent=`t_7244f294`，assignee=`default`，session `20260719_183115_f659c8`，主 `state.db` 驗證 model=`gpt-5.6-terra`；review metadata=`review_outcome: accepted`。
- 兩張卡都使用 `goal_mode=true`、`goal_max_turns=4`，事件順序為 execution `created → claimed → spawned → completed`，其後 review 才 `claimed → spawned → completed`。

## 2026-07-21 Thread 2 與瀏覽器邊界修正

- 已登記 `telegram/-1003938559457/thread/2` 為 `二手拍賣 / secondhand_commerce`，memory namespace 為 `telegram:-1003938559457:2/secondhand_commerce`。
- Grace 可直接使用瀏覽器做唯讀預檢：導覽、snapshot、scroll、vision、images，以及明確 allow-list 的唯讀 CDP method。用途只限理解需求與判斷 task type。
- Grace／cron session 的 click、type、press、dialog、upload、terminal、patch、write、任意 `Runtime.evaluate` 等執行行為由 middleware 擋下；`HERMES_KANBAN_TASK` 的 ClawOps worker 保留執行能力。
- 當時先以 `GRACE_CLAWOPS_POLICY_VERSION: 2` 建立 prompt cache 失效機制；2026-07-21 交接強化後已升級為 version 3。thread 2 既有 prompt 不含 v3，因此下一則訊息會被判定為 stale、重建並注入標準巢狀合約與禁止 dry-run 降級規則。原 v2 DB 備份為 `/Users/kj/.hermes/state.db.bak-grace-loop-v2-20260721`。
- 二手刊登兩個 cron job 已加入 `openclaw` toolset，prompt 改為 `Grace 唯讀預檢 → clawops_delegate → ClawOps 執行 → Grace review`，並使用 `context_alias=secondhand_commerce`。
- Live execution `t_3c83e716`：`clawops-ops / gpt-5.5`，只讀 registry 與 AGENTS policy，驗證 thread 2 專案與 policy version，無外部副作用。
- Live review `t_c3fb65a8`：`default / gpt-5.6-terra`；第一次 provider request 遇 HTTP 500 後自動重試，最終驗收通過，沒有略過 Grace review。

## 測試與效能複測

程式回歸：

- OpenClaw bridge、`/clawops`、Kanban notifier：36 項通過。
- 2026-07-21 巢狀合約、prompt 注入、Grace 執行邊界與 Loop Contract：47 項通過。
- Pre-gateway dispatch、Kanban notify：17 項通過。
- 原 fast-lane 合計：53 項通過、0 項失敗。
- Loop Contract 最終 focused suite：61 項通過、0 項失敗。第一輪曾發現 1 個缺少 `product_marketing` worker route 的問題；補上後，合約／未知 Topic／雙卡依賴／原文隔離／通知狀態測試均通過。
- `git diff --check`：通過。

專案原本的 `.venv` 是已失效的 Python 3.9 環境；正式 Gateway 使用 `.venv312`。測試入口新增 `HERMES_TEST_VENV` 覆寫能力，驗收使用專案規定的 `scripts/run_tests.sh` 搭配實際 Python 3.12 runtime 執行。

切換後，完全不指定 provider/model、直接讀取即時預設設定的 10 次短回覆：

| 次數 | 平均 | P50 | P95 | 最小值 | 最大值 |
|---:|---:|---:|---:|---:|---:|
| 10 | 8.375 秒 | 8.088 秒 | 10.108 秒 | 6.947 秒 | 10.535 秒 |

此結果接近受控 Terra A/B，證明設定切換生效。這項優化主要改善第一步回覆與尾端穩定度；真正的長任務仍會由 ClawOps / GPT-5.5 執行，因此工具等待與多輪推理時間不會消失，但 Grace 不再被這些工作綁住。

## 回復方式

切換前設定已備份至：

`/Users/kj/.hermes/config.yaml.bak-grace-fast-lane-20260719`

若需回復，應以單一明確檔案的內容還原 `~/.hermes/config.yaml`，再重啟 `ai.hermes.gateway`；本次沒有執行任何批量刪除。
