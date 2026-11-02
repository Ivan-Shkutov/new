# Pulse 0.14.7 — единые правила локальной LLM для DevOps

Точная сборка приложения: **0.14.7 / 39e2f01092c3446a**. Build ID должен совпасть в `/healthz`, `RELEASE.json` и `./pulse check`.

## Что изменилось после 0.14.6

- Общий профиль Senior DevOps/SRE для Kubernetes, Linux и сетей применяется и в обычном чате, и в анализе проблемы. Убрана замена полной инструкции общим кратким prompt, из-за которой в чате терялись правила диагностики. Роль задаёт стандарт ответа, а не личный стаж модели.
- В пакет добавлены реальные автоматически подключаемые правила `pulse_app/ai_rules/BASE.md`, `KUBERNETES.md`, `NETWORK.md`. Они включаются в system сообщения и в build ID. Сетевые правила добавляются к соответствующему вопросу; выбор сценария опирается на симптом/вопрос, а не имена полей nodeName или общие заметки о Pressure.
- По умолчанию обычный ответ до 150 слов; сжатый анализ — таблица из четырёх строк до 120 слов, расширенный — до 350. Это инструкции модели, не универсальный валидатор. Факты/гипотезы и первая полезная проверка отделяются; journal не пересказывается.
- Локальный адаптер отделяет один ведущий `<think>...</think>` от итогового текста; reasoning-only/незавершённый thinking не выдаются за ответ. Режим самой модели и её веса не меняются. Обрыв/length остаются неуспехом.
- Новая команда `./pulse llm-check` проверяет настоящую сохранённую локальную модель через четыре временных диалога. Отчёт содержит ответы и критерии ручной оценки; автоматическое завершение генерации не объявляется подтверждённой точностью.

[Полная настройка, правила, рекомендации и критерии приёмки](LLM-LOCAL-RU.md). Для использования локальной модели выберите «Локальная LLM напрямую» в анализе либо отключите Astra AI/Holmes в настройках Peoplehub. Сохранённые провайдеры обновление не переключает скрытно.

## Обновить существующий Pulse

Скачайте из приватного GitHub `releases/pulse-clusters-0.14.7-complete.tar.gz` и соседний `.sha256`. Передайте оба файла в `/home/dito` на сервер Pulse. Команды выполняются от root и рассчитаны на подтверждённое ранее имя контейнера `pulse-clusters-8090-pulse-1`. Если имя изменилось, уточните его через `docker ps` и замените в двух строках `inspect`.

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_runtime/registry.key"
cd /home/dito
sha256sum -c pulse-clusters-0.14.7-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.14.7 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.14.7-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.14.7
sha256sum -c MANIFEST.sha256
./pulse --runtime "$pulse_runtime" build
pulse_backup_dir="/root/pulse-backup-0147-$(date +%Y%m%d-%H%M%S)"
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

Откройте страницу с **Ctrl+F5**. `check` должен подтвердить 0.14.7 / 39e2f01092c3446a, а `doctor` — текущие CPU/RAM/root filesystem для ожидаемых узлов. Неуспех `doctor` — причина проверить конкретный источник, его сеть, labels и RBAC; переустановка не исправляет неправильный токен или недоступный адрес.

После проверки приложения выберите локальную модель и выполните контрольные сценарии из этого же каталога:

```bash
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
./pulse --runtime "$pulse_runtime" llm-check --show-answers
```

Отчёт: `<runtime>/llm-check-report.json`. Проверка явно использует локальную LLM, учебные факты и временные сессии. Для реального known incident отдельно проверьте UID, E## и готовность замены. После изменения правил приложение нужно пересобрать, веса vLLM перезапускать ради этого не требуется.

## Откат

В `previous-paths.txt` первая строка — прежний каталог приложения, вторая — действующий runtime. Подставьте выведенный путь backup:

```bash
(
set -eu
pulse_backup_dir=/root/pulse-backup-0147-ДАТА-ВРЕМЯ
pulse_old_dir="$(sed -n '1p' "$pulse_backup_dir/previous-paths.txt")"
pulse_runtime="$(sed -n '2p' "$pulse_backup_dir/previous-paths.txt")"
test -f "$pulse_old_dir/pulse"
/opt/pulse-clusters-0.14.7/pulse --runtime "$pulse_runtime" stop
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" start-offline
"$pulse_old_dir/pulse" --runtime "$pulse_runtime" check
)
```

Схема данных не менялась: для отката обновления применяется тот же runtime. Восстановление самого backup — отдельная операция, когда действительно нужно вернуть прежние данные.

## Пустой сервер и перенос

Для новой установки после проверки архива/MANIFEST:

```bash
cd /opt/pulse-clusters-0.14.7
./pulse init --port 8090
./pulse build
./pulse start-offline
./pulse check
```

Подключение кластера — [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md), база знаний — [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md), внутренний HTTPS-домен — [DEPLOY-DOMAIN-RU](DEPLOY-DOMAIN-RU.md). A-запись задаётся на адрес сервера во внутреннем DNS; Caddy принимает HTTPS и передаёт запросы на loopback Pulse. Для внутренней CA клиентам нужно доверие к её сертификату.

Backup Pulse включает SQLite, каталог документов и ключи, но не Docker volumes Qdrant/Ollama/Caddy. На другом сервере отдельно переносятся эти volumes либо переиндексируются документы, загружаются веса embeddings и настраивается доверие новой CA. Не удаляйте прежние volumes при обычном обновлении приложения.

В качестве приёмки ИИ проверьте один известный инцидент: выбранные namespace/name/UID, status.message, фактически полученные логи/события и ссылки на E##. Проверка соединения не оценивает истинность диагноза. Истёкший Astra AI token требует замены; локальная модель выбирается явно.
