# Pulse 0.14.5 — понятный анализ и продолжение диалога

Точная сборка приложения: **0.14.5 / b0afc4664ae9c6d1**. Build ID должен совпасть в `/healthz`, `RELEASE.json` и `./pulse check`.

## Что изменилось после 0.14.4

- Режим выбирается кнопками под полем вопроса; отдельный дублирующий список убран, строка движка уплотнена. Сжатый анализ задаёт одну таблицу «Параметр / Значение»: ошибка, важность с основанием, что произошло, причина и первая проверка. Расширенный анализ, причины, последствия и команды имеют отдельные структуры. Свободное уточнение продолжает разговор без обязательного повторения всего отчёта.
- В чате сначала показан ответ модели. Программная сводка Pulse, источники, skills и журнал API собраны в раскрываемом блоке «Источники и проверки». Ограничения данных видны рядом с ответом. При отказе модели проверенные факты остаются видимы; отказ не становится успешным анализом.
- Таблицы, выделение, inline code, нумерованные и вложенные списки отображаются безопасно через текстовые DOM-узлы. HTML модели не исполняется. Открытый блок источников сохраняется при обновлении ответа; новое отправленное сообщение прокручивает диалог к продолжению.
- В историю успешного ответа передаётся текст модели без повторной программной сводки. Новый вход в чат по-прежнему пустой. Контекст текущей проблемы с UID/временем и доказательствами передаётся заново.

Пользователь предоставил рабочий запрос Astra: POST `http://10.128.77.23/api/v1/settings/ai/request/chat/completions`, Bearer, `model=qwen`, `stream=true`, массив `messages`. Присланный SSE-поток успешно воспроизведён текущим адаптером Pulse: 212 событий, `finish_reason=stop`, `[DONE]`, 803 символа ответа. Секреты и реальные логи в выпуск не включаются.

Это проверка совместимости формата. Серверная авторизация Pulse при прежнем HTTP 403 всё ещё требует проверки: сохраните действующий токен рабочего AI-запроса в настройках Pulse и выполните «Проверить поток Astra AI». Для постоянной работы нужен поддерживаемый служебный доступ. Заголовки Origin/Referer сами по себе не доказывают необходимость их передачи; обновление не добавляет их вслепую.

Инструкции Astra из примера не копируются как идентичность Pulse. Одно `info SendText` не доказывает успешную доставку или исправность всего сервиса: соответствующее ограничение добавлено в инструкции модели. Правильная структура ответа не гарантирует правильность диагноза. Контракт реальных логов `filter_preview` ещё не получен. [Подключение ИИ, логов, знаний и дашбордов](AI-ASTRA-DASHBOARDS-RU.md).

## Обновить существующий Pulse

Скачайте из приватного GitHub `releases/pulse-clusters-0.14.5-complete.tar.gz` и соседний `.sha256`. Передайте оба файла в `/home/dito` на сервер Pulse. Команды выполняются от root и рассчитаны на подтверждённое ранее имя контейнера `pulse-clusters-8090-pulse-1`. Если имя изменилось, уточните его через `docker ps` и замените в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.14.5-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.14.5 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.14.5-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.14.5
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0145-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$pulse_backup_dir"
printf '%s\n%s\n' "$pulse_old_dir" "$pulse_runtime" > "$pulse_backup_dir/previous-paths.txt"
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" backup --output-dir "$pulse_backup_dir"
printf 'Backup: %s\nRuntime: %s\n' "$pulse_backup_dir" "$pulse_runtime"
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" stop
./pulse --runtime "$pulse_runtime" start-offline
./pulse --runtime "$pulse_runtime" check
./pulse --runtime "$pulse_runtime" doctor --cluster peoplehub
)
```

При ошибке остановитесь на этом шаге; runtime не удаляйте и `init` поверх действующих данных не выполняйте. Уже распакованный каталог проверяется MANIFEST: изменённые файлы не следует молча заменять. Архив содержит Linux amd64 wheelhouse; базовый Python-образ из Dockerfile должен быть доступен или заранее загружен. Backup содержит credentials и требует защищённого хранения.

Откройте страницу с **Ctrl+F5**. `check` должен подтвердить 0.14.5 / b0afc4664ae9c6d1, а `doctor` — текущие CPU/RAM/root filesystem для ожидаемых узлов. Неуспех `doctor` — причина проверить конкретный источник, его сеть, labels и RBAC; переустановка не исправляет неправильный токен или недоступный адрес.

## Откат

В `previous-paths.txt` первая строка — прежний каталог приложения, вторая — действующий runtime. Подставьте выведенный путь backup:

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0145-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
/opt/pulse-clusters-0.14.5/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Схема данных не менялась: для отката обновления применяется тот же runtime. Восстановление самого backup — отдельная операция, когда действительно нужно вернуть прежние данные.

## Пустой сервер и перенос

Для новой установки после проверки архива/MANIFEST:

```bash
cd /opt/pulse-clusters-0.14.5
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md), база знаний — [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md), внутренний HTTPS-домен — [DEPLOY-DOMAIN-RU](DEPLOY-DOMAIN-RU.md). A-запись задаётся на адрес сервера во внутреннем DNS; Caddy принимает HTTPS и передаёт запросы на loopback Pulse. Для внутренней CA клиентам нужно доверие к её сертификату.

Backup Pulse включает SQLite, каталог документов и ключи, но не Docker volumes Qdrant/Ollama/Caddy. На другом сервере отдельно переносятся эти volumes либо переиндексируются документы, загружаются веса embeddings и настраивается доверие новой CA. Не удаляйте прежние volumes при обычном обновлении приложения.

В качестве приёмки ИИ проверьте один известный инцидент: выбранные namespace/name/UID, status.message, фактически полученные логи/события и ссылки на E##. Проверка соединения не оценивает истинность диагноза. Истёкший Astra AI token требует замены; локальная модель выбирается явно.
