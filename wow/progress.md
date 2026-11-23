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

---

# v0.2 進度（2026-11-23 起）

## 資源宣告
- 時間盒：分階段（A→B→P→F→S→C→E→V→I→D），每階段 Opus 逾時 120–360 分；新增付費成本上限 0（只用現有訂閱 CLI、免費工具鏈；AI 範例全用本機 mock provider，API 花費 0）
- 角色：建造＝Claude Opus（`claude -p --model opus`，wow-agent build2*）；答案卷／holdout＝Codex（answerkey2/2b，不寫產品碼）；獨立審查＋測試＝Codex（review1/2，未寫被審程式碼）；評審＝Claude Fable 5-1 judge only（≤3 次）；協調＝Grok Bot
- 呼叫上限：Opus 建造 ≤ 1/階段（+修復 ≤ 2）、Codex 審查 ≤ 2、Fable ≤ 3、盲測 ≤ 2
- 工具鏈（免費安裝）：Go 1.27、Node 24、bun 1.4、deno、OpenJDK 21、kotlinc 2.4.21、dotnet 8、PHP 8.4、Ruby 3.3、gcc/g++ 14、pipx

## 範圍（wow/spec.md V-1..V-19）
多語言 adapter（對齊 fast-anything 10 種）、v0.1 缺口、Python 邊界擴充、韌性模式目錄（量測保留／淘汰）、可服務性（MTTD/診斷/還原）、SLI/SLO、旗標／熱更新／模組化／飛航記錄器＋replay＋RCA、爆炸半徑（canary/shadow/隔離/fail-safe）、AI 依賴 adapter、AI 時代工具調研、AX 結構化錯誤＋人類閘門、視覺化資料契約、安裝／README／發佈準備、30 秒新手測試。


## 接續點（2026-11-23 17:30 台北時間，C 階段完成）

評審改用 `claude -p --model claude-fable-5-2 --effort high --tools Read`（最多 3 次）。Codex 獨立審查＋測試（未寫被審程式碼）。

### 已完成（不要重做）
| 項 | 證據 |
|---|---|
| V-5(a) pickle→JSON cassette | `2e2c308` |
| V-5(b) fake-fallback 必須有觀測到的邊界失敗 | `0b23471` |
| V-5(c) 範例 `.ras` JSON 無絕對路徑 | `c940866` |
| V-5(d) GET-then-POST 負對照進 `tests/` | `0ed4cc1` |
| V-5(e) enable/disable symlink／原子寫入 | `aa72a74` |
| V-1 語言 adapter 架構 `lang/` + `docs/ARCHITECTURE.md` | `c1e484a` |
| V-4 Python httpx/aiohttp/socket＋methods/async | `c9387c6`；weather/payments 再量測 |
| V-2/V-3 Node.js/TS adapter + `examples/orders-node/` 實測 | `d1fc117` `e2c827c` `4da9986` `09ee610`；verify 四函式皆到 100%；bench **0.0% → 90.0%**（20 seeds，dups 6→0） |
| V-13/V-16.3 patterns-as-plugins（Python+Node，12 模組，parity 測試） | `17db78c` `bdbc197` |
| V-13 量測保留/淘汰 + retry amplification | `cc78a58`：`docs/PATTERNS.md`、`examples/patterns-lab/`、`ras-anything bench --patterns`；seeds 1001–1020 |
| V-16.1/16.2 + V-17 flags / 熱更新 / canary / shadow / 隔離 / fail-safe | `docs/FLAGS.md`、`examples/flags-hot/`（p50 0.0001s、p99 0.0002s、dropped 0）、`ras-anything flag`、`enable --canary/--shadow`；322 tests OK |
| Holdout 答案卷（多語言+AI fixtures） | gitignored `wow/work/holdout/`（INDEX-v02.md 已核實跑過） |
| **S** `build2s` V-8/V-9/V-16.4/V-18 | `adba2d1` `f80aaa6` + 本輪收尾；355 tests OK；weather SLO `missed→met`（6000%→0% burn）、DORA `restore_s=0.1023s`、blind replay `8/8` 重現（`87.5%` RCA）、AX catalog `30%→100%`（0 leaks） |

