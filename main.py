import os
import json
import base64
import tempfile
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN       = os.environ["BOT_TOKEN"]
OPENAI_KEY      = os.environ["OPENAI_API_KEY"]
NOTION_TOKEN    = os.environ["NOTION_TOKEN"]
NOTION_PARENT   = os.environ["NOTION_PARENT_PAGE_ID"]
DIRECTOR_CHAT   = os.environ.get("DIRECTOR_CHAT_ID", "")
EXTELLA_TOKEN   = os.environ.get("EXTELLA_API_TOKEN", "")
EXTELLA_URL     = os.environ.get("EXTELLA_API_URL", "https://api.extella.ai")
PORT            = int(os.environ.get("PORT", 8080))
TG              = "https://api.telegram.org/bot" + BOT_TOKEN

USER_OBJECTS       = {}
USER_SUBCONTRACTOR = {}
USER_KS2_NUMBER    = {}

# Защита от двойной обработки: храним update_id последних обработанных
PROCESSED_UPDATES = set()
PROCESSED_LOCK    = threading.Lock()

GREEN  = "🟢"
YELLOW = "🟡"
RED    = "🔴"


def get_icon(assess):
    if "НОРМ" in assess or "ПЕРЕВЫП" in assess:
        return GREEN
    elif "НЕЗНАЧ" in assess:
        return YELLOW
    return RED


def send(chat_id, text, parse_mode=None):
    payload = {"chat_id": chat_id, "text": text[:4096]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    try:
        requests.post(TG + "/sendMessage", json=payload, timeout=15)
    except Exception:
        pass


def download_file(file_id):
    r = requests.get(TG + "/getFile", params={"file_id": file_id}, timeout=10)
    if r.status_code != 200:
        return None
    file_path = r.json()["result"]["file_path"]
    resp = requests.get(
        f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}",
        timeout=60
    )
    return resp.content if resp.status_code == 200 else None


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
    system = "Ты опытный начальник смены строительного объекта. Структурируй рапорт прораба в JSON."
    obj_hint = ("Объект: " + object_name + ". ") if object_name else ""
    prompt = (
        obj_hint +
        "Структурируй рапорт. Верни JSON со следующими полями:\n"
        "object_name (string),\n"
        "supervisor (string),\n"
        "overall_assessment (одно из: В НОРМЕ / НЕЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ / ЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ / ПЕРЕВЫПОЛНЕНИЕ),\n"
        "completion_pct (number 0-100),\n"
        "works (array of {name, plan, fact, completion_pct}),\n"
        "problems (array of {description, urgency: СРОЧНО|СЕГОДНЯ, suggested_action, responsible}),\n"
        "material_requests (array of {material, needed_qty, deadline, urgency: СРОЧНО|ПЛАНОВЫЙ}),\n"
        "equipment_downtime (array of {equipment, downtime_hours, reason}),\n"
        "headcount ({actual, planned}),\n"
        "next_shift_tasks (array of {task, responsible, priority: ВЫСОКИЙ|СРЕДНИЙ}),\n"
        "summary_for_director (string - 2 предложения).\n\n"
        "Рапорт прораба: " + transcript
    )
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": "Bearer " + OPENAI_KEY, "Content-Type": "application/json"},
        json={"model": "gpt-4o",
              "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt}],
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
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [rt(text)]}}
    def h2(text):
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt(text)]}}
    def bullet(text):
        return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [rt(text)]}}
    def todo(text):
        return {"object": "block", "type": "to_do", "to_do": {"rich_text": [rt(text)], "checked": False}}
    def callout(text, emoji):
        return {"object": "block", "type": "callout",
                "callout": {"rich_text": [rt(text)], "icon": {"type": "emoji", "emoji": emoji}}}
    def divider():
        return {"object": "block", "type": "divider", "divider": {}}

    blocks = []
    summary = d.get("summary_for_director", "")
    if summary:
        blocks.append(callout("Для директора: " + summary, "📋"))
        blocks.append(divider())

    pct = d.get("completion_pct", 0) or 0
    hc  = d.get("headcount", {})
    blocks.append(para(icon + " " + assess + " — " + str(int(pct)) + "% плана"))
    if hc and hc.get("actual"):
        blocks.append(para("Персонал: " + str(hc.get("actual")) + "/" + str(hc.get("planned", "?")) + " чел."))
    blocks.append(divider())

    works = d.get("works", [])
    if works:
        blocks.append(h2("Выполнение"))
        for w in works:
            wp = w.get("completion_pct", 0) or 0
            ic = "✅" if wp >= 95 else "⚠️" if wp >= 75 else "🔴"
            blocks.append(bullet(ic + " " + w.get("name", "") + " — " + str(w.get("fact", "")) + " из " + str(w.get("plan", "")) + " (" + str(int(wp)) + "%)"))
        blocks.append(divider())

    problems = d.get("problems", [])
    if problems:
        blocks.append(h2("Проблемы (" + str(len(problems)) + ")"))
        for pr in problems:
            u = pr.get("urgency", "")
            blocks.append(callout("[" + u + "] " + pr.get("description", ""), "⚠️"))
            if pr.get("suggested_action"):
                blocks.append(todo("→ " + pr.get("suggested_action", "") + " (" + pr.get("responsible", "-") + ")"))
        blocks.append(divider())

    mats = d.get("material_requests", [])
    if mats:
        blocks.append(h2("Заявки на материалы"))
        for m in mats:
            u  = m.get("urgency", "")
            ic = "🔴" if "СРОЧНО" in u else "🟡"
            blocks.append(todo(ic + " " + m.get("material", "") + " — " + str(m.get("needed_qty", "")) + " | срок: " + str(m.get("deadline", ""))))
        blocks.append(divider())

    downtime = d.get("equipment_downtime", [])
    if downtime:
        blocks.append(h2("Простои"))
        for dt in downtime:
            resolved = "✅" if dt.get("resolved") else "🔴"
            blocks.append(bullet(resolved + " " + dt.get("equipment", "") + " — " + str(dt.get("downtime_hours", 0)) + " ч | " + dt.get("reason", "")))
        blocks.append(divider())

    next_tasks = d.get("next_shift_tasks", [])
    if next_tasks:
        blocks.append(h2("Следующей смене"))
        for t in next_tasks:
            p_ic = "🔴" if "ВЫСОКИЙ" in t.get("priority", "") else "🟡"
            blocks.append(todo(p_ic + " " + t.get("task", "") + " → " + t.get("responsible", "-")))

    blocks.append(divider())
    blocks.append(para("Создано Extella AI | " + today))

    obj_name = d.get("object_name", "Объект")
    sup      = d.get("supervisor", "")
    title    = icon + " " + obj_name + (" | " + sup if sup else "") + " | " + today

    payload = {
        "parent": {"type": "page_id", "page_id": NOTION_PARENT},
        "properties": {"title": {"title": [{"text": {"content": title}}]}},
        "children": blocks[:100]
    }
    r = requests.post("https://api.notion.com/v1/pages", headers=NH, json=payload, timeout=30)
    if r.status_code in [200, 201]:
        page_id = r.json().get("id", "").replace("-", "")
        return "https://notion.so/" + page_id
    return None


