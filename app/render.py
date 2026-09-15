"""Рендер карточки парковки под пиксельную сетку панели 160x160.

Раскладка закреплена (правило проекта от 2026-09-07): QR сверху ~80 % высоты,
внизу полоса текста — госномер, под ним время / стоянка / сумма. QR на весь экран
без текста не выводим: водитель не понимает, за что платит.

Главное ограничение — читаемость QR с улицы: **модуль QR должен быть >= 4 px**.
На солнце камера зажимает экспозицию, в тени свечение светодиодов растекается —
при 3 px модули сливаются и код не берётся. Отсюда: на 160 px влезает только
короткая ссылка (~28 символов). Длинные платёжные payload укорачивайте редиректом.
"""
from __future__ import annotations

import io
import os

import qrcode
from qrcode.constants import (ERROR_CORRECT_H, ERROR_CORRECT_L, ERROR_CORRECT_M,
                              ERROR_CORRECT_Q)
from PIL import Image, ImageDraw, ImageFont

ECC = {"L": ERROR_CORRECT_L, "M": ERROR_CORRECT_M, "Q": ERROR_CORRECT_Q, "H": ERROR_CORRECT_H}
MIN_BOX = 4                      # минимум пикселей на модуль QR для съёмки с улицы
MIN_TEXT_STRIP = 12              # абсолютный минимум полосы: ниже не влезает и одна строка
COMFORT_TEXT_STRIP = 24          # ниже этого в полосе помещается только одна строка

C_PLATE = (255, 220, 0)          # номер — жёлтый
C_TIME = (255, 255, 255)         # время — белый
C_DUR = (0, 224, 255)            # стоянка — голубой
C_AMOUNT = (0, 255, 102)         # сумма — зелёный

_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
    "arialbd.ttf",
)


def _font(px: int):
    for name in _FONTS:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit_font(draw, text: str, box_w: int, box_h: int):
    """Подобрать кегль так, чтобы строка влезла в ячейку по ширине."""
    size = max(5, int(box_h * 0.95))
    while size > 5:
        f = _font(size)
        if draw.textlength(text, font=f) <= box_w:
            return f
        size -= 1
    return _font(5)


def qr_matrix(data: str, ecc: str | None = None):
    """Матрица QR минимальной версии с самым сильным ECC, который в неё влезает."""
    best = None
    for lv in ([ecc] if ecc else ["H", "Q", "M", "L"]):
        q = qrcode.QRCode(error_correction=ECC[lv], border=0)
        q.add_data(data)
        try:
            q.make(fit=True)
        except Exception:
            continue
        m = q.get_matrix()
        if best is None or len(m) < len(best[0]):
            best = (m, lv, q.version)
    if best is None:
        raise ValueError("не удалось построить QR: слишком длинные данные")
    return best


