import os
import json
import tempfile
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN       = os.environ["BOT_TOKEN"]
OPENAI_KEY      = os.environ["OPENAI_API_KEY"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
NOTION_PARENT   = os.environ["NOTION_PARENT_PAGE_ID"]
DIRECTOR_CHAT   = os.environ.get("DIRECTOR_CHAT_ID", "")
PORT            = int(os.environ.get("PORT", 8080))

TG = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Telegram helpers ────────────────────────────────────────────────────────

def send(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    requests.post(f"{TG}/sendMessage", json=payload, timeout=15)

def download_voice(file_id):
    r = requests.get(f"{TG}/getFile", params={"file_id": file_id}, timeout=10)
    if r.status_code != 200:
        return None
    file_path = r.json()["result"]["file_path"]
    audio = requests.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
        timeout=30
    )
    return audio.content if audio.status_code == 200 else None

# ── Whisper ─────────────────────────────────────────────────────────────────

def transcribe(audio_bytes, ext="ogg"):
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as audio_f:
            r = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_KEY}"},
                files={"file": (f"voice.{ext}", audio_f, "audio/ogg")},
                data={"model": "whisper-1", "language": "ru",
                      "prompt": "Рапорт прораба. Строительный объект, бетон, арматура, рабочие."},
                timeout=120
            )
        return r.json().get("text", "").strip() if r.status_code == 200 else None
    finally:
        os.unlink(tmp_path)

# ── GPT report structuring ───────────────────────────────────────────────────

def structure_report(transcript, object_name="Объект"):
    prompt = f"""Ты — начальник смены. Структурируй рапорт прораба. Верни JSON:
{{
  "object_name": "название объекта",
  "supervisor": "прораб если назвал",
  "overall_assessment": "В НОРМЕ" или "НЕЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ" или "ЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ" или "ПЕРЕВЫПОЛНЕНИЕ",
  "completion_pct": число,
  "works": [{{"name":"работа","plan":"план","fact":"факт","completion_pct":число,"status":"ВЫПОЛНЕНО/ОТСТАВАНИЕ"}}],
  "problems": [{{"description":"проблема","urgency":"СРОЧНО/СЕГОДНЯ","suggested_action":"действие","responsible":"кто"}}],
  "material_requests": [{{"material":"материал","needed_qty":"кол-во","deadline":"срок","urgency":"СРОЧНО/ПЛАНОВЫЙ"}}],
  "equipment_downtime": [{{"equipment":"техника","downtime_hours":число,"reason":"причина"}}],
  "total_downtime_hours": число,
  "headcount": {{"actual":число,"planned":число}},
  "safety_incidents": "нет или описание",
  "next_shift_tasks": [{{"task":"задание","responsible":"кто","priority":"ВЫСОКИЙ/СРЕДНИЙ"}}],
  "summary_for_director": "2 предложения для директора"
}}
Рапорт: {transcript}"""

    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1, "response_format": {"type": "json_object"}},
        timeout=90
    )
    if r.status_code != 200:
        return None
    return json.loads(r.json()["choices"][0]["message"]["content"])

# ── Notion ───────────────────────────────────────────────────────────────────

