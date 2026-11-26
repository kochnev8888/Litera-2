"""Временное хранение загруженных шрифтов для сценария модификации.

Загруженный файл получает ``base_font_id``. Последующие запросы
``/api/v1/fonts/generate`` могут ссылаться на этот идентификатор, чтобы
пользователь продолжал работу с исходными контурами, а не с потерей
данных между запросами.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

STORAGE_DIR = Path(__file__).resolve().parent / "storage"
_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")


def save_font(data: bytes) -> str:
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    font_id = uuid.uuid4().hex
    (STORAGE_DIR / f"{font_id}.bin").write_bytes(data)
    return font_id


def load_font(font_id: str) -> bytes:
    if not _ID_PATTERN.fullmatch(font_id or ""):
        raise ValueError("Некорректный идентификатор исходного шрифта.")
    path = STORAGE_DIR / f"{font_id}.bin"
    if not path.is_file():
        raise ValueError("Исходный шрифт не найден. Загрузите файл заново.")
    return path.read_bytes()
