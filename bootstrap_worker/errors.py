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
        "docker_repository_incomplete": (
            "В выбранном официальном репозитории Docker отсутствует необходимый пакет."
        ),
        "docker_failed_install_recovery_unsafe": (
            "Незавершённую установку Docker нельзя безопасно восстановить автоматически."
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
