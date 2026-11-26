"""ИИ-генератор стилистических вариантов гарнитуры.

По умолчанию используется локальная Ollama (``http://127.0.0.1:11434``)
и установленная модель ``qwen2.5:3b``. Если Ollama недоступна или модель
не ответила, включается детерминированный эвристический парсер — прототип
остаётся полностью функциональным. Для внешнего OpenAI-совместимого API
достаточно задать ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from font_schema import FontMetrics, FontParameters, FontVariant

try:  # httpx уже используется в окружении, но не обязателен.
    import httpx
except Exception:  # pragma: no cover
    httpx = None


SYSTEM_PROMPT = """
Ты — типограф-эксперт. Преобразуй описание пользователя в параметры
шрифтовой гарнитуры. Верни ТОЛЬКО JSON-объект вида:
{"variants":[{"variant_name":"...","description":"...","parameters":{...}}]}

Поля parameters и допустимые значения:
weight (100..900), width (50..200, 100 — норма), slant (-12..12),
contrast (0..1), roundness (0..1), sharpness (0..1), serif (true/false),
terminal_style ("flat"|"rounded"|"angled"),
stroke_contrast_axis ("vertical"|"horizontal"|"diagonal"|"none"),
x_height (300..800), cap_height (500..800, обязательно больше x_height),
overshoot (true/false), ink_trap (true/false),
mood (строка), style (строка),
purpose ("print"|"screen"|"display"),
language_support (["latin","cyrillic"]).

Правила:
- serif=true для антиквы, книг, журналов, засечек; false для гротеска,
  интерфейса, техно и киберпанка.
- style: "editorial" для книг/журналов/антиквы, "cyberpunk" для киберпанка,
  "techno" для техно/кода/терминала, "geometric" для геометрии/минимализма,
  "modern" иначе.
- mood: "aggressive" для агрессивного/дерзкого/плакатного, "elegant" для
  элегантного/классического, "friendly" для мягкого/дружелюбного.
- purpose: "print" для книги/печати, "screen" для экрана/интерфейса,
  "display" для плаката/акциденции/заголовка.
- "узкий/сжатый/condensed" → width 80..85; "широкий/wide" → width 115..120.
- "жирный/bold/heavy/black" → weight 700..900; "тонкий/light" → weight 300.
- "контрастный" → contrast 0.7..0.8; "моно/линейный" → contrast 0.1.
- "острый/резкий/агрессивный" → sharpness 0.8..0.9, roundness 0.05..0.15.
- "округлый/мягкий" → roundness 0.7..0.9, sharpness 0.1..0.3.
- x_height 480..560; cap_height 700..760; cap_height > x_height.
- variant_name и description — на русском языке.
- Сделай 3 РАЗНЫХ варианта: базовый, акцентный (смелее) и текстовый
  (сдержаннее). У акцентного измени минимум два поля из weight, width,
  contrast, roundness, sharpness, purpose. У текстового — тоже минимум два.
- Не добавляй units_per_em, ascender и descender — сетку фиксирует бэкенд.

