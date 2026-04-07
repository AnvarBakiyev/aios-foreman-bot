# 🏗️ AIOS Foreman Bot

Telegram бот для прорабов. Отправь голосовое — получи рапорт в Notion + уведомление директору.

## Деплой на Railway

1. Fork/clone этот репо
2. Создай новый проект на Railway из этого репо
3. Добавь переменные окружения:

| Variable | Description |
|----------|-------------|
| `BOT_TOKEN` | Telegram Bot Token |
| `OPENAI_API_KEY` | OpenAI API key |
| `NOTION_TOKEN` | Notion integration token |
| `NOTION_PARENT_PAGE_ID` | Notion page ID where reports go |
| `DIRECTOR_CHAT_ID` | Telegram chat ID of director |

4. После деплоя установи webhook:
```
GET https://your-railway-url.railway.app/set_webhook?url=https://your-railway-url.railway.app/webhook
```

## Как пользоваться

Прораб просто отправляет голосовое в бот. Всё остальное автоматически:
- Расшифровка через Whisper
- Структурирование через GPT-4o  
- Страница в Notion с задачами
- Уведомление директору в Telegram
