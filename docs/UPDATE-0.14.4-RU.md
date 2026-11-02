# Pulse 0.14.4 — свежие диалоги и факты при отказе ИИ

Точная сборка приложения: **0.14.4 / 30570f2ec209548b**. Build ID должен совпасть в `/healthz`, `RELEASE.json` и `./pulse check`.

## Что изменилось после 0.14.3

- Повторный вход через активный пункт «Чат с ИИ» открывает новый пустой диалог. Переход между страницами закрывает окно; запоздалое событие закрытия старого окна не очищает новую сессию.
- Временные диалоги больше не получают автоматически решения прежних инцидентов. Follow-up сохраняется только в текущем открытом диалоге. Постоянная история расследований остаётся отдельной функцией. Документы базы знаний и текущие факты кластера могут использоваться; переписка не становится долговременной памятью Pulse.
- Для режима «Последствия» Pulse отдельно показывает ограниченную оценку по фактам, даже если модель отказала. Это программная сводка, а не выданный за успешный ответ ИИ. Она не объявляет потерю данных или простой всего сервиса доказанными без подтверждений.
- В настройках Astra читаются обратно сохранённые URL и поля привязки логов. Токен не возвращается в браузер. Поля больше не выглядят пустыми при существующем подключении.

Astra AI HTTP 403 требует проверки авторизации/прав или правил прокси на стороне AI endpoint. Обновление не обходит запрет, не подставляет токен логов и не переключает модель скрытно. Реальный контракт логов Astra по `filter_preview` ещё требует проверки: существование такого запроса в браузере не доказывает совместимость адаптера. [Порядок подключения ИИ, логов, знаний и дашбордов](AI-ASTRA-DASHBOARDS-RU.md).

## Обновить существующий Pulse

Скачайте из приватного GitHub `releases/pulse-clusters-0.14.4-complete.tar.gz` и соседний `.sha256`. Передайте оба файла в `/home/dito` на сервер Pulse. Команды выполняются от root и рассчитаны на подтверждённое ранее имя контейнера `pulse-clusters-8090-pulse-1`. Если имя изменилось, уточните его через `docker ps` и замените в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.14.4-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.14.4 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.14.4-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.14.4
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0144-$(date +%Y%m%d-%H%M%S)"
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

Откройте страницу с **Ctrl+F5**. `check` должен подтвердить 0.14.4 / 30570f2ec209548b, а `doctor` — текущие CPU/RAM/root filesystem для ожидаемых узлов. Неуспех `doctor` — причина проверить конкретный источник, его сеть, labels и RBAC; переустановка не исправляет неправильный токен или недоступный адрес.

## Откат

В `previous-paths.txt` первая строка — прежний каталог приложения, вторая — действующий runtime. Подставьте выведенный путь backup:

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0144-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
/opt/pulse-clusters-0.14.4/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Схема данных не менялась: для отката обновления применяется тот же runtime. Восстановление самого backup — отдельная операция, когда действительно нужно вернуть прежние данные.

## Пустой сервер и перенос

Для новой установки после проверки архива/MANIFEST:

```bash
cd /opt/pulse-clusters-0.14.4
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md), база знаний — [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md), внутренний HTTPS-домен — [DEPLOY-DOMAIN-RU](DEPLOY-DOMAIN-RU.md). A-запись задаётся на адрес сервера во внутреннем DNS; Caddy принимает HTTPS и передаёт запросы на loopback Pulse. Для внутренней CA клиентам нужно доверие к её сертификату.

Backup Pulse включает SQLite, каталог документов и ключи, но не Docker volumes Qdrant/Ollama/Caddy. На другом сервере отдельно переносятся эти volumes либо переиндексируются документы, загружаются веса embeddings и настраивается доверие новой CA. Не удаляйте прежние volumes при обычном обновлении приложения.

В качестве приёмки ИИ проверьте один известный инцидент: выбранные namespace/name/UID, status.message, фактически полученные логи/события и ссылки на E##. Проверка соединения не оценивает истинность диагноза. Истёкший Astra AI token требует замены; локальная модель выбирается явно.
