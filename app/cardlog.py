"""
Журнал карточек табло: что пришло, что нарисовали, что ушло на панель.

Раскладка - как у логов SmartParking (logs/<раздел>/<ГГГГ>/<ММ>/<ДД>/),
раздел - номер панели:

    <LED_LOG_DIR>/<панель>/<ГГГГ>/<ММ>/<ДД>/panel.log      - строка на карточку
    <LED_LOG_DIR>/<панель>/<ГГГГ>/<ММ>/<ДД>/cards.jsonl    - то же в JSON
    <LED_LOG_DIR>/<панель>/<ГГГГ>/<ММ>/<ДД>/images/<ЧЧММСС>_<md5>.png

Картинка сохраняется такой, какой её нарисовали (не повёрнутой): человеку
её смотреть, а поворот на 180° делает сама отправка.

Зачем: 23-29.09.2026 разбор «что было на табло» шёл по косвенным признакам -
кадрам камеры и счётчику файлов. Теперь по времени проезда видно, какие
данные пришли, какую картинку нарисовали и что ответила панель.

Выключено, пока не задан LED_LOG_DIR: сервис живёт и без журнала, а сбой
записи журнала никогда не роняет отправку карточки.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("led_api.cardlog")


def _panel_id(host: str) -> str:
    explicit = os.environ.get("LED_PANEL_ID", "").strip()
    if explicit:
        return explicit
    return (host or "panel").rsplit(".", 1)[-1] or "panel"


def _fmt(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("\r", " ")


def write(*, settings, endpoint: str, request: dict, png: bytes | None = None,
          render_info: dict | None = None, result=None, trace: dict | None = None,
          http_status: int = 200, error: str = "", started: float, finished: float) -> None:
    """Записать карточку. Никогда не бросает исключений наружу."""
    base = os.environ.get("LED_LOG_DIR", "").strip()
    if not base:
        return
    try:
        tz = dt.timezone(dt.timedelta(hours=settings.tz_offset_hours))
        now = dt.datetime.fromtimestamp(started, tz)
        day = Path(base) / _panel_id(settings.host) / now.strftime("%Y/%m/%d")
        day.mkdir(parents=True, exist_ok=True)

        image_name = ""
        md5 = getattr(result, "md5", "") if result is not None else ""
        if png:
            import hashlib
            md5 = md5 or hashlib.md5(png).hexdigest()
            (day / "images").mkdir(exist_ok=True)
            image_name = f"images/{now.strftime('%H%M%S')}_{md5[:12]}.png"
            (day / image_name).write_bytes(png)

        took = round(finished - started, 2)
        record = {
            "time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "panel": settings.host,
            "device_id": settings.device_id,
            "endpoint": endpoint,
            "http_status": http_status,
            "request": request,
            "render": render_info or {},
            "image": image_name,
            "image_bytes": len(png) if png else 0,
            "md5": md5,
            "trace": trace or {},
            "took_s": took,
        }
        if result is not None:
            record["result"] = {
                "ok": result.ok, "verified": result.verified,
                "program_files": result.program_files, "degraded": result.degraded,
                "files_on_panel": list(result.files_on_panel or []),
                "detail": result.detail,
            }
        if error:
            record["error"] = error

        with open(day / "cards.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        r = record.get("result", {})
        t = record["trace"]
        rd = record["render"]
        level = "INFO" if r.get("ok") else ("WARNING" if http_status < 500 else "ERROR")
        parts = [
            f"[{record['time']}] [{level}] {endpoint} http={http_status} took={took}s",
            f"plate={_fmt(request.get('plate'))!r} amount={_fmt(request.get('amount'))!r} "
            f"time={_fmt(request.get('time'))!r} dur={_fmt(request.get('dur'))!r} "
            f"qr={_fmt(request.get('qr'))!r}",
        ]
        if rd:
            parts.append(
                "render: " + " ".join(f"{k}={_fmt(v)}" for k, v in rd.items() if k != "warning")
                + (f" WARNING={_fmt(rd['warning'])!r}" if rd.get("warning") else "")
            )
        if image_name:
            parts.append(f"image={image_name} bytes={record['image_bytes']} md5={md5}")
        if t:
            parts.append("panel: " + " ".join(f"{k}={_fmt(v)}" for k, v in t.items()))
        if r:
            parts.append(
                f"result: ok={r['ok']} verified={r['verified']} program_files={r['program_files']} "
                f"degraded={r['degraded']} detail={_fmt(r['detail'])!r}"
            )
        if error:
            parts.append(f"error={_fmt(error)!r}")
        with open(day / "panel.log", "a", encoding="utf-8") as f:
            f.write(" | ".join(parts) + "\n")
    except Exception as exc:  # журнал не должен ронять отправку
        log.warning("журнал карточек не записан: %s", exc)
