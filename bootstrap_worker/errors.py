"""Safe, stable failures exposed by the bootstrap worker."""

from __future__ import annotations


class BootstrapError(Exception):
    """An operational failure whose public fields are safe to serialize.

    Raw SSH exceptions, remote commands, stdout, stderr, and secret values must
    never be used as either argument. The original exception may be chained for
    local debugging, but the worker's API and logs only use ``code`` and
    ``safe_message``.
    """

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class InvalidTransitionError(RuntimeError):
    """Raised when a bootstrap state transition violates the state machine."""


class JobConflictError(BootstrapError):
    """Raised when the worker's bounded concurrency policy rejects a job."""


class JobNotFoundError(BootstrapError):
    """Raised when an in-memory job does not exist in this worker instance."""


class WorkerRestartedError(BootstrapError):
    """Raised when a caller presents an ID from another worker instance."""


class CancellationRequested(BootstrapError):
    """Internal cooperative-cancellation signal."""


def safe_failure(code: str) -> BootstrapError:
    """Build a localized, non-sensitive error from a stable code."""

    messages = {
        "invalid_target": "Адрес сервера не разрешён политикой безопасности.",
        "target_resolution_changed": (
            "IP-адрес сервера изменился во время проверки. Подключение остановлено."
        ),
        "ssh_connection_failed": (
            "Не удалось подключиться по SSH. Проверьте адрес, логин, пароль и порт."
        ),
        "ssh_host_key_changed": (
            "SSH-ключ сервера изменился. Подключение остановлено для защиты от подмены."
        ),
        "ssh_host_key_unsupported": "SSH-сервер использует неподдерживаемый тип ключа.",
        "ssh_authentication_failed": "SSH-аутентификация не выполнена.",
        "sudo_password_invalid": "Пароль sudo не подошёл.",
        "unsupported_operating_system": "Операционная система сервера не поддерживается.",
        "unsupported_relay_operating_system": (
            "Для Moblin Relay нужен amd64-сервер с Ubuntu 22.04/24.04 или Debian 12/13."
        ),
        "unsupported_package_manager": (
            "На сервере отсутствует поддерживаемый системный менеджер пакетов."
        ),
        "insufficient_cpu": "На сервере недостаточно процессорных ядер.",
        "insufficient_memory": "На сервере недостаточно свободной памяти.",
        "insufficient_disk": "На сервере недостаточно свободного места.",
        "outbound_https_unavailable": "Сервер не может установить исходящее HTTPS-соединение.",
        "unsupported_docker_installation": (
            "Обнаружена неподдерживаемая установка Docker. Изменения не выполнялись."
        ),
        "conflicting_container_runtime": (
            "Обнаружен конфликтующий контейнерный runtime. Изменения не выполнялись."
        ),
        "docker_repository_unavailable": (
            "Официальный репозиторий Docker временно недоступен с сервера."
        ),
        "docker_repository_key_invalid": (
            "Ключ подписи официального репозитория Docker не прошёл проверку."
        ),
        "docker_install_failed": "Не удалось безопасно установить Docker Engine.",
        "remote_directory_conflict": (
            "Каталог установки уже существует и не принадлежит AdoJapan Restream."
        ),
        "credential_rotation_unavailable": (
            "Существующий ключ узла нельзя безопасно заменить в текущем состоянии."
        ),
        "remote_command_failed": "Не удалось выполнить безопасный шаг установки.",
        "remote_upload_failed": "Не удалось безопасно загрузить файлы установки.",
        "agent_install_failed": "Node Agent не удалось установить.",
        "agent_enrollment_failed": (
            "Node Agent установлен, но не смог подключиться к панели. Изменения откатаны."
        ),
        "relay_bundle_invalid": "Пакет Moblin Relay повреждён или неполон.",
        "remote_relay_conflict": (
            "На сервере уже есть файлы или службы, конфликтующие с Moblin Relay."
        ),
        "remote_relay_account_conflict": (
            "Системные пользователи Moblin Relay уже существуют с небезопасными параметрами."
        ),
        "relay_active_during_install": (
            "Moblin Relay уже запущен или включён. Установка остановлена без изменений."
        ),
        "relay_port_conflict": ("Один из портов Moblin Relay уже занят другой службой на сервере."),
        "invalid_relay_control_origin": "Адрес панели управления Relay не прошёл проверку.",
        "invalid_relay_target": (
            "Не удалось определить публичный IPv4-адрес нового Relay-сервера."
        ),
        "invalid_enrollment_token": "Защищённый ключ подключения Relay не прошёл проверку.",
        "relay_dependency_install_failed": (
            "Не удалось установить системные компоненты Moblin Relay."
        ),
        "relay_dependency_check_failed": (
            "FFmpeg на сервере не поддерживает необходимые H.264 или SRT-функции."
        ),
        "mediamtx_download_failed": ("Не удалось скачать закреплённую версию MediaMTX."),
        "mediamtx_checksum_failed": "Контрольная сумма MediaMTX не прошла проверку.",
        "mediamtx_archive_invalid": "Закреплённый архив MediaMTX повреждён или неполон.",
        "mediamtx_license_missing": "В архиве MediaMTX отсутствует файл лицензии.",
        "mediamtx_binary_invalid": "Исполняемый файл MediaMTX не прошёл проверку.",
        "relay_slate_generation_failed": "Не удалось создать серверную заставку 1080×1920.",
        "relay_install_failed": "Не удалось безопасно установить Moblin Relay.",
        "relay_agent_install_failed": "Не удалось установить агент управления Moblin Relay.",
        "relay_agent_preflight_failed": "Проверка пакета агента Moblin Relay не пройдена.",
        "relay_agent_accounts_failed": "Не удалось создать системного пользователя Relay Agent.",
        "relay_agent_sysusers_failed": (
            "Не удалось зарегистрировать системного пользователя Relay Agent."
        ),
        "relay_agent_tmpfiles_failed": "Не удалось создать защищённые каталоги Relay Agent.",
        "relay_agent_journal_failed": "Не удалось подготовить безопасный журнал Relay Agent.",
        "relay_agent_copy_failed": "Не удалось проверить и установить код Relay Agent.",
        "relay_agent_units_failed": "Не удалось установить службы Relay Agent.",
        "relay_agent_broker_failed": "Не удалось запустить защищённый канал Relay Agent.",
        "relay_self_test_failed": "Локальная самопроверка Moblin Relay не пройдена.",
        "relay_unit_verify_failed": "Проверка системных служб Moblin Relay не пройдена.",
        "relay_self_test_startup_failed": "Не удалось запустить самопроверку Moblin Relay.",
        "relay_self_test_assets_failed": "Медиафайлы Moblin Relay не прошли самопроверку.",
        "relay_self_test_topology_failed": "Локальный медиатракт Moblin Relay не запустился.",
        "relay_self_test_auth_failed": "Защита входящего SRT-потока не прошла самопроверку.",
        "relay_self_test_auth_source_failed": (
            "Основной тестовый SRT-источник Moblin Relay не запустился."
        ),
        "relay_self_test_auth_source_helper_failed": (
            "Основной тестовый SRT-помощник Moblin Relay не запустился."
        ),
        "relay_self_test_auth_source_publisher_bind_failed": (
            "Тестовый RTMP-издатель не привязал локальный UDP-порт."
        ),
        "relay_self_test_auth_source_feeder_failed": (
            "Тестовая подача LIVE-потока не перешла в состояние готовности."
        ),
        "relay_self_test_auth_source_path_failed": (
            "Основной тестовый SRT-путь не перешёл в состояние готовности."
        ),
        "relay_self_test_auth_live_failed": (
            "Тестовый LIVE-поток не прошёл самопроверку через SRT и нормализатор."
        ),
        "relay_self_test_auth_scan_failed": (
            "Граница секретов активного тестового потока не прошла самопроверку."
        ),
        "relay_self_test_auth_exclusivity_failed": (
            "Запрет второго SRT-издателя не прошёл самопроверку."
        ),
        "relay_self_test_auth_exclusivity_core_failed": (
            "Основной медиатракт остановился во время проверки второго SRT-издателя."
        ),
        "relay_self_test_auth_exclusivity_candidate_failed": (
            "Тестовый второй SRT-источник завершился до проверки серверного отказа."
        ),
        "relay_self_test_auth_exclusivity_primary_failed": (
            "Основной SRT-источник остановился во время проверки эксклюзивности."
        ),
        "relay_self_test_auth_exclusivity_live_failed": (
            "LIVE-состояние изменилось во время проверки второго SRT-издателя."
        ),
        "relay_self_test_auth_exclusivity_ingest_failed": (
            "Основное SRT-соединение изменилось во время проверки эксклюзивности."
        ),
        "relay_self_test_auth_exclusivity_normalizer_failed": (
            "Нормализатор изменился во время проверки второго SRT-издателя."
        ),
        "relay_self_test_auth_exclusivity_downstream_failed": (
            "Выходной поток изменился во время проверки второго SRT-издателя."
        ),
        "relay_self_test_auth_exclusivity_progress_failed": (
            "Основной SRT-поток не подтвердил передачу данных при проверке эксклюзивности."
        ),
        "relay_self_test_auth_exclusivity_observability_failed": (
            "Метрики стали недоступны во время проверки второго SRT-издателя."
        ),
        "relay_self_test_auth_exclusivity_proof_failed": (
            "Серверный отказ второго SRT-издателя не был подтверждён."
        ),
        "relay_self_test_live_ingest_failed": (
            "Тестовый зашифрованный SRT-поток не был принят сервером."
        ),
        "relay_self_test_live_normalize_failed": (
            "Нормализатор не сформировал тестовый LIVE-поток."
        ),
        "relay_self_test_normalizer_hook_failed": ("Supervisor нормализатора не остался активным."),
        "relay_self_test_normalizer_child_failed": ("FFmpeg нормализатора не остался активным."),
        "relay_self_test_normalizer_publish_failed": (
            "FFmpeg нормализатора не опубликовал тестовый LIVE-поток."
        ),
        "relay_self_test_normalizer_flap_failed": (
            "Тестовый LIVE-поток нормализатора оказался нестабильным."
        ),
        "relay_self_test_stall_slate_failed": (
            "Переход на заставку при зависании LIVE не прошёл самопроверку."
        ),
        "relay_self_test_stall_precondition_failed": (
            "LIVE перед проверкой зависания не прошёл самопроверку."
        ),
        "relay_self_test_stall_pause_failed": (
            "Тестовый LIVE-источник не удалось безопасно приостановить."
        ),
        "relay_self_test_stall_switch_failed": (
            "Переключение с зависшего LIVE на заставку не прошло самопроверку."
        ),
        "relay_self_test_stall_capture_failed": (
            "Рост видеопотока заставки после зависания LIVE не подтверждён."
        ),
        "relay_self_test_stall_resume_failed": (
            "Тестовый LIVE-источник не удалось возобновить до разрыва SRT-сессии."
        ),
        "relay_self_test_stall_live_failed": (
            "Возврат LIVE в той же SRT-сессии не прошёл самопроверку."
        ),
        "relay_self_test_stall_core_failed": (
            "Основной медиатракт остановился при восстановлении LIVE."
        ),
        "relay_self_test_stall_source_failed": (
            "Тестовый источник остановился при восстановлении LIVE."
        ),
        "relay_self_test_stall_ingest_failed": (
            "Исходная SRT-сессия не сохранилась при восстановлении LIVE."
        ),
        "relay_self_test_stall_ingest_offline_failed": (
            "Тестовый SRT helper работает, но входная SRT-сессия стала недоступна."
        ),
        "relay_self_test_stall_ingest_identity_failed": (
            "Входная SRT-сессия была заменена при восстановлении LIVE."
        ),
        "relay_self_test_stall_ingest_identity_pre_resume_failed": (
            "Входная SRT-сессия была заменена до возобновления тестового LIVE-источника."
        ),
        "relay_self_test_stall_ingest_identity_recovery_failed": (
            "Входная SRT-сессия была заменена после возобновления тестового LIVE-источника."
        ),
        "relay_self_test_stall_ingest_progress_failed": (
            "Входная SRT-сессия сохранилась, но поток входящих байтов не возобновился."
        ),
        "relay_self_test_stall_helper_observability_failed": (
            "Метрики тестового SRT helper стали недоступны при восстановлении LIVE."
        ),
        "relay_self_test_stall_helper_path_failed": (
            "Входной медиапуть тестового SRT helper стал недоступен."
        ),
        "relay_self_test_stall_helper_forward_failed": (
            "SRT-forward тестового helper перешёл в состояние ошибки."
        ),
        "relay_self_test_stall_helper_state_failed": (
            "SRT-forward тестового helper не вернулся в рабочее состояние."
        ),
        "relay_self_test_stall_normalizer_failed": (
            "Нормализатор не вернул LIVE после возобновления источника."
        ),
        "relay_self_test_stall_downstream_failed": (
            "Выходной тракт изменился при восстановлении LIVE."
        ),
        "relay_self_test_stall_observability_failed": (
            "Метрики стали недоступны при восстановлении LIVE."
        ),
        "relay_self_test_stall_identity_failed": (
            "Нормализатор не заменил зависшее медиасоединение."
        ),
        "relay_self_test_stall_continuity_failed": (
            "Непрерывность выхода при зависании LIVE не подтверждена."
        ),
        "relay_self_test_crash_death_failed": (
            "Изоляция падения supervisor нормализатора не прошла самопроверку."
        ),
        "relay_self_test_crash_live_failed": (
            "LIVE не восстановился после падения supervisor нормализатора."
        ),
        "relay_self_test_crash_continuity_failed": (
            "Непрерывность выхода после падения supervisor не подтверждена."
        ),
        "relay_self_test_outages_failed": "Переключение LIVE и заставки не прошло самопроверку.",
        "relay_self_test_outage_slate_failed": (
            "Переход на заставку после обрыва LIVE не прошёл самопроверку."
        ),
        "relay_self_test_outage_normal_failed": (
            "Остановка нормализации после обрыва LIVE не подтверждена."
        ),
        "relay_self_test_outage_hold_failed": (
            "Непрерывность заставки во время обрыва LIVE не подтверждена."
        ),
        "relay_self_test_outage_live_failed": (
            "Возврат LIVE после восстановления источника не прошёл самопроверку."
        ),
        "relay_self_test_continuity_failed": "Непрерывность выходного потока не подтверждена.",
        "relay_self_test_decode_failed": "Декодирование и временные метки не прошли проверку.",
        "relay_self_test_decode_streams_failed": (
            "Состав выходных медиапотоков не прошёл самопроверку."
        ),
        "relay_self_test_decode_format_failed": (
            "Формат выходного видео и аудио не прошёл самопроверку."
        ),
        "relay_self_test_decode_gop_failed": ("Интервал ключевых кадров не прошёл самопроверку."),
        "relay_self_test_decode_decoder_failed": (
            "Декодирование выходного потока завершилось с ошибкой."
        ),
        "relay_self_test_decode_frames_failed": ("Кадры выходного видео не прошли самопроверку."),
        "relay_self_test_decode_timestamps_failed": (
            "Временные метки и синхронизация потока не прошли самопроверку."
        ),
        "relay_self_test_timestamp_probe_pts_failed": (
            "Проверка наличия PTS и данных медиапотока не пройдена."
        ),
        "relay_self_test_timestamp_packet_dts_failed": (
            "Порядок пакетных DTS не прошёл самопроверку."
        ),
        "relay_self_test_timestamp_video_pts_failed": (
            "Временные метки PTS видеопотока не прошли самопроверку."
        ),
        "relay_self_test_timestamp_audio_pts_failed": (
            "Временные метки PTS аудиопотока не прошли самопроверку."
        ),
        "relay_self_test_timestamp_gaps_failed": (
            "Интервалы между временными метками потока не прошли самопроверку."
        ),
        "relay_self_test_timestamp_av_sync_failed": (
            "Длительность и окончание аудио и видео не прошли проверку синхронизации."
        ),
        "relay_self_test_secrets_failed": "Защита тестовых секретов не прошла самопроверку.",
        "relay_self_test_cleanup_failed": "Очистка после самопроверки Moblin Relay не завершена.",
        "relay_final_check_failed": "Финальная проверка Moblin Relay не пройдена.",
        "overall_timeout": "Время подключения сервера истекло. Изменения откатаны.",
        "bootstrap_worker_restarted": (
            "Сервис установки был перезапущен. Введите SSH-пароль повторно."
        ),
        "cancelled": "Подключение сервера отменено.",
        "job_conflict": "Другая задача подключения сервера уже выполняется.",
        "job_not_found": "Задача подключения сервера не найдена.",
        "invalid_job_state": "Операция недоступна в текущем состоянии задачи.",
    }
    return BootstrapError(code, messages.get(code, "Подключение сервера не выполнено."))


__all__ = [
    "BootstrapError",
    "CancellationRequested",
    "InvalidTransitionError",
    "JobConflictError",
    "JobNotFoundError",
    "WorkerRestartedError",
    "safe_failure",
]