### 未完成（按階段；完成一項就 commit+push，並更新本表）
| 階段 | 規格 | 狀態 | 下一步 |
|---|---|---|---|
| **P 餘** | V-13 量測保留/淘汰：`docs/PATTERNS.md`、retry amplification、ON vs OFF 數字、TASK.md 指引 | **完成** | adopted：fallback / deadline / idempotency / retry_budget / retry_after / retry / circuit_breaker / attempt_timeout / jitter。opt-in：bulkhead / adaptive_throttle / adaptive_concurrency / hedge。無 rejected。retry storm 可用性 bare 41.0% → stack 88.1%（breaker）；idempotency 重複寫入 76.5 → 0 |
| **F** `build2f` | V-16.1 旗標、V-16.2 熱更新（切換延遲、0 丟請求）、V-17 canary/shadow/隔離/fail-safe/爆炸半徑 | **完成**（自動 promote 是 `control.analyse` 純函式，尚未接到長跑服務） | |
| **S** `build2s` | V-8 可服務性（OTel 日誌、health、RUNBOOK、diagnoser、MTTD/診斷/還原）、V-9 SLI/SLO、V-16.4 飛航記錄器+replay+RCA、V-18 AX 結構化錯誤+人類閘門 | **完成** | 355 tests OK（`adba2d1`、`f80aaa6`＋範例/文件收尾）；weather/payments/orders-node `.ras/slo.json`＋`RUNBOOK.md` 已重生成；`payments`/`orders-node` 舊範例 `logEvent` 仍印原始 `err` URL/路徑待 C/I 後一併清 |
| **C** `build2c` | Go adapter + `examples/inventory-go/` 實測＋holdout 拒壞加固 | **完成** | `examples/inventory-go/` verify（FetchStock 28.3%→100.0%、ReserveStock 35.0%→100.0%、AuditTag 31.7%→100.0%，0 leaks）、bench 20 seeds **5.0% → 100.0%**（identical 5%→95%、crashes 80%→0%、hangs 15%→0%、dups 0→0、SLO 4/4 met）、holdout 4/4 壞加固全拒、360 tests OK |
| **E** `build2e` | V-14 AI 依賴 adapter + `examples/support-ai/`（本機 mock，API $0）、V-15 生態決策寫入 docs/AI.md | **完成** | `examples/support-ai/` verify（`support.triage_ticket` 9.9%→100.0%，semantic 9.9%→100.0%，injection 100.0%，cost/ok $0.000188→$0.000055，0 leaks）、bench 20 seeds **50.0% → 100.0%**（crashes 40%→0%、hangs 10%→0%、SLO 4/4 met）、`drift` `small→backup` PASS（100%）、`docs/AI.md`（V-14/V-15）、Node parity、holdout 5/5 壞加固全拒、367 tests OK |
| **V** `build2v` | V-19 視覺化資料契約（docs/schema、`.ras/out`、export、MCP、dashboard-sample） | **部分完成（核心契約可用）** | `docs/schema/`（draft 2020-12 `ras-run`/`ras-event` + `README.md`）、`.ras/out/` 寫出、`ras-anything export`（`json`/`ndjson`/`prom`/`otlp-json`）、MCP `ras_export` + `ras_events`、`examples/dashboard-sample/` + `docs/cards/dashboard-sample.png`（已修服務性跨函式彙總 `7.1%` 與 SLO Target 欄）。佔位保留項：`mttd_s`/`mttr_s`/`restore_s`、`breakers`/`bulkheads`、`ax.records` 仍為空值，events 目前寫出 4 種里程碑事件 |
| **I** `build2i` | V-6 `./install.sh`+`make install`、雙用途 README、skill 複製到 ~/.claude 與 ~/.codex、MCP/plugin 驗證、V-11 `docs/RELEASE.md`（不公開、不上架） | **完成** | `./install.sh`／`make install`（pipx 或 `~/.local/share/ras-anything` venv + `~/.local/bin` shim，冪等、`--uninstall`）；skill 複製到 `~/.claude/skills` 與 `~/.codex/skills` 並在乾淨暫存 HOME 驗證兩邊讀得到；MCP `initialize` + 12 tools 啟動通過；plugin 清單可載入（未對外上架）；`docs/RELEASE.md` 只寫流程（Alex gate，未公開、未 `twine upload`）；本機 `twine check` sdist+wheel **PASSED**（wheel 含 node_agent 與 go_agent）；證據 `wow/proof/install-clean-home.md`；383 tests OK |
| **D** `build2d` | 其餘 fast-anything 語言：Java、C#、PHP、Ruby、Kotlin、bun/deno、C/C++（各 adapter+範例+測試+holdout；做不完要在覆蓋表寫原因與下一步） | 未開始 | |
| **W3** V-10 | 新手 30 秒／首次結果計時（只給 README 的盲測 agent） | **完成** | `12.10s`（`0` 次猜錯，讀 `README.md` → `ras-anything scan -- python app.py`），證據 `wow/proof/w3-blind-timer.md`（haiku 盲測，CLI 已預裝） |
| **審查** | Codex 獨立跑測試+兩邊範例+安裝+嘗試破壞；判決 SHIP/FIX-FIRST；修 P0/P1 再跑一次 | **完成** | r1 `FIX-FIRST`（2 P0 + 1 P1 + 1 P2，已於 `cab2582` 全修）；r2 驗證 4 項全修＋3 項對抗繞過全拒＋381 tests OK，唯餘 1 P2 `agent.log` 路徑已於 `2071425` 修畢並補測試 |
| **評審** | Fable 5-2（`--model claude-fable-5-2 --effort high --tools Read`）最多 3 次 | **完成** | `fable-v02-r1` 與 `fable-v02-r2` 皆評 **達成**（7/7 關通過附保留，無阻塞項），並已依 r2 補齊 `verify.py` AI 故障數、setuptools>=77、`install-clean-home.md` 重產、383 tests OK |