def notify_director(d, notion_url):
    if not DIRECTOR_CHAT:
        return
    assess = d.get("overall_assessment", "")
    pct    = d.get("completion_pct", 0) or 0
    icon   = get_icon(assess)
    obj    = d.get("object_name", "Объект")
    sup    = d.get("supervisor", "")
    summ   = d.get("summary_for_director", "")
    problems = d.get("problems", [])
    mats   = [m for m in d.get("material_requests", []) if "СРОЧНО" in m.get("urgency", "")]

    lines = [icon + " " + obj + " — " + str(int(pct)) + "% плана"]
    if sup:
        lines.append("Прораб: " + sup)
    if summ:
        lines.append(summ)
    if problems:
        lines.append("Проблем: " + str(len(problems)))
        for pr in problems[:3]:
            lines.append("  • " + pr.get("description", "")[:60])
    if mats:
        lines.append("Срочные заявки: " + str(len(mats)))
        for m in mats[:2]:
            lines.append("  • " + m.get("material", "") + " — " + str(m.get("needed_qty", "")))
    if notion_url:
        lines.append("Рапорт: " + notion_url)
    send(DIRECTOR_CHAT, "\n".join(lines))


# ─────────────────────────────────────────────────────────────────────────────
# КС-2 PIPELINE (асинхронный — через threading)
# ─────────────────────────────────────────────────────────────────────────────

