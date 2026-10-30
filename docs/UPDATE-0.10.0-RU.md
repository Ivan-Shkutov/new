# Pulse 0.10.0: доказательства расследования, все Pod и метрики ClusterIP

## Что изменилось

- **Pods здоровы / все:** Running с Ready=True плюс Succeeded с подтверждёнными успешными завершениями контейнеров. Failed/Evicted учитываются в общем числе и дают красную карточку; Completed не засчитывается без проверяемого exitCode. Например, 145 Ready + 17 успешных Completed + 1 Failed → **162/163**, красный. Сохранённый Failed не означает автоматически продолжающийся отказ приложения: отдельно проверяется замена.
- Перед анализом выбранного Pod Pulse собирает живые доказательства: status.reason/message, события UID, фактический Node, ownerReferences до Deployment/Job, ReplicaSets и замены, PVC, current/previous logs контейнеров, историю метрик, логи Astra при наличии настройки. Проверки ограничены бюджетом/лимитами; отсутствующие сведения не выдаются за полученные.
- Журнал E01… сохраняется с путями/методами API, временем, результатами/ошибками и эквивалентами kubectl. Shell не запускается; infrastructure API используются только для чтения. Существующий поиск Astra — POST с семантикой чтения. Журнал сохраняется и при отказе LLM.
- Меню ✧: Об инструменте / Сжатый анализ / Расширенный анализ / Причины / Последствия / **Нужные команды**. Исправляющие действия только предлагаются оператору отдельно от диагностики; не выполняются.
- Метрики можно читать через Kubernetes API до конкретного Prometheus ClusterIP service. Появились поиск сервиса и проверка источника. HTTP 403/401, пустые серии и отсутствие exporter показываются как ошибки/пробелы, не как ноль.

[Расследование конкретного Pod и команды администратора](PEOPLEHUB-INVESTIGATION-RU.md). [Как изучить API ИИ Astra](ASTRA-AI-RU.md).

**Собственный ИИ Astra ещё не подключён к кнопке Pulse:** известен адрес интерфейса, но не предоставлены контракт endpoint/авторизация/формат ответа. Меню и автоматический сбор доказательств работают с вашим сохранённым qwen/Holmes. Существующий адаптер логов Astra требует проверки по API именно вашей версии; его совместимость с вашим сервером здесь не подтверждена.

## Обновиться с 0.8.0 или 0.9.0

Скачайте из приватного GitHub через Download raw file:

- `releases/pulse-clusters-0.10.0-complete.tar.gz`;
- `releases/pulse-clusters-0.10.0-complete.tar.gz.sha256`.

На Windows передайте оба файла:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.10.0-complete.tar.gz" dito@10.129.160.10:/home/dito/
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.10.0-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
```

На сервере 10.129.160.10 от root. Команды читают фактические пути текущего контейнера; прежние каталоги не удаляются:

```bash
(
set -eu
umask 077
pulse_old_dir="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}')"
pulse_old_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
test -f "$pulse_old_dir/pulse"
test -f "$pulse_old_runtime/registry.key"
printf 'Текущая установка: %s\nRuntime: %s\n' "$pulse_old_dir" "$pulse_old_runtime"

cd /home/dito
sha256sum -c pulse-clusters-0.10.0-complete.tar.gz.sha256
tar --keep-old-files -xzf pulse-clusters-0.10.0-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.10.0
sha256sum -c MANIFEST.sha256
./pulse init --port 8090
./pulse build

pulse_backup_dir="/root/pulse-backup-0100-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$pulse_backup_dir"
printf '%s\n%s\n' "$pulse_old_dir" "$pulse_old_runtime" > "$pulse_backup_dir/previous-paths.txt"
cd "$pulse_old_dir"
./pulse --runtime "$pulse_old_runtime" backup --output-dir "$pulse_backup_dir"
pulse_backup_files=("$pulse_backup_dir"/pulse-*.tar.gz)
test "${#pulse_backup_files[@]}" -eq 1
test -f "${pulse_backup_files[0]}"
./pulse --runtime "$pulse_old_runtime" stop

cd /opt/pulse-clusters-0.10.0
./pulse restore "${pulse_backup_files[0]}" --confirm-restore
./pulse start-offline
./pulse check
printf 'Backup: %s\n' "$pulse_backup_dir"
)
```

При ошибке остановитесь; не удаляйте runtime. Ctrl+F5 обновит интерфейс. Пароль, PeopleHub, ключ, qwen, история и настройки сохраняются; `evidence` добавляется миграцией. vLLM и отдельный Holmes этот блок не перезапускает. Старый Holmes bundle продолжает работать с прежними tools; новые `/diagnostics` и `/astra-logs` входят в новые bundle. При ручном добавлении этих путей в старый whitelist сохраните его токены и конфигурацию, проверьте контракт и перезапустите только соответствующий Holmes. Уже собранные доказательства передаются ему в контексте и без новых tool endpoints.

Откат: остановите 0.10.0, затем запустите прежний `pulse --runtime ПРЕЖНИЙ_RUNTIME start-offline` из каталога, записанного в `previous-paths.txt`. Старый runtime не изменяется миграцией нового. Новые записи 0.10.0 обратно автоматически не переносятся; предварительно сохраните отдельный backup.

## Подключить найденный Prometheus PeopleHub

Источник из вашего вывода: `monitoring/kube-prom-kube-prometheus-prometheus`, порт 9090. Сервисы coredns/operator/alertmanager/node-exporter сами по себе не являются API Prometheus. Headless VictoriaMetrics 8428 — альтернативный источник, но наличие сервиса ещё не подтверждает наличие в нём node_exporter метрик.

На управляющем узле PeopleHub, с выбранным правильным административным context, перенесите из нового проекта **только** `deploy/metrics-proxy-peoplehub.yaml`, проверьте его содержимое и выполните:

```bash
kubectl config current-context
kubectl apply -f metrics-proxy-peoplehub.yaml
kubectl auth can-i get 'services/http:kube-prom-kube-prometheus-prometheus:9090' \
  --subresource=proxy -n monitoring \
  --as=system:serviceaccount:pulse-observer:pulse-reader