HEAD：見最新 main。工作樹應保持可推。中間產物在 `wow/work/`。

### v0.2 核心範圍贏面表（Python / Node / Go + AI；Stage D 刻意延後）
| 關 | 狀態 | 關鍵證據 |
|---|---|---|
| W1 痛點 | **通過** | 5 範例重現 hang／5xx／dup／13 AI 故障（`0%→90%` / `5%→100%` / `50%→100%`） |
| W2 美觀 | **通過（附保留）** | 5 張 SVG 卡片 + `docs/cards/dashboard-sample.png`（1440×1740 全頁無裁切）；SPDX `license = "MIT"` 已消除 2027-02-18 棄用警告，仍餘 `check.warn(importable)`（`node_agent/`/`go_agent/` 非 Python package 目錄提示） |
| W3 好用 | **通過（附保留）** | `wow/proof/w3-blind-timer.md`：`12.10s`、`0` 猜錯（haiku 讀 `README.md` 跑出 `scan`） |
| W4 可視化與介面 | **通過（附保留）** | `README.md` p99 已與報告/卡片對齊、`export.py` 跨函式彙總 `7.1%`、SLO Target 欄補齊；V-19 佔位欄位已誠實標註 |
| W5 驚豔 | **通過** | 精準抓出重複扣款通話；Go `5%→100%`、AI `9.9%→100%`（cost/ok `$0.000188→$0.000055`）、13 模式量測表 |
| W6 領域與實作 | **通過（附保留）** | 3 項對抗繞過全拒、symlink 防護、unowned skill 保護；保留項：`payments`/`orders-node` 舊範例報告仍列 minor raw-URL leaks（2+3 筆）、Go 服務性偏低（`7.1%`）、AI 注入字詞與成本門檻待加強 |
| W7 工作流 | **通過（附保留）** | `wow/spec.md` 先行、W3+Codex 2 輪+Fable 2 輪角色分離、Alex gate 嚴守；保留項：`wow/work/` 原始紀錄不入版控，最後收尾修訂由 383 tests 把關 |

接續備註（2026-11-23 18:30 台北時間）：W3 計時盲測、Codex 兩輪獨立審查與修復、Fable 評審與低成本收尾修訂皆已完成（`wow/work/STOP` 保留）。Stage D（其餘 8 語言）依決策留待下一步，或依 `docs/RELEASE.md` 由 Alex 決定是否公開。
