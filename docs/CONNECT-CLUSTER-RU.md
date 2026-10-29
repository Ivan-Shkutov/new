# Подключение кластера и проверка всей цепочки

Инструкция для Pulse 0.6.1 на 10.129.160.10. Рабочая панель сама по себе не подтверждает подключение Kubernetes, метрик, Astra и LLM. Выполняйте этапы по порядку и останавливайтесь при ошибке.

## 1. Найти административный kubectl

На сервере:

```bash
command -v kubectl
kubectl config current-context
kubectl get nodes -o wide
```

Если kubectl отсутствует, context не выбран или доступ запрещён, следующие административные действия выполните на доверенной машине/управляющем узле, где kubectl уже управляет нужным кластером. Перенесите туда только `deploy/rbac-readonly.yaml` и `scripts/export-kubeconfig.py`. Не переносите admin kubeconfig в Pulse. Проверьте, что выбран именно целевой кластер; не переключайте context между командами.

## 2. Создать права чтения

Если kubectl настроен на сервере Pulse:

```bash
cd /opt/pulse-clusters-0.6.1
kubectl config current-context
kubectl apply -f deploy/rbac-readonly.yaml
```

Команда создаёт namespace pulse-observer, service account pulse-reader и роль чтения. Никакие workload не изменяются.

**Для ранее скачанного complete-архива 0.6.1 нужна поправка:** исходный RBAC пропускал ReplicationController и PodDisruptionBudget. Текущий файл в main исправлен; для старого архива добавьте эти два разрешения:

```bash
kubectl patch clusterrole pulse-reader --type=json -p='[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["replicationcontrollers"],"verbs":["get","list","watch"]}},{"op":"add","path":"/rules/-","value":{"apiGroups":["policy"],"resources":["poddisruptionbudgets"],"verbs":["get","list","watch"]}}]'
```

Проверка от имени reader:

```bash
kubectl auth can-i list pods --all-namespaces --as=system:serviceaccount:pulse-observer:pulse-reader
kubectl auth can-i list poddisruptionbudgets.policy --all-namespaces --as=system:serviceaccount:pulse-observer:pulse-reader
kubectl auth can-i get secrets --all-namespaces --as=system:serviceaccount:pulse-observer:pulse-reader
kubectl auth can-i create pods/exec --all-namespaces --as=system:serviceaccount:pulse-observer:pulse-reader
```

Ожидается yes, yes, no, no. Если impersonation запрещён вашей административной учётной записи, проверяйте созданным reader kubeconfig, а не расширяйте её права автоматически.

## 3. Экспортировать reader kubeconfig

Из каталога проекта на административной машине:

```bash
python3 scripts/export-kubeconfig.py \
  --context "$(kubectl config current-context)" \
  --duration 24h \
  --output "$HOME/peoplehub-reader.json"
```

Запрошено 24 часа; фактический срок определяет API. Файл содержит секретный токен, не загружайте его в GitHub/чат. Скрипт не перезаписывает существующий файл: для следующего экспорта выберите новое имя.

Проверка этим credential:

```bash
kubectl --kubeconfig "$HOME/peoplehub-reader.json" get nodes
kubectl --kubeconfig "$HOME/peoplehub-reader.json" get pods -A
```

Endpoint из файла должен быть доступен с 10.129.160.10. Если в административном kubeconfig loopback/API-адрес недоступной сети, при экспорте добавьте `--server https://РЕАЛЬНЫЙ-API:6443`. Используйте известный endpoint, соответствующий сертификату; не подставляйте IP сервера Pulse вместо API и не отключайте TLS.

## 4. Заполнить веб-форму

Настройки → Кластеры → «+ Добавить»:

- ID: peoplehub (или другое уникальное имя латиницей, цифрами и дефисами).
- Название: PeopleHub.
- Сбор: включён.
- Kubeconfig: загрузить созданный JSON с компьютера браузера либо вставить его содержимое в поле формы. Копируйте только в свою панель через SSH-туннель; не отправляйте credential в чат.
- Метрики и Astra пока оставить пустыми.

Сохраните. Выберите кластер слева, откройте Ресурсы → «Собрать сейчас». Один цикл ограничен по времени и может занять около минуты. Проверьте обновившееся время снимка и отсутствие ошибок 401/403/TLS/timeout.

## 5. Сопоставить факты

На административной машине выполните `kubectl get nodes`, `kubectl get namespaces`, `kubectl get pods -A` и сравните с панелью с учётом времени сбора. Для Pod посмотрите phase и Ready отдельно. Running не гарантирует готовность всех контейнеров.

У одного известного Pod нажмите «Логи», выберите контейнер и получите строки. Сравните с `kubectl logs -n ИМЯ_NAMESPACE ИМЯ_POD -c ИМЯ_КОНТЕЙНЕРА --tail=30`. Не запускайте эту команду с буквальными placeholders. Старый Warning в Events не доказывает текущую аварию. Зелёный статус сбора означает доступность наблюдений, а не исправность всех приложений.

## 6. Подключить оставшиеся источники

- Метрики: доступный Prometheus/VictoriaMetrics URL до `/api/v1/query`; реальные cluster labels для общего хранилища либо dedicated для отдельного. Проверить CPU/RAM с контрольным запросом источника. PVC usage может отсутствовать из-за exporter/CSI; это не нулевое использование.
- Astra: URL и сервисный bearer token, точные поля cluster/namespace/pod. Сравнить логи выбранного Pod с оригиналом, проверить отсутствие чужого кластера. API разных версий Astra требует проверки. Incidents API в 0.6.1 не реализован.
- LLM: на сервере `curl --fail --silent --show-error http://127.0.0.1:8002/v1/models`; взять точный id, задать URL/model в настройках, сохранить и нажать «Проверить ответ». При 401 требуется ваша авторизация vLLM; ключ не присылать в чат.
- Документы: загрузить текстовый файл без секретов с уникальным словом, проверить поиск в выбранном кластере.

## 7. Проверка проекта целиком

| Этап | Критерий |
|---|---|
| Процесс | `./pulse check` возвращает 0.6.1 и рабочий overview |
| Kubernetes | Актуальные Nodes/Pods/Namespaces и статусы согласуются с kubectl, полнота видна |
| Логи | Совпадают namespace, Pod, container и интервал; работают и без Astra |
| Метрики | Получены реальные серии, отсутствие обозначается явно |
| Модель | Тест ответа прошёл, расследование известного случая содержит реальные факты и обозначает пробелы |
| Знания | Загруженный документ находится поиском |
| История | После обновления страницы расследование доступно; можно продолжить обсуждение |
| Перезапуск | `./pulse stop`, `./pulse start-offline`, `./pulse check`; подключения и документы сохранены |
| Восстановление | Backup создан; проверка restore выполняется на отдельном runtime, не поверх рабочей установки без необходимости |

LLM получает подготовленный контекст и не исполняет исправления. После ручной проверки фактического решения сохраните подтверждённый инцидент. Проверка известного сбоя выполняется в тестовом контуре; не создавайте аварии в production.

Токен reader нужно заменить до expiry. Если после успешной работы возник 401, экспортируйте новый kubeconfig и обновите подключение. Для 403 проверьте RBAC; для TLS/timeout — endpoint, CA, доступность сети. Эти ошибки не устраняются настройкой LLM.
