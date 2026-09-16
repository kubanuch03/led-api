"""
Регрессии на два дефекта протокола панели, каждый из которых стоил времени
на объекте (чёрный экран двое суток, а сервис рапортовал «показано»).
Правило: дефект, стоивший инцидента, закрывается тестом, а не только
исправлением — иначе следующий человек «упростит» проверку обратно.

REG-18 (LED-06): проверка показа сравнивает md5 отправленного кадра с
    АКТИВНОЙ программой (0x0013→0x0014), а не ищет его в списке ВСЕХ файлов
    карты. Файл ложится на карту всегда; поиск в списке отвечал «показано»
    на что угодно, включая чёрный экран.

REG-19 (LED-07): при несовпадении повторяется ПРИМЕНЕНИЕ, а файл пишется
    ОДИН раз. Прежний повтор перезаписывал файл — лечил не ту стадию
    (запись проходила, отказывало применение) и растил карту, для которой
    нет команды очистки. Плюс имена файлов ограничены двумя слотами.

Сеть не трогаем: подменяются низкоуровневая отправка кадров, открытие
сокета, чтение активной программы и пауза.
"""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import panel as pm  # noqa: E402
from app.panel import Panel, SLOTS  # noqa: E402


class _FakeSock:
    """Контекст-менеджер вместо реального сокета — никуда не ходит."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def settimeout(self, *a):
        pass


@pytest.fixture
def panel(monkeypatch):
    p = Panel("10.0.0.1", device_id="C16L-TEST", rot180=False)
    monkeypatch.setattr(pm.socket, "create_connection", lambda *a, **k: _FakeSock())
    monkeypatch.setattr(pm.time, "sleep", lambda *a: None)  # без реальных пауз
    # Одноцветный валидный PNG, чтобы png_size/rot180 не спотыкались.
    monkeypatch.setattr(pm, "png_size", lambda img: (160, 160))
    return p


def _tiny_png() -> bytes:
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )


def test_reg18_verify_uses_active_program_not_file_list(panel, monkeypatch):
    """
    Панель ПРИНЯЛА файл (он есть в списке файлов), но активной осталась
    другая программа. Прежняя проверка «md5 в списке файлов» вернула бы
    успех; правильная — провал, потому что на экране не наш кадр.
    """
    frames_sent = []
    monkeypatch.setattr(panel, "_session", lambda sock, frames: frames_sent.append(frames) or [])
    # Активная программа — ЧУЖАЯ, и не меняется сколько ни применяй.
    monkeypatch.setattr(panel, "active_program_md5", lambda: "ffffffffffffffffffffffffffffffff")

    res = panel.send_png(_tiny_png())

    assert res.ok is False
    assert res.verified is False
    assert "показывает другую" in res.detail


def test_reg18_verify_true_only_when_active_matches(panel, monkeypatch):
    """Обратная сторона: показано ⇔ активная программа совпала с нашим md5."""
    import hashlib

    img = _tiny_png()
    want = hashlib.md5(img).hexdigest()  # rot180=False, значит шлём как есть
    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    monkeypatch.setattr(panel, "active_program_md5", lambda: want)

    res = panel.send_png(img)

    assert res.ok is True
    assert res.verified is True


def test_reg19_retries_apply_not_write(panel, monkeypatch):
    """
    Применение не срабатывает → файл пишется ОДИН раз, а команда показа
    повторяется. Раньше повтор слал файл заново и растил карту.
    """
    writes = {"n": 0}
    applies = {"n": 0}

    # send_png шлёт файл ровно одной сессией (первый _session-вызов).
    monkeypatch.setattr(panel, "_session", lambda sock, frames: writes.__setitem__("n", writes["n"] + 1) or [])
    monkeypatch.setattr(panel, "_apply_program", lambda: applies.__setitem__("n", applies["n"] + 1))
    # Активная программа НИКОГДА не совпадает — худший случай, все попытки.
    monkeypatch.setattr(panel, "active_program_md5", lambda: "0" * 32)

    res = panel.send_png(_tiny_png())

    assert res.ok is False
    assert writes["n"] == 1, "файл должен писаться один раз, а не на каждую попытку"
    assert applies["n"] == pm.APPLY_ATTEMPTS - 1, "повторяется применение между попытками"


def test_reg19_two_slots_bound_the_card(panel):
    """
    Имена кадров ограничены двумя слотами и чередуются — карта не растёт.
    Проверяется на самом источнике имён (`_next_slot`), а не через разбор
    кадров: чередование — это его контракт.
    """
    names = [panel._next_slot() for _ in range(6)]

    assert set(names) == set(SLOTS), f"имена кадров вне слотов {SLOTS}: {set(names)}"
    assert all(a != b for a, b in zip(names, names[1:])), "слоты обязаны чередоваться"


def test_reg19_send_png_writes_into_a_slot(panel, monkeypatch):
    """send_png действительно пишет в слотовое имя, а не в md5-имя."""
    captured = {}

    real_next = panel._next_slot

    def _spy():
        captured["slot"] = real_next()
        return captured["slot"]

    monkeypatch.setattr(panel, "_next_slot", _spy)
    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    monkeypatch.setattr(panel, "active_program_md5", lambda: "0" * 32)

    panel.send_png(_tiny_png())

    assert captured.get("slot") in SLOTS, "кадр должен уходить в один из двух слотов"
