"""Настройки сервиса — всё через переменные окружения."""
import os


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "no", "false", "off", "")


class Settings:
    # Адрес панели. По умолчанию — экран парковки Ала-Арча.
    # Раньше карта жила на link-local 169.254.255.254; её перевели на статический IP.
    host: str = os.environ.get("HD_HOST", "10.30.205.20")
    device_id: str = os.environ.get("HD_DEVICE_ID", "C16L-B25-07457")
    width: int = int(os.environ.get("HD_WIDTH", 160))
    height: int = int(os.environ.get("HD_HEIGHT", 160))
    timeout: float = float(os.environ.get("HD_TIMEOUT", 5))

    # Панель физически выводит кадр повёрнутым на 180° — поворачиваем перед отправкой.
    # Выключать только если панель перевесят или поменяют ScreenR на самой карте.
    rot180: bool = _flag("HD_ROT180", True)

    # Яркость светлых модулей QR: 255 на солнце, 170-200 в тени и ночью.
    white: int = int(os.environ.get("HD_WHITE", 255))

    # Часовой пояс для времени по умолчанию (у карты в программе TimeZone=UTC+6).
    tz_offset_hours: int = int(os.environ.get("HD_TZ_OFFSET", 6))

    # Если задан — требуется заголовок Authorization: Bearer <token> или ?token=
    token: str = os.environ.get("API_TOKEN", "")

    # Сколько ждать освобождения панели, прежде чем ответить 429.
    busy_wait: float = float(os.environ.get("HD_BUSY_WAIT", 20))


settings = Settings()
