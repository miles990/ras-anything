# ras-anything 專業做法審查：可維護、可擴充、組件化

日期：2026-11-23（台北時間）。範圍：`ras_anything/`、測試、CI、文件；不含 Java 線的檔案（`ras_anything/lang/java*`、`examples/shipping-java`、`tests/test_java_pipeline.py`），那些只量不改。
量測工具：wow `tools/wow-eng.mjs`（W8，E1–E11；規格見 wow `references/engineering.md`、`spec/INTEROP.md`、`spec/COMMON.md`），設定 `wow/eng.json`，棘輪 baseline `wow/eng-baseline.json`。

狀態標記：**做到**／**部分**／**沒有**。優先序：**P0** 高價值低成本、**P1** 高價值中成本、**P2** 中價值或高成本、**—** 不採用。

## 0. 第一原理檢查

- **目標（不可再化約）**：使用者的程式在相依故障時仍可用，而且 ras-anything 自己能長期被人與 agent 修改，不越改越難。
- **硬限制**：不得改寫使用者原始碼本體（只寫加固副本＋開關）；零執行期相依；私有 repo、不發佈、不付費；Java 檔案屬另一條線；每一步要測試全過才推。
- **由此推出的判準**：(1) 新增一種語言／模式／故障／匯出格式，要改幾個「核心」檔——用真實 commit 的 diff 量，不用估；(2) 依賴方向與循環——用 import 圖量；(3) 介面是否穩定——用 schema 與 API 快照鎖；(4) 證據要能被別的工具讀——manifest＋JSON Pointer。
- **被拒絕的慣性做法**：「先導入大型框架（pluggy、Spring 風格 DI）」——違反零相依且不解決核心問題；「一次大重構把複雜度壓到 15 以下」——風險高、無法小步驗證，改用棘輪。

## 1. 結論與數字

| W8 子項 | 前（本次開始，b87aa0e 之前） | 後（本次結束） | 狀態 |
|---|---|---|---|
| E1 可靠＋E2 可用（examples/weather，經 manifest 讀） | 無法自動讀取 | 可用率 100.0%、SLO met、verify 通過 | 通過 |
| E3 可維護 | maxCC 122、CC>10 占 14.4%、最長函式 209 行、最大檔 1194 行、重複 0 | 同左（cli.main 34→5；baseline 107 筆，只擋新違規） | 通過（含 baseline 107 筆） |
| E4 可擴充（真實 diff） | Go：核心 1 檔；Java：核心 3 檔；匯出格式：核心 2 檔 | 新語言（暫存 toy commit）：核心 **0** 檔；匯出格式仍 2 | 部分 |
| E5 模組化 | 循環 2（diagnose→enable→verify、agents 內一組） | 循環 1（agents 內延遲 import，已進 baseline）；分層違規 0 | 通過（含 baseline） |
| E6 組件化 | 語言、模式都是手寫清單；不可停用 | 語言 4、模式 13：自動發現、各有測試、可停用 | 通過 |
| E7 lib 化 | 未量 | L1 核心不依賴外殼 ✓；L3 `ras_anything.runtime` 60 個公開名稱、無副作用 ✓；L2 外殼最大 CC 34→11（mcp.call_tool、handle；cli.main 34→6、mcp._argv 22→9） | 部分 |
| E8 共同編輯 | 沒有 AGENTS.md、CHANGELOG、ADR | C1、C2、C3、C5 ✓；C4 不適用（沒有編輯用 UI） | 通過 |
| E9 互通 | 沒有 manifest；錯誤非機讀 | X1–X8 全過（manifest、schema、CLI、RFC 9457、MCP 參數說明、SKILL、CloudEvents、輸出 schema）；`--json` 可放子指令前後 | 通過（缺口：`--json` 只讓錯誤機讀，結果的機讀走 `export --format json` 與 MCP；INTEROP §1 要求每個出結果的指令都有 JSON 輸出 → P1） |
| E10 安全與可移植 | 沒有 CI | 憑證樣式 0、執行期相依 0；CI workflow 已寫好（`permissions: contents: read`、三平台矩陣，macOS／Windows 手動觸發），但代理的 token 沒有 `workflow` 權限，推不上 `.github/workflows/`，暫放 `docs/ci/github-workflow-ci.yml` | 未通過（S3、S4 要等 workflow 放進 `.github/workflows/`） |
| E11 共同開發規範 | — | wow-common 1.0、採用差異 ADR 0001、零共用執行期 | 通過 |

