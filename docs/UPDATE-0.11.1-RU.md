# Pulse 0.11.1 — сохранение метрик без ложного сообщения об успехе

Воспроизведён сценарий: HTTP-транспорт с пустым URL и заполненными параметрами Kubernetes давал успешное сохранение общего подключения, но секция metrics вообще не отправлялась. При повторном открытии пользователь видел значения по умолчанию. Полная корректная конфигурация Kubernetes на стенде сохранялась и в 0.11.0; этот отчёт не доказывает, какой именно сценарий был на удалённом сервере пользователя.

Исправлено:

- Показываются только поля выбранного транспорта.
- Изменённая, но неполная конфигурация блокирует сохранение и выводит видимую ошибку внизу формы.
- После PUT/POST настройки сравниваются с новым GET /api/clusters. Успех показывается только при совпадении публичных полей transport/namespace/service/port/mode/labels или URL.
- Кнопка проверки источника предупреждает о несохранённых изменениях.
- Проверены повторное открытие, перезагрузка страницы, изменение имени и несовместимый/старый ответ API. Пароли и токены при обратном чтении не возвращаются.

## Быстро подключить PeopleHub на действующей установке

Скачайте из GitHub только `scripts/configure-peoplehub-metrics.py` через Download raw file. Это stdlib-скрипт, установка Python-пакетов не нужна. Он предназначен для вашего подтверждённого отдельного Prometheus PeopleHub; для общего хранилища с несколькими кластерами этот preset применять нельзя.

В PowerShell:

```powershell
scp "$env:USERPROFILE\Downloads\configure-peoplehub-metrics.py" dito@10.129.160.10:/home/dito/
```

На сервере Pulse 10.129.160.10 от root:

```bash
pulse_runtime_path="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
python3 /home/dito/configure-peoplehub-metrics.py --runtime "$pulse_runtime_path" --cluster peoplehub
```

Скрипт использует известный локальный пароль Pulse для его штатного API на localhost. Пароль не печатается. PATCH-поведение реализовано PUT с единственным полем metrics: другие интеграции, reader kubeconfig и история не меняются. Задаются transport=kubernetes, namespace=monitoring, service=kube-prom-kube-prometheus-prometheus, port=9090, mode=dedicated, labels={}. Прежние credentials прямого Prometheus URL не используются для Kubernetes proxy. Скрипт читает ответ обратно и завершается ошибкой, если API не подтверждает конфигурацию.

Ожидается «Настройки метрик подтверждены». После этого Ctrl+F5 → настройки PeopleHub → Проверить сохранённый источник. Ваш управляющий узел уже подтвердил 6/6 для CPU/RAM/дисков; реальный reader token и доступ из контейнера Pulse ещё проверяются этой кнопкой. При 401 проверьте срок токена. Скрипт не изменяет Kubernetes RBAC и не выполняет shell/SQL в кластере.

## Обновить форму приложения до 0.11.1

Скачайте `releases/pulse-clusters-0.11.1-complete.tar.gz` и соседний `.sha256`. Передайте оба файла:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.11.1-complete.tar.gz" dito@10.129.160.10:/home/dito/
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.11.1-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
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
sha256sum -c pulse-clusters-0.11.1-complete.tar.gz.sha256
tar --keep-old-files -xzf pulse-clusters-0.11.1-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.11.1
sha256sum -c MANIFEST.sha256
./pulse init --port 8090
./pulse build
pulse_backup_dir="/root/pulse-backup-0110-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$pulse_backup_dir"
printf '%s\n%s\n' "$pulse_old_dir" "$pulse_old_runtime" > "$pulse_backup_dir/previous-paths.txt"
cd "$pulse_old_dir"
./pulse --runtime "$pulse_old_runtime" backup --output-dir "$pulse_backup_dir"
pulse_backup_files=("$pulse_backup_dir"/pulse-*.tar.gz)
test "${#pulse_backup_files[@]}" -eq 1
test -f "${pulse_backup_files[0]}"
./pulse --runtime "$pulse_old_runtime" stop
cd /opt/pulse-clusters-0.11.1
./pulse restore "${pulse_backup_files[0]}" --confirm-restore
./pulse start-offline
./pulse check
printf 'Backup: %s\n' "$pulse_backup_dir"
)
```

При ошибке остановитесь, не удаляйте runtime. Старый пароль и подключения восстанавливаются штатным backup/restore. Откат: остановить новую версию и запустить прежнюю с путями из backup/previous-paths.txt. Настройки ИИ Astra остаются по [инструкции 0.11.0](UPDATE-0.11.0-RU.md).

Проверки: 166 pytest, браузерные сценарии настроек метрик, ИИ Astra, общего UI и компактного Обзора; helper проверен через реальный локальный API на одноразовом кластере. Реальная конфигурация пользователя отсюда не изменялась.