def create_notion_page(d):
    NH = {"Authorization": f"Bearer {NOTION_TOKEN}",
          "Content-Type": "application/json", "Notion-Version": "2022-06-28"}

    from datetime import datetime
    today = datetime.now().strftime("%d.%m.%Y")
    assess = d.get("overall_assessment", "")
    icon = "🟢" if "НОРМ" in assess or "ПЕРЕВЫП" in assess else "🟡" if "НЕЗНАЧ" in assess else "🔴"

    def rt(text, bold=False):
        r = {"type": "text", "text": {"content": str(text)[:2000]}}
        if bold:
            r["annotations"] = {"bold": True}
        return r

    def para(text):
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [rt(text)]}}

    def h2(text):
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [rt(text, bold=True)]}}

    def bullet(text):
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [rt(text)]}}

    def todo(text):
        return {"object": "block", "type": "to_do",
                "to_do": {"rich_text": [rt(text)], "checked": False}}

    def callout(text, emoji):
        return {"object": "block", "type": "callout",
                "callout": {"rich_text": [rt(text)], "icon": {"type": "emoji", "emoji": emoji}}}

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    blocks = []
    summary = d.get("summary_for_director", "")
    if summary:
        blocks.append(callout(f"📋 ДЛЯ ДИРЕКТОРА: {summary}", "📋"))
        blocks.append(divider())

    pct = d.get("completion_pct", 0) or 0
    blocks.append(para(f"{icon} {assess} — {pct:.0f}% плана | Прораб: {d.get('supervisor','-')} | {today}"))

    hc = d.get("headcount", {})
    if hc:
        blocks.append(para(f"👥 Персонал: {hc.get('actual','-')}/{hc.get('planned','-')} чел."))

    safety = d.get("safety_incidents", "")
    if safety and safety.lower() not in ["нет", "не было", "no"]:
        blocks.append(callout(f"⚠️ ОТ: {safety}", "⚠️"))

    blocks.append(divider())

    works = d.get("works", [])
    if works:
        blocks.append(h2("📊 Выполнение"))
        for w in works:
            wp = w.get("completion_pct", 0) or 0
            ic = "✅" if wp >= 95 else "⚠️" if wp >= 75 else "🔴"
            blocks.append(bullet(f"{ic} {w.get('name','')} — {w.get('fact','')} из {w.get('plan','')} ({wp:.0f}%)"))
        blocks.append(divider())

    problems = d.get("problems", [])
    if problems:
        blocks.append(h2(f"🚨 Проблемы ({len(problems)} шт.)"))
        for pr in problems:
            u = pr.get("urgency","")
            ic = "🔴" if "СРОЧНО" in u else "🟡"
            blocks.append(callout(f"{ic} [{u}] {pr.get('description','')}", ic))
            if pr.get("suggested_action"):
                blocks.append(todo(f"→ {pr.get('suggested_action','')} ({pr.get('responsible','-')})"))
        blocks.append(divider())

    mats = d.get("material_requests", [])
    if mats:
        blocks.append(h2(f"📦 Заявки на материалы"))
        for m in mats:
            u = m.get("urgency","")
            ic = "🔴" if "СРОЧНО" in u else "🟡"
            blocks.append(todo(f"{ic} {m.get('material','')} — {m.get('needed_qty','')} | срок: {m.get('deadline','')}"))
        blocks.append(divider())

    next_tasks = d.get("next_shift_tasks", [])
    if next_tasks:
        blocks.append(h2("📋 Следующей смене"))
        for t in next_tasks:
            p_ic = "🔴" if "ВЫСОКИЙ" in t.get("priority","") else "🟡"
            blocks.append(todo(f"{p_ic} {t.get('task','')} → {t.get('responsible','-')}"))

    blocks.append(divider())
    blocks.append(para(f"Создано Extella AI · {today}"))

    title = f"{icon} {d.get('object_name','Объект')} | {today}"
    payload = {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": blocks[:100]
    }

    r = requests.post("https://api.notion.com/v1/pages", headers=NH,
                      json=payload, timeout=30)
    if r.status_code in [200, 201]:
        page_id = r.json().get("id","").replace("-","")
        return f"https://notion.so/{page_id}"
    return None

# ── Telegram director alert ──────────────────────────────────────────────────

def notify_director(d, notion_url):
    if not DIRECTOR_CHAT:
        return
    assess = d.get("overall_assessment","")
    pct = d.get("completion_pct",0) or 0
    icon = "🟢" if "НОРМ" in assess or "ПЕРЕВЫП" in assess else "🟡" if "НЕЗНАЧ" in assess else "🔴"
    problems = d.get("problems",[])
    mats = [m for m in d.get("material_requests",[]) if "СРОЧНО" in m.get("urgency","")]

    text = f"{icon} *{d.get('object_name','Объект')}* — {pct:.0f}% плана
"
    text += f"👷 Прораб: {d.get('supervisor','-')}
