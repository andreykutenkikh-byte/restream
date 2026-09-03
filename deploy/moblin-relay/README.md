# Moblin relay

Эта папка содержит переносимый исходный runtime-пакет. В ней намеренно нет
MediaMTX binary, сгенерированной `slate.mp4`, install manifest и каких-либо
SRT, YouTube или preview credentials. Установщик обязан проверить закреплённые
версии и SHA-256 загружаемых артефактов, а секреты создать непосредственно на
целевом сервере.

Relay использует вертикальный поток 9:16 H.264/AAC. Публичный SRT path
`iphone-live` принимает исходный поток Moblin. Локальный FFmpeg-мост копирует
H.264 без перекодирования, приводит AAC mono/stereo к AAC-LC stereo 48 kHz и
публикует результат во внутренний path `relay-output`. Только `relay-output`
содержит заставку и отправляется в YouTube.

Нормализатор работает под небольшим supervisor: перед запуском FFmpeg он
проверяет рост входа через закрытые loopback metrics, а после запуска следит
именно за медиабайтами локального RTMP publisher `relay-output`. Поэтому сетевой
джиттер сначала поглощается SRT-буфером, а после фактической остановки выхода
supervisor быстро освобождает path для заставки. Внешний SRT timeout остаётся
10 секунд и сохраняет соединение при кратком сбое. FFmpeg привязан к жизни
supervisor через Linux parent-death signal, поэтому аварийно осиротевший процесс
не может удержать publisher. Окружение MediaMTX hook перед запуском supervisor
полностью заменяется, поэтому параметры подключения не остаются в `/proc`.

Серверная заставка: 1080x1920, H.264/AVC Main Level 4.0, 30 FPS,
`yuv420p`, GOP 60 (keyframe каждые 2 секунды), AAC-LC stereo 48 kHz,
цикл 12 секунд. Такой же стартовый формат не позволяет YouTube зафиксировать
720p до подключения Moblin.

## Настройки Media в Moblin

```text
Settings → Streams → профиль → Media
Portrait: ON
Resolution: 1080p (actual output: 1080x1920)
FPS: 30
Codec: H.264/AVC
Bitrate: 8–10 Mbit/s (10 Mbit/s при устойчивом uplink)
Adaptive bitrate: ON
Keyframe interval: 2 seconds
SRT latency: 2000–3000 ms
Local recording: ON
SRT implementation: Official
Big packets: ON
```

В поле URL потока Moblin вводится только защищённый SRT URL из
`sudo relayctl show-moblin-url`. YouTube RTMPS URL и stream key настраиваются
только на сервере через `sudo relayctl configure-youtube`.

Публичный адрес не зашит в `relayctl`. Установщик атомарно создаёт root-owned
`/etc/moblin-relay/node.json` по схеме из `node.json.example`. Обязательное
поле `public_srt_host` принимает IPv4, DNS-имя или IPv6 в квадратных скобках.
`fallback_srt_hosts` — необязательный список (по умолчанию пустой); установщик
не должен угадывать VPN-адрес. Значения `srt_port` и `srt_path` обязаны точно
совпадать с установленным runtime (`8890` и `iphone-live`). Повторный запуск
установщика может обновить только этот не секретный node-файл и не должен
пересоздавать `/etc/moblin-relay/secrets.json`.

После bootstrap `moblin-relay.service` остаётся `inactive` и `disabled`.
Установщик включает только control agent и broker socket. Эфир запускается
вручную после настройки YouTube; это исключает автоматическую отправку в
непроверенный destination при первом запуске или перезагрузке нового сервера.

Входящий SRT listener MediaMTX явно привязан к IPv4 `0.0.0.0:8890` (UDP),
чтобы публичный IPv4 URL из `show-moblin-url` не зависел от системной настройки
IPv6-only sockets.

Первый реальный сеанс Moblin нужно проверить через ffprobe. Входящий AAC может
быть mono: сервер штатно преобразует его в stereo. H.264 profile/level,
разрешение, FPS и GOP должны оставаться совместимыми с заставкой. LIVE-видео
проходит через сервер с `-c:v copy`: разрешение и закодированные видеокадры не
уменьшаются и не перекодируются.

## Защищённое превью в панели

Превью является необязательным и не влияет на основной relay. Если файла
`/etc/adojapan-relay-agent/preview-reader.token` нет, MediaMTX запускается с
отключённым HLS. Некорректный или небезопасный существующий файл приводит к
отказу запуска, а не к анонимному доступу.

После установки агента и только при остановленных relay и control agent
credential создаётся интерактивной командой:

```bash
sudo adojapan-relay-install-preview-token --generate
```

Значение генерируется локально без вывода секрета; один и тот же защищённый файл читают renderer и агент,
поэтому второго хранилища credential нет. Само значение не передаётся в
аргументах процесса или unit-файле. Файл принадлежит
`restream-agent`, имеет режим `0600`, а runtime-конфигурация содержит только
SHA-256 hash.

При наличии безопасного credential renderer включает только локальный MPEG-TS
HLS:

```text
listener: 127.0.0.1:8888
path: relay-output
variant: mpegts
segment duration: 2s
segment count: 4
reader: relay-preview (read only)
```

API, playback и WebRTC остаются отключёнными. RTSP и RTMP используются только
локальным нормализатором и слушают исключительно `127.0.0.1`; HLS также не
публикуется на внешнем интерфейсе и не требует порта в firewall.
Удаление credential и следующий штатный запуск relay полностью отключают HLS;
основной SRT → YouTube маршрут и его secrets при этом не изменяются.

Проверка исходного renderer без настоящих ключей:

```bash
python3 ./test-render-config.py
```
