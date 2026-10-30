# PeopleHub: расследование Evicted Pod только чтением

Цель: `am-agent/am-agent-am-agent-deployment-8d6ddf79-lwzwg`. Из присланного старого ответа известно лишь утверждение `Evicted`; действительные `status.message`, UID, узел и состояние замены пока не представлены. Нельзя назначать виновным control-plane по предположению. Время снимка — не время выселения. Текущие Ready/Pressure узла не восстанавливают его прошлое состояние.

Команды выполняются на машине с доступом к **нужному** контексту Kubernetes. Они ничего не изменяют. Не публикуйте необработанные JSON/логи с секретами.

```bash
kubectl config current-context
pulse_ns=am-agent
pulse_pod=am-agent-am-agent-deployment-8d6ddf79-lwzwg
kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.metadata.uid}{"\n"}{.status.phase}{"\n"}{.status.reason}{"\n"}{.status.message}{"\nnode="}{.spec.nodeName}{"\n"}'
pulse_uid="$(kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.metadata.uid}')"
pulse_node="$(kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.spec.nodeName}')"
kubectl -n "$pulse_ns" describe pod "$pulse_pod"
kubectl -n "$pulse_ns" get events --field-selector "involvedObject.uid=$pulse_uid" --sort-by=.metadata.creationTimestamp
kubectl get node "$pulse_node" -o wide
kubectl describe node "$pulse_node"
kubectl get events -A --field-selector "involvedObject.kind=Node,involvedObject.name=$pulse_node" --sort-by=.metadata.creationTimestamp
kubectl get pods -A --field-selector "spec.nodeName=$pulse_node" -o wide
kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.metadata.ownerReferences}{"\n"}{.status.containerStatuses}{"\n"}{.status.initContainerStatuses}{"\n"}'
kubectl -n "$pulse_ns" get pods,replicasets,deployments -o wide
kubectl -n "$pulse_ns" logs "$pulse_pod" --all-containers=true --tail=500 --timestamps
kubectl -n "$pulse_ns" logs "$pulse_pod" --all-containers=true --previous --tail=500 --timestamps
```

Начните с `reason/message`: для Evicted там часто прямо назван `ephemeral-storage`, memory, PID или другое основание kubelet. `OOMKilled` контейнера — другой механизм. Нехватка CPU сама по себе не является обычной причиной node-pressure eviction. HPA не выполняет выселение Pod.

`--previous` может закономерно вернуть отсутствие предыдущего контейнера. Отсутствие логов/событий после удаления и истечения retention — пробел, а не доказательство отсутствия ошибки. Если Pod удалён, имя занято другим UID или команда сообщает NotFound, не анализируйте новый объект как старый: нужны сохранённый UID, исторические события/Astra и состояние владельца.

Из ownerReferences установите ReplicaSet и затем его Deployment по реальным именам. Изучите их `get -o json` / `describe`, desired/ready/available и новые Pod **того же владельца**. Не выводите имя Deployment отрезанием суффикса. Ready замены не делает прежний Failed Pod успешным, но отделяет сохранённую ошибку от продолжающегося отказа сервиса.

В Prometheus/Astra возьмите окно вокруг реального container finishedAt/события, а не observed_at Pulse. Сопоставьте memory available, filesystem available на **фактическом узле**, container requests/limits, kubelet eviction сообщения. `kubectl top` показывает текущие значения и не доказывает исторический пик. Для дискового давления различайте корень узла, kubelet/container runtime ФС и PVC.

При необходимости администратор фактического узла отдельно читает kubelet/runtime journal за найденный интервал. Pulse не получает SSH/root, nodes/proxy, exec или права очистки файлов. Сведения вне доступных API будут отмечены недоступными. Реальные пути/имена systemd services и timezone проверяют на этом узле; они не угадываются.

Никакого удаления Pod, очистки диска, drain/reboot или изменения requests до установленной причины. «Нужные команды» в Pulse показывает диагностические команды и отдельно условные действия исправления, которые оператор оценивает перед выполнением.

## Что выполняет Pulse 0.10.0

Для выбранного Pod перед вызовом LLM автоматически делает GET Pod, UID events, фактического Node и его events, цепочки ownerReferences, ReplicaSets/замен, PVC, current/previous logs до четырёх контейнеров с проверкой UID, scoped историю метрик, поиск логов Astra (если настроена). Записи E01… содержат метод/путь, время, результат/ошибку и эквивалент kubectl. Выполняются API-запросы; shell-команды не запускаются. Чтение ограничено бюджетом и лимитами страниц/серий/контейнеров, это не неограниченный доступ к любой системе.

Журнал сохраняется в SQLite даже при ошибке LLM. Модель получает сам выбранный Pod и доказательства вместо обрезанного списка чужих объектов. Исторические метрики и Astra могут быть недоступны из-за retention/RBAC/маршрута/версии API; тогда диагноз не объявляется доказанным.
