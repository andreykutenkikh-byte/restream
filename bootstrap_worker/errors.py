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
        "relay_slate_generation_failed": "Не удалось создать серверную заставку 1080×1920.",
        "relay_install_failed": "Не удалось безопасно установить Moblin Relay.",
        "relay_agent_install_failed": "Не удалось установить агент управления Moblin Relay.",
        "relay_self_test_failed": "Локальная самопроверка Moblin Relay не пройдена.",
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
