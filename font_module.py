"""Опциональный FastAPI-фасад для ИИ-сервиса.

Основной прототип работает на ``server.py`` без внешних веб-фреймворков,
но этот модуль можно запустить отдельно, если нужен чистый REST-сервис
``POST /api/v1/ai/font-style``. Логика и fallback берутся из ``ai_service``.
"""

from fastapi import FastAPI

from ai_service import generate_variants
from font_schema import FontStyleRequest, FontStyleResponse

app = FastAPI(title="AI Font Style Service", version="1.0.0")


@app.post("/api/v1/ai/font-style", response_model=FontStyleResponse)
def create_font_style(body: FontStyleRequest):
    mode, variants = generate_variants(body.prompt, body.variants_count)
    return FontStyleResponse(mode=mode, variants=variants)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
