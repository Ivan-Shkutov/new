# Обновление Pulse до 0.15.0

В 0.15.0 основной экран диагностики использует шесть фиксированных сценариев: краткий и подробный разбор, причины, последствия, план устранения и проверка восстановления. У каждого сценария свой набор чтений API и ограниченный формат результата. Ответы не образуют растущую историю чата. Проверенные факты сохраняются при ошибке модели, подробные источники свёрнуты. [Описание сценариев и приёмка](SCENARIOS-RU.md).

Точная сборка: **0.15.0 / 4eb526ece01ebd14**. Сборка проверяется по `RELEASE.json`, `/healthz` и `./pulse check`: версия и build ID должны совпадать. Наличие новой версии в GitHub не означает, что сервер уже обновлён. Реальные Kubernetes, Astra, ваша локальная модель и Holmes требуют проверки с сервера Pulse.

## Файлы

Скачайте из приватного репозитория оба файла:

- [pulse-clusters-0.15.0-complete.tar.gz](https://github.com/Ivan-Shkutov/new/blob/main/releases/pulse-clusters-0.15.0-complete.tar.gz)
- [pulse-clusters-0.15.0-complete.tar.gz.sha256](https://github.com/Ivan-Shkutov/new/blob/main/releases/pulse-clusters-0.15.0-complete.tar.gz.sha256)

В GitHub используйте Download raw file. Передайте оба файла на `p0-app5000` в `/home/dito`. Архив содержит исходники и Linux amd64 wheelhouse; базовый Python-образ Dockerfile должен быть доступен либо загружен заранее. Настройки и токены из действующего runtime в архив не входят.

## Обновление действующей установки

Выполните весь блок от root. Он определяет прежний каталог приложения и **действующий runtime** через Docker, собирает новую версию, создаёт защищённый backup и запускает приложение с теми же данными. Известное имя контейнера — `pulse-clusters-8090-pulse-1`. Если оно изменилось, найдите имя через `docker ps` и замените его в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.15.0-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.15.0 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.15.0-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.15.0
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0150-$(date +%Y%m%d-%H%M%S)"
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

При ошибке остановитесь на этом шаге. Не удаляйте runtime и не запускайте `init` поверх действующих данных. Если каталог 0.15.0 уже существует, его содержимое проверяется по MANIFEST. Изменённые или неполные файлы не следует молча перезаписывать.

Запишите выведенный путь backup. Backup содержит ключи доступа, поэтому хранится в каталоге root с ограниченными правами. Старый каталог приложения и его Docker-образ сохраняются для отката. Обновление не переключает провайдера ИИ и не меняет веса модели. Настройки Astra AI, Holmes и метрик сохраняются вместе с runtime.

## Проверка результата

Обновите страницу с **Ctrl+F5** и откройте **«Диагностика»**. Проверьте версию 0.15.0 и build ID из `RELEASE.json`; `./pulse check` должен подтвердить ту же сборку. На экране должны быть шесть сценариев и один заменяемый результат. Раздел [приёмки](SCENARIOS-RU.md#короткая-приёмка-на-вашем-сервере) описывает контрольный инцидент, UID, ошибки источников и восстановление.

`doctor` проверяет доступ к сохранённым источникам, но не исправляет токены, RBAC, labels и сетевые маршруты. Отказ Astra AI HTTP 403 требует проверки прав и токена. Успешное соединение с LLM не доказывает правильность её диагноза; сверяйте вывод с источниками `E##`.

По желанию отдельно проверьте сохранённую локальную модель на учебных случаях:

```bash
cd /opt/pulse-clusters-0.15.0
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
./pulse --runtime "$pulse_runtime" llm-check --show-answers
```

Отчёт создаётся в `<runtime>/llm-check-report.json`. Команда явно использует локальную LLM; это проверка модели, а не полная приёмка сценариев или Holmes.

## Откат

Подставьте путь backup, напечатанный при обновлении. Первая строка `previous-paths.txt` содержит старый каталог приложения, вторая — его runtime.

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0150-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
/opt/pulse-clusters-0.15.0/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Сценарии используют временные сессии без миграции архива расследований. Для отката приложения используется тот же runtime. Восстановление backup нужно только при необходимости вернуть сами данные; это отдельная операция. Обычный откат не должен терять настройки, сделанные после обновления.

## Установка на пустом сервере

После проверки архива и MANIFEST выполните из `/opt/pulse-clusters-0.15.0`:

```bash
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU.md](CONNECT-CLUSTER-RU.md), локальная модель — [LLM-LOCAL-RU.md](LLM-LOCAL-RU.md), HolmesGPT — [HOLMES-RU.md](HOLMES-RU.md). Для Holmes используйте путь реально созданного runtime вместо путей старых релизов из примеров. Qdrant и модель embeddings подключаются отдельно, [AI-KNOWLEDGE-RU.md](AI-KNOWLEDGE-RU.md).

Backup Pulse не включает внешние Docker volumes Qdrant, Ollama и Caddy. При переносе на другой сервер их переносят отдельно либо восстанавливают из соответствующих резервных копий. Прежние volumes при обновлении Pulse не удаляются.
