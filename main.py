import os
import json
import base64
import tempfile
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN     = os.environ["BOT_TOKEN"]
OPENAI_KEY    = os.environ["OPENAI_API_KEY"]
NOTION_TOKEN  = os.environ["NOTION_TOKEN"]
NOTION_PARENT = os.environ["NOTION_PARENT_PAGE_ID"]
DIRECTOR_CHAT = os.environ.get("DIRECTOR_CHAT_ID", "")
EXTELLA_TOKEN = os.environ.get("EXTELLA_API_TOKEN", "")
EXTELLA_URL   = os.environ.get("EXTELLA_API_URL", "https://api.extella.ai")
PORT          = int(os.environ.get("PORT", 8080))
TG            = "https://api.telegram.org/bot" + BOT_TOKEN

USER_OBJECTS       = {}
USER_SUBCONTRACTOR = {}
USER_KS2_NUMBER    = {}

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
        "Структурируй рапорт. Верни JSON:\n"
        "object_name, supervisor, overall_assessment (В НОРМЕ|НЕЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ|ЗНАЧИТЕЛЬНОЕ ОТСТАВАНИЕ|ПЕРЕВЫПОЛНЕНИЕ),\n"
        "completion_pct, works [{name,plan,fact,completion_pct}],\n"
        "problems [{description,urgency:СРОЧНО|СЕГОДНЯ,suggested_action,responsible}],\n"
        "material_requests [{material,needed_qty,deadline,urgency:СРОЧНО|ПЛАНОВЫЙ}],\n"
        "equipment_downtime [{equipment,downtime_hours,reason}],\n"
        "headcount {actual,planned}, next_shift_tasks [{task,responsible,priority:ВЫСОКИЙ|СРЕДНИЙ}],\n"
        "summary_for_director (2 предложения).\n\n"
        "Рапорт: " + transcript
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

    def rt(t): return {"type": "text", "text": {"content": str(t)[:2000]}}
    def para(t): return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [rt(t)]}}
    def h2(t): return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [rt(t)]}}
    def bullet(t): return {"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [rt(t)]}}
    def todo(t): return {"object": "block", "type": "to_do", "to_do": {"rich_text": [rt(t)], "checked": False}}
    def callout(t, e): return {"object": "block", "type": "callout", "callout": {"rich_text": [rt(t)], "icon": {"type": "emoji", "emoji": e}}}
    def divider(): return {"object": "block", "type": "divider", "divider": {}}

    blocks = []
    if d.get("summary_for_director"):
        blocks += [callout("Для директора: " + d["summary_for_director"], "📋"), divider()]
    pct = d.get("completion_pct", 0) or 0
    hc  = d.get("headcount", {})
    blocks.append(para(icon + " " + assess + " — " + str(int(pct)) + "% плана"))
    if hc and hc.get("actual"):
        blocks.append(para("Персонал: " + str(hc["actual"]) + "/" + str(hc.get("planned", "?")) + " чел."))
    blocks.append(divider())
    if d.get("works"):
        blocks.append(h2("Выполнение"))
        for w in d["works"]:
            wp = w.get("completion_pct", 0) or 0
            ic = "✅" if wp >= 95 else "⚠️" if wp >= 75 else "🔴"
            blocks.append(bullet(f"{ic} {w.get('name','')} — {w.get('fact','')} из {w.get('plan','')} ({int(wp)}%)"))
        blocks.append(divider())
    if d.get("problems"):
        blocks.append(h2("Проблемы (" + str(len(d["problems"])) + ")"))
        for pr in d["problems"]:
            blocks.append(callout("[" + pr.get("urgency", "") + "] " + pr.get("description", ""), "⚠️"))
            if pr.get("suggested_action"):
                blocks.append(todo("→ " + pr["suggested_action"] + " (" + pr.get("responsible", "-") + ")"))
        blocks.append(divider())
    if d.get("material_requests"):
        blocks.append(h2("Заявки на материалы"))
        for m in d["material_requests"]:
            ic = "🔴" if "СРОЧНО" in m.get("urgency", "") else "🟡"
            blocks.append(todo(f"{ic} {m.get('material','')} — {m.get('needed_qty','')} | срок: {m.get('deadline','')}"))
        blocks.append(divider())
    if d.get("equipment_downtime"):
        blocks.append(h2("Простои"))
        for dt in d["equipment_downtime"]:
            blocks.append(bullet(("✅" if dt.get("resolved") else "🔴") + f" {dt.get('equipment','')} — {dt.get('downtime_hours',0)} ч | {dt.get('reason','')}"))
        blocks.append(divider())
    if d.get("next_shift_tasks"):
        blocks.append(h2("Следующей смене"))
        for t in d["next_shift_tasks"]:
            p_ic = "🔴" if "ВЫСОКИЙ" in t.get("priority", "") else "🟡"
            blocks.append(todo(f"{p_ic} {t.get('task','')} → {t.get('responsible','-')}"))
    blocks += [divider(), para("Создано Extella AI | " + today)]
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
        return "https://notion.so/" + r.json().get("id", "").replace("-", "")
    return None


def notify_director(d, notion_url):
    if not DIRECTOR_CHAT:
        return
    pct   = d.get("completion_pct", 0) or 0
    icon  = get_icon(d.get("overall_assessment", ""))
    lines = [icon + " " + d.get("object_name", "Объект") + " — " + str(int(pct)) + "% плана"]
    if d.get("supervisor"): lines.append("Прораб: " + d["supervisor"])
    if d.get("summary_for_director"): lines.append(d["summary_for_director"])
    probs = d.get("problems", [])
    mats  = [m for m in d.get("material_requests", []) if "СРОЧНО" in m.get("urgency", "")]
    if probs:
        lines.append("Проблем: " + str(len(probs)))
        for pr in probs[:3]: lines.append("  • " + pr.get("description", "")[:60])
    if mats:
        lines.append("Срочные заявки: " + str(len(mats)))
        for m in mats[:2]: lines.append("  • " + m.get("material", "") + " — " + str(m.get("needed_qty", "")))
    if notion_url: lines.append("Рапорт: " + notion_url)
    send(DIRECTOR_CHAT, "\n".join(lines))


# ───────────────────────────────────────────────────────────────────
# КС-2: fire-and-forget
# ───────────────────────────────────────────────────────────────────

def launch_ks2_pipeline(chat_id, pdf_bytes, subcontractor, ks2_number):
    """
    Fire-and-forget: запускаем эксперт с telegram_chat_id.
    Эксперт сам отправляет результат в Telegram.
    Бот НИЧЕГО не ждёт и НИЧЕГО не отправляет.
    """
    if not EXTELLA_TOKEN:
        send(chat_id, "❌ EXTELLA_API_TOKEN не настроен.")
        return

    headers = {"X-Auth-Token": EXTELLA_TOKEN, "Content-Type": "application/json"}
    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")

    try:
        # Просто запускаем. Не ждём результата.
        requests.post(
            f"{EXTELLA_URL}/api/expert/run",
            headers=headers,
            json={
                "expert_name": "aios_ks2_pipeline_full",
                "params": {
                    "base64_pdf":         b64_pdf,
                    "openai_key":         OPENAI_KEY,
                    "subcontractor_name": subcontractor,
                    "ks2_number":         ks2_number,
                    "telegram_chat_id":   str(chat_id),  # ВОТ ГЛАВНОЕ: реальный chat_id
                }
            },
            timeout=30  # ждём только подтверждения запуска, не выполнения
        )
        # Дальше ничего не делаем.
        # Эксперт сам уведомит пользователя через telegram_chat_id.
    except requests.exceptions.Timeout:
        pass  # нормально, задача всё равно запущена
    except Exception as e:
        send(chat_id, f"❌ Ошибка запуска: {e}")


# ───────────────────────────────────────────────────────────────────
# WEBHOOK
# ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    update    = request.json or {}
    update_id = update.get("update_id")
    message   = update.get("message", {})
    chat_id   = message.get("chat", {}).get("id")
    if not chat_id:
        return jsonify({"ok": True})

    if update_id:
        with PROCESSED_LOCK:
            if update_id in PROCESSED_UPDATES:
                return jsonify({"ok": True})
            PROCESSED_UPDATES.add(update_id)
            if len(PROCESSED_UPDATES) > 500:
                for uid in sorted(PROCESSED_UPDATES)[:250]:
                    PROCESSED_UPDATES.discard(uid)

    text     = message.get("text", "").strip()
    voice    = message.get("voice")
    document = message.get("document")

    if text.startswith("/start"):
        send(chat_id, "Привет! Я помогаю прорабам и ПТО.\n\n"
                     "📢 Голосовой рапорт:\n  1. /object ЖК Северный блок 3\n  2. Запиши голосовое\n\n"
                     "📄 Проверка КС-2 (PDF):\n  1. /subcontractor ТОО СтройМонтаж\n  2. /ks2number 1\n  3. Прикрепи PDF")
        return jsonify({"ok": True})

    if text.startswith("/object"):
        obj_name = text[7:].strip()
        if obj_name:
            USER_OBJECTS[chat_id] = obj_name
            send(chat_id, "✅ Объект: " + obj_name)
        else:
            send(chat_id, "Текущий: " + USER_OBJECTS.get(chat_id, "не задан"))
        return jsonify({"ok": True})

    if text.startswith("/subcontractor"):
        name = text[14:].strip()
        if name:
            USER_SUBCONTRACTOR[chat_id] = name
            send(chat_id, f"✅ Субподрядчик: {name}\nОтправь PDF.")
        else:
            send(chat_id, f"Текущий: {USER_SUBCONTRACTOR.get(chat_id, 'не задан')}")
        return jsonify({"ok": True})

    if text.startswith("/ks2number"):
        num = text[10:].strip()
        if num:
            USER_KS2_NUMBER[chat_id] = num
            send(chat_id, f"✅ Номер КС-2: {num}")
        else:
            send(chat_id, f"Текущий: {USER_KS2_NUMBER.get(chat_id, '1')}")
        return jsonify({"ok": True})

    if text.startswith("/help"):
        send(chat_id, f"Команды:\n/object, /subcontractor, /ks2number\n\n"
                      f"Объект: {USER_OBJECTS.get(chat_id, 'не задан')}\n"
                      f"Субподрядчик: {USER_SUBCONTRACTOR.get(chat_id, 'не задан')}\n"
                      f"Номер КС-2: {USER_KS2_NUMBER.get(chat_id, '1')}\n\n"
                      f"PDF отправляйте по одному, результат придёт автоматически")
        return jsonify({"ok": True})

    # ── PDF → КС-2 (fire-and-forget)
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
            try:
                USER_KS2_NUMBER[chat_id] = str(int(ks2_number) + 1)
            except Exception:
                pass

            # Сообщаем что получили файл
            send(chat_id, "⏳ Анализирую КС-2... Результат придёт через ~30-60 секунд.")

            # Запускаем в фоновом потоке
            threading.Thread(
                target=launch_ks2_pipeline,
                args=(chat_id, pdf_bytes, subcontractor, ks2_number),
                daemon=True
            ).start()
            return jsonify({"ok": True})
        else:
            send(chat_id, "⚠️ Я принимаю только PDF.")
            return jsonify({"ok": True})

    # ── Голосовой рапорт
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
            lines  = [icon + " Рапорт принят!", "Объект: " + obj, assess + " — " + str(int(pct)) + "% плана"]
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
    return jsonify({"status": "ok", "service": "aios-foreman-bot", "version": "2.8"})


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
    app.run(host="0.0.0.0", port=PORT, debug=False)
