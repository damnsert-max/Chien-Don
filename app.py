import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


@app.route("/")
def index() -> str:
    return render_template(
        "index.html",
        openai_model=OPENAI_MODEL,
        anthropic_model=ANTHROPIC_MODEL,
        gemini_model=GEMINI_MODEL,
    )


def call_openai(prompt: str) -> Dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"ok": False, "error": "Missing OPENAI_API_KEY"}

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        return {"ok": True, "text": text}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def call_claude(prompt: str) -> Dict[str, Any]:
    if not ANTHROPIC_API_KEY:
        return {"ok": False, "error": "Missing ANTHROPIC_API_KEY"}

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = data.get("content", [])
        text = "\n".join([p.get("text", "") for p in parts if p.get("type") == "text"]).strip()
        return {"ok": True, "text": text or "(No text returned)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def call_gemini(prompt: str) -> Dict[str, Any]:
    if not GOOGLE_API_KEY:
        return {"ok": False, "error": "Missing GOOGLE_API_KEY"}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
        f"?key={GOOGLE_API_KEY}"
    )

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        text: Optional[str] = None
        candidates = data.get("candidates", [])
        if candidates:
            content = candidates[0].get("content", {})
            parts = content.get("parts", [])
            text = "\n".join([p.get("text", "") for p in parts if "text" in p]).strip()
        return {"ok": True, "text": text or "(No text returned)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


@app.post("/chat")
def chat() -> Any:
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_openai = pool.submit(call_openai, prompt)
        fut_claude = pool.submit(call_claude, prompt)
        fut_gemini = pool.submit(call_gemini, prompt)

        result = {
            "openai": fut_openai.result(),
            "claude": fut_claude.result(),
            "gemini": fut_gemini.result(),
        }

    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "7860")), debug=True)
