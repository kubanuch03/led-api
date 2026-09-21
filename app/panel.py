"""Клиент LED-панели Huidu C16L — протокол HDPlayer, TCP 9527.

Без внешних зависимостей: сокеты, zlib, hashlib. Три вещи, которых не было в скриптах:

1. **Поворот кадра на 180°.** Панель выводит картинку перевёрнутой (проверено на живом
   экране 2026-09-15): HDPlayer поворачивает сам, а `Rotation=2` в программе — лишь метка.
2. **Честная проверка доставки.** `ack` в этом протоколе значит «кадр получен», а НЕ
   «файл записан»: карта умеет подтверждать всё подряд и не сохранять ничего. Поэтому
   после отправки переоткрываем сессию и сверяем md5 в списке активной программы.
3. **UDP-статус.** Карта отвечает на UDP-запрос `01 00 00 00 01 00` на порт 9527 и отдаёт
   ID, IP, MAC, размер экрана, включён ли экран и играет ли программа.
"""
from __future__ import annotations

import hashlib
import os
import re
import socket
import struct
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field

PORT = 9527
CHUNK = 9212                      # размер куска файла в кадре 0x0019

#: Имена файлов кадров. Их РОВНО ДВА и они чередуются.
#:
#: Раньше кадр писался под именем `<md5>.png`, то есть каждый показ создавал
#: файл с новым именем. На карте их накопилось 22 за два дня работы, а команды
#: удаления в протоколе нет - строить уборку не на чем. Два слота дают жёсткую
#: верхнюю границу без единой новой команды: расти нечему.
#:
#: Чередование, а не одно постоянное имя: запись в то же имя, что показывается
#: сейчас, - это перезапись файла, который панель держит открытым, и она
#: вправе показать старое содержимое из кеша. Следующий кадр всегда уходит в
#: слот, который сейчас не на экране.
#: ⚠️ НЕ ИСПОЛЬЗУЕТСЯ И НЕ ВОЗВРАЩАТЬ. Оставлено как запись об измеренном факте.
#:
#: Идея переиспользовать фиксированные имена выглядит очевидной: удаления в
#: протоколе нет, файлы копятся, а два чередующихся имени дали бы жёсткую
#: верхнюю границу без единой новой команды. Проверено на живой панели
#: 21.09.2026 - НА ЭТОМ ЖЕЛЕЗЕ ЭТО НЕ РАБОТАЕТ.
#:
#: Замер, A/B подряд на одной панели (10.30.205.76):
#:   * запись в slot_b.png -> "кадр записан, но панель показывает другую
#:     программу", в активной программе остаётся ОДИН файл вместо двух -
#:     программа разваливается и на экран не встаёт;
#:   * тот же кадр уникальным именем <md5>.png -> активная программа собирается
#:     из двух файлов и СРАЗУ применяется, подтверждено кадром с камеры.
#:
#: Причина: карта не заменяет содержимое файла при записи по существующему
#: имени. В программе лежит md5 картинки, он перестаёт сходиться с тем, что
#: реально лежит на карте под этим именем, и карта программу отвергает.
#:
#: Поэтому имя кадра и boot-файла ОБЯЗАНО быть уникальным. Плата за это -
#: накопление файлов, и решать его надо не именами: слать карточку только когда
#: она изменилась, и чистить карту отдельно (перезагрузка/вендорский софт).
SLOTS = ("slot_a.png", "slot_b.png")

#: Сколько раз спрашивать панель, показала ли она кадр, и повторять команду
#: показа между попытками. Три обращения с паузой укладываются примерно в
#: десять секунд.
APPLY_ATTEMPTS = 3
APPLY_WAIT = 3.0
DEFAULT_TIMEOUT = 5.0

