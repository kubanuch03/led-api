"""
Регрессии на два дефекта протокола панели, каждый из которых стоил времени
на объекте (чёрный экран двое суток, а сервис рапортовал «показано»).
Правило: дефект, стоивший инцидента, закрывается тестом, а не только
исправлением — иначе следующий человек «упростит» проверку обратно.

REG-18 и REG-19 (LED-06, LED-07) ОТМЕНЕНЫ замером 21.09.2026 и заменены на
    REG-21. Они требовали проверять показ кадром 0x0013→0x0014 и повторять
    ПРИМЕНЕНИЕ при несовпадении. На панелях объекта 0x0014 не отвечает вовсе —
    проверено на обеих (10.30.205.75 и .76) двумя независимыми клиентами, —
    поэтому проверка не сходилась никогда: каждый показ объявлялся неудачей и
    тянул за собой ещё два применения. Три переключения программы на одну
    карточку — это и есть непрерывно моргающее табло, которое заметил
    заказчик. Требование «файл пишется ОДИН раз» из REG-19 верное и сохранено
    в REG-21.

REG-21 (LED-09): показ проверяется СОСТАВОМ АКТИВНОЙ ПРОГРАММЫ
    (0x0011→0x0012), одним опросом и без повторного применения. Что 0x0012
    отдаёт именно активную программу, а не «все файлы карты», видно по
    количеству: после нескольких сотен показанных карточек в ответе
    по-прежнему две записи — кадр и boot-файл.

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


def test_reg21_verify_reads_active_program_contents(panel, monkeypatch):
    """
    Показано ⇔ наш md5 попал в СОСТАВ АКТИВНОЙ ПРОГРАММЫ (0x0011 -> 0x0012).

    Панель приняла кадр и подтвердила все кадры ack'ами, но программу не
    переключила - в активной осталась чужая. Это и есть «проглотила карточку»:
    водитель по-прежнему видит счёт предыдущей машины.
    """
    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    # В активной программе - ЧУЖИЕ файлы.
    monkeypatch.setattr(panel, "files_on_panel", lambda: ["f" * 32, "e" * 32])

    res = panel.send_png(_tiny_png())

    assert res.ok is False
    assert res.verified is False
    assert "проглотила карточку" in res.detail


def test_reg21_verify_true_when_our_frame_is_in_active_program(panel, monkeypatch):
    """Обратная сторона: показано ⇔ наш md5 есть в активной программе."""
    import hashlib

    img = _tiny_png()
    want = hashlib.md5(img).hexdigest()  # rot180=False, значит шлём как есть
    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    monkeypatch.setattr(panel, "files_on_panel", lambda: [want, "b" * 32])

    res = panel.send_png(img)

    assert res.ok is True
    assert res.verified is True


def test_reg21_apply_is_retried_exactly_once_and_only_on_failure(panel, monkeypatch):
    """
    Показ не подтвердился → команда применения повторяется РОВНО ОДИН раз, и
    файл при этом НЕ переписывается.

    Три числа здесь одинаково важны.

    Ноль повторов - мало: панель роняет программы случайно (замер: десять
    карточек подряд дали шесть отказов, три показа и отказ), и вторая попытка
    при такой доле окупается.

    Три повтора вслепую - то, что было раньше и моргало табло на КАЖДОЙ
    карточке: проверка через 0x0014 не работала в принципе, поэтому неудачей
    объявлялся любой показ.

    Файл ровно один: повтор шлёт команду показа, а не запись. Новое имя
    добавило бы на карту ещё один неудаляемый файл, а именно их накопление
    панель и убивает.
    """
    applies = {"n": 0}
    file_frames = []

    def _spy_session(sock, frames):
        if any(_sent_file_names([fr]) for fr in frames):
            file_frames.append(frames)
        return []

    monkeypatch.setattr(panel, "_session", _spy_session)
    monkeypatch.setattr(panel, "_apply_program", lambda: applies.__setitem__("n", applies["n"] + 1))
    # Активная программа ЧУЖАЯ и не меняется — худший случай.
    monkeypatch.setattr(panel, "files_on_panel", lambda: ["0" * 32])

    res = panel.send_png(_tiny_png())

    assert res.ok is False
    assert applies["n"] == 1, "повтор применения ровно один: ноль — мало, три — моргание"
    assert len(file_frames) == 1, "файл пишется один раз, а не на каждую попытку"


def test_reg21_no_retry_when_first_apply_worked(panel, monkeypatch):
    """Показ подтвердился с первой команды → повтора нет, табло не моргает."""
    import hashlib

    img = _tiny_png()
    want = hashlib.md5(img).hexdigest()
    applies = {"n": 0}

    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    monkeypatch.setattr(panel, "_apply_program", lambda: applies.__setitem__("n", applies["n"] + 1))
    monkeypatch.setattr(panel, "files_on_panel", lambda: [want])

    res = panel.send_png(img)

    assert res.verified is True
    assert applies["n"] == 0, "успешный показ не должен вызывать повторное применение"


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