def render_card(*, qr_data: str | None = None, qr_image: bytes | None = None,
                plate: str = "", time_: str = "", amount: str = "", dur: str = "",
                width: int = 160, height: int = 160, qr_pct: int = 100,
                pos: str = "bottom", white: int = 255,
                ecc: str | None = None) -> tuple[bytes, dict]:
    """Нарисовать карточку. Возвращает (PNG-байты, диагностика).

    `qr_pct` — доля зоны, ОСТАВШЕЙСЯ после полосы текста (полоса уже забрала 20 %),
    поэтому 100 — это и есть штатные «QR на 80 % экрана»: 128 px и модуль 5 px.

    `qr_data` — ссылка текстом: QR рисуется по модулям точно в пиксели панели.
    `qr_image` — готовая картинка QR от бэкенда: масштабируется NEAREST, но модули
    почти всегда ложатся неровно, и на улице такой код читается хуже.
    """
    if not qr_data and not qr_image:
        raise ValueError("нужен либо qr_data (ссылка), либо qr_image (картинка)")

    has_text = bool(plate.strip() or time_.strip() or amount.strip() or dur.strip())
    pos = pos if has_text else "none"

    # Раскладка подстраивается под ДЛИНУ ссылки, а не задаётся раз и навсегда.
    #
    # Полоса текста забирает 20 % высоты, и на коротких ссылках это ничего не
    # стоит: 23 символа дают модуль 5 px при подписях. Но чем длиннее ссылка,
    # тем больше модулей в QR, и в какой-то момент модуль падает ниже 4 px —
    # с улицы камера телефона такой код уже не разделит. Отдать в этом случае
    # весь экран под QR выгоднее, чем показать номер и сумму: номер водитель
    # и так знает, а нечитаемый QR делает карточку бесполезной целиком.
    #
    # Порог по количеству символов здесь сознательно НЕ используется: он
    # зависит от уровня коррекции и набора символов (цифры пакуются плотнее
    # букв), и любое «до N символов» врёт в обе стороны. Считается реальная
    # геометрия того QR, который будет нарисован.
    layout_auto = None
    default_strip = round(height * 0.20) if pos in ("top", "bottom") else 0

    # Полоса текста подстраивается под ДЛИНУ ссылки, но не исчезает.
    #
    # Чем длиннее ссылка, тем больше модулей в QR, и при штатных 20 % высоты
    # под подписи модуль в какой-то момент падает ниже 4 px — с улицы камера
    # телефона такой код уже не разделит. Раньше в этом месте подписи
    # снимались целиком; это чинило QR, но отнимало у водителя сумму, ради
    # которой он на табло и смотрит.
    #
    # Поэтому полоса не убирается, а сжимается ровно настолько, чтобы модуль
    # дотянул до 4 px: QR забирает то, что ему необходимо, текст оставляет
    # себе остаток. Экономия обычно копеечная — 33 модулям нужно 132 px из
    # 160, то есть полосе остаётся 28 px вместо 32.
    #
    # Порог по числу символов здесь сознательно НЕ используется: он зависит
    # от уровня коррекции и набора символов (цифры пакуются плотнее букв) и
    # врёт в обе стороны. Считается геометрия того QR, который будет
    # нарисован.
    if qr_data and default_strip:
        modules = len(qr_matrix(qr_data, ecc)[0])
        box_default = max(1, int(min(width, height - default_strip) * qr_pct / 100)) // modules
        if box_default < MIN_BOX:
            # Сколько высоты нужно самому QR, чтобы модуль стал читаемым.
            needed = modules * MIN_BOX
            candidate = height - needed
            if candidate >= MIN_TEXT_STRIP:
                layout_auto = (default_strip, candidate, box_default, MIN_BOX)
                default_strip = candidate
            # Иначе оставляем как есть: если даже пустой экран не даёт 4 px
            # (длинный платёжный payload), отнимать у водителя сумму незачем -
            # QR всё равно не прочитается, и об этом будет warning ниже.

    text_strip = default_strip
    avail = min(width, height - text_strip)
    target = max(1, int(avail * qr_pct / 100))

    img = Image.new("RGB", (width, height), (0, 0, 0))
    d = ImageDraw.Draw(img)
    band_y = text_strip if pos == "top" else 0
    light = (white, white, white)
    info: dict = {"width": width, "height": height, "text_strip": text_strip}
    if layout_auto:
        was_strip, now_strip, was_box, now_box = layout_auto
        info["layout"] = "text_compact"
        info["layout_reason"] = (
            f"полоса текста сжата с {was_strip} до {now_strip} px ради читаемости QR: "
            f"модуль был бы {was_box} px (< {MIN_BOX} px), стал {now_box} px"
        )

    # светлая зона QR — на всю ширину: иначе модули упираются в чёрный фон и сканер
    # теряет границу кода
    d.rectangle([0, band_y, width - 1, band_y + (height - text_strip) - 1], fill=light)

    if qr_data:
        matrix, level, version = qr_matrix(qr_data, ecc)
        n = len(matrix)
        box = max(1, target // n)
        grid = n * box
        side = target
        pad = (side - grid) // 2
        qx = round((width - side) / 2)
        qy = band_y + (height - text_strip - side) // 2
        for r in range(n):
            row = matrix[r]
            for c in range(n):
                if row[c]:
                    x0, y0 = qx + pad + c * box, qy + pad + r * box
                    d.rectangle([x0, y0, x0 + box - 1, y0 + box - 1], fill=(0, 0, 0))
        info.update(qr_version=version, qr_ecc=level, qr_modules=n,
                    module_px=box, qr_side_px=grid, payload_bytes=len(qr_data.encode()))
        if box < MIN_BOX:
            info["warning"] = (f"модуль QR {box} px < {MIN_BOX} px — с улицы такой код "
                               f"камера не разделит; укоротите ссылку редиректом")
    else:
        src = Image.open(io.BytesIO(qr_image)).convert("RGB")
        # NEAREST, а не сглаживание: мыло по краям модулей убивает читаемость сильнее,
        # чем ступеньки. Всё равно предупреждаем — рисовать из ссылки надёжнее.
        src = src.resize((target, target), Image.NEAREST)
        qx = round((width - target) / 2)
        qy = band_y + (height - text_strip - target) // 2
        img.paste(src, (qx, qy))
        info.update(qr_source="готовая картинка", qr_side_px=target)
        info["warning"] = ("QR принят картинкой и отмасштабирован — модули ложатся неровно. "
                           "Надёжнее передавать ссылку в поле qr: она рисуется в пиксели панели")

    if text_strip:
        top = 0 if pos == "top" else height - text_strip
        d.rectangle([0, top, width - 1, top + text_strip - 1], fill=(0, 0, 0))
        rows = []
        if text_strip < COMFORT_TEXT_STRIP:
            # Полоса сжата ради читаемости QR, и делить её на две строки
            # нельзя: по 6 px на строку номер превращается в кашу - проверено
            # на отрисовке. Остаётся одна строка во всю высоту полосы, и в
            # ней сумма: за ней водитель на табло и смотрит, номер своей
            # машины он знает и без экрана.
            main = amount.strip() or plate.strip() or dur.strip() or time_.strip()
            if main:
                color = C_AMOUNT if amount.strip() else C_PLATE
                rows.append([(main, color)])
        else:
            if plate.strip():
                rows.append([(plate, C_PLATE)])
            second = [(t, c) for t, c in ((time_, C_TIME), (dur, C_DUR), (amount, C_AMOUNT)) if t.strip()]
            if second:
                rows.append(second)
        if rows:
            lh = text_strip // len(rows)
            tpad, gap = 2, 2
            for i, cells in enumerate(rows):
                free = (width - 2 * tpad) - gap * (len(cells) - 1)
                lens = [max(1, len(t)) for t, _ in cells]
                total = sum(lens)
                x = tpad
                for j, (text, color) in enumerate(cells):
                    # ширина ячейки пропорциональна длине текста, чтобы «1 250 KGS»
                    # не сжималось в кашу рядом с «12:35»
                    cw = (tpad + (width - 2 * tpad) - x) if j == len(cells) - 1 \
                        else int(free * lens[j] / total)
                    f = _fit_font(d, text, cw, lh)
                    tw = d.textlength(text, font=f)
                    bbox = f.getbbox(text)
                    th = bbox[3] - bbox[1]
                    d.text((x + (cw - tw) / 2, top + lh * i + (lh - th) / 2 - bbox[1]),
                           text, font=f, fill=color)
                    x += cw + gap

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue(), info
