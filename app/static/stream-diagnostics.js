(() => {
  "use strict";
  const form = document.getElementById("diagnostic-period");
  if (!form) return;
  const names = {
    source_lost: "Нет входящего видео", publisher_failed: "Ошибка отправки", retry_exhausted: "Повторные попытки исчерпаны",
    publisher_stalled: "Отправка перестала продвигаться", publisher_retry: "Повторный запуск отправки", process_exit: "Процесс завершился",
    input_ended: "Чтение видео прервалось", io_timeout: "Тайм-аут соединения", io_error: "Ошибка чтения или записи",
    connection_reset: "Соединение сброшено", connection_refused: "Соединение отклонено", broken_pipe: "Канал записи закрыт",
    rtp_packets_lost: "Потеря RTP-пакетов", too_many_reordered_frames: "Нарушен порядок кадров",
    non_monotonic_dts: "Время кадров идёт назад", selector_timestamp_overlap: "Перекрытие времени кадров",
    selector_nonadvancing_dts: "Время кадров идёт назад", mapped_timestamp_regression: "Нарушена выходная шкала времени",
    active_queue_overflow: "Переполнение очереди видео", queue_full: "Очередь заполнена",
    selector_sink_failed: "Не удалось передать данные отправителю", target_readiness_lost: "Целевой сервер потерял готовность",
    control_unreachable: "Нет связи с панелью", control_recovered: "Связь с панелью восстановлена",
    collector_overflow: "Часть диагностики не поместилась в буфер", diagnostic_disk_error: "Не удалось записать локальный журнал",
    agent_started: "Медиаагент запущен",
  };
  for (const element of document.querySelectorAll("[data-diagnostic-code]")) {
    const code = element.dataset.diagnosticCode;
    if (names[code]) { element.textContent = names[code]; element.title = code; }
  }
  for (const element of document.querySelectorAll("time[datetime]")) {
    element.textContent = new Date(element.dateTime).toLocaleString("ru-RU");
  }
  document.getElementById("diagnostic-timezone").textContent = `Часовой пояс: ${Intl.DateTimeFormat().resolvedOptions().timeZone}.`;
  const input = document.getElementById("diagnostic-until"), end = new Date(input.dataset.until);
  const local = new Date(end.getTime() - end.getTimezoneOffset() * 60000);
  input.value = local.toISOString().slice(0, 19);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = new URLSearchParams(new FormData(form));
    if (input.value) query.set("until", new Date(input.value).toISOString());
    location.search = query.toString();
  });
})();
