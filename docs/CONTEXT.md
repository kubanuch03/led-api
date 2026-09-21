# CONTEXT — screen_service

Контекст проекта для продолжения работы (человеком или ИИ-ассистентом). Обновлено: 2026-09-05.

## 1. Задача

Вывести на LED-экран карточку оплаты парковки: **QR-код + госномер + время + сумма**.
Экран управляется контроллером **Huidu HD-C16L**, к нему уже есть готовый REST-сервис **ScreenService** (написан коллегой из PB, репозиторий bitbucket `U92/screenservice`, .NET Core 3.1). Наша интеграция идёт через этот сервис по HTTP.

Экран: 640×640 мм, 8 модулей P4 320×160 мм → **160×160 px**. Контроллер сейчас на **IP 169.254.255.254** (link-local, без DHCP), TCP-порт SDK **10001**.
Из логов в архиве известны ID контроллеров: `C16L-D24-00532`, `C16L-D24-0052A`.

## 2. Что лежит в папке

```
screen_service/
├─ CONTEXT.md                ← этот файл
├─ README.md                 ← быстрый старт
├─ ScreenService/            ← ГОТОВАЯ СБОРКА сервиса (Release, из архива), запускать run.cmd
│   ├─ appsettings.json      ← ScreenScanSubnet = ["169.254.255.254"], порт 8040, токен
│   ├─ ScreenService.runtimeconfig.json  ← rollForward=Major (работает на .NET 3.1/6/8)
│   └─ run.cmd
├─ screen-qr.html            ← страница-отправитель: рисует карточку на canvas, шлёт UploadFile+SendImage
├─ send_parking_qr.py        ← то же из Python (qrcode + pillow), CLI
├─ screenservice_client.py   ← Python-клиент ко всем методам API + CLI
├─ ScreenService-API.md      ← полное описание API, curl/JS-примеры, таблица эффектов
├─ screenservice-openapi.yaml← OpenAPI 3.0 (Postman/Swagger)
└─ src/                      ← исходники (ScreenService.sln: ScreenService + SDKLibrary), без .git/bin/obj
```

## 3. Как работает ScreenService

- ASP.NET Core 3.1, Kestrel `http://localhost:8040` (только localhost! для доступа извне менять `Kestrel.Endpoints.Http.Url` или `ASPNETCORE_URLS`).
- `ScreenManager` раз в 180 с (первый раз через 1 с) проверяет IP из `AppSettings.ScreenScanSubnet` (поддерживает маску `"10.77.1.*"`), при открытом порту 10001 подключается через `HDCommunicationManager.AddDevice`. Экран появляется в `GetDevices` только после получения DeviceInfo.
- Авторизация: `SimpleAuthorizationMiddleware` — заголовок `Authorization: Bearer <AccessToken>` или query `?access_token=`. Закрыты все маршруты, включая Ping. Токен: `7524e064a9b047eb896c99243b983010`.
- CORS открыт полностью → можно дёргать из браузера с file:// и с любого origin.
- Каждый `Send*` строит `HdScreen → HdProgram → HdArea(0,0,W,H)` с одним элементом и вызывает `Device.SendScreen` — **полностью заменяет** содержимое экрана. Ротаций/зон/расписаний через API нет (в `LEDScreen.Send` есть заготовка `AddProgram/UpdateProgram`).
- Ошибки SDK → голый `500`, подробности только в `logs/AppLog.<дата>.txt`. Несуществующий DeviceID → тихо `200`.
- Все команды сериализованы глобальным `lock`; `UploadFile` синхронный до 5 мин и блокирует остальные.
- `UploadFile` сохраняет файл в `UPLOADS/<DeviceID>/` и **не перезаписывает** одноимённый → перед повторной загрузкой вызывать `DeleteFile`.
- `Config` читает `appsettings.json` из текущего каталога → запускать из папки сервиса (run.cmd делает `cd /d %~dp0`).

## 4. API (кратко)

База `/api/Screen/`. `DeviceID` — из GetDevices, допускает несколько через запятую.

