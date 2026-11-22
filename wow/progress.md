# 進度

- 開始：2026-11-23 02:40 (UTC+8)
- 交付模式：輕量（需求已指定；L1–L13 + 七關）
- 預算：小-中（建造 ≤ 3 輪、評審 ≤ 2 輪；Fable ≤ 3、Codex 審查 ≤ 2、盲測 ≤ 2、新增付費 $0）
- 最佳輪：Round 2（commit `dc2942a`，Fable 5-1 總評 **達成**）

## 目前步驟
完成收斂與交付報告（v0.1 私有 repo，不對外發布）

## 未達標數走勢
| 輪 | tag / commit | 結果摘要 | 未達標關數（Fable） |
|---|---|---|---|
| Round 1 | `cce2c98` | Fable: 未達成 (proof tool) / 達成 (demo)；Codex: FIX-FIRST；Holdout `retry_post` 誤過 | 3（W4, W5, W6）+ W3 未驗證 |
| Round 2 | `dc2942a` | Fable: **達成**；Codex: FIX-FIRST（剩餘 P1/P2 誠實列缺口）；Holdout 5/5 對照全對 | 0（W1–W7 全通過；W3 時限未計時；W6/W7 附保留項） |

## 代表成果與核心旅程
- 代表成果（連結／證據）：私有 repo `miles990/ras-anything`（`README.md`, `ras_anything/`, `examples/weather/`, `examples/payments/`, `docs/cards/weather.svg`, `docs/cards/payments.svg`）
- 核心旅程（從入口到完成）：`pip install -e .` → `ras-anything go --agent claude -- python app.py`（或分步 `scan` → `harden` → `verify` → `enable` → `bench` → `report` → `disable`）
- 實際入口／版本（可操作位置與候選狀態）：CLI `ras-anything` + stdio MCP `ras-anything mcp` + Claude Code plugin（`.claude-plugin/`）+ skill（`skills/ras-anything/SKILL.md`），commit `dc2942a`

## 技術能力與完整成果
- 技術能力（已驗證什麼、證據）：
  - `python -m unittest discover -s tests`：`Ran 33 tests in 2.216s, OK`（離線）
  - Holdout 盲測（`wow/proof/holdout-blind.md`）：`good_hardening.py`（`--budget 1`）PASS（`4.2% → 12.5%`, `0 dups`, `0 hangs`）；`swallow.py`、`swallow + unlogged RAS_FALLBACK`、`retry_post.py`（`dups 0→2`）、`change_output.py` 全數 FAIL
  - Weather 實測：`verify` `32.5% → 100.0%`（120 trials）；`bench` `40.0% → 100.0%`（20 seeds），hangs `15% → 0%`，p99 `2.10s → 889ms`
  - Payments 實測：`verify` `charge` `76.9% → 100.0%`（dups `38 → 0`）、`render_receipt` `33.1% → 100.0%`（160 trials each）；`bench` `0.0% → 90.0%`（identical `0.0% → 45.0%`），dups `5 → 0`，hangs `25% → 0%`，p99 `3.94s → 3.87s`
- 完整成果（整體效果／核心旅程、評審與使用者回饋）：Fable 5-1 Round 2 總評 **達成**；首次使用盲測（`blind2`）0 次因輸出不清猜錯，並實際跑完 `scan → harden → verify → enable → bench → report → disable` 全流程

## 待整合與交付缺口
- 尚未接回實際入口的能力：S-5 跨呼叫持續斷線與恢復（`outage` 仍為 per-key/per-call）
- 距離可交付完整成果的缺口：Codex2 列出之 P1/P2（`calls.pkl` 反序列化信任邊界、自宣 `RAS_FALLBACK` + `"trace_id"` log 仍可遮罩吞錯、`_runtime.py` 寫入前 symlink 檢查、`bench` `classify` 缺延遲 SLO 與逐行檢查、`examples/` `meta.json` 仍含建置機 venv `cmd`/`sys_path`、單元測試未納入 GET-then-POST 負對照僅在 holdout）