```

Если вы выполняете из корня checkout, путь apply — `deploy/metrics-proxy-peoplehub.yaml`. Это однократное административное предоставление GET к **конкретному** service proxy в namespace monitoring. AI не выдаёт себе права. Нет новых прав на Secrets, exec, nodes/proxy, write или произвольные сервисы. Ваш текущий Role pulse-reader сам по себе не содержит services/proxy.

Проверка настоящим reader kubeconfig (если файл существует и токен ещё действителен):

```bash
kubectl --kubeconfig /root/peoplehub-reader.json get --raw \
  '/api/v1/namespaces/monitoring/services/http:kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=node_memory_MemTotal_bytes'
```

В Pulse → Настройки → PeopleHub → Метрики:

1. Подключение **Через Kubernetes API**.
2. Namespace `monitoring`, Service `kube-prom-kube-prometheus-prometheus`, порт `9090`. Либо «Найти в кластере» и выбрать этот сервис.
3. Если это Prometheus только PeopleHub — явно выберите **Выделенный**. Для общего источника укажите реальные labels, иначе чужие данные смешаются. Выделенность не определяется автоматически по имени сервиса.
4. Сохраните кластер. Откройте настройки заново → **Проверить сохранённый источник**. Должны быть непустые node_memory_MemTotal_bytes.
5. Откройте Обзор и CPU/RAM/диски. Наличие истории зависит от scrape/retention. Диски в общем круге — `/` узлов; PVC и другие mountpoints исследуются отдельно.

Если 401 — проверьте срок Kubernetes reader token; он раньше выдавался на 24 часа. Если 403 — RoleBinding/точное имя services/proxy и namespace. Если 404 — имя/порт service и API route. Если HTTP 200 с пустой выборкой — exporter, targets и scope labels. ClusterIP не обязан быть доступен напрямую с 10.129.160.10; выбранный режим использует уже доступный API Kubernetes и его CA/token.

## Приёмка

Проверьте число Pod сравнением с `kubectl get pods -A`. Для Evicted нажмите ✧ → Расширенный анализ, откройте журнал E01… и проверьте status.message, фактический Node, владельца и замену. Отдельно нажмите «Нужные команды». Недоступная Astra или история должна быть явно отмечена.

**151 тест прошёл**, три браузерных сценария и Docker проверены. На отдельном Docker-стенде проверен backup/restore 0.9.0 → 0.10.0 с сохранением данных. [Пример окна анализа](images/compact-0100/ai-context.png) — явно заданные тестовые данные, не диагноз пользователя.

Тесты и браузерные сценарии описаны в [QA-RU](QA-RU.md). Живые Kubernetes/Prometheus/Astra/vLLM пользователя отсюда не проверялись; работа собственного ИИ Astra не заявляется. Single-process SQLite и один администратор сохраняются; неограниченный анализ и гарантированно точный диагноз не обещаются.