"
    text += f"📋 {d.get('summary_for_director','')}
"
    if problems:
        text += f"
🚨 Проблем: {len(problems)}"
        for pr in problems[:2]:
            text += f"
  • {pr.get('description','')[:60]}"
    if mats:
        text += f"
📦 Срочные заявки: {len(mats)}"
        for m in mats[:2]:
            text += f"
  • {m.get('material','')} — {m.get('needed_qty','')}"
    if notion_url:
        text += f"

📎 [Полный рапорт]({notion_url})"

    requests.post(f"{TG}/sendMessage", json={
        "chat_id": DIRECTOR_CHAT,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }, timeout=15)

# ── Webhook handler ──────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    text = message.get("text", "")
    voice = message.get("voice")

    if text.startswith("/start"):
        send(chat_id,
             "👷 Привет! Я помогаю прорабам отправлять рапорты.\n\n"
             "Просто запиши голосовое сообщение о том что сделали за смену — "
             "я сам создам рапорт в Notion и уведомлю директора.")
        return jsonify({"ok": True})

    if text.startswith("/help"):
        send(chat_id,
             "Как пользоваться:\n"
             "1. Запиши голосовое прямо в этом чате\n"
             "2. Расскажи: что сделали, проблемы, сколько рабочих, заявки на материалы\n"
             "3. Я сделаю остальное автоматически\n\n"
             "Пример: \"Залили колонны 8 из 10, не хватило бетона, заказали на завтра. Рабочих 34\."")
        return jsonify({"ok": True})

    if voice:
        send(chat_id, "⏳ Расшифровываю...")

        # Download
        audio_bytes = download_voice(voice["file_id"])
        if not audio_bytes:
            send(chat_id, "❌ Не удалось скачать аудио. Попробуй ещё раз.")
            return jsonify({"ok": True})

        # Transcribe
        transcript = transcribe(audio_bytes, "ogg")
        if not transcript:
            send(chat_id, "❌ Не удалось расшифровать. Говори чуть громче и чётче.")
            return jsonify({"ok": True})

        send(chat_id, f"📝 Расшифровал:\n_{transcript[:300]}_\n\n⏳ Создаю рапорт...")

        # Structure
        report = structure_report(transcript)
        if not report:
            send(chat_id, "❌ Ошибка обработки. Попробуй ещё раз.")
            return jsonify({"ok": True})

        # Notion
        notion_url = create_notion_page(report)

        # Director
        notify_director(report, notion_url)

        # Reply to foreman
        assess = report.get("overall_assessment","")
        pct = report.get("completion_pct",0) or 0
        icon = "🟢" if "НОРМ" in assess or "ПЕРЕВЫП" in assess else "🟡" if "НЕЗНАЧ" in assess else "🔴"
        problems = report.get("problems",[])
        mats = report.get("material_requests",[])
        reply = f"{icon} Рапорт принят!\n\n"
        reply += f"📊 {assess} — {pct:.0f}% плана\n"
        if problems:
            reply += f"🚨 Проблем: {len(problems)}\n"
        if mats:
            reply += f"📦 Заявок на материалы: {len(mats)}\n"
        if notion_url:
            reply += f"\n📋 Notion: {notion_url}"
        else:
            reply += "\n⚠️ Notion: не удалось создать страницу"
        reply += "\n\n✅ Директор уведомлён."

        send(chat_id, reply)
        return jsonify({"ok": True})

    # Текстовый рапорт тоже принимаем
    if text and len(text) > 30 and not text.startswith("/"):
        send(chat_id, "📝 Обрабатываю текстовый рапорт...")
        report = structure_report(text)
        if report:
            notion_url = create_notion_page(report)
            notify_director(report, notion_url)
            send(chat_id, f"✅ Рапорт принят! {notion_url or ''}")

    return jsonify({"ok": True})

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aios-foreman-bot"})

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url param required"}), 400
    r = requests.post(f"{TG}/setWebhook", json={"url": url}, timeout=10)
    return jsonify(r.json())

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    print(f"Starting AIOS Foreman Bot on port {PORT}", flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
