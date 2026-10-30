# Pulse 0.13.0 — применить обновление на 10.129.160.10

Выпуск добавляет временный анализ по ✧, отдельный общий чат, прямую локальную LLM, Qdrant и локальные embeddings, официальный корпус Kubernetes 1.30, загрузчик VK и внутренний HTTPS-домен.

Сначала обновите само приложение с резервной копией. Затем включите Qdrant/модель и HTTPS — эти сервисы не запускаются автоматически при обновлении. Внешняя Astra и старые настройки кластера не отключаются без вашего действия.

Скачайте `releases/pulse-clusters-0.13.0-complete.tar.gz` и соседний `.sha256`. Передайте оба файла:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.13.0-complete.tar.gz" dito@10.129.160.10:/home/dito/
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.13.0-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
```

На сервере от root, после передачи:

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_old_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_old_runtime/registry.key"
printf 'Текущая установка: %s\n' "$pulse_old_dir"
cd /home/dito
sha256sum -c pulse-clusters-0.13.0-complete.tar.gz.sha256
tar --keep-old-files -xzf pulse-clusters-0.13.0-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.13.0
sha256sum -c MANIFEST.sha256
./pulse init --port 8090
./pulse build
pulse_backup_dir="/root/pulse-backup-0130-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$pulse_backup_dir"
printf '%s\n%s\n' "$pulse_old_dir" "$pulse_old_runtime" > "$pulse_backup_dir/previous-paths.txt"
cd "$pulse_old_dir"
./pulse --runtime "$pulse_old_runtime" backup --output-dir "$pulse_backup_dir"
pulse_backup_files=("$pulse_backup_dir"/pulse-*.tar.gz)
test "${#pulse_backup_files[@]}" -eq 1
test -f "${pulse_backup_files[0]}"
./pulse --runtime "$pulse_old_runtime" stop
cd /opt/pulse-clusters-0.13.0
./pulse restore "${pulse_backup_files[0]}" --confirm-restore
./pulse start-offline
./pulse check
printf 'Backup: %s\n' "$pulse_backup_dir"
)
```

При ошибке остановитесь, не удаляйте runtime. Старый пароль и подключения восстанавливаются штатным backup/restore. Откат: остановить новую версию и запустить прежнюю с путями из backup/previous-paths.txt. Настройки ИИ Astra остаются по [инструкции 0.11.0](UPDATE-0.11.0-RU.md).


После запуска `./pulse check` должен показать **0.13.0**. Откройте страницу и нажмите Ctrl+F5. Проверка сразу: ✧ → вопрос → закрыть → ✧; диалог должен быть пустым, без нового сохранённого расследования.

Далее последовательно:

1. [Включить локальную LLM, Qdrant и индексировать документы](AI-KNOWLEDGE-RU.md).
2. [Создать внутреннюю A-запись и запустить HTTPS](DEPLOY-DOMAIN-RU.md).
3. Выполнить live-acceptance с проверкой сертификата и реальных источников.

На новом сервере используйте [START-HERE](../START-HERE-RU.md), а не восстановление поверх неизвестной установки. Архив включает Python-зависимости и официальный текстовый корпус; Docker Engine, Docker-образы сервисов, веса моделей, ваши credentials и DNS-запись в нём отсутствуют.

[Проверки и ограничения выпуска](QA-RU.md). С вашей инфраструктурой из этой облачной среды соединения нет; установка/DNS здесь не изменялись.
