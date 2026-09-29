"""
Журнал карточек табло (app/cardlog.py): на каждую карточку - строка в
panel.log, JSON в cards.jsonl и сама картинка в images/, по папкам
<панель>/<ГГГГ>/<ММ>/<ДД>/. Все флаги ответа панели - в журнале.
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import cardlog


@pytest.fixture
def settings():
    return SimpleNamespace(host="10.30.205.75", device_id="C16L-B25-1BA54", tz_offset_hours=6)


PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 50


def _day(base, started):
    import datetime as dt
    d = dt.datetime.fromtimestamp(started, dt.timezone(dt.timedelta(hours=6)))
    return Path(base) / "75" / d.strftime("%Y/%m/%d")


def test_card_is_logged_with_image_and_all_flags(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("LED_LOG_DIR", str(tmp_path))
    started = time.time()
    res = SimpleNamespace(ok=False, verified=False, md5="abc123def4567890", program_files=0,
                          degraded=False, files_on_panel=[], detail="кадр записан, но на экране ничего")
    trace = {"files_before": 279, "cleared_indexes": 279, "slot": "abc.png", "shown_first": "",
             "show_retried": True, "shown_after": "", "files_after": 2,
             "panel_status_after": {"online": True, "screen_on": True, "playing": True, "locked": False}}
    cardlog.write(settings=settings, endpoint="/card",
                  request={"qr": "https://pay.kg/x", "plate": "01KG123ABC", "amount": "50 сом",
                           "time": "10:42", "dur": "0ч23м", "verify": True},
                  png=PNG, render_info={"qr_version": 3, "module_px": 5}, result=res, trace=trace,
                  started=started, finished=started + 1.5)

    day = _day(tmp_path, started)
    line = (day / "panel.log").read_text(encoding="utf-8")
    for needle in ("01KG123ABC", "50 сом", "https://pay.kg/x", "module_px=5", "files_before=279",
                   "cleared_indexes=279", "show_retried=True", "ok=False", "verified=False",
                   "degraded=False", "program_files=0", "screen_on", "took=1.5s"):
        assert needle in line, needle
    rec = json.loads((day / "cards.jsonl").read_text(encoding="utf-8"))
    assert rec["result"]["degraded"] is False and rec["result"]["verified"] is False
    assert rec["trace"]["panel_status_after"]["playing"] is True
    assert (day / rec["image"]).read_bytes() == PNG, "картинка сохранена как есть"


def test_busy_429_is_logged_too(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("LED_LOG_DIR", str(tmp_path))
    started = time.time()
    cardlog.write(settings=settings, endpoint="/card", request={"plate": "01KG1"}, png=PNG,
                  http_status=429, error="панель занята", started=started, finished=started)
    line = (_day(tmp_path, started) / "panel.log").read_text(encoding="utf-8")
    assert "http=429" in line and "панель занята" in line and "[WARNING]" in line


def test_disabled_without_log_dir(tmp_path, monkeypatch, settings):
    monkeypatch.delenv("LED_LOG_DIR", raising=False)
    cardlog.write(settings=settings, endpoint="/card", request={}, png=PNG,
                  started=time.time(), finished=time.time())
    assert not any(tmp_path.iterdir())


def test_log_failure_never_breaks_sending(tmp_path, monkeypatch, settings):
    blocker = tmp_path / "file"
    blocker.write_text("не каталог")
    monkeypatch.setenv("LED_LOG_DIR", str(blocker))
    cardlog.write(settings=settings, endpoint="/card", request={}, png=PNG,
                  started=time.time(), finished=time.time())  # не бросает


def test_panel_id_from_env_or_host(monkeypatch, settings):
    assert cardlog._panel_id("10.30.205.76") == "76"
    monkeypatch.setenv("LED_PANEL_ID", "exit1")
    assert cardlog._panel_id("10.30.205.76") == "exit1"


def test_card_endpoint_writes_journal_end_to_end(tmp_path, monkeypatch):
    """POST /card: журнал получает запрос, картинку, трассу панели и её статус."""
    monkeypatch.setenv("LED_LOG_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from app import main
    from app.panel import SendResult

    def fake_send(png, w, h, verify):
        main.panel.last_trace = {"files_before": 2, "cleared_indexes": 8, "slot": "x.png",
                                 "shown_first": "m", "shown_after": "m", "files_after": 2}
        return SendResult(True, True, "m" * 32, files_on_panel=["a", "b"], program_files=2,
                          degraded=False, detail="показывается")

    monkeypatch.setattr(main.panel, "send_png", fake_send)
    monkeypatch.setattr(main, "udp_status", lambda host: SimpleNamespace(
        online=True, device_id="C16L", ip=host, mac="", width=160, height=160,
        screen_on=True, playing=True, program="p", locked=False, error=None))
    r = TestClient(main.app).post("/card", json={"qr": "https://pay.kg/p/1", "plate": "01KG777AAA",
                                                 "amount": "75 сом"})
    assert r.status_code == 200 and r.json()["ok"] is True
    panel_id = main.settings.host.rsplit(".", 1)[-1]
    logs = list(Path(tmp_path, panel_id).rglob("panel.log"))
    assert logs, "журнал не записан"
    line = logs[0].read_text(encoding="utf-8")
    assert "01KG777AAA" in line and "cleared_indexes=8" in line and "verified=True" in line
    rec = json.loads(next(Path(tmp_path, panel_id).rglob("cards.jsonl")).read_text(encoding="utf-8"))
    assert rec["trace"]["panel_status_after"]["screen_on"] is True
    assert (logs[0].parent / rec["image"]).exists()