| Метод | Тело | Что делает |
|---|---|---|
| GET Ping | — | `{"Message":"Pong"}` |
| GET GetDevices | — | список контроллеров: DeviceID, ScreenWidth/Height, EthernetInfo.IP, Connected |
| POST SendText | DeviceID, Text, FontName, FontSize, Color, BackgroundColor, InEffect, OutEffect, InSpeed, OutSpeed, Duration | текст на весь экран |
| POST SendImage | DeviceID, FileName, эффекты, Duration | показать загруженную картинку |
| POST SendVideo | DeviceID, FileName | видео (асинхронно) |
| POST SendClock | DeviceID, ClockType(0/1), Time/Date/Week/Title Display+Format+Color, TitleText | часы |
| POST SetLuminanceInfo | DeviceID, Value 1–100 | яркость, возвращает LuminanceInfo |
| POST GetFiles / GetDeviceFontInfo | DeviceID | файлы / шрифты на контроллере |
| POST UploadFile | DeviceID, FileName, Data(base64) | загрузить файл |
| POST DeleteFile | DeviceID, FileName | удалить |
| POST OpenScreen / CloseScreen | DeviceID | вкл/выкл |

Эффекты: 0 сразу, 17 fade, 20 не очищать, 21 бегущая влево, 25 random (полная таблица в ScreenService-API.md).
Для карточки используем `InEffect:0, OutEffect:20, Duration:600`.

## 5. Сценарий вывода карточки

```
GetDevices → DeviceID, W×H
рисуем PNG W×H: QR (~58% высоты, чёрный на белом) + 3 строки (номер жёлтый, время белый, сумма жёлтый)
DeleteFile(name) → UploadFile(name, base64 PNG) → SendImage(name, 0, 20, Duration)
```
Реализовано одинаково в `screen-qr.html` (canvas + встроенный qrcode-generator) и `send_parking_qr.py` (qrcode+pillow). QR на 160 px проверен — декодируется.

## 6. Что уже сделано / проверено

- Архив разобран, API изучено, документация и клиенты написаны.
- Сервис запущен на .NET 8 через `DOTNET_ROLL_FORWARD=Major` — стартует, Ping/401 работают; полная цепочка DeleteFile→UploadFile→SendImage проходит (без реального экрана: GetDevices пуст, команда на несуществующий ID даёт 200).
- `screen-qr.html` прогнана в Chromium против сервиса — работает.
- Пересобрать под net8 в облаке не удалось (NuGet заблокирован), поэтому лежит готовая сборка Release из архива.

## 7. Что осталось (на стороне ПК с экраном)

1. Убедиться, что у ПК на том же адаптере адрес `169.254.x.x` (Windows выдаёт сам, если DHCP нет): `ping 169.254.255.254`, `Test-NetConnection 169.254.255.254 -Port 10001`.
2. Установлен .NET Runtime (ASP.NET Core 3.1/6/8): `dotnet --version`.
3. `ScreenService\run.cmd` → ждать `C16L-…: Connected` (до 60 с — таймаут коннекта для одиночного IP).
4. Открыть `screen-qr.html` → «Найти экраны» → заполнить → «Отправить на экран».
5. Для боевого использования: перевести контроллер на нормальный статический IP (HDPlayer/HDSet) и прописать его в `ScreenScanSubnet`; сменить токен; если сервис нужен с другой машины — открыть Kestrel на `0.0.0.0` и прикрыть nginx'ом.

## 8. Идеи для интеграции с парковкой

- Бэкенд парковки (Dahua ANPR → событие въезда/оплаты) вызывает `send_parking_qr.py` или напрямую API: генерирует ссылку на оплату, рендерит PNG, UploadFile+SendImage.
- После оплаты/по таймауту возвращать «дежурный» экран: `SendClock` или `SendImage` заставки.
- Яркость день/ночь: `SetLuminanceInfo` по расписанию.
- Если нужна ротация элементов (QR + бегущая строка), дорабатывать `LEDScreen.Send` в `src/` — добавлять несколько `HdArea`/элементов в одну программу.