_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Node Level="1" Type="HD_Controller_Plugin">
<Attribute Name="AppVersion">7.11.18.0</Attribute>
<Attribute Name="BindTypeEnable">0</Attribute>
<Attribute Name="DeviceModel">C16L</Attribute>
<Attribute Name="Height">__H__</Attribute>
<Attribute Name="InsertProject">0</Attribute>
<Attribute Name="NewSpecialEffect">close</Attribute>
<Attribute Name="Rotation">2</Attribute>
<Attribute Name="Stretch">0</Attribute>
<Attribute Name="SvnVersion">16086</Attribute>
<Attribute Name="TimeZone">21600</Attribute>
<Attribute Name="Width">__W__</Attribute>
<Attribute Name="ZoomModulus">2</Attribute>
<Attribute Name="__NAME__">Screen5</Attribute>
<Attribute Name="mimiScreen">0</Attribute>
<List Name="communication" Index="0">
<ListItem id="__DEVID__" name="BoxPlayer"/>
</List>
<Node Level="2" Type="HD_OrdinaryScene_Plugin">
<Attribute Name="AbsorbEnable">1</Attribute>
<Attribute Name="Alpha">255</Attribute>
<Attribute Name="BgColor">-16777216</Attribute>
<Attribute Name="BgMode">BgImage</Attribute>
<Attribute Name="Checked">2</Attribute>
<Attribute Name="FixedDuration">30000</Attribute>
<Attribute Name="FrameEffect">0</Attribute>
<Attribute Name="FrameSpeed">4</Attribute>
<Attribute Name="FrameType">0</Attribute>
<Attribute Name="Friday">0</Attribute>
<Attribute Name="HaveNeon">0</Attribute>
<Attribute Name="Monday">0</Attribute>
<Attribute Name="MotleyIndex">0</Attribute>
<Attribute Name="OrdinarySceneVolume">0</Attribute>
<Attribute Name="PlayIndex">0</Attribute>
<Attribute Name="PlayMode">LoopTime</Attribute>
<Attribute Name="PlayTimes">1</Attribute>
<Attribute Name="PlayeTime">30</Attribute>
<Attribute Name="PurityColor">255</Attribute>
<Attribute Name="PurityIndex">0</Attribute>
<Attribute Name="Saturday">0</Attribute>
<Attribute Name="SpaceStartTime">00:00:00</Attribute>
<Attribute Name="SpaceStopTime">23:59:59</Attribute>
<Attribute Name="Sunday">0</Attribute>
<Attribute Name="Thursday">0</Attribute>
<Attribute Name="TricolorIndex">0</Attribute>
<Attribute Name="Tuesday">0</Attribute>
<Attribute Name="UseSpacifiled">0</Attribute>
<Attribute Name="Volume">100</Attribute>
<Attribute Name="Wednesday">0</Attribute>
<Attribute Name="__GUID__">{__GS__}</Attribute>
<Attribute Name="__NAME__">Program1</Attribute>
<List Name="__FileList__" Index="-1"/>
<Node Level="3" Type="HD_Frame_Plugin">
<Attribute Name="Alpha">255</Attribute>
<Attribute Name="ChildType">HD_Photo_Plugin</Attribute>
<Attribute Name="FrameSpeed">4</Attribute>
<Attribute Name="FrameType">0</Attribute>
<Attribute Name="Height">__H__</Attribute>
<Attribute Name="Index">0</Attribute>
<Attribute Name="LockArea">0</Attribute>
<Attribute Name="MotleyIndex">0</Attribute>
<Attribute Name="PurityColor">255</Attribute>
<Attribute Name="PurityIndex">0</Attribute>
<Attribute Name="TricolorIndex">0</Attribute>
<Attribute Name="Width">__W__</Attribute>
<Attribute Name="X">0</Attribute>
<Attribute Name="Y">0</Attribute>
<Attribute Name="__GUID__">{__GF__}</Attribute>
<Attribute Name="__NAME__">Photo1</Attribute>
<Node Level="4" Type="HD_Photo_Plugin">
<Attribute Name="ClearEffect">0</Attribute>
<Attribute Name="ClearTime">4</Attribute>
<Attribute Name="DispEffect">0</Attribute>
<Attribute Name="DispTime">4</Attribute>
<Attribute Name="HoldTime">50</Attribute>
<Attribute Name="KeepConvert">0</Attribute>
<Attribute Name="KeepRatio">0</Attribute>
<Attribute Name="PreloadFilePath">parking.png</Attribute>
<Attribute Name="SpeedTimeIndex">4</Attribute>
<Attribute Name="__GUID__">{__GP__}</Attribute>
<Attribute Name="__NAME__">parking</Attribute>
<List Name="__FileList__" Index="0">
<ListItem MD5="__MD5__" FileKey="Photo" FileName="__FILE__"/>
<ListItem MD5="__MD5__" FileKey="PhotoSource" FileName="__FILE__"/>
</List>
</Node>
</Node>
</Node>
</Node>"""

# --- поворот PNG на 180° без Pillow (нужен и там, где картинку не перерисовываем) ---

_SIG = b"\x89PNG\r\n\x1a\n"
_BPP = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)


def png_decode(buf: bytes):
    """PNG -> (w, h, colortype, bpp, rows, extra). Глубина 8 бит, без interlace."""
    if buf[:8] != _SIG:
        raise ValueError("не PNG")
    meta, idat, extra, i = None, b"", [], 8
    while i < len(buf):
        ln = struct.unpack(">I", buf[i:i + 4])[0]
        tag, data = buf[i + 4:i + 8], buf[i + 8:i + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ct, _c, _f, inter = struct.unpack(">IIBBBBB", data)
            if depth != 8 or inter != 0:
                raise ValueError(f"нужна глубина 8 без interlace (тут {depth}/{inter})")
            meta = (w, h, ct)
        elif tag == b"IDAT":
            idat += data
        elif tag in (b"PLTE", b"tRNS"):
            extra.append((tag, data))
        i += 12 + ln
    if meta is None:
        raise ValueError("нет IHDR")
    w, h, ct = meta
    bpp = _BPP[ct]
    raw = zlib.decompress(idat)
    stride, rows, pos = w * bpp, [], 0
    prev = bytearray(stride)
    for _ in range(h):
        f = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:   line[x] = (line[x] + a) & 255
            elif f == 2: line[x] = (line[x] + b) & 255
            elif f == 3: line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4: line[x] = (line[x] + _paeth(a, b, c)) & 255
        rows.append(line); prev = line
    return w, h, ct, bpp, rows, extra


def png_encode(w: int, h: int, ct: int, rows, extra=()) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    out = _SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, ct, 0, 0, 0))
    for tag, data in extra:
        out += chunk(tag, data)
    out += chunk(b"IDAT", zlib.compress(b"".join(b"\x00" + bytes(r) for r in rows), 9))
    return out + chunk(b"IEND", b"")


def png_rot180(buf: bytes) -> bytes:
    """Повернуть PNG на 180°."""
    w, h, ct, bpp, rows, extra = png_decode(buf)
    flipped = []
    for r in reversed(rows):
        px = [bytes(r[i:i + bpp]) for i in range(0, w * bpp, bpp)]
        flipped.append(bytearray(b"".join(reversed(px))))
    return png_encode(w, h, ct, flipped, extra)


def png_size(buf: bytes) -> tuple[int, int]:
    if len(buf) > 24 and buf[12:16] == b"IHDR":
        return struct.unpack(">I", buf[16:20])[0], struct.unpack(">I", buf[20:24])[0]
    return 160, 160


# --- статус панели по UDP ------------------------------------------------

@dataclass
class PanelStatus:
    online: bool = False
    device_id: str | None = None
    ip: str | None = None
    mac: str | None = None
    width: int | None = None
    height: int | None = None
    screen_on: bool | None = None
    playing: bool | None = None
    program: str | None = None
    locked: bool | None = None
    error: str | None = None


def _u16(b: bytes, o: int) -> int:
    return struct.unpack("<H", b[o:o + 2])[0]


def udp_status(host: str, timeout: float = 3.0) -> PanelStatus:
    """Опросить панель по UDP/9527. Только чтение, показ не трогает."""
    st = PanelStatus()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(bytes([1, 0, 0, 0, 1, 0]), (host, PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                break
            st.online = True
            m = re.search(rb"(C16L-[A-Z0-9-]+)\x00", data)
            if m and not st.device_id:
                st.device_id = m.group(1).decode("latin-1")
            bp = data.find(b"BoxPlayer")
            # Сетевой блок и размеры экрана есть только в пакете с DeviceInfo.
            # Пакет <ext1> тоже содержит ID, но за ним лежит уже другое — не разбираем его.
            if m and bp > 0:
                end = m.end()                       # сразу за NUL идёт блок сети
                if len(data) >= end + 11:
                    st.ip = ".".join(str(x) for x in data[end + 1:end + 5])
                    st.mac = ":".join(f"{x:02x}" for x in data[end + 5:end + 11])
            if bp >= 6:                             # [w:2][h:2][00][len:1]"BoxPlayer"
                st.width, st.height = _u16(data, bp - 6), _u16(data, bp - 4)
            if b"<DeviceInfo>" in data:
                on = re.search(rb'ScreenOnOff Value="(\d+)"', data)
                if on:
                    st.screen_on = on.group(1) == b"1"
            if b"<ext1>" in data:
                ps = re.search(rb'PlayStatus value="(\d+)"', data)
                if ps:
                    st.playing = ps.group(1) == b"1"
                pn = re.search(rb'ProgramName name="([^"]*)"', data)
                if pn:
                    st.program = pn.group(1).decode("latin-1")
                lk = re.search(rb'DeviceLocker enable="(\d+)"', data)
                if lk:
                    st.locked = lk.group(1) != b"0"
            if st.device_id and st.screen_on is not None and st.playing is not None:
                break
    except OSError as e:
        st.error = str(e)
    finally:
        sock.close()
    if not st.online and not st.error:
        st.error = "панель не ответила на UDP-запрос"
    return st


# --- отправка программы по TCP -------------------------------------------

def _frame(cmd: int, payload: bytes = b"") -> bytes:
    """Кадр протокола: [len:2 LE][cmd:2 LE][payload], len включает заголовок."""
    return struct.pack("<HH", 4 + len(payload), cmd) + payload


def _file_frames(name: str, data: bytes) -> list[bytes]:
    out = [_frame(0x0017, name.encode("latin-1") + b"\x00")]
    for i in range(0, len(data), CHUNK):
        out.append(_frame(0x0019, data[i:i + CHUNK]))
    out.append(_frame(0x001B))
    return out


# Идентификация клиента (кадр 0x0410) и кадр 0x000f — из перехвата HDPlayer.
# Карта принимает произвольную строку идентификации, поэтому собираем её сами,
# а не таскаем бинарный файл из старых скриптов.
_HELLO_000F = bytes(8)


def _client_id() -> bytes:
    stamp = time.strftime("%Y-%m-%d_%H:%M:%S")
    line = f"Linux,LED-API,parking,pb,,,_,{stamp},ethernet_0-0.0.0.0-00:00:00:00:00:00,"
    return line.encode("latin-1", "replace") + b"\x00"


@dataclass
class SendResult:
    ok: bool
    verified: bool
    md5: str
    files_on_panel: list[str] = field(default_factory=list)
    detail: str = ""


class Panel:
    """Панель Huidu C16L. Команды сериализованы: карта держит одно соединение на 9527."""

    def __init__(self, host: str, timeout: float = DEFAULT_TIMEOUT,
                 device_id: str = "C16L-B25-07457", rot180: bool = True):
        self.host = host
        self.timeout = timeout
        self.device_id = device_id
        self.rot180 = rot180
        self._lock = threading.Lock()
        self.last_sent: bytes | None = None
        self.last_md5: str | None = None
        # Начинаем со второго слота, чтобы первый же кадр после старта сервиса
        # ушёл в slot_a: так имя первого файла предсказуемо при разборе.
        self._slot_index = len(SLOTS) - 1

    # --- низкий уровень ---------------------------------------------------

    def _session(self, sock: socket.socket, frames: list[bytes]) -> list[tuple[int, bytes]]:
        """Прогнать кадры lock-step: на 0xNNNN карта отвечает 0xNNNN+1.

        Возвращает все полученные ответы. Кадр, разорванный между TCP-сегментами,
        дочитывается: выходим по таймауту ожидания, а не по неполному буферу.
        """
        buf, answers = bytearray(), []

        def take_frame():
            """Снять один целый кадр из буфера, либо None если он ещё не дочитан."""
            if len(buf) < 4:
                return None
            ln, rc = struct.unpack("<HH", buf[:4])
            if ln < 4:                      # мусор в потоке — выбрасываем байт и пробуем снова
                del buf[:1]
                return take_frame()
            if len(buf) < ln:
                return None
            body = bytes(buf[4:ln])
            del buf[:ln]
            return rc, body

        for fr in frames:
            cmd = struct.unpack("<H", fr[2:4])[0]
            sock.sendall(fr)
            want, deadline = cmd + 1, time.time() + self.timeout
            while time.time() < deadline:
                got = take_frame()
                if got is None:
                    try:
                        chunk = sock.recv(65536)
                    except socket.timeout:
                        break               # нет ack — идём дальше, это не всегда фатально
                    if not chunk:
                        return answers      # карта закрыла соединение
                    buf += chunk
                    continue
                answers.append(got)
                if got[0] == want:
                    break
        return answers

    def _handshake(self) -> list[bytes]:
        """Читающая часть рукопожатия — до 0x0011 включительно, ничего не пишет."""
        return [
            _frame(0x000B, bytes([0x09, 0x00, 0x00, 0x01])),
            _frame(0x0730, bytes([0x02, 0, 0, 0, 0, 0, 0, 0])),
            _frame(0x0410, _client_id()),
            _frame(0x000D),
            _frame(0x040A),
            _frame(0x000F, _HELLO_000F),
            _frame(0x0011),
        ]

    # --- операции ---------------------------------------------------------

    def files_on_panel(self) -> list[str]:
        """md5 файлов активной программы (кадр 0x0011 -> ответ 0x0012). Только чтение."""
        with self._lock:
            with socket.create_connection((self.host, PORT), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                answers = self._session(sock, self._handshake())
        for rc, body in answers:
            if rc == 0x0012:
                # Имена разделены нулевым байтом; берём всё, что выглядит как md5.
                return [m.decode() for m in re.findall(rb"[0-9a-f]{32}", body)]
        return []

    def active_program_md5(self) -> str | None:
        """
        Хеш кадра, который панель показывает ПРЯМО СЕЙЧАС (0x0013 -> 0x0014).

        Единственная честная проверка показа, какая есть в протоколе. До неё
        успехом считалось «наш md5 есть в списке файлов карты» (0x0012) - и
        эта проверка не могла увидеть залипание В ПРИНЦИПЕ: файл ложится на
        карту всегда, показывается он или нет. Хуже того, она активно
        маскировала неисправность - сервис рапортовал «карточка показана» на
        физически чёрный экран, и так продолжалось двое суток.

        Поэтому не «упрощать» обратно к проверке по списку файлов: список
        отвечает на вопрос «записалось ли», а нужен ответ на «видит ли это
        водитель». Это разные вопросы, и цена ошибки между ними - выезд на
        объект.

        Ответ 0x0014 несёт восемь служебных байт перед хешем. Их назначение
        неизвестно (документации Huidu нет, всё снято с живого устройства) и
        разбирать их незачем: для сравнения достаточно самого хеша.

        Порядок кадров важен: 0x0014 приходит только после двойного 0x0011 -
        одиночный 0x0013 после рукопожатия ответа не даёт. Установлено
        замером, объяснения нет.
        """
        with self._lock:
            with socket.create_connection((self.host, PORT), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                answers = self._session(
                    sock,
                    self._handshake()[:-1] + [_frame(0x0011), _frame(0x0011), _frame(0x0013)],
                )
        for rc, body in answers:
            if rc == 0x0014:
                found = re.findall(rb"[0-9a-f]{32}", body)
                return found[0].decode() if found else None
        return None

    def _next_slot(self) -> str:
        """
        Слот для следующего кадра - всегда не тот, что писали в прошлый раз.

        Состояние живёт в процессе сервиса. При его перезапуске отсчёт
        начинается заново, и первый кадр может уйти в слот, который сейчас на
        экране; на практике это одна перезапись после рестарта, а не рост
        карты, ради которого слоты и заведены.
        """
        self._slot_index = (self._slot_index + 1) % len(SLOTS)
        return SLOTS[self._slot_index]

    def _apply_program(self) -> None:
        """Сказать панели показать записанную программу (0x001D, 0x001F)."""
        with self._lock:
            with socket.create_connection((self.host, PORT), timeout=self.timeout) as sock:
                sock.settimeout(self.timeout)
                self._session(sock, self._handshake()[:-1] + [_frame(0x001D), _frame(0x001F)])

    def send_png(self, img: bytes, width: int | None = None, height: int | None = None,
                 verify: bool = True) -> SendResult:
        """
        Показать PNG на весь экран и убедиться, что панель его ПОКАЗЫВАЕТ.

        Различие между «записан» и «показан» здесь принципиальное: панель
        принимает файлы исправно даже когда перестала применять программы, и
        по факту записи о показе судить нельзя.

        Повторяется ПРИМЕНЕНИЕ, а не запись. Прежняя версия при неудаче слала
        файл заново - лечила не ту стадию: запись проходила всегда, отказывало
        применение, и каждый такой повтор лишь добавлял на карту ещё один
        файл. Здесь файл пишется один раз, а при несовпадении повторяется
        только команда показа.

        Ответы 0x001E/0x0020 на применение приходят пустыми и успех от отказа
        не отличают - сигналом они не являются. Достоверен только обратный
        вопрос панели: что у тебя сейчас на экране.
        """
        if self.rot180:
            try:
                img = png_rot180(img)
            except Exception as e:                  # битый PNG — показать важнее, чем повернуть
                return SendResult(False, False, "", detail=f"не удалось повернуть PNG: {e}")

        md5 = hashlib.md5(img).hexdigest()
        w, h = png_size(img)
        w, h = int(width or w), int(height or h)
        slot = md5 + ".png"

        xml = (_XML.replace("__H__", str(h)).replace("__W__", str(w))
                   .replace("__DEVID__", self.device_id)
                   .replace("__GS__", str(uuid.uuid4()))
                   .replace("__GF__", str(uuid.uuid4()))
                   .replace("__GP__", str(uuid.uuid4()))
                   .replace("__MD5__", md5)
                   .replace("__FILE__", slot))
        xml_bytes = xml.replace("\n", "\r\n").encode("utf-8")
        boot = hashlib.md5(xml_bytes).hexdigest() + ".boo"

        frames = self._handshake()[:-1] + [
            _frame(0x0011), _frame(0x0011), _frame(0x0013),
            _frame(0x0015, bytes(4)),
            *_file_frames(slot, img),
            *_file_frames(boot, xml_bytes),
            _frame(0x001D), _frame(0x001F),
        ]

        with self._lock:
            try:
                with socket.create_connection((self.host, PORT), timeout=self.timeout) as sock:
                    sock.settimeout(self.timeout)
                    self._session(sock, frames)
                    time.sleep(2)                   # карта дочитывает программу перед разрывом
            except OSError as e:
                return SendResult(False, False, md5, detail=f"панель недоступна: {e}")

        self.last_sent, self.last_md5 = img, md5
        if not verify:
            return SendResult(True, False, md5, detail=f"кадр записан в {slot}, показ не проверялся")

        # Панель переключается не мгновенно, поэтому первое чтение - после
        # паузы. Пауза и число попыток подобраны на живом устройстве: три
        # обращения укладываются примерно в 10 секунд, дольше держать
        # вызывающего незачем - карточка не стоит задержки платёжного пути.
        for attempt in range(1, APPLY_ATTEMPTS + 1):
            time.sleep(APPLY_WAIT)
            try:
                active = self.active_program_md5()
            except OSError as e:
                return SendResult(True, False, md5, detail=f"кадр записан в {slot}, проверка показа не удалась: {e}")

            if active == md5:
                return SendResult(True, True, md5, detail=f"показывается (слот {slot})")

            if attempt < APPLY_ATTEMPTS:
                # Файл НЕ переписываем - он на карте и целый. Повторяем
                # только команду показа.
                try:
                    self._apply_program()
                except OSError as e:
                    return SendResult(True, False, md5, detail=f"кадр записан в {slot}, применение не прошло: {e}")

        return SendResult(
            False, False, md5,
            detail=(
                f"кадр записан в {slot}, но панель показывает другую программу "
                f"({(active or 'неизвестно')[:8]}…). Панель не применяет новые программы - "
                f"помогает только перезагрузка по питанию."
            ),
        )

    def blank(self, width: int = 160, height: int = 160) -> SendResult:
        """Погасить экран: сплошной чёрный кадр."""
        rows = [bytearray(width * 3) for _ in range(height)]
        return self.send_png(png_encode(width, height, 2, rows), width, height)