測試：407 → **426**，全過（`bash wow/work/s/ptest.sh`）。ruff、mypy 通過。分支覆蓋率 **71%**（`coverage run --branch`，424 個測試那一版；11,424 行、未覆蓋 3,061 行）。

## 2. 擴充成本：改幾個檔（`wow-eng change`，真實 diff）

| 情境 | commit | 程式檔 | 新增 | 修改 | 改到核心 |
|---|---|---|---|---|---|
| 加 Go（元件化前） | e813569 | 7 | 6 | 1 | 1（`lang/__init__.py`） |
| 加 Java（元件化前） | ecd9852 | 23 | 17 | 6 | 3（`export.py`、`lang/__init__.py`、`lang/base.py`） |
| 加匯出格式 cloudevents | fc0f734 | 2 | 0 | 2 | 2（`contracts.py`、`export.py`） |
| 加玩具語言（元件化後，暫存 worktree，未推） | — | 2 | 2 | 0 | **0** |

玩具語言 commit 只新增 `lang/toy.py`、`lang/toy_agent/`、一個測試；`tests/test_contracts.py` 的故障一致性測試隨即擋下「toy 的 agent 不認得 timeout」——這是預期的護欄：組件可以只加檔，但要符合契約才能過。

## 3. 差距清單

### 3.1 可靠性來源

| 項目 | 狀態 | 較好的做法 | 來源 | 採用？理由 | 優先 |
|---|---|---|---|---|---|
| 退避與抖動 | 做到 | full jitter、重試預算 | AWS Builders' Library〈Timeouts, retries and backoff with jitter〉 | 已採用 | — |
| 重試預算／放大控制 | 做到 | 全域 token bucket | Google SRE 書〈Handling Overload〉、Polly v8 | 已採用（retry_budget） | — |
| 斷路器、艙壁、逾時、備援 | 做到 | 可組合的管線 | Resilience4j、Polly v8 resilience pipeline、Failsafe | 已採用（Policy 層） | — |
| 適應性併發 | 做到 | 依延遲調整上限 | Netflix concurrency-limits、Envoy adaptive concurrency | 已採用 | — |
| 故障注入矩陣 | 做到 | 宣告式故障種類 | Chaos Mesh、LitmusChaos、Gremlin、ToxiProxy | 已採用（faults.MATRIX＋agent 一致性測試） | — |
| Hystrix 式執行緒池隔離 | — | — | Hystrix（已停止維護） | 不採用：維護停止，且執行緒池隔離對腳本工具成本過高 | — |
| 遙測 | 做到 | OTLP JSON、語意慣例 | OpenTelemetry | 已採用 | — |
| 旗標 | 做到 | 標準格式 | OpenFeature／flagd | 已採用；本次補 `docs/schema/ras-flags.schema.json` | — |
| 機讀錯誤 | 做到（本次） | problem+json | RFC 9457 | 已採用（`--json` 錯誤） | — |
| 交付指標 | 部分 | 部署頻率、變更失敗率、恢復時間 | DORA | 部分：CI 已有；指標未收 | P2 |

### 3.2 相似工具

| 工具 | 學到的做法 | ras-anything | 優先 |
|---|---|---|---|
| fast-anything | 三 OS × 兩 Python 的 CI、CHANGELOG | 做到（本次）：Linux 每次跑，macOS／Windows 手動（私有 repo 分鐘數倍率 10×／2×） | — |
| Semgrep | 規則是獨立檔案、規則目錄、LSP | 模式已是獨立模組；LSP 未做 | P2 |
| OpenRewrite | recipe 可組合、dry-run、可回復 | 加固副本＋enable／disable 已可回復；沒有 dry-run diff 報告 | P1 |
| tree-sitter | 每種語言是獨立 grammar 套件 | 語言已是組件，可經 entry point 外掛 | — |
| pluggy／entry points | 標準外掛註冊 | 用 `importlib.metadata` entry points，不加 pluggy 相依 | — |

### 3.3 設計方法

