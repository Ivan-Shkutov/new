# Pulse 0.14.3 — подготовка к повторному развёртыванию

Точная сборка приложения: **0.14.3 / d149f40d51d0fe59**. Build ID должен совпасть в `/healthz`, `RELEASE.json` и `./pulse check`.

## Что исправлено после 0.14.2

- При восстановлении `https/Caddyfile` остаётся доступным Caddy с ограниченными capabilities. Пароль, ключ реестра и остальные файлы runtime остаются закрытыми.
- Некорректные UID/GID/порт, формат ключа и короткий пароль в backup проверяются до замены действующего runtime. Пустой файл SHA256 даёт понятную ошибку. Это проверка целостности/формата, а не гарантия подлинности полученного извне backup.
- Локальная команда `check` обращается к loopback напрямую: переменные HTTP_PROXY не перенаправляют локальную авторизацию.
- `knowledge-start` заполняет отсутствующие и пустые настройки `.env`, сохраняя явно заданные значения. `knowledge-check` сверяет точный тег и закреплённый digest embeddings; другая версия модели с тем же именем не считается готовой.
- Сохранение проверенного источника метрик и генерация Holmes выполняют сравнение конфигурации под блокировкой записи SQLite. Генерация Holmes не перезаписывает параллельно изменённые метрики. Незавершённая запись файлов/настроек не оставляет комплект с непригодными токенами, блокирующий повторную попытку.
- В анализе ИИ различаются недоступные и неполные проверки. При смене UID факт нового Pod явно исключается и не помечается полным доказательством старой ошибки.
- Некорректные метаданные Qdrant и каталог embedding-моделей дают явную ошибку источника. HTTP-код сохраняется без раскрытия тела ответа; boolean вместо численного embedding отклоняется.

Исправления метрик/контекста 0.14.2 включены. Набор выполненных проверок и границы — в [QA-RU](QA-RU.md). Действующая установка пользователя из среды разработки не изменялась.

## Обновить существующий Pulse

Скачайте из приватного GitHub `releases/pulse-clusters-0.14.3-complete.tar.gz` и соседний `.sha256`. Передайте оба файла в `/home/dito` на сервер Pulse. Команды выполняются от root и рассчитаны на подтверждённое ранее имя контейнера `pulse-clusters-8090-pulse-1`. Если имя изменилось, уточните его через `docker ps` и замените в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.14.3-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.14.3 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.14.3-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.14.3
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0143-$(date +%Y%m%d-%H%M%S)"
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

Откройте страницу с **Ctrl+F5**. `check` должен подтвердить 0.14.3 / d149f40d51d0fe59, а `doctor` — текущие CPU/RAM/root filesystem для ожидаемых узлов. Неуспех `doctor` — причина проверить конкретный источник, его сеть, labels и RBAC; переустановка не исправляет неправильный токен или недоступный адрес.

## Откат

В `previous-paths.txt` первая строка — прежний каталог приложения, вторая — действующий runtime. Подставьте выведенный путь backup:

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0143-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
/opt/pulse-clusters-0.14.3/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Схема данных не менялась: для отката обновления применяется тот же runtime. Восстановление самого backup — отдельная операция, когда действительно нужно вернуть прежние данные.

## Пустой сервер и перенос

Для новой установки после проверки архива/MANIFEST:

```bash
cd /opt/pulse-clusters-0.14.3
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md), база знаний — [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md), внутренний HTTPS-домен — [DEPLOY-DOMAIN-RU](DEPLOY-DOMAIN-RU.md). A-запись задаётся на адрес сервера во внутреннем DNS; Caddy принимает HTTPS и передаёт запросы на loopback Pulse. Для внутренней CA клиентам нужно доверие к её сертификату.

Backup Pulse включает SQLite, каталог документов и ключи, но не Docker volumes Qdrant/Ollama/Caddy. На другом сервере отдельно переносятся эти volumes либо переиндексируются документы, загружаются веса embeddings и настраивается доверие новой CA. Не удаляйте прежние volumes при обычном обновлении приложения.

В качестве приёмки ИИ проверьте один известный инцидент: выбранные namespace/name/UID, status.message, фактически полученные логи/события и ссылки на E##. Проверка соединения не оценивает истинность диагноза. Истёкший Astra AI token требует замены; локальная модель выбирается явно.
