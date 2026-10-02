"""
Карточка без QR: госномер и время стоянки (галочка панели «Показывать QR» выкл).

Запуск:  python3 -m pytest tests/ -q
"""

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app.render import C_DUR, C_PLATE, render_card  # noqa: E402


def _img(png):
    return Image.open(io.BytesIO(png)).convert("RGB")


def _colors(im):
    return {c for _, c in im.getcolors(maxcolors=1 << 16)}


def test_no_qr_draws_plate_and_duration():
    png, info = render_card(qr_data="", plate="01125ASZ", dur="1ч12м", time_="16:42")
    im = _img(png)
    assert im.size == (160, 160)
    assert info["layout"] == "text_card"
    assert info["rows"] == ["01125ASZ", "1ч12м"]
    colors = _colors(im)
    assert C_PLATE in colors and C_DUR in colors
    # без QR - нет светлой подложки кода
    assert (255, 255, 255) not in colors


def test_no_qr_fonts_are_readable():
    _, info = render_card(qr_data=None, plate="01125ASZ", dur="12ч05м", amount="250 сом")
    assert info["rows"] == ["01125ASZ", "12ч05м", "250 сом"]
    assert min(info["font_px"]) >= 20


def test_no_qr_empty_is_idle_not_black():
    png, info = render_card(qr_data="", plate="", dur="")
    assert info.get("layout") == "text_card_empty"
    assert len(_colors(_img(png))) > 1


def test_with_qr_layout_unchanged():
    _, info = render_card(qr_data="https://pay.kg/p/A1B2C3", plate="01KG777AAA",
                          time_="22:45", amount="250 сом")
    assert info["text_strip"] == 32 and info.get("qr_modules")


def test_card_endpoint_accepts_no_qr(tmp_path, monkeypatch):
    """POST /card без qr - 200 и текстовая карточка, а не 422."""
    from types import SimpleNamespace

    monkeypatch.setenv("LED_LOG_DIR", str(tmp_path))
    from fastapi.testclient import TestClient

    from app import main
    from app.panel import SendResult

    sent = {}

    def fake_send(png, w, h, verify):
        sent["png"] = png
        return SendResult(True, True, "m" * 32, files_on_panel=["a"], program_files=1,
                          degraded=False, detail="показывается")

    monkeypatch.setattr(main.panel, "send_png", fake_send)
    monkeypatch.setattr(main, "udp_status", lambda host: SimpleNamespace(
        online=True, device_id="C16L", ip=host, mac="", width=160, height=160,
        screen_on=True, playing=True, program="p", locked=False, error=None))
    r = TestClient(main.app).post("/card", json={"plate": "01125ASZ", "dur": "1ч12м"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _img(sent["png"]).size == (160, 160)