| 方法 | 狀態 | 說明 | 來源 | 優先 |
|---|---|---|---|---|
| SOLID：單一職責 | 部分 | `sync_out` CC 122、`bench` 99、`verify.measure` 84 | Martin《Clean Architecture》 | P1（分階段拆） |
| 開放封閉 | 部分 | 語言、模式做到；匯出格式還要改核心 | 同上 | P1 |
| 依賴反轉／Hexagonal | 部分 | runtime 不依賴 pipeline；pipeline 直接做 I/O，沒有 port | Cockburn〈Hexagonal Architecture〉 | P2 |
| DDD 邊界 | 部分 | 語言、故障、旗標、遙測有自然邊界；未明文 | Evans《DDD》 | P2 |
| Plugin／Strategy | 做到 | LangAdapter、PATTERN 模組 | GoF | — |
| clig.dev | 做到 | `--json`、stderr、結束碼、NO_COLOR | clig.dev | — |
| SemVer 與相容政策 | 做到（本次） | 函式庫 API 快照、schema_version 規則 | semver.org | — |

### 3.4 標準

| 標準 | 狀態 | 說明 | 優先 |
|---|---|---|---|
| PEP 8／257 | 部分 | ruff F／E9／部分 B 已開；docstring 未強制 | P2 |
| 型別提示 | 部分 | mypy 只檢 5 個檔（contracts、lang/base、lang、patterns、hardens）；`py.typed` 已出 | P1：逐檔擴大 |
| OpenSSF Scorecard | 部分 | Token-Permissions 做到；Pinned-Dependencies（actions 以 SHA 釘選）沒有 | P1 |
| SLSA／SBOM | 沒有 | 不發佈，暫不需要 | P2 |
| 12-factor | 做到 | 設定來自環境與 `.ras/` 檔；日誌寫 stderr | — |
| 錯誤與日誌慣例 | 做到 | 人：發生什麼／影響／下一步；機：RFC 9457；日誌 OTel 欄位 | — |
| OWASP 與安全改寫 | 部分 | 不改原檔、寫副本；agent 改寫的範圍檢查存在；沒有對加固副本做靜態安全掃描 | P1 |

### 3.5 注意事項

| 項目 | 狀態 | 說明 | 優先 |
|---|---|---|---|
| 自動改寫使用者程式的風險 | 做到 | 只寫 `_ras_hardened/` 副本＋開關，原檔不動；verify 通過才可 enable | — |
| 可回復 | 做到 | `disable`、旗標 kill switch `RAS_ANYTHING_DISABLE=1` | — |
| 決定性 | 部分 | 種子化故障可重現；`export` 會改寫追蹤中的 `.ras/out` 時間戳，diff 有雜訊 | P1 |
| 效能負擔 | 部分 | bench 量了；沒有每次呼叫的額外延遲預算 | P2 |
| Windows | 部分 | CI 可手動跑 Windows；本次未實跑（私有 repo 分鐘數） | P1：Alex 同意後手動跑一次 |
| 併發與執行緒安全 | 部分 | 斷路器、預算有鎖；沒有專門的壓力測試 | P2 |
| 相依最小化 | 做到 | 執行期相依 0 | — |

## 4. 模組邊界、契約與跨執行期重複

- **分層**（`wow/eng.json`）：外殼 → 管線 → 語言 → agents → 訊號／錄製／AI → 故障／旗標／控制 → runtime／patterns → 共用。分層違規 0。
- **循環**：本次拆掉 diagnose → enable → verify（`resolve_id` 移到 `hardens.py`，4192e18）。剩 agents 內一組延遲 import（boundary ↔ fake_aiohttp），列入 baseline。
- **契約**：`contracts.py` 是共享詞彙；`tests/test_contracts.py` 檢查 CLI／MCP／export 格式一致、外掛發現、agent 打包、每個 agent 認得每種故障、每個語言模組被發現、語言可停用。
- **Schema 版本**：`docs/schema/` 有 `schema_version` 規則；但 `"1.0.0"` 寫死在 4 處以上 → P1：集中到 `contracts.py`。
- **跨執行期重複**：模式清單在 Python runtime `_PATTERN_NAMES` 與 Node `patterns/index.cjs` 各一份手寫 → P1：由 Python 產生 Node 清單並加對照測試（已有 `test_patterns_parity.py` 可延伸）。
- **故障**是橫切關注：每加一種故障要改每個 agent，這是本質，用一致性測試守住而不是硬拆成組件。

## 5. 程式品質量測

