import argparse
import json
import logging
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from ai_service import generate_variants
from font_analyzer import analyze_font
from font_engine import (
    FONT_DIR, MAX_FONT_BYTES, PRESETS, RequestError, describe, preset_data, preset_option, read_font,
)
from font_schema import FontMetrics, FontParameters, GenerateRequest
from font_storage import save_font
from font_synthesis import (
    base_parameters_for_preset, generate_font, license_for_preset, select_base_preset,
)

STATIC = Path(__file__).resolve().parent / "static"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}
ASSETS.update({f"/fonts/{key}.ttf": (f"fonts/{key}.ttf", "font/ttf") for key in PRESETS})


def _as_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _variant_option(variant):
    preset_id = select_base_preset(variant.parameters)
    option = preset_option(PRESETS[preset_id])
    option.update(
        {
            "id": preset_id,
            "variant_name": variant.variant_name,
            "description": variant.description,
            "parameters": variant.parameters.model_dump(),
            "metrics": variant.metrics.model_dump(),
            "base_preset": preset_id,
        }
    )
    return option


def _multipart_file(body, content_type):
    """Минимальный разбор multipart/form-data для поля file."""

    match = re.search(r"boundary=([^;]+)", content_type or "")
    if not match:
        return body
    boundary = ("--" + match.group(1).strip().strip('"')).encode("utf-8")
    for part in body.split(boundary):
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = part[:header_end].decode("latin-1", "ignore")
        if 'name="file"' not in headers and "filename=" not in headers:
            continue
        payload = part[header_end + 4 :]
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        return payload
    raise RequestError("В multipart-запросе не найден файл шрифта.")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/api/health", "/api/v1/health"):
            self.send_json(200, {"status": "ok", "version": "1.0.0"})
        elif path.startswith("/api/v1/fonts/base/"):
            self.handle_base_font(path.rsplit("/", 1)[-1])
        elif path in ASSETS:
            file, mime = ASSETS[path]
            self.respond(200, (STATIC / file).read_bytes(), mime)
        else:
            self.send_json(404, {"error": "Страница не найдена."})

    def do_POST(self):
        url = urlsplit(self.path)
        params = {key: values[0] for key, values in parse_qs(url.query).items()}
        try:
            if url.path in ("/api/recommend", "/api/v1/ai/font-style"):
                self.handle_recommendation()
            elif url.path in ("/api/import", "/api/v1/fonts/analyze"):
                self.handle_analysis()
            elif url.path in ("/api/export", "/api/preview", "/api/v1/fonts/generate"):
                self.handle_compile(url.path, params)
            else:
                self.send_json(404, {"error": "Метод API не найден."})
        except RequestError as exc:
            self.send_json(400, {"error": str(exc)})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            logging.exception("Request failed")
            self.send_json(500, {"error": "Не удалось обработать запрос. Попробуйте другой файл или параметры."})

    def handle_base_font(self, preset_id):
        if preset_id not in PRESETS:
            self.send_json(404, {"error": "Вариант шрифта не найден."})
            return
        parameters, metrics, family, serif = base_parameters_for_preset(preset_id)
        self.send_json(
            200,
            {
                "preset_id": preset_id,
                "family": family,
                "serif": serif,
                "parameters": parameters.model_dump(),
                "metrics": metrics.model_dump(),
            },
        )

    def handle_recommendation(self):
        try:
            request = json.loads(self.body(32768))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestError("Отправьте описание в формате JSON.") from exc
        prompt = request.get("prompt") if isinstance(request, dict) else None
        if not isinstance(prompt, str) or not 3 <= len(prompt.strip()) <= 2000:
            raise RequestError("Добавьте описание длиной от 3 до 2000 символов.")
        try:
            count = int(request.get("variants_count", 3))
        except (TypeError, ValueError):
            count = 3
        mode, variants = generate_variants(prompt.strip(), max(1, min(5, count)))
        self.send_json(
            200,
            {
                "mode": mode,
                "variants": [_variant_option(variant) for variant in variants],
            },
        )

    def handle_analysis(self):
        content_type = self.headers.get("Content-Type", "")
        raw = self.body(MAX_FONT_BYTES + 1024 * 1024)
        data = _multipart_file(raw, content_type) if content_type.startswith("multipart/form-data") else raw
        with read_font(data) as font:
            profile = analyze_font(font)
            old = describe(font)
        profile.update(old)
        profile["base_font_id"] = save_font(data)
        profile["font"] = old
        profile["base_parameters"] = profile["parameters"]
        self.send_json(200, profile)

    def handle_compile(self, path, params):
        content_type = self.headers.get("Content-Type", "")
        is_json = content_type.startswith("application/json")
        if is_json or path == "/api/v1/fonts/generate":
            request = self.json_body()
            self.compile_json(path, request)
            return
        self.compile_legacy(path, params)

    def compile_json(self, path, request):
        try:
            body = GenerateRequest.model_validate(request)
        except Exception as exc:
            raise RequestError("Проверьте параметры шрифта и метрики.") from exc
        preset_id = select_base_preset(body.parameters)
        license_text = license_for_preset(preset_id) if not body.base_font_id else None
        output_format = body.output_format
        if output_format == "woff2":
            try:
                import brotli  # noqa: F401
            except ImportError:
                output_format = "ttf"
        result = generate_font(
            body.parameters,
            body.metrics,
            base_font_id=body.base_font_id,
            base_parameters=body.base_parameters,
            family_name=body.family_name,
            tracking=body.tracking,
            output_format=output_format,
            autohint=body.autohint,
            license_text=license_text,
        )
        headers = {}
        if path == "/api/export" or body.download:
            filename = "".join(char for char in body.family_name.strip() if char.isalnum() or char in " -_")
            filename = quote((filename.strip() or "AI Font") + "." + output_format)
            headers["Content-Disposition"] = f"attachment; filename=AIFont.{output_format}; filename*=UTF-8''{filename}"
        mime = "font/woff2" if output_format == "woff2" else "font/ttf"
        self.respond(200, result, mime, headers)

    def compile_legacy(self, path, params):
        preset = params.get("preset")
        if preset is not None:
            data = preset_data(preset)
            base_parameters, metrics, _, _ = base_parameters_for_preset(preset)
            license_text = (FONT_DIR / f"OFL-{preset}.txt").read_text(encoding="utf-8")
        else:
            data = self.body(MAX_FONT_BYTES)
            preset = next((key for key in PRESETS if data == preset_data(key)), None)
            license_text = (FONT_DIR / f"OFL-{preset}.txt").read_text(encoding="utf-8") if preset else None
            with read_font(data) as font:
                analysis = analyze_font(font)
            base_parameters = FontParameters.model_validate(analysis["parameters"])
            metrics = FontMetrics.model_validate(analysis["metrics"])

        def number(key, default):
            try:
                return float(params.get(key, default))
            except (TypeError, ValueError) as exc:
                raise RequestError("Параметр шрифта должен быть числом.") from exc

        updates = {
            "weight": int(number("weight", base_parameters.weight)),
            "width": int(number("width", base_parameters.width)),
            "slant": int(number("slant", base_parameters.slant)),
            "contrast": number("contrast", base_parameters.contrast),
            "roundness": number("roundness", base_parameters.roundness),
            "sharpness": number("sharpness", base_parameters.sharpness),
            "x_height": int(number("x_height", base_parameters.x_height)),
            "cap_height": int(number("cap_height", base_parameters.cap_height)),
            "serif": _as_bool(params.get("serif"), base_parameters.serif),
            "terminal_style": params.get("terminal_style", base_parameters.terminal_style),
            "purpose": params.get("purpose", base_parameters.purpose),
        }
        try:
            parameters = base_parameters.model_copy(update=updates)
            parameters = FontParameters.model_validate(parameters.model_dump())
        except Exception as exc:
            raise RequestError("Проверьте параметры шрифта.") from exc
        tracking = number("tracking", 0)
        name = params.get("name", "AI Font")
        result = generate_font(
            parameters,
            metrics,
            base_data=data,
            base_parameters=base_parameters,
            family_name=name,
            tracking=tracking,
            license_text=license_text,
        )
        headers = {}
        if path == "/api/export":
            filename = "".join(char for char in name.strip() if char.isalnum() or char in " -_")
            filename = quote((filename.strip() or "AI Font") + ".ttf")
            headers["Content-Disposition"] = f"attachment; filename=AIFont.ttf; filename*=UTF-8''{filename}"
        self.respond(200, result, "font/ttf", headers)

    def json_body(self):
        try:
            value = json.loads(self.body(MAX_FONT_BYTES))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RequestError("Отправьте данные в формате JSON.") from exc
        if not isinstance(value, dict):
            raise RequestError("JSON-запрос должен быть объектом.")
        return value

    def body(self, limit):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("Некорректный размер запроса.") from exc
        if not 0 < length <= limit:
            raise RequestError(
                "Файл должен быть меньше 10 МБ." if limit >= MAX_FONT_BYTES else "Описание слишком длинное или отсутствует."
            )
        return self.rfile.read(length)

    def send_json(self, status, data):
        self.respond(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def respond(self, status, data, mime, headers=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description="Локальная шрифтовая студия «Шрифты от ИИ»")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        print(f"Шрифты от ИИ · http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
