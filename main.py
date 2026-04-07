import os
import json
import tempfile
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN     = os.environ["BOT_TOKEN"]
OPENAI_KEY    = os.environ["OPENAI_API_KEY"]
NOTION_TOKEN  = os.environ["NOTION_TOKEN"]
NOTION_PARENT = os.environ["NOTION_PARENT_PAGE_ID"]
DIRECTOR_CHAT = os.environ.get("DIRECTOR_CHAT_ID", "")
PORT          = int(os.environ.get("PORT", 8080))
TG            = "https://api.telegram.org/bot" + BOT_TOKEN


def send(chat_id, text):
    requests.post(TG + "/sendMessage",
                  json={"chat_id": chat_id, "text": text[:4096]}, timeout=15)


def send_md(chat_id, text):
    r = requests.post(TG + "/sendMessage",
                      json={"chat_id": chat_id, "text": text[:4096],
                            "parse_mode": "Markdown",
                            "disable_web_page_preview": False}, timeout=15)
    if r.status_code != 200:
        requests.post(TG + "/sendMessage",
                      json={"chat_id": chat_id,
                            "text": text[:4096].replace("*","").replace("_","")},
                      timeout=15)


def download_voice(file_id):
    r = requests.get(TG + "/getFile", params={"file_id": file_id}, timeout=10)
    if r.status_code != 200:
        return None
    file_path = r.json()["result"]["file_path"]
    audio = requests.get(
        "https://api.telegram.org/file/bot" + BOT_TOKEN + "/" + file_path,
        timeout=30)
    return audio.content if audio.status_code == 200 else None


def transcribe(audio_bytes):
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name
    try:
        with open(tmp_path, "rb") as af:
            r = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + OPENAI_KEY},
                files={"file": ("voice.ogg", af, "audio/ogg")},
                data={"model": "whisper-1", "language": "ru"},
                timeout=120)
        return r.json().get("text", "").strip() if r.status_code == 200 else None
    finally:
        os.unlink(tmp_path)


def structure_report(transcript):
    prompt = (
        "Структурируй рапорт прораба. Верни JSON:\n"
        "{\n"
        "  \"object_name\": \"название объекта\",\n"
        "  \"supervisor\": \"прораб если назвал или пусто\",\n"
        "  \"overall_assessment\": \"В НОРМЕ или НЕЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ или ЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ или ПЕРЕВЫПОЛНЕНИЕ\",\n"
        "  \"completion_pct\": число,\n"
        "  \"works\": [{\"name\":\"\",\"plan\":\"\",\"fact\":\"\",\"completion_pct\":0}],\n"
        "  \"problems\": [{\"description\":\"\",\"urgency\":\"СРОЧНО или СЕГОДНЯ\",\"suggested_action\":\"\",\"responsible\":\"\"}],\n"
        "  \"material_requests\": [{\"material\":\"\",\"needed_qty\":\"\",\"deadline\":\"\",\"urgency\":\"СРОЧНО или ПЛАНОВЫЙ\"}],\n"
        "  \"headcount\": {\"actual\":0,\"planned\":0},\n"
        "  \"next_shift_tasks\": [{\"task\":\"\",\"responsible\":\"\",\"priority\":\"ВЫСОКИЙ или СРЕДНИЙ\"}],\n"
        "  \"summary_for_director\": \"2 предложения для директора\"\n"
        "}\n"
        "Рапорт: " + transcript
    )
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + OPENAI_KEY,
                 "Content-Type": "application/json"},
        json={"model": "gpt-4o",
              "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.1,
              "response_format": {"type": "json_object"}},
        timeout=90)
    if r.status_code != 200:
        return None
    return json.loads(r.json()["choices"][0]["message"]["content"])