- 複雜度：內建 McCabe 與 radon 對照一致（`sync_out` 122、`bench` 99）。CC>10 的函式占 13.9%（門檻 5%），用棘輪凍結。
- ruff：通過（F、E9、部分 B；agents 目錄排除）。mypy：5 個檔通過。
- 覆蓋率：分支覆蓋 71%（本機一次完整執行）。CI workflow 會在每次推送印出總數。
- CI：lint（ruff、mypy）＋測試（Ubuntu × py3.10／3.13；三平台手動）；`permissions: contents: read`。
- ADR：`docs/adr/0001–0005`。CONTRIBUTING：新語言、模式、匯出格式的步驟。版本與變更紀錄：`CHANGELOG.md`（Keep a Changelog）。

## 6. 唯讀「架構健康檢查」給使用者程式（評估，未建）

| 選項 | 價值 | 成本 | 風險 |
|---|---|---|---|
| import-linter（Python） | 分層、獨立、禁止規則成熟 | 使用者要寫設定 | 低 |
| dependency-cruiser（JS） | 規則豐富、可視化 | Node 相依 | 低 |
| ArchUnit（Java）、deptrac（PHP） | 各語言標準 | 一語言一工具 | 低 |
| lizard | 多語言複雜度 | 外部工具 | 低 |
| SonarQube 可維護性評等 | 業界熟悉的 A–E | 需要伺服器 | 中（外部服務） |
| Fitness functions（Ford 等） | 以可執行測試守架構 | 要先定義指標 | 低 |

結論：**放在 wow `tools/`，不放進 ras-anything，也不另立工具**（wow ADR 0001）。ras-anything 的本業是可靠／可用的量測；架構健康屬於開發期品質，wow 已用它量自己的產品。若使用者要在自己的程式跑，可直接執行 `wow-eng arch`；要不要包成 ras-anything 子指令，待 Alex 決定。

## 7. lib、框架、編輯器

- **lib（第一優先，本次 phase 1 完成）**：`ras_anything.runtime` 是函式庫：`__all__` 60 個名稱、`py.typed`、API 快照 `docs/api/runtime-api.json` 與 `tests/test_library_api.py`（移除或改簽名＝主版本）、`docs/LIBRARY.md`。Phase 2：拆成獨立的 `ras-anything-runtime` 發行形狀（不發佈）。參照 Resilience4j、Polly：核心是函式庫，框架整合是薄殼。
- **框架（只做骨架）**：不做 Spring／Nest 式框架；wow 的骨架與 INTEROP 是規格。
- **編輯器（不做）**：人用 `ras-anything flag set`、agent 用 MCP `ras_flag` 改同一份 `.ras/flags.json`（schema 已補）。LSP 診斷（Ruff、Semgrep 的做法）列為選配。缺口：沒有讓人視覺化編輯旗標的 UI（E8 C4 不適用、E11 G3 不適用）→ P2。

## 8. 組件化

| 組件種類 | 獨立加入 | 替換 | 停用 | 契約與測試 | 狀態 |
|---|---|---|---|---|---|
| 語言 adapter | 只加 `lang/<x>.py`＋`lang/<x>_agent/`；或外部套件 entry point | 外掛不能覆蓋內建（刻意） | `RAS_ANYTHING_LANGUAGES_OFF` | LangAdapter 抽象方法、故障一致性、打包 | 做到 |
| 韌性模式 | 只加 `patterns/<x>.py`（定義 `PATTERN`） | 換模組 | 旗標 `ras.pattern.<x>` | 每個模式有測試 | 部分（runtime 與 Node 清單仍手寫） |
| 故障 | 要改每個 agent | — | 故障矩陣 | 一致性測試 | 本質上橫切 |
| 匯出格式 | 要改 `contracts.py`、`export.py` | — | — | 格式一致性測試 | 沒有（P1） |
| UI 元件 | 沒有 UI | — | — | — | 不適用 |

D 階段其餘語言**暫停**，直到上表的 adapter 介面寫成文件化的契約（CONTRIBUTING「Add a language」＋ADR 0003 已是第一版）。

## 9. 研究組合與標準調整

| 參考 | 採用的部分 | 不採用 |
|---|---|---|
| ISO/IEC 25010:2023 | 9 個特性當覆蓋檢查：可維護（模組化、可重用、可分析、可修改、可測試）→ E3–E7；彈性（含可替換）→ E6、E10；可靠 → E1；安全 → E10；相容（互通）→ E9 | 不逐條打分：標準不給門檻 |
| Google engineering practices | 小步 CL、審查看設計與測試 | — |
| Thoughtworks Tech Radar | fitness functions、paved road、platform as product | — |
| Building Evolutionary Architectures | 可執行的 fitness function、棘輪 | — |
| AWS Well-Architected | 可靠性支柱的故障隔離、測試恢復 | 雲端專屬項 |
| arc42／C4 | 文件結構參考（`docs/ARCHITECTURE.md`） | 不強制全套章節 |
| AGENTS.md | 採用 | — |
| Spec-driven（GitHub Spec Kit、Kiro） | 規格先行、可檢查的驗收 | 不引入其工具鏈 |