def run_ks2_pipeline(chat_id, pdf_bytes, subcontractor, ks2_number):
    """Выполняется в фоновом потоке — Telegram уже получил 200 OK"""
    if not EXTELLA_TOKEN:
        send(chat_id, "❌ EXTELLA_API_TOKEN не настроен.")
        return

    send(chat_id, "⏳ Запускаю анализ КС-2... Это займёт ~30-40 секунд.")

    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

    try:
        resp = requests.post(
            f"{EXTELLA_URL}/api/expert/run",
            headers={"X-Auth-Token": EXTELLA_TOKEN, "Content-Type": "application/json"},
            json={
                "expert_name": "aios_ks2_pipeline_full",
                "params": {
                    "base64_pdf":         b64_pdf,
                    "openai_key":         OPENAI_KEY,
                    "subcontractor_name": subcontractor,
                    "ks2_number":         ks2_number,
                    "telegram_chat_id":   str(chat_id)
                }
            },
            timeout=150
        )
    except requests.exceptions.Timeout:
        send(chat_id, "⏱ Устройство занято. Попробуй через минуту.")
        return
    except Exception as e:
        send(chat_id, f"❌ Ошибка: {e}")
        return

    if resp.status_code != 200:
        send(chat_id, f"❌ Extella API вернул {resp.status_code}. Попробуй позже.")
        return

    result = resp.json().get("result", {})

    if result.get("status") == "error":
        send(chat_id, f"❌ Ошибка на шаге {result.get('step','?')}: {result.get('message','')}")
        return

    risk       = result.get("overall_risk", "")
    dispute    = result.get("dispute_needed", False)
    overrun    = result.get("overrun_total_tg", 0)
    total      = result.get("current_total_tg", 0)
    pct        = result.get("contract_completion_pct", 0)
    period     = result.get("period", "")
    items      = result.get("items_extracted", 0)
    conf       = result.get("ocr_confidence", "?")
    violations = result.get("violations", [])

    risk_emoji = "🔴" if risk == "ВЫСОКИЙ" else ("🟡" if risk == "СРЕДНИЙ" else "🟢")

    lines = [
        f"{risk_emoji} <b>КС-2 №{ks2_number} — {subcontractor}</b>",
        f"📅 Период: {period}",
        f"📋 Позиций: {items} | OCR: {conf}",
        f"",
        f"💰 Сумма акта: <b>{total:,} тг</b>",
        f"📊 Освоение договора: <b>{pct}%</b>",
    ]
    if dispute:
        lines += [f"", f"⚠️ <b>ЗАМЕЧАНИЯ! Превышение: {overrun:,} тг</b>"]
        for v in violations[:3]:
            lines.append(f"  • {v[:80]}")
        if len(violations) > 3:
            lines.append(f"  ... и ещё {len(violations)-3}")
        lines += [f"", f"📄 Письмо-замечание сформировано автоматически"]
    else:
        lines += [f"", f"✅ <b>Замечаний нет — КС-2 можно принять</b>"]

    lines += [f"", f"📁 Excel сверка и реестр сохранены на устройстве"]
    send(chat_id, "\n".join(lines), parse_mode="HTML")


# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    update  = request.json or {}
    update_id = update.get("update_id")
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    # Защита от двойной обработки (Telegram retry)
    if update_id:
        with PROCESSED_LOCK:
            if update_id in PROCESSED_UPDATES:
                return jsonify({"ok": True})  # Уже обработали
            PROCESSED_UPDATES.add(update_id)
            # Очищаем старые (храним последние 500)
            if len(PROCESSED_UPDATES) > 500:
                oldest = sorted(PROCESSED_UPDATES)[:250]
                for uid in oldest:
                    PROCESSED_UPDATES.discard(uid)

    text     = message.get("text", "").strip()
    voice    = message.get("voice")
    document = message.get("document")

    # ── /start
    if text.startswith("/start"):
        send(chat_id, (
            "Привет! Я помогаю прорабам и ПТО.\n\n"
            "📢 Голосовой рапорт прораба:\n"
            "  1. /object ЖК Северный блок 3\n"
            "  2. Запиши голосовое — создам рапорт в Notion\n\n"
            "📄 Проверка КС-2 (PDF скан):\n"
            "  1. /subcontractor ТОО СтройМонтаж\n"
            "  2. /ks2number 1\n"
            "  3. Прикрепи PDF — проверю и сообщу результат"
        ))
        return jsonify({"ok": True})

    # ── /object
    if text.startswith("/object"):
        obj_name = text[7:].strip()
        if obj_name:
            USER_OBJECTS[chat_id] = obj_name
            send(chat_id, "✅ Объект запомнен: " + obj_name)
        else:
            send(chat_id, "Текущий объект: " + USER_OBJECTS.get(chat_id, "не задан"))
        return jsonify({"ok": True})

    # ── /subcontractor
    if text.startswith("/subcontractor"):
        name = text[14:].strip()
        if name:
            USER_SUBCONTRACTOR[chat_id] = name
            send(chat_id, f"✅ Субподрядчик: {name}\n\nТеперь отправь PDF скан КС-2.")
        else:
            send(chat_id, f"Текущий: {USER_SUBCONTRACTOR.get(chat_id, 'не задан')}")
        return jsonify({"ok": True})

    # ── /ks2number
    if text.startswith("/ks2number"):
        num = text[10:].strip()
        if num:
            USER_KS2_NUMBER[chat_id] = num
            send(chat_id, f"✅ Номер КС-2: {num}\n\nОтправь PDF.")
        else:
            send(chat_id, f"Текущий номер: {USER_KS2_NUMBER.get(chat_id, '1')}")
        return jsonify({"ok": True})

    # ── /help
    if text.startswith("/help"):
        send(chat_id, (
            "Команды:\n"
            "/object [название] — объект для рапорта\n"
            "/subcontractor [название] — субподрядчик\n"
            "/ks2number [номер] — номер акта\n\n"
            f"Объект: {USER_OBJECTS.get(chat_id, 'не задан')}\n"
            f"Субподрядчик: {USER_SUBCONTRACTOR.get(chat_id, 'не задан')}\n"
            f"Номер КС-2: {USER_KS2_NUMBER.get(chat_id, '1')}\n\n"
            "Голосовое → рапорт прораба\n"
            "PDF → проверка КС-2\n"
            "PDF отправляйте по одному и ждите ответа перед следующим"
        ))
        return jsonify({"ok": True})

    # ── PDF → КС-2 пайплайн (асинхронно)
    if document:
        mime  = document.get("mime_type", "")
        fname = document.get("file_name", "").lower()
        if mime == "application/pdf" or fname.endswith(".pdf"):
            subcontractor = USER_SUBCONTRACTOR.get(chat_id, "Субподрядчик")
            ks2_number    = USER_KS2_NUMBER.get(chat_id, "1")
            pdf_bytes     = download_file(document["file_id"])
            if not pdf_bytes:
                send(chat_id, "❌ Не удалось скачать файл.")
                return jsonify({"ok": True})
            if len(pdf_bytes) > 20 * 1024 * 1024:
                send(chat_id, "❌ Файл > 20MB.")
                return jsonify({"ok": True})

            # Авто-инкремент номера КС
            try:
                USER_KS2_NUMBER[chat_id] = str(int(ks2_number) + 1)
            except Exception:
                pass

            # Запускаем в фоновом потоке — сразу возвращаем 200 OK
            t = threading.Thread(
                target=run_ks2_pipeline,
                args=(chat_id, pdf_bytes, subcontractor, ks2_number),
                daemon=True
            )
            t.start()
            return jsonify({"ok": True})  # ← возвращаем Telegram сразу!
        else:
            send(chat_id, "⚠️ Я принимаю только PDF.")
            return jsonify({"ok": True})

    # ── Голосовой рапорт (тоже асинхронно)
    if voice:
        audio_bytes = download_file(voice["file_id"])
        if not audio_bytes:
            send(chat_id, "Не удалось скачать аудио.")
            return jsonify({"ok": True})

        def process_voice():
            send(chat_id, "Расшифровываю...")
            transcript = transcribe(audio_bytes)
            if not transcript:
                send(chat_id, "Не удалось расшифровать.")
                return
            send(chat_id, "Создаю рапорт...")
            report = structure_report(transcript, USER_OBJECTS.get(chat_id, ""))
            if not report:
                send(chat_id, "Ошибка обработки.")
                return
            notion_url = create_notion_page(report)
            notify_director(report, notion_url)
            assess = report.get("overall_assessment", "")
            pct    = report.get("completion_pct", 0) or 0
            icon   = get_icon(assess)
            obj    = report.get("object_name", USER_OBJECTS.get(chat_id, "Объект"))
            lines  = [icon + " Рапорт принят!", "Объект: " + obj,
                      assess + " — " + str(int(pct)) + "% плана"]
            if report.get("problems"): lines.append("Проблем: " + str(len(report["problems"])))
            if report.get("material_requests"): lines.append("Заявок: " + str(len(report["material_requests"])))
            if notion_url: lines.append("Notion: " + notion_url)
            lines.append("Директор уведомлён.")
            send(chat_id, "\n".join(lines))

        threading.Thread(target=process_voice, daemon=True).start()
        return jsonify({"ok": True})

    # ── Текст как рапорт
    if text and len(text) > 20 and not text.startswith("/"):
        def process_text():
            send(chat_id, "Обрабатываю...")
            report = structure_report(text, USER_OBJECTS.get(chat_id, ""))
            if report:
                notion_url = create_notion_page(report)
                notify_director(report, notion_url)
                icon = get_icon(report.get("overall_assessment", ""))
                send(chat_id, icon + " Готово! " + (notion_url or "Рапорт создан"))
        threading.Thread(target=process_text, daemon=True).start()

    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aios-foreman-bot", "version": "2.2"})


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
    print("Starting AIOS Foreman Bot v2.2 on port", PORT, flush=True)
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