## 資源宣告
- 時間盒：≤ 5 小時牆鐘；新增付費成本上限 0（實際額外付費 $0，均走現有訂閱 CLI）
- 可用模型與配額：Claude Opus（建造）、Claude Fable 5-1（judge only）、Codex CLI 0.165.0（答案卷、第二意見審查、盲測）
- 呼叫上限：Opus 建造 ≤ 4、Fable 評審 ≤ 3、Codex 審查 ≤ 2、盲測 ≤ 2
- 實際消耗彙整：
  | 子任務 | 選用 | 上限 | 實際消耗 | 狀態 |
  |---|---|---|---|---|
  | 規格／協調／修復 | Grok Bot | — | 1 次修復（`dc2942a`） | 完成 |
  | 答案卷＋保留樣本 | Codex (`answerkey`) | 1 | 1 | 完成 |
  | 建造 | Claude Opus (`build1` + examples) | ≤ 4 | 1 主建造 + 3 次範例 agent 跑 | 完成 |
  | 終審 | Claude Fable 5-1 (`fable1`, `fable2`) | ≤ 3 | 2 | 完成 |
  | 第二意見審查 | Codex (`codex1`, `codex2`) | ≤ 2 | 2 | 完成 |
  | 首次使用盲測 | Codex `--blind` (`blind1`, `blind2`) | ≤ 2 | 2（`blind1` 被沙盒擋 socket，`blind2` 跑完 Phase A+B） | 完成 |

## 贏面表
| 關 | 適用 | 狀態 | 判準與證據路徑 | 未抵達／未知 | 下一步 |
|---|---|---|---|---|---|
| W1 痛點 | 是 | **通過** | `wow/spec.md:6` 定義；`examples/payments/.ras/bench.json` 實測重現 hang / EAGAIN / 截斷 JSON / 重複扣款 | 市場需求僅依指定任務（market unknown） | 視需求另行市場驗證 |
| W2 美觀 | 是 | **通過** | `wow/work/evidence/holdout-verify-v2.txt` 層級清楚、對齊、`what/impact/next` 錯誤；`docs/cards/*.svg` 自標條件 | 小修辭（`"1 duplicates"`、範例報告含 venv 路徑） | v0.2 修飾 |
| W3 好用 | 是 | **通過（理解）／未驗證（≤30s 計時）** | `wow/proof/first-read.md`、`wow/work/blind/phaseA-run2.md`（0 次猜錯、12 步走完） | 未實測讀秒 ≤30 s；盲測跑在修復前 | 修復後如公開再補計時盲測 |
| W4 可視化與介面 | 是 | **通過** | `docs/cards/payments.svg`、`weather.svg` 比例正確、含 `identical output` 與對稱統計、`README.md` 實測表吻合 JSON | `RAS-REPORT.md` 卡片連結經手工改為 `../../docs/cards/` | `report` 可支援 `--card-dir` |
| W5 驚豔 | 是 | **通過** | `billing.charge/verify.json` 重現雙重扣款序；`holdout-verify-v2.txt` 精準攔截 GET-then-POST `retry_post.py` | `verify` 假設上游實作 `Idempotency-Key` 去重 | README 補上該假設說明 |
| W6 領域與實作 | 是 | **通過（附保留）** | timeout/full-jitter/Retry-After/單探針 half-open/不重試 4xx/未記 log fallback 擋下/相對路徑隔離/`tests/` 33 OK | pickle cassette、logged fake fallback、無跨呼叫 outage 恢復、bench 缺 SLO | 納入 v0.2 backlog |
| W7 工作流 | 是 | **通過（附保留）** | `wow/spec.md` 先行、trunk `main` 乾淨、真 agent log、`REPRODUCE.md` 誠實揭露修復與重測 | holdout 在 gitignored `wow/work/`、範例 `meta.json` 仍含 `cmd` 絕對 venv 路徑 | 公開前將 holdout 昇格進 `tests/` |
