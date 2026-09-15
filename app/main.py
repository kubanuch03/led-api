"""HTTP-сервис вывода карточки парковки на LED-экран Huidu C16L.

Заменяет россыпь .cmd/PowerShell/Node из tools/hd9527: один вход, один процесс,
чистый Python. Бэкенд парковки по событию оплаты шлёт POST /card — и всё.

Чем отличается от прежних скриптов:
  * честный ответ об успехе — карта подтверждает кадры ack-ами, даже когда ничего
    не записывает, поэтому после отправки сверяем md5 в списке активной программы;
  * поворот кадра на 180° под установку панели — единый для всех путей;
  * /preview рисует карточку, ничего не отправляя, — верстку видно без экрана.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .config import settings
from .panel import Panel, udp_status
from .render import render_card

app = FastAPI(
    title="LED Parking API",
    description="Карточка парковки (QR + госномер + время + стоянка + сумма) на экран Huidu C16L",
    version="1.0.0",
)

panel = Panel(settings.host, timeout=settings.timeout,
              device_id=settings.device_id, rot180=settings.rot180)

# Панель принимает одно соединение на 9527 — команды строго по очереди.
_busy = asyncio.Lock()


async def auth(request: Request, token: str | None = Query(None)) -> None:
    """Проверка токена, если он задан через API_TOKEN. Иначе сервис открыт."""
    if not settings.token:
        return
    header = request.headers.get("authorization", "")
    given = header[7:].strip() if header.lower().startswith("bearer ") else (token or "")
    if given != settings.token:
        raise HTTPException(401, "неверный или отсутствующий токен")


def _now_hhmm() -> str:
    tz = dt.timezone(dt.timedelta(hours=settings.tz_offset_hours))
    return dt.datetime.now(tz).strftime("%H:%M")


class CardRequest(BaseModel):
    qr: str = Field(..., description="Ссылка для QR. Короткая: на 160 px влезает ~28 символов")
    plate: str = Field("", description="Госномер")
    amount: str | int | float = Field("", description="Сумма, например '150 KGS'")
    time: str = Field("", description="Время HH:MM; пусто — текущее")
    dur: str = Field("", description="Сколько простояла машина, например '2ч15м'")
    qr_pct: int = Field(100, ge=20, le=100,
                        description="Доля зоны НАД полосой текста под QR, %. "
                                    "100 = во всю зону (модуль крупнее, читается лучше)")
    pos: Literal["top", "bottom", "none"] = Field("bottom", description="Где полоса текста")
    white: int | None = Field(None, ge=120, le=255, description="Яркость светлых модулей QR")
    ecc: Literal["L", "M", "Q", "H"] | None = None
    verify: bool = Field(True, description="Сверить после отправки, что карта записала файл")


async def _send(png: bytes, verify: bool = True):
    """Отправить готовый PNG, соблюдая очередь к панели."""
    try:
        await asyncio.wait_for(_busy.acquire(), timeout=settings.busy_wait)
    except asyncio.TimeoutError:
        raise HTTPException(429, "панель занята другой отправкой, попробуйте позже")
    try:
        return await run_in_threadpool(panel.send_png, png, settings.width,
                                       settings.height, verify)
    finally:
        _busy.release()


def _result(res, info: dict | None = None) -> dict:
    out = {
        "ok": res.ok,
        "verified": res.verified,
        "md5": res.md5,
        "detail": res.detail,
        "files_on_panel": res.files_on_panel,
    }
    if info:
        out["render"] = info
    return out


@app.get("/health", summary="Жив ли сервис и сама панель")
async def health():
    st = await run_in_threadpool(udp_status, settings.host)
    return {
        "service": "ok",
        "panel": {
            "host": settings.host,
            "online": st.online,
            "device_id": st.device_id,
            "ip": st.ip,
            "mac": st.mac,
            "screen": f"{st.width}x{st.height}" if st.width else None,
            "screen_on": st.screen_on,
            "playing": st.playing,
            "program": st.program,
            "locked": st.locked,
            "error": st.error,
        },
    }


@app.get("/screen", summary="Что реально лежит на карте")
async def screen():
    """Карта не отдаёт скриншот, но сообщает md5 файлов активной программы."""
    try:
        files = await run_in_threadpool(panel.files_on_panel)
    except OSError as e:
        raise HTTPException(503, f"панель недоступна: {e}")
    return {
        "files_on_panel": files,
        "last_sent_md5": panel.last_md5,
        "showing_ours": panel.last_md5 in files if panel.last_md5 else None,
    }


@app.post("/card", summary="Показать карточку парковки", dependencies=[Depends(auth)])
async def card(req: CardRequest):
    png, info = await run_in_threadpool(
        render_card,
        qr_data=req.qr, plate=req.plate, time_=req.time or _now_hhmm(),
        amount=str(req.amount), dur=req.dur,
        width=settings.width, height=settings.height,
        qr_pct=req.qr_pct, pos=req.pos,
        white=req.white if req.white is not None else settings.white, ecc=req.ecc,
    )
    res = await _send(png, req.verify)
    return _result(res, info)


@app.post("/card/form", summary="То же, но формой — можно прислать готовую картинку QR",
          dependencies=[Depends(auth)])
async def card_form(
    qr: str = Form("", description="Ссылка для QR (предпочтительно)"),
    qr_image: UploadFile | None = File(None, description="Готовая картинка QR от бэкенда"),
    plate: str = Form(""), amount: str = Form(""), time: str = Form(""), dur: str = Form(""),
    qr_pct: int = Form(100), pos: str = Form("bottom"),
    white: int | None = Form(None), ecc: str | None = Form(None), verify: bool = Form(True),
):
    if not qr and qr_image is None:
        raise HTTPException(422, "нужен либо qr (ссылка), либо qr_image (файл)")
    blob = await qr_image.read() if qr_image is not None else None
    png, info = await run_in_threadpool(
        render_card,
        qr_data=qr or None, qr_image=blob,
        plate=plate, time_=time or _now_hhmm(), amount=amount, dur=dur,
        width=settings.width, height=settings.height, qr_pct=qr_pct, pos=pos,
        white=white if white is not None else settings.white, ecc=ecc,
    )
    res = await _send(png, verify)
    return _result(res, info)


@app.post("/preview", summary="Нарисовать карточку и вернуть PNG, НЕ отправляя на экран")
async def preview(req: CardRequest, scale: int = Query(1, ge=1, le=8,
                                                       description="Увеличить для глаз")):
    png, info = await run_in_threadpool(
        render_card,
        qr_data=req.qr, plate=req.plate, time_=req.time or _now_hhmm(),
        amount=str(req.amount), dur=req.dur,
        width=settings.width, height=settings.height,
        qr_pct=req.qr_pct, pos=req.pos,
        white=req.white if req.white is not None else settings.white, ecc=req.ecc,
    )
    if scale > 1:                       # только для просмотра: на экран уходит 1:1
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png))
        im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        buf = io.BytesIO(); im.save(buf, format="PNG"); png = buf.getvalue()
    headers = {"X-Render-Info": str(info)}
    return Response(png, media_type="image/png", headers=headers)


@app.post("/image", summary="Показать готовую картинку как есть", dependencies=[Depends(auth)])
async def image(file: UploadFile = File(...), verify: bool = Form(True)):
    data = await file.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(422, "нужен PNG (глубина 8 бит, без interlace)")
    res = await _send(data, verify)
    return _result(res)


@app.post("/blank", summary="Погасить экран", dependencies=[Depends(auth)])
async def blank():
    try:
        await asyncio.wait_for(_busy.acquire(), timeout=settings.busy_wait)
    except asyncio.TimeoutError:
        raise HTTPException(429, "панель занята другой отправкой, попробуйте позже")
    try:
        res = await run_in_threadpool(panel.blank, settings.width, settings.height)
    finally:
        _busy.release()
    return _result(res)
