# relay — Codex ⇄ Claude 用量接力

兩個 agent 輪流做同一件事：誰還有額度誰就拿棒子。額度剩 2% 時交棒，
交棒的那一方**不停工**，把最後一點額度用完才進入等待，重置後再回來接手。

```
working ──額度≤2%──► draining ──撞到上限──► blocked ──重置──► ready
   ▲                    │                                      │
   └────────── 重新拿到棒子 ◄──────────────────────────────────┘
```

## 快速開始（在 Mac 上，專案資料夾內）

```bash
python3 relay.py init          # 產生 config.json
$EDITOR relay/config.json      # 填 task、budget、resume_cmd
python3 relay.py status        # 看一次用量表，不會啟動任何 agent
python3 relay.py run           # 正式跑（Ctrl-C 停）
```

背景常駐：

```bash
nohup python3 relay.py run >> relay/relay.log 2>&1 &
```

## 它怎麼量用量

兩個訊號，可信度不同，`confidence` 欄位會標示：

| 訊號 | 來源 | 可信度 |
|---|---|---|
| `blocked` / `reset_at` | log 裡真正的上限通知，例如 `try again at Oct 27th, 2026 2:16 AM` | **measured**，確定的 |
| `remaining_pct` | 滾動視窗內的加權 token ÷ 你設的 `token_budget` | **estimate**，要自己校準 |

訂閱制的額度官方沒有公開 token 數字，所以百分比是估的。
先用 `relay.py status` 跑幾天，看撞到上限時 `used_tokens` 是多少，
再把那個數字填回 `token_budget`，之後的 2% 就會準。

- Claude：讀 `~/.claude/projects/*/*.jsonl` 的 `message.usage`。
- Codex：讀 `~/.codex/sessions/**/*.jsonl`，key 名含 `total` 的當累計值取最大，其餘逐筆累加。
- 上限偵測只看 CLI 自己發的通知（`system`/`error`/`rate_limit_event`），
  **不看**你和 agent 的對話內容——否則聊到「usage limit」就會被誤判成沒額度。

## 換成真的用量 API

probe 就是「印一行 JSON 到 stdout」的任何程式，換掉不用改 relay：

```json
{"ok": true, "agent": "codex", "remaining_pct": 37.5,
 "blocked": false, "reset_at": null, "source": "official-api", "confidence": "measured"}
```

config 裡 `"probe": "curl -s ... | jq ..."` 也可以。

## 檔案

| 檔案 | 用途 |
|---|---|
| `config.json` | 門檻、輪替順序、每個 agent 的 budget 與啟動指令 |
| `status.json` | 棒子現在在誰身上、任務、進度、交接歷史 |
| `usage.json` | 最近一次各 agent 的量測結果 |
| `handoff/*.md` | 每次交棒自動產生的交接單 |
| `relay.log` | 事件記錄 |

## agent 要做的事

拿到棒子後更新 `status.json` 的 `progress`，relay 會把它抄進下一張交接單：

```json
"progress": {
  "done": ["首頁白框跟框延遲已修", "字幕選單閃爍已複現"],
  "next": ["集數選單白框", "回歸：略過介紹／長按快轉"],
  "notes": "B9 實拍用前鏡頭，閃爍只在 60Hz 模式出現"
}
```

`draining` 狀態的 agent 會收到 `directive`：把當前這一步收尾、寫完交接單再停。

## autostart

`"autostart": true` 時，交棒會直接執行接手方的 `resume_cmd`。
先用 `python3 relay.py run --dry-run` 確認指令長相正確再打開。
`{handoff}` / `{status}` / `{agent}` 會被替換成實際路徑。
