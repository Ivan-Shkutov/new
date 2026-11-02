# Pulse 0.14.6 — короткий вывод при обрыве ответа

Точная сборка приложения: **0.14.6 / f24a66d855914eac**. Build ID должен совпасть в `/healthz`, `RELEASE.json` и `./pulse check`.

## Что изменилось после 0.14.5

- При отказе или обрыве ИИ основное окно показывает короткую программную сводку по состоянию выбранного Pod. Для подтверждённого EmptyDir — точный том/лимит, граница вывода о доступности сервиса и первая проверка. Для cached источника явно указан сохранённый характер наблюдения. Недоступный объект не выдаётся за проверенный.
- Ошибка показана коротко; полный текст, каноническая сводка и журнал доступны в закрытом блоке источников. Оборванный текст ИИ находится внутри отдельного закрытого блока с предупреждением и не выглядит завершённым анализом. Его ошибочные утверждения не переносятся в следующий Pod follow-up; проверенные данные сохраняются.
- Сжатый анализ запрашивает у модели одну таблицу до 120 слов и короткие свободные уточнения. Это инструкция модели, не гарантия её соблюдения. Не добавляются общие альтернативы, команды и повторный полный отчёт к краткому ответу.
- В инструкции явно указано: exit code 137 сам по себе не исключает и не подтверждает OOM; текущие агрегаты кластера не исключают прошлого давления на конкретный узел.

В новом примере пользователя Astra начала генерировать ответ, затем произошёл обрыв/ошибка чтения потока. Этот выпуск исправляет представление такого случая; первопричина сетевой ошибки не установлена и транспорт не объявлен исправленным. Присланный текст содержит старые элементы UI; фактическая версия на p0-app5000 требует проверки через `/healthz` и `check`, а после обновления — Ctrl+F5.

## Обновить существующий Pulse

Скачайте из приватного GitHub `releases/pulse-clusters-0.14.6-complete.tar.gz` и соседний `.sha256`. Передайте оба файла в `/home/dito` на сервер Pulse. Команды выполняются от root и рассчитаны на подтверждённое ранее имя контейнера `pulse-clusters-8090-pulse-1`. Если имя изменилось, уточните его через `docker ps` и замените в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.14.6-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.14.6 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.14.6-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.14.6
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0146-$(date +%Y%m%d-%H%M%S)"
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

Откройте страницу с **Ctrl+F5**. `check` должен подтвердить 0.14.6 / f24a66d855914eac, а `doctor` — текущие CPU/RAM/root filesystem для ожидаемых узлов. Неуспех `doctor` — причина проверить конкретный источник, его сеть, labels и RBAC; переустановка не исправляет неправильный токен или недоступный адрес.

## Откат

В `previous-paths.txt` первая строка — прежний каталог приложения, вторая — действующий runtime. Подставьте выведенный путь backup:

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0146-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
/opt/pulse-clusters-0.14.6/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Схема данных не менялась: для отката обновления применяется тот же runtime. Восстановление самого backup — отдельная операция, когда действительно нужно вернуть прежние данные.

## Пустой сервер и перенос

Для новой установки после проверки архива/MANIFEST:

```bash
cd /opt/pulse-clusters-0.14.6
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md), база знаний — [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md), внутренний HTTPS-домен — [DEPLOY-DOMAIN-RU](DEPLOY-DOMAIN-RU.md). A-запись задаётся на адрес сервера во внутреннем DNS; Caddy принимает HTTPS и передаёт запросы на loopback Pulse. Для внутренней CA клиентам нужно доверие к её сертификату.

Backup Pulse включает SQLite, каталог документов и ключи, но не Docker volumes Qdrant/Ollama/Caddy. На другом сервере отдельно переносятся эти volumes либо переиндексируются документы, загружаются веса embeddings и настраивается доверие новой CA. Не удаляйте прежние volumes при обычном обновлении приложения.

В качестве приёмки ИИ проверьте один известный инцидент: выбранные namespace/name/UID, status.message, фактически полученные логи/события и ссылки на E##. Проверка соединения не оценивает истинность диагноза. Истёкший Astra AI token требует замены; локальная модель выбирается явно.