調整：Alex 的八項目標之外，依 ISO 25010 缺口**新增 E10 安全與可移植**；依「共同開發規範」**新增 E11**；「組件化」與「模組化」分成 E6、E5。效能效率與安全性（safety）對應 wow 既有的 wow-perf 與領域審查，不在 W8 重複。

## 10. 分階段計畫

| 階段 | 內容 | 價值／成本 |
|---|---|---|
| 1（本次，完成） | contracts、語言／模式自動發現與停用、RFC 9457、CloudEvents、manifest、CI、ADR、AGENTS／CONTRIBUTING／CHANGELOG、函式庫 API 快照、W8 量測 | 高／低 |
| 2（完成，2026-11-23） | 匯出格式改成 `exporters/<fmt>.py` 自動發現（新格式核心 **0**）；`schema_version` 集中為 1.1.0；Node 模式清單由 Python 產生；mypy 擴到 pipeline（28 檔）；actions 以 SHA 釘選；`export` 不改寫追蹤快照的時間戳；每個出結果的指令有 `--json` | 高／中 |
| 3 | 拆 `sync_out`、`bench`、`verify.measure`、`diagnose`、`doctor.status`（每次一個函式，baseline 逐步歸零）；mcp `call_tool`／`handle` 壓到 CC ≤ 10 | 中／中 |
| 4 | runtime 獨立發行形狀；旗標的視覺編輯頁（token 化）；LSP 診斷 | 中／高 |

## 11. 給其他工作線

- 測試依賴未追蹤的 `wow/work/holdout/`：已改成缺少時 skip，並附原因。新鮮 clone（無 holdout）426 個測試、0 失敗、11 skip。
- D 階段其餘語言暫停（見 §8）。

## 12. 獨立審查（Codex）與處理

`codex exec`（唯讀沙盒）審了兩個 repo 的變更，沒有 P0；P1／P2 都已修並加回歸測試：

| # | 發現 | 處理 |
|---|---|---|
| 1 | rel 不看結束碼、宣告了卻沒取到的 evidence 會被略過 | 結束碼非 0 就未通過；每個宣告的 evidence 都要取到值且型別對 |
| 2 | Python 解析失敗只記數字，檔案從分析消失 | 解析失敗變成擋關違規 |
| 3 | S3 只看有沒有 `permissions:` 字樣 | 解析最上層權限，`write-all`／`write` 未通過 |
| 4 | MCP 參數說明有算但不影響 X5 | 納入 X5；ras-anything 的 `seeds`、`format` 補說明 |
| 5 | schema-lite 沒做 maxLength；oneOf 當 anyOf | 實作 maxLength、minItems；oneOf 改成恰好一個 |
| 6 | INTEROP 允許 HTTP MCP，但 schema 要求 command、checker 只會跑 stdio | schema 改成 stdio／streamable-http 兩種；HTTP 標未驗證 |
| 7 | 全部語言關掉時 `detect` 丟 IndexError | 改成 UserError，告訴使用者是哪個環境變數 |
| 8 | 關掉的語言在 coverage 仍顯示可驗證 | 新增選填欄位 `enabled`；關掉時 verify／ax 為 false |
| 9 | `--json` 只能放在子指令前 | 各子指令都接受 `--json`；結果機讀的缺口照實列在 E9 |
| 10 | 共同規範只比主版本，未來的次版本會被當成支援 | 比 checker 新的次版本標未驗證 |
| 11 | 暫存副本保留 symlink，可能寫回原檔 | 複製時解開 symlink |

`schema_version` 已集中到 `contracts.SCHEMA_VERSION` 並升為 **1.1.0**（additive `coverage.adapters[].enabled`；見 `docs/schema/README.md`）。

## 13. Phase 2 量測（2026-11-23，台北時間）

`wow-eng all --run --cwd examples/weather`。前：phase 1 結束（表 §1，約 62a222c）。後：本階段結束。

