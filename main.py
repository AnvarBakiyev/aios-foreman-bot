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

# Per-user object name storage (in-memory, resets on redeploy)
# Format: {chat_id: "ЖК Северный блок 3"}
USER_OBJECTS = {}

GREEN  = "🟢"
YELLOW = "🟡"
RED    = "🔴"


def get_icon(assess):
    if "НОРМ" in assess or "ПЕРЕВЫП" in assess:
        return GREEN
    elif "НЕЗНАЧ" in assess:
        return YELLOW
    return RED


def send(chat_id, text):
    requests.post(TG + "/sendMessage",
                  json={"chat_id": chat_id, "text": text[:4096]}, timeout=15)


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


def structure_report(transcript, object_name=""):
    obj_hint = ""
    if object_name:
        obj_hint = "Название объекта: " + object_name + ". "
    prompt = (
        obj_hint +
        "Структурируй рапорт прораба. Верни JSON:\n"
        "{\n"
        "  \"object_name\": \"" + (object_name or "название объекта из текста или Объект") + "\",\n"
        "  \"supervisor\": \"имя прораба если назвал или пусто\",\n"
        "  \"overall_assessment\": \"В НОРМЕ или НЕЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ или ЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ или ПЕРЕВЫПОЛНЕНИЕ\",\n"
        "  \"completion_pct\": число от 0 до 100,\n"
        "  \"works\": [{\"name\":\"вид работы\",\"plan\":\"план\",\"fact\":\"факт\",\"completion_pct\":число}],\n"
        "  \"problems\": [{\"description\":\"проблема\",\"urgency\":\"СРОЧНО или СЕГОДНЯ\",\"suggested_action\":\"что делать\",\"responsible\":\"кто\"}],\n"
        "  \"material_requests\": [{\"material\":\"материал\",\"needed_qty\":\"количество если сказал или уточнить\",\"deadline\":\"срок\",\"urgency\":\"СРОЧНО или ПЛАНОВЫЙ\"}],\n"
        "  \"headcount\": {\"actual\":число,\"planned\":число},\n"
        "  \"next_shift_tasks\": [{\"task\":\"задача\",\"responsible\":\"кто\",\"priority\":\"ВЫСОКИЙ или СРЕДНИЙ\"}],\n"
        "  \"summary_for_director\": \"2 предложения для директора\"\n"
        "}\n"
        "Рапорт прораба: " + transcript
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
    icon = get_icon(assess)

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
        blocks.append(callout("📋️Для директора: " + summary, "📋"))
        blocks.append(divider())

    pct = d.get("completion_pct", 0) or 0
    hc = d.get("headcount", {})
    blocks.append(para(icon + " " + assess + " — " + str(int(pct)) + "% плана"))
    if hc and hc.get("actual"):
        blocks.append(para("👥 Персонал: " + str(hc.get("actual","-")) + "/" + str(hc.get("planned","-")) + " чел."))
    blocks.append(divider())

    works = d.get("works", [])
    if works:
        blocks.append(h2("📊 Выполнение"))
        for w in works:
            wp = w.get("completion_pct", 0) or 0
            ic = "✅" if wp >= 95 else "⚠️" if wp >= 75 else "🔴"
            blocks.append(bullet(ic + " " + w.get("name","") + " — " + str(w.get("fact","")) + " из " + str(w.get("plan","")) + " (" + str(int(wp)) + "%)"))
        blocks.append(divider())

    problems = d.get("problems", [])
    if problems:
        blocks.append(h2("🚨 Проблемы (" + str(len(problems)) + ")"))
        for pr in problems:
            u = pr.get("urgency","")
            blocks.append(callout("[" + u + "] " + pr.get("description",""), "⚠️"))
            if pr.get("suggested_action"):
                blocks.append(todo("→ " + pr.get("suggested_action","") + " (" + pr.get("responsible","-") + ")"))
        blocks.append(divider())

    mats = d.get("material_requests", [])
    if mats:
        blocks.append(h2("📦 Заявки на материалы"))
        for m in mats:
            u = m.get("urgency","")
            ic = "🔴" if "СРОЧНО" in u else "🟡"
            blocks.append(todo(ic + " " + m.get("material","") + " — " + str(m.get("needed_qty","")) + " | срок: " + str(m.get("deadline",""))))
        blocks.append(divider())

    next_tasks = d.get("next_shift_tasks", [])
    if next_tasks:
        blocks.append(h2("📋 Следующей смене"))
        for t in next_tasks:
            p_ic = "🔴" if "ВЫСОКИЙ" in t.get("priority","") else "🟡"
            blocks.append(todo(p_ic + " " + t.get("task","") + " → " + t.get("responsible","-")))

    blocks.append(divider())
    blocks.append(para("Создано Extella AI | " + today))

    obj_name = d.get("object_name","Объект")
    sup = d.get("supervisor","")
    title = icon + " " + obj_name + " | " + today
    if sup:
        title = icon + " " + obj_name + " | " + sup + " | " + today

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
    icon = get_icon(assess)
    problems = d.get("problems",[])
    mats = [m for m in d.get("material_requests",[]) if "СРОЧНО" in m.get("urgency","")]
    obj = d.get("object_name","Объект")
    sup = d.get("supervisor","")
    summ = d.get("summary_for_director","")
    lines = []
    lines.append(icon + " " + obj + " — " + str(int(pct)) + "% плана")
    if sup:
        lines.append("👷 " + sup)
    if summ:
        lines.append(summ)
    if problems:
        lines.append("🚨 Проблем: " + str(len(problems)))
        for pr in problems[:2]:
            lines.append("  • " + pr.get("description","")[:60])
    if mats:
        lines.append("📦 Срочные заявки: " + str(len(mats)))
        for m in mats[:2]:
            lines.append("  • " + m.get("material","") + " — " + str(m.get("needed_qty","")))
    if notion_url:
        lines.append("
📎 Рапорт: " + notion_url)
    send(DIRECTOR_CHAT, "\n".join(lines))


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json or {}
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    text = message.get("text", "").strip()
    voice = message.get("voice")

    # /start
    if text.startswith("/start"):
        send(chat_id,
             "👷 Привет! Я помогаю прорабам отправлять рапорты.\n\n"
             "🏗 Сначала укажи свой объект:\n"
             "/object ЖК Северный блок 3\n\n"
             "Потом просто записывай голосовое и отправляй сюда.")
        return jsonify({"ok": True})

    # /object ЖК Северный блок 3
    if text.startswith("/object"):
        obj_name = text[7:].strip()
        if obj_name:
            USER_OBJECTS[chat_id] = obj_name
            send(chat_id, "✅ Объект запоминал: " + obj_name + "\n\nТеперь просто записывай голосовое о смене.")
        else:
            current = USER_OBJECTS.get(chat_id, "не установлен")
            send(chat_id, "Текущий объект: " + current + "\nИспользование: /object Название")
        return jsonify({"ok": True})

    # /help
    if text.startswith("/help"):
        current_obj = USER_OBJECTS.get(chat_id, "не установлен")
        send(chat_id,
             "📋 Команды:\n"
             "/object [название] — запомнить свой объект\n"
             "/object — показать текущий\n\n"
             "🏗 Твой объект: " + current_obj + "\n\n"
             "🎤 Просто запиши голосовое: что сделали, проблемы, сколько рабочих.")
        return jsonify({"ok": True})

    # Voice message
    if voice:
        send(chat_id, "⏳ Расшифровываю...")
        audio_bytes = download_voice(voice["file_id"])
        if not audio_bytes:
            send(chat_id, "❌ Не удалось скачать аудио. Попробуй ещё раз.")
            return jsonify({"ok": True})
        transcript = transcribe(audio_bytes)
        if not transcript:
            send(chat_id, "❌ Не удалось расшифровать. Говори чуть громче.")
            return jsonify({"ok": True})
        send(chat_id, "✅ Расшифровал. Создаю рапорт...")
        object_name = USER_OBJECTS.get(chat_id, "")
        report = structure_report(transcript, object_name)
        if not report:
            send(chat_id, "❌ Ошибка обработки. Попробуй ещё раз.")
            return jsonify({"ok": True})
        notion_url = create_notion_page(report)
        notify_director(report, notion_url)
        assess = report.get("overall_assessment","")
        pct = report.get("completion_pct",0) or 0
        icon = get_icon(assess)
        problems = report.get("problems",[])
        mats = report.get("material_requests",[])
        obj = report.get("object_name", object_name or "Объект")
        lines = []
        lines.append(icon + " Рапорт принят!")
        lines.append("🏗 " + obj)
        lines.append("📊 " + assess + " — " + str(int(pct)) + "% плана")
        if problems:
            lines.append("🚨 Проблем: " + str(len(problems)))
        if mats:
            lines.append("📦 Заявок: " + str(len(mats)))
        if notion_url:
            lines.append("📋 Notion: " + notion_url)
        lines.append("✅ Директор уведомлён.")
        if not object_name:
            lines.append("\n💡 Чтобы запомнить название объекта: /object ЖК Северный блок 3")
        send(chat_id, "\n".join(lines))
        return jsonify({"ok": True})

    # Text report
    if text and len(text) > 20 and not text.startswith("/"):
        send(chat_id, "⏳ Обрабатываю...")
        object_name = USER_OBJECTS.get(chat_id, "")
        report = structure_report(text, object_name)
        if report:
            notion_url = create_notion_page(report)
            notify_director(report, notion_url)
            assess = report.get("overall_assessment","")
            icon = get_icon(assess)
            send(chat_id, icon + " Готово! " + (notion_url or "Рапорт создан"))
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aios-foreman-bot"})


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