def create_notion_page(d):
    NH = {"Authorization": "Bearer " + NOTION_TOKEN,
          "Content-Type": "application/json",
          "Notion-Version": "2022-06-28"}
    from datetime import datetime
    today = datetime.now().strftime("%d.%m.%Y")
    assess = d.get("overall_assessment", "")
    icon = "🟢" if ("НОРМ" in assess or "ПЕРЕВЫП" in assess) else "🟡" if "НЕЗНАЧ" in assess else "🔴"

    def rt(text):
        return {"type": "text", "text": {"content": str(text)[:2000]}}

    def para(text):
        return {"object": "block", "type": "paragraph",
                "paragraph": {"rich_text": [rt(text)]}}

    def h2(text):
        return {"object": "block", "type": "heading_2",
                "heading_2": {"rich_text": [rt(text)]}}

    def bullet(text):
        return {"object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": [rt(text)]}}

    def todo(text):
        return {"object": "block", "type": "to_do",
                "to_do": {"rich_text": [rt(text)], "checked": False}}

    def callout(text, emoji):
        return {"object": "block", "type": "callout",
                "callout": {"rich_text": [rt(text)],
                            "icon": {"type": "emoji", "emoji": emoji}}}

    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    blocks = []
    summary = d.get("summary_for_director", "")
    if summary:
        blocks.append(callout("FOR DIRECTOR: " + summary, "📋"))
        blocks.append(divider())

    pct = d.get("completion_pct", 0) or 0
    blocks.append(para(icon + " " + assess + " — " + str(int(pct)) + "% плана | Прораб: " + d.get("supervisor", "-") + " | " + today))

    hc = d.get("headcount", {})
    if hc:
        blocks.append(para("Персонал: " + str(hc.get("actual","-")) + "/" + str(hc.get("planned","-")) + " чел."))

    blocks.append(divider())

    for w in d.get("works", []):
        wp = w.get("completion_pct", 0) or 0
        ic = "OK" if wp >= 95 else "WARNING" if wp >= 75 else "PROBLEM"
        blocks.append(bullet(ic + " " + w.get("name","") + " — " + str(w.get("fact","")) + " из " + str(w.get("plan","")) + " (" + str(int(wp)) + "%)"))

    problems = d.get("problems", [])
    if problems:
        blocks.append(divider())
        blocks.append(h2("Проблемы (" + str(len(problems)) + ")"))
        for pr in problems:
            u = pr.get("urgency","")
            blocks.append(callout("[" + u + "] " + pr.get("description",""), "⚠️"))
            if pr.get("suggested_action"):
                blocks.append(todo("→ " + pr.get("suggested_action","") + " (" + pr.get("responsible","-") + ")"))

    mats = d.get("material_requests", [])
    if mats:
        blocks.append(divider())
        blocks.append(h2("Заявки на материалы"))
        for m in mats:
            blocks.append(todo(m.get("material","") + " — " + str(m.get("needed_qty","")) + " | срок: " + str(m.get("deadline",""))))

    for t in d.get("next_shift_tasks", []):
        blocks.append(todo(t.get("task","") + " → " + t.get("responsible","-")))

    blocks.append(divider())
    blocks.append(para("Создано Extella AI | " + today))

    title = icon + " " + d.get("object_name","Объект") + " | " + today
    payload = {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": blocks[:100]
    }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers=NH, json=payload, timeout=30)
    if r.status_code in [200, 201]:
        page_id = r.json().get("id","").replace("-","")
        return "https://notion.so/" + page_id
    return None


def notify_director(d, notion_url):
    if not DIRECTOR_CHAT:
        return
    assess = d.get("overall_assessment","")
    pct = d.get("completion_pct",0) or 0
    icon = "OK" if ("НОРМ" in assess or "ПЕРЕВЫП" in assess) else "WARNING" if "НЕЗНАЧ" in assess else "CRITICAL"
    problems = d.get("problems",[])
    mats = [m for m in d.get("material_requests",[]) if "СРОЧНО" in m.get("urgency","")]
    obj = d.get("object_name","Объект")
    sup = d.get("supervisor","-")
    summ = d.get("summary_for_director","")
    lines = []
    lines.append("[" + icon + "] " + obj + " — " + str(int(pct)) + "% плана")
    lines.append("Прораб: " + sup)
    if summ:
        lines.append(summ)
    if problems:
        lines.append("Проблем: " + str(len(problems)))
        for pr in problems[:2]:
            lines.append("  - " + pr.get("description","")[:60])
    if mats:
        lines.append("Срочные заявки: " + str(len(mats)))
        for m in mats[:2]:
            lines.append("  - " + m.get("material","") + " " + str(m.get("needed_qty","")))
    if notion_url:
        lines.append("Рапорт: " + notion_url)
    text = "\n".join(lines)
    send(DIRECTOR_CHAT, text)


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
        send(chat_id, "Привет! Запиши голосовое сообщение о том что сделали за смену. Я сам создам рапорт в Notion и уведомлю директора.")
        return jsonify({"ok": True})

    if voice:
        send(chat_id, "Расшифровываю...")
        audio_bytes = download_voice(voice["file_id"])
        if not audio_bytes:
            send(chat_id, "Не удалось скачать аудио. Попробуй ещё раз.")
            return jsonify({"ok": True})
        transcript = transcribe(audio_bytes)
        if not transcript:
            send(chat_id, "Не удалось расшифровать. Говори чуть громче.")
            return jsonify({"ok": True})
        send(chat_id, "Расшифровал: " + transcript[:200] + "\nСоздаю рапорт...")
        report = structure_report(transcript)
        if not report:
            send(chat_id, "Ошибка обработки. Попробуй ещё раз.")
            return jsonify({"ok": True})
        notion_url = create_notion_page(report)
        notify_director(report, notion_url)
        assess = report.get("overall_assessment","")
        pct = report.get("completion_pct",0) or 0
        icon = "OK" if ("НОРМ" in assess or "ПЕРЕВЫП" in assess) else "WARNING" if "НЕЗНАЧ" in assess else "CRITICAL"
        reply = "[" + icon + "] Рапорт принят! " + assess + " — " + str(int(pct)) + "% плана"
        if notion_url:
            reply = reply + "\nNotion: " + notion_url
        reply = reply + "\nДиректор уведомлён."
        send(chat_id, reply)
        return jsonify({"ok": True})

    if text and len(text) > 20 and not text.startswith("/"):
        send(chat_id, "Обрабатываю...")
        report = structure_report(text)
        if report:
            notion_url = create_notion_page(report)
            notify_director(report, notion_url)
            send(chat_id, "Готово! " + (notion_url or "Рапорт создан"))

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "url param required"}), 400
    r = requests.post(TG + "/setWebhook", json={"url": url}, timeout=10)
    return jsonify(r.json())


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    print("Starting AIOS Foreman Bot on port", PORT, flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
