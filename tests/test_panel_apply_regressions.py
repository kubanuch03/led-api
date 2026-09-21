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
    (запись проходила, отказывало применение).

REG-20 (LED-08): имена файлов кадра и программы ОБЯЗАНЫ быть уникальными.
    Попытка ограничить рост карты двумя фиксированными слотами выглядела
    очевидной (команды удаления в протоколе нет) и была закреплена тестом —
    но на живой панели 21.09.2026 она положила вывод на обоих гейтах. Замер,
    A/B подряд на 10.30.205.76: запись в `slot_b.png` → активная программа
    собирается из ОДНОГО файла вместо двух и на экран не встаёт; тот же кадр
    под именем `<md5>.png` → программа собирается и применяется, подтверждено
    кадром с камеры. Причина: карта не заменяет содержимое файла при записи по
    существующему имени, md5 в программе перестаёт сходиться с тем, что лежит
    на карте, и карта программу отвергает. Рост карты решается не именами.

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


def _sent_file_names(frames) -> list:
    """Имена файлов из кадров 0x0017 — так панель узнаёт, куда писать."""
    import struct

    names = []
    for fr in frames:
        cmd = struct.unpack("<H", fr[2:4])[0]
        if cmd == 0x0017:
            names.append(fr[4:].rstrip(b"\x00").decode("latin-1"))
    return names


def test_reg20_frame_name_is_content_unique(panel, monkeypatch):
    """
    Кадр уходит под именем `<md5 содержимого>.png`, а НЕ под постоянным
    слотовым именем. Переиспользование имени карта не отрабатывает: она не
    заменяет содержимое файла, и программа отвергается (см. REG-20).
    """
    import hashlib

    sent = []
    monkeypatch.setattr(panel, "_session", lambda sock, frames: sent.extend(frames) or [])
    monkeypatch.setattr(panel, "active_program_md5", lambda: "0" * 32)

    img = _tiny_png()
    panel.send_png(img)  # rot180=False — шлём байты как есть

    names = _sent_file_names(sent)
    assert names, "кадры с именами файлов не найдены"
    assert names[0] == hashlib.md5(img).hexdigest() + ".png", (
        f"имя кадра обязано быть уникальным по содержимому, получено {names[0]!r}"
    )
    assert not any(n in SLOTS for n in names), (
        f"постоянные слотовые имена ломают применение на живой панели: {names}"
    )


def test_reg20_two_different_cards_get_two_different_names(panel, monkeypatch):
    """
    Две разные карточки — два разных имени файлов, и кадра, и программы.
    Иначе вторая карточка легла бы поверх имени первой и не применилась.
    """
    batches = []
    monkeypatch.setattr(panel, "_session", lambda sock, frames: batches.append(list(frames)) or [])
    monkeypatch.setattr(panel, "active_program_md5", lambda: "0" * 32)

    first = _tiny_png()
    # Тот же PNG с дописанным хвостом — валидный файл, но другое содержимое.
    second = first + b"\x00"

    panel.send_png(first)
    panel.send_png(second)

    # Через _session проходят и повторы ПРИМЕНЕНИЯ — в них файлов нет.
    # Берём только пакеты, в которых реально передавались имена файлов.
    with_files = [n for n in (_sent_file_names(b) for b in batches) if n]
    assert len(with_files) == 2, f"ожидались две передачи файлов, получено {len(with_files)}"
    names_a, names_b = with_files
    assert not set(names_a) & set(names_b), (
        f"имена файлов повторились между карточками: {names_a} vs {names_b}"
    )
