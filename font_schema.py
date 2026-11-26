"""Единый контракт данных между ИИ, фронтендом и шрифтовым движком.

Схема повторяет Core Domain Schema из технического задания: оси и
семантические параметры описаны в ``FontParameters``, а метрики сетки —
в ``FontMetrics``. Метрики всегда фиксируются бэкендом на сетке 1000
units-per-em, поэтому LLM не может их изменить.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TerminalStyle = Literal["flat", "rounded", "angled"]
ContrastAxis = Literal["vertical", "horizontal", "diagonal", "none"]
Purpose = Literal["print", "screen", "display"]
Language = Literal["latin", "cyrillic"]


class FontMetrics(BaseModel):
    """Сетка Microsoft DirectWrite, зафиксированная бэкендом."""

    model_config = ConfigDict(extra="forbid")

    units_per_em: Literal[1000] = 1000
    ascender: int = Field(default=800, ge=600, le=1000)
    descender: int = Field(default=-200, ge=-500, le=0)


class FontParameters(BaseModel):
    """Параметры стилистического варианта гарнитуры."""

    model_config = ConfigDict(extra="forbid")

    weight: int = Field(default=400, ge=100, le=900, description="Насыщенность, 100..900.")
    width: int = Field(default=100, ge=50, le=200, description="Ширина в процентах, 100 — норма.")
    slant: int = Field(default=0, ge=-12, le=12, description="Наклон в градусах; положительный — вправо.")
    contrast: float = Field(default=0.35, ge=0, le=1, description="Контраст штрихов, 0 — моноширинный.")
    roundness: float = Field(default=0.15, ge=0, le=1, description="Скругление углов, 0 — острые.")
    sharpness: float = Field(default=0.5, ge=0, le=1, description="Острота срезов и вершин.")
    serif: bool = False
    terminal_style: TerminalStyle = "flat"
    stroke_contrast_axis: ContrastAxis = "vertical"
    x_height: int = Field(default=520, ge=300, le=800, description="Высота строчных, units.")
    cap_height: int = Field(default=720, ge=500, le=900, description="Высота прописных, units.")
    overshoot: bool = True
    ink_trap: bool = False
    mood: str = Field(default="neutral", min_length=1, max_length=40)
    style: str = Field(default="modern", min_length=1, max_length=60)
    purpose: Purpose = "print"
    language_support: list[Language] = Field(default_factory=lambda: ["latin", "cyrillic"])

    @model_validator(mode="after")
    def check_vertical_metrics(self) -> "FontParameters":
        if not self.x_height < self.cap_height:
            raise ValueError("x_height должен быть меньше cap_height")
        if not self.language_support:
            raise ValueError("language_support должен содержать хотя бы один алфавит")
        return self


class FontVariant(BaseModel):
    """Один стилистический вариант, который возвращает ИИ-сервис."""

    model_config = ConfigDict(extra="forbid")

    variant_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    parameters: FontParameters
    metrics: FontMetrics = Field(default_factory=FontMetrics)

    @model_validator(mode="after")
    def check_metrics_grid(self) -> "FontVariant":
        if self.parameters.cap_height > self.metrics.ascender:
            raise ValueError("cap_height не может превышать ascender")
        return self


class FontStyleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=3, max_length=2000)
    variants_count: int = Field(default=3, ge=1, le=5)


class FontStyleResponse(BaseModel):
    mode: Literal["llm", "heuristic"]
    variants: list[FontVariant]


class GenerateRequest(BaseModel):
    """Запрос на синтез и сборку TTF."""

    model_config = ConfigDict(extra="forbid")

    parameters: FontParameters
    metrics: FontMetrics = Field(default_factory=FontMetrics)
    base_font_id: str | None = Field(default=None, max_length=64)
    base_parameters: FontParameters | None = None
    family_name: str = Field(default="AI Font", min_length=1, max_length=60)
    tracking: float = Field(default=0.0, ge=-100, le=200, description="Трекинг в тысячных долях em.")
    output_format: Literal["ttf", "woff2"] = "ttf"
    autohint: bool = True
    download: bool = False


class AnalyzeResponse(BaseModel):
    """Результат геометрического анализа загруженного шрифта."""

    base_font_id: str
    family: str
    style: str
    format: Literal["TTF", "OTF"]
    glyph_count: int
    unicode_count: int
    cyrillic_coverage: dict[str, int]
    parameters: FontParameters
    metrics: FontMetrics
    notes: list[str] = Field(default_factory=list)


class BaseFontResponse(BaseModel):
    preset_id: str
    family: str
    serif: bool
    parameters: FontParameters
    metrics: FontMetrics


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str = "1.0.0"