| W8 子項 | 前（phase 1） | 後（phase 2） | 狀態 |
|---|---|---|---|
| E1＋E2 | 可用率 100.0%、SLO met、verify 通過 | 同左 | 通過 |
| E3 可維護 | maxCC 122、最長函式 209、baseline 107 筆 | maxCC **114**、最長函式 **194**（`sync_out` 的 SLO 時間戳邏輯抽成 `_slo_payload`）；搬家的 `_prom_lines`／`_otlp_json` baseline 鍵改指向 `exporters/` | 通過（含 baseline） |
| E4 可擴充 | 新語言核心 0；匯出格式核心 2 | 新增匯出格式（暫存 toy commit，不推）：核心 **0**、只新增 `exporters/toy.py`。歷史 cloudevents 仍記 core 2（`maxCoreEdits: 2`） | 通過（新格式）；歷史紀錄保留 |
| E5 模組化 | 循環 1、分層 0 | 同左 | 通過 |
| E6 組件化 | 語言 4、模式 13 | 再加上匯出格式 5：自動發現、各有測試；停用標不適用（`--format` 每次選） | 通過 |
| E7 lib 化 | L2 外殼最大 CC 11（mcp） | 同左（`--json` 曾讓 `cli.main` 升到 13，已拆回 ≤10；mcp `call_tool`／`handle` 仍 11 → phase 3） | 部分 |
| E8 共同編輯 | C1–C3、C5 ✓ | 同左 | 通過 |
| E9 互通 | X1–X8 過，但 `--json` 只讓錯誤機讀 | X1–X8 全過（含 `--run`）；每個出結果的指令 stdout 可為 JSON（`cli-result` 或 export 本體） | 通過 |
| E10 安全與可移植 | S3／S4 未過（workflow 還在 `docs/ci/`） | 擬議 workflow 已用 SHA 釘選、`permissions: contents: read`；仍等 Alex 複製到 `.github/workflows/`（本 token 沒有 workflow 權限） | 未通過 |
| E11 共同規範 | 通過 | 同左 | 通過 |

新鮮 clone（commit 7299031，模擬 Linux CI、無 `wow/work/holdout/`）：**426 tests, 0 failures, 11 skipped**；ruff、mypy 通過。分支覆蓋率約 69%（缺 holdout 資料，低於本機有 holdout 的 71%）。

## 14. Phase 3 量測（2026-11-23，台北時間）

`wow-eng all --run --cwd examples/weather`。前：phase 2 結束。後：本階段結束（E7 外殼拆分、incident `schema_version` 同步、Python 3.10 lab worker 修正；CI workflow 已在 `.github/workflows/ci.yml`）。

| W8 子項 | 前（phase 2） | 後（phase 3） | 狀態 |
|---|---|---|---|
| E1＋E2 | 可用率 100.0%、SLO met、verify 通過 | 同左（export@weather） | 通過 |
| E3 可維護 | maxCC 114、最長函式 194、baseline 107 筆 | 同左 | 通過（含 baseline） |
| E4 可擴充 | 新格式核心 0；歷史 cloudevents core 2 | 同左 | 通過（新格式）；歷史紀錄保留 |
| E5 模組化 | 循環 1、分層 0 | 同左 | 通過（含 baseline） |
| E6 組件化 | 語言 4、模式 13、匯出格式 5 | 同左 | 通過 |
| E7 lib 化 | L2 外殼最大 CC 11（mcp `call_tool`／`handle`） | L2 外殼最大 CC **9**（方法與工具改走 dispatch table，邏輯留在核心／小函式） | 通過 |
| E8 共同編輯 | C1–C3、C5 ✓ | 同左 | 通過 |
| E9 互通 | X1–X8 全過 | 同左 | 通過 |
| E10 安全與可移植 | S3／S4 未過（workflow 還在 `docs/ci/`） | S1–S4 全過（`.github/workflows/ci.yml` 已在 main，a7f6019；`permissions: contents: read`、Actions SHA 釘選、Linux 每次跑、macOS／Windows 手動） | 通過 |
| E11 共同規範 | 通過 | 同左 | 通過 |

本機完整套件（有 holdout）：**443 tests, 0 failures**（Python 3.13）。Python 3.10 先前 CI 因 lab worker 把 `asyncio.TimeoutError` 當致命錯誤而失敗（3.10 裡它不是 `OSError`）；已修正並加回歸測試。

Node／Go／Java 事件文件的 `schema_version` 與 `contracts.SCHEMA_VERSION`（1.1.0）對齊：`write_schema_version()` 產生 Node 模組並更新 Go／Java 標記常數；契約測試會在漂移時失敗。

