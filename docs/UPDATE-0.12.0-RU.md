# Pulse 0.12.0 — обновлённый Обзор

Главный экран показывает состояние и основные характеристики кластера без длинных таблиц. Подробности открываются по нажатию, настройки подключений сохраняются. [Описание и снимки экрана](OVERVIEW-RU.md).

- Вверху: версия Kubernetes API отдельно от kubelet, CPU/RAM capacity, namespaces и время снимка.
- Три карточки: Ready-узлы, здоровые Pod относительно **всех** Pod, критические сигналы. В Pods видна разбивка Ready / проверенные Completed / Failed.
- CPU/RAM/корневая ФС: общий процент, история и текущий пик по отдельным узлам. Карточка подсвечивается по худшему значению; цвет кольца относится к общему проценту, цвет строки «Пик» — к наиболее загруженному узлу.
- Семь workloads в одной строке на desktop; проблемы с поиском, фильтром и контекстным ИИ ✧.
- Кнопка резервирования показывает узлы с CPU requests ≥90% allocatable или RAM limits >100%. Это объявленный бюджет, а не фактическая утилизация.

## Обновление на сервере 10.129.160.10

Подходит для новой реализации Pulse 0.6.1–0.11.1 с CLI backup/restore. Архив не включает runtime пользователя, Docker-образы и веса моделей. Вторая установка на том же порту не запускается до остановки старой.

Скачайте `releases/pulse-clusters-0.12.0-complete.tar.gz` и соседний `.sha256`. Передайте оба файла:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.12.0-complete.tar.gz" dito@10.129.160.10:/home/dito/
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.12.0-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
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
sha256sum -c pulse-clusters-0.12.0-complete.tar.gz.sha256
tar --keep-old-files -xzf pulse-clusters-0.12.0-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.12.0
sha256sum -c MANIFEST.sha256
./pulse init --port 8090
./pulse build
pulse_backup_dir="/root/pulse-backup-0120-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$pulse_backup_dir"
printf '%s\n%s\n' "$pulse_old_dir" "$pulse_old_runtime" > "$pulse_backup_dir/previous-paths.txt"
cd "$pulse_old_dir"
./pulse --runtime "$pulse_old_runtime" backup --output-dir "$pulse_backup_dir"
pulse_backup_files=("$pulse_backup_dir"/pulse-*.tar.gz)
test "${#pulse_backup_files[@]}" -eq 1
test -f "${pulse_backup_files[0]}"
./pulse --runtime "$pulse_old_runtime" stop
cd /opt/pulse-clusters-0.12.0
./pulse restore "${pulse_backup_files[0]}" --confirm-restore
./pulse start-offline
./pulse check
printf 'Backup: %s\n' "$pulse_backup_dir"
)
```

При ошибке остановитесь, не удаляйте runtime. Старый пароль и подключения восстанавливаются штатным backup/restore. Откат: остановить новую версию и запустить прежнюю с путями из backup/previous-paths.txt. Настройки ИИ Astra остаются по [инструкции 0.11.0](UPDATE-0.11.0-RU.md).

После `./pulse check` ожидается версия **0.12.0**. Откройте панель через прежний SSH-туннель, нажмите Ctrl+F5 и дождитесь нового снимка (обычно 30 секунд). `/version` читается дополнительно; если reader не имеет доступа, будет «API —», а ресурсы останутся доступны. Версия API никогда не подменяется версией kubelet.

Если CPU/RAM/диски показывают «—», источник не подтвердил значения: проверьте сохранённый Prometheus/VictoriaMetrics кнопкой в настройках кластера. Для PeopleHub остаётся применимой [инструкция настройки источника](UPDATE-0.11.1-RU.md). Обновление интерфейса само по себе не создаёт доступ к Prometheus.

Проверки и ограничения: [QA-RU](QA-RU.md). Установка на вашем сервере выполняется этими командами; из облачной среды она не изменялась.
