# GPT + Claude + Gemini 三模型聊天室

這是一個免費可自架的 Flask 小工具：
- 你自己放 API Key（不綁特定平台 UI）。
- 一次送出同一句問題。
- 同步拿到 GPT / Claude / Gemini 的回答，並排顯示。

> 注意：ChatGPT / Claude / Gemini 的「訂閱」通常不等於 API 點數。若你要用 API，三家多半需要各自 API 計費。

## 1) 安裝

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) 設定金鑰

```bash
cp .env.example .env
```

填入 `.env`：

```bash
export $(grep -v '^#' .env | xargs)
```

或手動 `export OPENAI_API_KEY=...` 等。

## 3) 啟動

```bash
python app.py
```

開啟：`http://localhost:7860`

## API 路由

- `GET /`：聊天室頁面
- `POST /chat`：送 `{"prompt":"..."}`，回傳三家結果

## 免費最好用建議

- 將模型設成較便宜版本（預設已是偏省成本的選項）。
- 若某家暫時不用，可不填該 API key，頁面會顯示該家錯誤但其他照常回答。
- 可直接部署在自己的機器或免費層（Render/Fly.io 等）以便隨時使用。
