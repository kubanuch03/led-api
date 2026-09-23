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

REG-22 (LED-10): на непоказанную карточку НЕ повторяется ничего — ни команда
    применения, ни запись. A/B на живой панели 23.09.2026 (10.30.205.76,
    блоками по 14, по 28 карточек в плече): с повтором 17/28 = 60%, без
    повтора 25/28 = 89%, Фишер p = 0.029. Рабочая гипотеза: панель платит за
    СЕАНС, а не за карточку; провал уже стоит двух сеансов, повтор добавляет
    ещё два, и топят они СЛЕДУЮЩУЮ карточку.
    ⚠️ Замер не чистый: опыт шёл отдельным процессом мимо `_busy`, и за те же
    24 минуты сервис отправил на ту же панель 15 боевых карточек — помеха
    двусторонняя. Направление правдоподобно, величина не доказана; чистая
    проверка — выкат на одну панель при второй в роли контроля.
    ⚠️ Боевая «прилипчивость» отказа (после успеха 79%, после провала 31%,
    n=257) гипотезу НЕ подтверждает: в проде повтор назначается именно после
    отказа, и те же числа объясняются полосами отказов у панели.
    Отложенный повтор тоже не выход: карта не помнит записанную программу —
    проверено выдержкой 45 с, `_apply_program()` кадр не поднимает.

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


def test_reg22_failed_show_is_not_retried_at_all(panel, monkeypatch):
    """
    Показ не подтвердился → НИКАКОГО повтора: ни команды применения, ни
    перезаписи файла.

    Этот тест стоит на месте прямо противоположного, и отменяет его сознательно.
    Прежний требовал ровно один повтор и опирался на замер, который смотрел
    только на ТЕКУЩУЮ карточку. Повтор действительно иногда поднимал её - и при
    этом ронял СЛЕДУЮЩУЮ, чего тот замер увидеть не мог.

    A/B на живой панели 23.09.2026, 10.30.205.76, блоками по 14, по 28
    карточек в плече:
        с повтором  - встало 17/28 = 60%, после провала следующая встала 36%
        без повтора - встало 25/28 = 89%, после провала следующая встала 100%
    Общая разница: точный критерий Фишера p = 0.029. Разница «после провала»
    (4/11 против 3/3) не значима: p = 0.19.

    Рабочая гипотеза (не доказанный механизм): цена для панели - СЕАНС, а не
    карточка. Провал уже стоит двух сеансов, повтор добавляет ещё два.

    ⚠️ ЗАМЕР НЕ ЧИСТЫЙ, и это надо знать, а не узнать потом. Опыт шёл
    отдельным процессом, без `_busy` из `main.py`, и за те же 24 минуты
    сервис отправил на ту же панель 15 боевых карточек. Они мешали опыту, а
    опыт - им. Направление эффекта правдоподобно, величина не доказана.
    Чистая проверка - выкат на одну панель при второй в роли контроля и
    сравнение боевых долей по логам.

    Отложенный повтор с перезаписью тоже не выход: карта не помнит записанную
    программу (проверено выдержкой 45 с, `_apply_program()` кадр не поднимает,
    три раунда из трёх), поэтому он стоил бы нового неудаляемого файла.

    Если этот тест захочется «починить», вернув повтор, - сначала повторите
    A/B чисто и посмотрите на СЛЕДУЮЩУЮ карточку, а не на текущую.
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
    assert res.verified is False
    assert applies["n"] == 0, "повтор применения роняет следующую карточку — см. A/B 23.09.2026"
    assert len(file_frames) == 1, "файл пишется один раз, перезаписи на отказе нет"


def test_reg22_panel_is_asked_about_the_screen_exactly_once(panel, monkeypatch):
    """
    На одну карточку - РОВНО ОДИН опрос активной программы.

    Считается именно число обращений к панели, а не внешний признак: платит
    она за сеансы, и лишняя сверка стоит столько же, сколько лишнее
    применение. Прежний код на отказе спрашивал дважды.
    """
    asks = {"n": 0}

    def _count_ask():
        asks["n"] += 1
        return ["0" * 32]                     # чужая программа — карточка не встала

    monkeypatch.setattr(panel, "_session", lambda sock, frames: [])
    monkeypatch.setattr(panel, "_apply_program", lambda: pytest.fail("повтора быть не должно"))
    monkeypatch.setattr(panel, "files_on_panel", _count_ask)

    res = panel.send_png(_tiny_png())

    assert res.ok is False
    assert asks["n"] == 1, "на карточку один опрос панели, иначе повтор вернулся окольным путём"


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