Пример: "агрессивный узкий киберпанк для плаката" → weight 800, width 80,
contrast 0.2, roundness 0.1, sharpness 0.9, serif false, style "cyberpunk",
mood "aggressive", purpose "display".
Пример: "элегантная антиква для книги" → weight 400, width 100, contrast 0.65,
roundness 0.2, sharpness 0.4, serif true, style "editorial",
mood "elegant", purpose "print".
""".strip()

_ALLOWED_KEYS = set(FontParameters.model_fields)


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _has(text: str, *tokens: str) -> bool:
    return any(token in text for token in tokens)


def _normalize_parameters(parameters: FontParameters) -> FontParameters:
    """Приводит вертикальные метрики к фиксированной сетке 800/-200."""

    cap = int(_clamp(parameters.cap_height, 500, 780))
    x_height = int(_clamp(parameters.x_height, 300, cap - 60))
    if x_height >= cap:
        x_height = max(300, cap - 60)
    return parameters.model_copy(update={"cap_height": cap, "x_height": x_height})


def _weight_name(weight: int) -> str:
    if weight <= 300:
        return "Light"
    if weight <= 450:
        return "Regular"
    if weight <= 600:
        return "Medium"
    if weight <= 750:
        return "Bold"
    return "Heavy"


def _style_name(style: str) -> str:
    mapping = {
        "cyberpunk": "Cyber",
        "techno": "Techno",
        "geometric": "Geo",
        "editorial": "Editorial",
        "modern": "Modern",
        "book": "Book",
    }
    return mapping.get(style, style.title() if style else "Modern")


def _variant_name(parameters: FontParameters, suffix: str = "") -> str:
    parts = [_style_name(parameters.style)]
    if parameters.width < 90:
        parts.append("Condensed")
    elif parameters.width > 110:
        parts.append("Wide")
    parts.append(_weight_name(parameters.weight))
    if suffix:
        parts.append(suffix)
    return " ".join(part for part in parts if part)


def _description(parameters: FontParameters) -> str:
    serif = "антиква с засечками" if parameters.serif else "гротеск без засечек"
    width = "узкий" if parameters.width < 90 else ("широкий" if parameters.width > 110 else "нормальной ширины")
    mood = {
        "aggressive": "агрессивный",
        "elegant": "элегантный",
        "friendly": "дружелюбный",
        "neutral": "нейтральный",
    }.get(parameters.mood, parameters.mood)
    purpose = {
        "print": "для печати",
        "screen": "для экрана",
        "display": "для акцидентного набора",
    }.get(parameters.purpose, "для печати")
    return f"{mood.capitalize()} {width} {serif}, контраст {parameters.contrast:.2f}, {purpose}."


def _parse_prompt(prompt: str) -> FontParameters:
    text = prompt.casefold()

    serif = _has(text, "засеч", "антикв", "serif", "книг", "редакц", "журнал", "элегант")
    if _has(text, "без засечек", "без засеч", "гротеск", "sans", "геометр", "минимал"):
        serif = False

    if _has(text, "киберпанк", "cyber"):
        style = "cyberpunk"
    elif _has(text, "техно", "техни", "терминал", "код", "mono", "машин"):
        style = "techno"
    elif _has(text, "геометр", "минимал", "интерфейс", "нейтрал"):
        style = "geometric"
    elif _has(text, "книг", "редакц", "журнал", "антикв", "классик"):
        style = "editorial"
    else:
        style = "modern"

    if _has(text, "агресс", "дерзк", "брутал", "плакат"):
        mood = "aggressive"
    elif _has(text, "элегант", "классич", "изящ"):
        mood = "elegant"
    elif _has(text, "дружелюб", "мягк", "тёпл", "тепл"):
        mood = "friendly"
    else:
        mood = "neutral"

    if _has(text, "экран", "интерфейс", "сайт", "веб", "screen"):
        purpose = "screen"
    elif _has(text, "акцидент", "плакат", "дисплей", "заголов"):
        purpose = "display"
    else:
        purpose = "print"

    if _has(text, "тонк", "light", "лёгк", "легк"):
        weight = 300
    elif _has(text, "жирн", "bold", "heavy", "black", "брутал", "агресс"):
        weight = 800
    elif _has(text, "средн", "medium"):
        weight = 500
    else:
        weight = 400

    if _has(text, "узк", "condensed", "сжат", "компакт"):
        width = 80
    elif _has(text, "широк", "wide", "расшир"):
        width = 115
    else:
        width = 100

    slant = 10 if _has(text, "наклон", "italic", "курсив") else 0

    if _has(text, "моно", "mono", "линейн"):
        contrast = 0.1
    elif _has(text, "контраст"):
        contrast = 0.75
    else:
        contrast = 0.55 if serif else 0.2

    if _has(text, "округл", "round", "мягк"):
        roundness = 0.7
    elif _has(text, "геометр", "острый", "sharp", "резк"):
        roundness = 0.1
    else:
        roundness = 0.2

    sharpness = 0.9 if _has(text, "острый", "sharp", "агресс", "резк", "срез") else 0.5
    terminal_style = "rounded" if _has(text, "округл", "round") else ("angled" if _has(text, "срез", "острый", "sharp") else "flat")
    x_height = 560 if _has(text, "высок", "large x") else (480 if _has(text, "низк", "small x") else 520)
    cap_height = 760 if _has(text, "высок", "large cap") else 720

    return _normalize_parameters(
        FontParameters(
            weight=weight,
            width=width,
            slant=slant,
            contrast=contrast,
            roundness=roundness,
            sharpness=sharpness,
            serif=serif,
            terminal_style=terminal_style,
            stroke_contrast_axis="vertical",
            x_height=x_height,
            cap_height=cap_height,
            overshoot=True,
            ink_trap=False,
            mood=mood,
            style=style,
            purpose=purpose,
            language_support=["latin", "cyrillic"],
        )
    )


def _vary(parameters: FontParameters, **updates) -> FontParameters:
    data = parameters.model_dump()
    data.update(updates)
    return _normalize_parameters(FontParameters.model_validate(data))


def _heuristic_variants(prompt: str, count: int) -> list[FontVariant]:
    base = _parse_prompt(prompt)
    candidates = [
        (base, ""),
        (
            _vary(
                base,
                weight=int(_clamp(base.weight + 200, 100, 900)),
                width=int(_clamp(base.width - 10, 50, 200)),
                slant=int(_clamp(base.slant + 2, -12, 12)),
                contrast=round(_clamp(base.contrast + 0.15, 0, 1), 3),
                roundness=round(_clamp(base.roundness - 0.15, 0, 1), 3),
                sharpness=round(_clamp(base.sharpness + 0.2, 0, 1), 3),
                mood="aggressive",
                purpose="display" if base.purpose != "screen" else "screen",
            ),
            "Accent",
        ),
        (
            _vary(
                base,
                weight=int(_clamp(base.weight - 100, 100, 900)),
                width=int(_clamp(base.width + 5, 50, 200)),
                contrast=round(_clamp(base.contrast - 0.1, 0, 1), 3),
                roundness=round(_clamp(base.roundness + 0.2, 0, 1), 3),
                sharpness=round(_clamp(base.sharpness - 0.15, 0, 1), 3),
                mood="neutral",
                purpose="print",
            ),
            "Text",
        ),
        (
            _vary(
                base,
                width=int(_clamp(base.width + 20, 50, 200)),
                roundness=0.9,
                sharpness=0.1,
                terminal_style="rounded",
                mood="friendly",
            ),
            "Soft",
        ),
        (
            _vary(
                base,
                width=int(_clamp(base.width - 20, 50, 200)),
                weight=int(_clamp(base.weight + 100, 100, 900)),
                serif=not base.serif,
                sharpness=0.9,
            ),
            "Contrast",
        ),
    ]

    variants: list[FontVariant] = []
    seen: set[str] = set()
    for parameters, suffix in candidates:
        key = parameters.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        variants.append(
            FontVariant(
                variant_name=_variant_name(parameters, suffix),
                description=_description(parameters),
                parameters=parameters,
                metrics=FontMetrics(),
            )
        )
        if len(variants) >= count:
            break
    return variants


def _enforce_prompt_semantics(parameters: FontParameters, prompt: str) -> FontParameters:
    """Не даёт модели противоречить очевидным ключевым словам запроса."""

    text = prompt.casefold()
    updates: dict[str, Any] = {}
    if _has(text, "киберпанк", "cyber"):
        updates.update(
            style="cyberpunk",
            serif=False,
            mood="aggressive",
            purpose="display",
            sharpness=max(parameters.sharpness, 0.8),
            roundness=min(parameters.roundness, 0.2),
        )
    elif _has(text, "техно", "техни", "терминал", "код", "mono", "моно"):
        updates.update(style="techno", serif=False)
    elif _has(text, "без засечек", "без засеч", "гротеск", "sans", "геометр", "минимал", "интерфейс"):
        updates.update(serif=False)
        updates["style"] = "geometric" if _has(text, "геометр", "минимал", "интерфейс") else "modern"
    elif _has(text, "антикв", "книг", "журнал", "засеч", "редакц"):
        updates.update(style="editorial", serif=True)
    if _has(text, "узк", "сжат", "condensed"):
        updates["width"] = min(parameters.width, 85)
    if _has(text, "широк", "wide"):
        updates["width"] = max(parameters.width, 115)
    if _has(text, "агресс", "дерзк", "брутал", "плакат"):
        updates.update(mood="aggressive", purpose="display", sharpness=max(parameters.sharpness, 0.8))
    if _has(text, "элегант", "классич"):
        updates["mood"] = "elegant"
    if _has(text, "жирн", "bold", "heavy", "black"):
        updates["weight"] = max(parameters.weight, 700)
    if _has(text, "тонк", "light"):
        updates["weight"] = min(parameters.weight, 300)
    return _normalize_parameters(parameters.model_copy(update=updates))


DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
_OLLAMA_CACHE: dict[str, tuple[float, bool]] = {}


def _normalize_ollama_host(value: str) -> str:
    host = (value or "").strip().rstrip("/")
    if not host:
        host = DEFAULT_OLLAMA_HOST
    if not host.startswith(("http://", "https://")):
        host = "http://" + host
    if host.endswith("/v1"):
        host = host[:-3].rstrip("/")
    return host


def _llm_disabled() -> bool:
    return os.environ.get("FONT_AI_DISABLE_LLM", "").strip().casefold() in {"1", "true", "yes", "on"}


def _llm_config() -> dict[str, str] | None:
    """Возвращает настройки LLM: локальная Ollama или внешний OpenAI-совместимый API."""

    if _llm_disabled():
        return None
    openai_base = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai_base or openai_key:
        return {
            "provider": "openai",
            "base_url": openai_base or "https://api.openai.com/v1",
            "api_key": openai_key,
            "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        }
    return {
        "provider": "ollama",
        "base_url": _normalize_ollama_host(os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST)),
        "api_key": "",
        "model": os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    }


def _ollama_available(host: str) -> bool:
    now = time.time()
    cached = _OLLAMA_CACHE.get(host)
    if cached and now - cached[0] < 15.0:
        return cached[1]
    available = False
    try:
        if httpx is not None:
            available = httpx.get(f"{host}/api/tags", timeout=1.5).status_code == 200
        else:  # pragma: no cover - запасной путь без httpx
            with urllib.request.urlopen(f"{host}/api/tags", timeout=1.5) as response:
                available = 200 <= response.status < 300
    except Exception:
        available = False
    _OLLAMA_CACHE[host] = (now, available)
    return available


def _extract_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return json.loads(text)


def _ollama_chat(prompt: str, count: int, host: str, model: str) -> str | None:
    """Запрос к нативному API Ollama с принудительным JSON-ответом."""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Запрос пользователя: {prompt}\nСделай ровно {count} варианта. Верни только JSON.",
            },
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.6, "num_predict": 1600},
    }
    try:
        if httpx is not None:
            response = httpx.post(f"{host}/api/chat", json=payload, timeout=90.0)
            response.raise_for_status()
            data = response.json()
        else:  # pragma: no cover - запасной путь без httpx
            request = urllib.request.Request(
                f"{host}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=90) as result:
                data = json.loads(result.read().decode("utf-8"))
        return data.get("message", {}).get("content")
    except Exception:
        return None


def _openai_chat(prompt: str, count: int, base_url: str, api_key: str, model: str) -> str | None:
    payload = {
        "model": model,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Запрос пользователя: {prompt}\nСделай {count} варианта."},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        if httpx is not None:
            response = httpx.post(f"{base_url}/chat/completions", json=payload, headers=headers, timeout=45.0)
            response.raise_for_status()
            data = response.json()
        else:  # pragma: no cover - запасной путь без httpx
            request = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=45) as result:
                data = json.loads(result.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def _llm_variants(prompt: str, count: int) -> list[FontVariant] | None:
    config = _llm_config()
    if not config:
        return None
    if config["provider"] == "ollama":
        if not _ollama_available(config["base_url"]):
            return None
        content = _ollama_chat(prompt, count, config["base_url"], config["model"])
    else:
        content = _openai_chat(prompt, count, config["base_url"], config["api_key"], config["model"])
    if not content:
        return None
    try:
        parsed = _extract_json(content)
    except Exception:
        return None

    raw_variants = parsed.get("variants") if isinstance(parsed, dict) else None
    if not isinstance(raw_variants, list):
        return None

    variants: list[FontVariant] = []
    seen: set[str] = set()
    for item in raw_variants:
        if not isinstance(item, dict):
            continue
        raw_parameters = item.get("parameters")
        if not isinstance(raw_parameters, dict):
            continue
        try:
            filtered = {key: value for key, value in raw_parameters.items() if key in _ALLOWED_KEYS}
            parameters = _enforce_prompt_semantics(
                _normalize_parameters(FontParameters.model_validate(filtered)),
                prompt,
            )
            suffixes = ("", "Accent", "Text", "Soft", "Contrast")
            raw_name = _variant_name(parameters, suffixes[min(len(variants), len(suffixes) - 1)])
            raw_description = str(item.get("description") or "").strip()
            lowered_description = raw_description.casefold()
            if (
                not raw_description
                or len(raw_description) > 500
                or prompt.casefold() in lowered_description
                or ("антикв" in lowered_description and not parameters.serif)
                or ("гротеск" in lowered_description and parameters.serif)
            ):
                raw_description = _description(parameters)
            variant = FontVariant(
                variant_name=raw_name[:80],
                description=raw_description[:500],
                parameters=parameters,
                metrics=FontMetrics(),
            )
        except Exception:
            continue
        key = parameters.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        variants.append(variant)
        if len(variants) >= count:
            break
    return variants or None


_DISTANCE_FIELDS = (
    "weight", "width", "slant", "contrast", "roundness", "sharpness",
    "serif", "style", "purpose", "terminal_style",
)


def _parameters_distance(first: FontParameters, second: FontParameters) -> int:
    return sum(getattr(first, field) != getattr(second, field) for field in _DISTANCE_FIELDS)


def _merge_variants(llm: list[FontVariant], prompt: str, count: int) -> list[FontVariant]:
    """Оставляет удачные варианты qwen и добивает набор эвристикой до трёх разных."""

    merged: list[FontVariant] = []
    for variant in llm:
        if len(merged) >= count:
            break
        if all(_parameters_distance(variant.parameters, existing.parameters) >= 2 for existing in merged):
            merged.append(variant)
    for variant in _heuristic_variants(prompt, count + 2):
        if len(merged) >= count:
            break
        if all(_parameters_distance(variant.parameters, existing.parameters) >= 2 for existing in merged):
            merged.append(variant)
    for variant in llm:
        if len(merged) >= count:
            break
        if variant not in merged:
            merged.append(variant)
    return merged[:count]


def generate_variants(prompt: str, count: int = 3) -> tuple[str, list[FontVariant]]:
    """Возвращает (режим, варианты). LLM опционален, эвристика всегда доступна."""

    count = max(1, min(5, int(count)))
    llm = _llm_variants(prompt, count)
    if llm:
        return "llm", _merge_variants(llm, prompt, count)
    return "heuristic", _heuristic_variants(prompt, count)
