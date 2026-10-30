# Команды для изучения Kubernetes и выбора данных для Pulse

Все команды ниже предназначены для чтения. Выполняйте их на машине с kubectl нужного кластера; команды Linux — отдельно на выбранном узле. Это каталог проверок, а не сценарий изменения инфраструктуры. Не запускайте весь документ вслепую: сначала выберите интересующий раздел. Доступность API зависит от версии, установленных компонентов и RBAC. Метрики могут быть недоступны без metrics-server/экспортера; это не означает нулевую нагрузку.

Из подтверждённых выводов PeopleHub: API `https://10.128.73.5:6443`, шесть Ready узлов, kubelet `v1.30.14`, containerd `1.6.33`, RED OS MUROM 7.3.6. Версию API-сервера проверяют отдельно; kubeletVersion не подменяет serverVersion.

## 1. Кластер, версии и API

```bash
kubectl config current-context
kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}{"\n"}'
kubectl version -o yaml
kubectl get --raw /version
kubectl cluster-info
kubectl get --raw='/readyz?verbose'
kubectl get --raw='/livez?verbose'
kubectl api-versions
kubectl api-resources -o wide
kubectl get namespaces
```

`readyz/livez` характеризуют отвечающий API-сервер; это не проверка каждого control-plane backend или etcd. Для этих URL может требоваться отдельное право. `config view --raw` не нужен: он может раскрывать credentials.

## 2. Узлы: версии, ОС, runtime, мощность и состояние

```bash
kubectl get nodes -o wide
kubectl get nodes -o custom-columns='NAME:.metadata.name,KUBELET:.status.nodeInfo.kubeletVersion,OS:.status.nodeInfo.osImage,KERNEL:.status.nodeInfo.kernelVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion,ARCH:.status.nodeInfo.architecture'
kubectl get nodes -o custom-columns='NAME:.metadata.name,CPU:.status.capacity.cpu,CPU_ALLOCATABLE:.status.allocatable.cpu,RAM:.status.capacity.memory,RAM_ALLOCATABLE:.status.allocatable.memory,EPHEMERAL:.status.allocatable.ephemeral-storage,PODS_MAX:.status.allocatable.pods'
kubectl get nodes -o custom-columns='NAME:.metadata.name,UNSCHEDULABLE:.spec.unschedulable,TAINTS:.spec.taints'
kubectl get nodes --show-labels
kubectl describe nodes
```

Для одного фактического узла:

```bash
pulse_node=v0-inf-peoplehub02.novalocal
kubectl get node "$pulse_node" -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{" | "}{.reason}{" | "}{.message}{"\n"}{end}'
kubectl describe node "$pulse_node"
kubectl get pods -A --field-selector "spec.nodeName=$pulse_node" -o wide
kubectl get events -A --field-selector "involvedObject.kind=Node,involvedObject.name=$pulse_node" --sort-by=.metadata.creationTimestamp
```

Capacity — физическая/объявленная мощность; allocatable — доступное для планирования; requests/limits в describe — не фактическая утилизация.

## 3. Pod, готовность и причины ошибок

```bash
kubectl get pods -A -o wide
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,NODE:.spec.nodeName,REASON:.status.reason,DELETING:.metadata.deletionTimestamp'
kubectl get pods -A --field-selector=status.phase=Failed -o wide
kubectl get pods -A --field-selector=status.phase=Pending -o wide
kubectl get pods -A --field-selector=status.phase=Succeeded -o wide
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,READY:.status.containerStatuses[*].ready,RESTARTS:.status.containerStatuses[*].restartCount,WAITING:.status.containerStatuses[*].state.waiting.reason,LAST_REASON:.status.containerStatuses[*].lastState.terminated.reason'
```

Для выбранного Pod (пример вашего сохранённого Failed):

```bash
pulse_ns=am-agent
pulse_pod=am-agent-am-agent-deployment-8d6ddf79-lwzwg
kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.metadata.uid}{"\n"}{.status.phase}{"\n"}{.status.reason}{"\n"}{.status.message}{"\n"}{.spec.nodeName}{"\n"}{.metadata.ownerReferences}{"\n"}'
kubectl -n "$pulse_ns" get pod "$pulse_pod" -o jsonpath='{.status.containerStatuses}{"\n"}{.status.initContainerStatuses}{"\n"}'
kubectl -n "$pulse_ns" describe pod "$pulse_pod"
kubectl -n "$pulse_ns" logs "$pulse_pod" --all-containers=true --tail=200 --timestamps
kubectl -n "$pulse_ns" logs "$pulse_pod" --all-containers=true --previous --tail=200 --timestamps
```

Если объект удалён/имя переиспользовано, сверяйте UID. Отсутствие `--previous`/событий закономерно при retention или отсутствии прошлого контейнера. Running не равен Ready; Succeeded/exitCode=0 отличается от Failed/Evicted. Не удаляйте сохранённые ошибки ради зелёной карточки.

## 4. Контроллеры и rollout

```bash
kubectl get deployments -A -o wide
kubectl get statefulsets -A -o wide
kubectl get daemonsets -A -o wide
kubectl get replicasets -A -o wide
kubectl get replicationcontrollers -A -o wide
kubectl get jobs -A -o wide
kubectl get cronjobs -A
kubectl get deployments -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,DESIRED:.spec.replicas,READY:.status.readyReplicas,AVAILABLE:.status.availableReplicas,UPDATED:.status.updatedReplicas,GENERATION:.metadata.generation,OBSERVED:.status.observedGeneration'
```

Из ownerReferences выберите реального владельца, затем подставьте его имя:

```bash
kubectl -n NAMESPACE describe deployment DEPLOYMENT
kubectl -n NAMESPACE rollout history deployment/DEPLOYMENT
kubectl -n NAMESPACE rollout status deployment/DEPLOYMENT --timeout=10s
kubectl -n NAMESPACE describe job JOB
kubectl -n NAMESPACE describe cronjob CRONJOB
```

`rollout status/history` только читают. `rollout restart/undo` сюда не входят. Активный Job, выключенный CronJob и replicas=0 могут быть нормальным состоянием.

## 5. CPU/RAM через metrics-server

```bash
kubectl get apiservices v1beta1.metrics.k8s.io
kubectl top nodes
kubectl top pods -A --sort-by=cpu
kubectl top pods -A --sort-by=memory
kubectl top pods -A --containers
kubectl get --raw /apis/metrics.k8s.io/v1beta1/nodes
```

Эти команды требуют metrics-server. Успешный сбор Prometheus node-exporter не гарантирует работу `kubectl top`. Показания top — текущее короткое окно, а CPU в Pulse — rate за 5 минут; мгновенного совпадения ждать нельзя.

## 6. Requests, limits, квоты и autoscaling

```bash
kubectl get pods -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,CPU_REQUEST:.spec.containers[*].resources.requests.cpu,CPU_LIMIT:.spec.containers[*].resources.limits.cpu,RAM_REQUEST:.spec.containers[*].resources.requests.memory,RAM_LIMIT:.spec.containers[*].resources.limits.memory'
kubectl get resourcequotas -A
kubectl get limitranges -A
kubectl get hpa -A
kubectl get hpa -A -o wide
kubectl get pdb -A
kubectl get priorityclasses
```

Подробнее по выбранной области:

```bash
kubectl -n NAMESPACE describe resourcequota
kubectl -n NAMESPACE describe limitrange
kubectl -n NAMESPACE describe hpa HPA
kubectl -n NAMESPACE describe pdb PDB
```

## 7. Хранилища, CSI и PVC

```bash
kubectl get storageclasses
kubectl get pv
kubectl get pvc -A
kubectl get pvc -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,PHASE:.status.phase,REQUESTED:.spec.resources.requests.storage,CAPACITY:.status.capacity.storage,CLASS:.spec.storageClassName,VOLUME:.spec.volumeName'
kubectl get csidrivers
kubectl get csinodes
kubectl get volumeattachments
kubectl -n NAMESPACE describe pvc PVC
kubectl describe pv PV
```

PVC capacity — не занятое место. Usage требует kubelet volume stats/CSI. Корневой диск узла, `/var/lib/containerd`, `/var/lib/kubelet` и PVC — разные области, их стоит показывать раздельно.

## 8. Сеть, Service, EndpointSlice, Ingress и политики

```bash
kubectl get services -A -o wide
kubectl get endpointslices -A
kubectl get ingresses -A -o wide
kubectl get ingressclasses
kubectl get networkpolicies -A
kubectl -n kube-system get pods,daemonsets,deployments -o wide
kubectl -n kube-system get daemonsets -o custom-columns='NAME:.metadata.name,IMAGES:.spec.template.spec.containers[*].image'
kubectl -n kube-system get deployments -o custom-columns='NAME:.metadata.name,IMAGES:.spec.template.spec.containers[*].image'
```

Для конкретного сервиса:

```bash
kubectl -n NAMESPACE describe service SERVICE
kubectl -n NAMESPACE get endpointslices -l kubernetes.io/service-name=SERVICE -o wide
kubectl -n NAMESPACE describe ingress INGRESS
kubectl -n NAMESPACE describe networkpolicy POLICY
```

Таблицы Service/Endpoints не доказывают прохождение реального клиентского трафика. Версию CNI/Ingress определяют по установленным образам и его собственным ресурсам, а не угадывают по ОС узла.

## 9. События и компоненты управления

```bash
kubectl get events -A --sort-by=.metadata.creationTimestamp
kubectl get events -A --field-selector=type=Warning --sort-by=.metadata.creationTimestamp
kubectl -n kube-system get pods -o wide
kubectl -n kube-system get pods -o custom-columns='NAME:.metadata.name,IMAGES:.spec.containers[*].image,RESTARTS:.status.containerStatuses[*].restartCount'
```

У событий ограниченный retention; отсутствие старого события не доказывает отсутствие аварии. Метки/имена control-plane могут отличаться. Если нужен лог, сначала выберите реальный Pod/контейнер из вывода, затем `kubectl logs --tail=200 --timestamps`.

## 10. Prometheus и Alertmanager, уже установленные в PeopleHub

```bash
kubectl -n monitoring get pods,services,daemonsets,statefulsets -o wide
kubectl -n monitoring get prometheuses.monitoring.coreos.com
kubectl -n monitoring get alertmanagers.monitoring.coreos.com
kubectl -n monitoring get servicemonitors.monitoring.coreos.com
kubectl -n monitoring get podmonitors.monitoring.coreos.com
kubectl get prometheusrules.monitoring.coreos.com -A
```

Ниже функция чтения API Prometheus через Kubernetes. Она использует текущий контекст kubectl; URL предназначен для вашего PeopleHub. На других кластерах подставьте их реальные namespace/service/port. Она ничего не записывает в Prometheus или Kubernetes.

```bash
pulse_prom_query() {
  python3 - "$1" <<'PY'
import subprocess,sys
from urllib.parse import urlencode
path='/api/v1/namespaces/monitoring/services/http:kube-prom-kube-prometheus-prometheus:9090/proxy/api/v1/query'
subprocess.run(['kubectl','get','--raw',path+'?'+urlencode({'query':sys.argv[1]})],check=True)
PY
}
```

Примеры запросов (результаты JSON содержат instance/namespace/pod/device/mountpoint):

```bash
pulse_prom_query 'up{job="node-exporter"}'
pulse_prom_query '100 * (1 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle"}[5m])))'
pulse_prom_query 'node_memory_MemAvailable_bytes'
pulse_prom_query 'node_memory_MemTotal_bytes'
pulse_prom_query '100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)'
pulse_prom_query 'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
pulse_prom_query 'node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
pulse_prom_query 'node_filesystem_files_free{fstype!~"tmpfs|overlay|squashfs|ramfs"}'
pulse_prom_query 'rate(node_disk_reads_completed_total[5m])'
pulse_prom_query 'rate(node_disk_writes_completed_total[5m])'
pulse_prom_query 'rate(node_network_receive_bytes_total[5m])'
pulse_prom_query 'rate(node_network_transmit_bytes_total[5m])'
pulse_prom_query 'topk(15,sum by(namespace,pod)(max by(namespace,pod,container)(rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[5m]))))'
pulse_prom_query 'topk(15,sum by(namespace,pod)(max by(namespace,pod,container)(container_memory_working_set_bytes{container!="",container!="POD"})))'
pulse_prom_query 'kubelet_volume_stats_used_bytes'
pulse_prom_query 'kubelet_volume_stats_capacity_bytes'
pulse_prom_query 'kubelet_volume_stats_inodes_free'
pulse_prom_query 'kube_node_status_condition{condition=~"MemoryPressure|DiskPressure|PIDPressure",status="true"}'
pulse_prom_query 'ALERTS{alertstate="firing"}'
```

Пустая серия означает «нет подходящих наблюдений», а не ноль. Prometheus targets/job/mountpoint зависят от установки. Не суммируйте устройства dm/LVM/физические диски и сетевые интерфейсы без понимания топологии: получится двойной счёт. Для общего источника добавьте фактические метки кластера. Чтение `/api/v1/targets`, `/api/v1/status/runtimeinfo` и `/api/v1/alerts` через тот же service proxy позволяет дополнительно проверить scrape, retention из runtimeinfo (если версия его отдаёт) и действующие alerts; это тоже GET.

## 11. Дополнительные API, CRD, операторы и Helm

```bash
kubectl get apiservices
kubectl get crd
kubectl get crd -o custom-columns='NAME:.metadata.name,GROUP:.spec.group,SCOPE:.spec.scope,VERSIONS:.spec.versions[*].name'
kubectl api-resources --verbs=list -o wide
kubectl get mutatingwebhookconfigurations
kubectl get validatingwebhookconfigurations
kubectl get serviceaccounts -A
kubectl get roles,rolebindings -A
kubectl get clusterroles,clusterrolebindings
kubectl auth can-i --list
```

Если установлен Helm:

```bash
helm list -A
```

Для дополнительных платформ (Calico/Cilium, cert-manager, ArgoCD, Kafka/БД-операторы) сначала найдите их реальные CRD/API в выводе. Универсального списка всех типов объектов нет. `helm get values`, полные YAML configmaps/workloads и credentials в командах сюда намеренно не включены: они могут раскрыть секреты и не нужны для выбора блоков дашборда.

## 12. На выбранном Linux-узле — отдельно от kubectl

Эти команды выполняются непосредственно на нужном хосте, не внутри Pod через exec:

```bash
hostnamectl
uname -r
uptime
lscpu
free -h
df -hT
df -i
lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
findmnt
vmstat 1 5
ip -br address
ip route
ss -s
systemctl --failed
journalctl -u kubelet --since '1 hour ago' --no-pager -n 200
journalctl -u containerd --since '1 hour ago' --no-pager -n 200
```

Если уже установлены sysstat и настроенный crictl:

```bash
iostat -xz 1 5
crictl info
crictl stats
```

Названия systemd services и параметры утилит зависят от ОС/дистрибутива. `df` показывает ФС, `lsblk` — блочные устройства, `iostat` — активность/задержки; это разные показатели.

## Как выбрать данные для Pulse

| Блок | Что имеет смысл показывать |
|---|---|
| Кластер | API server version отдельно от kubelet, endpoint, свежесть, полнота сбора |
| Узлы | Ready/все, смешанные версии, ОС/kernel/runtime, capacity/allocatable, taints/cordon/pressure |
| Ресурсы | Утилизация и запас по каждому узлу, requests/limits отдельно от использования |
| Pod | Ready, успешные Completed, Failed, Pending, restarts за интервал, OOM/Evicted с причиной |
| Хранилища | Корень и важные mountpoints, свободные байты/inode, PVC usage/capacity, Pending/Lost |
| Workloads | desired/ready/available/updated, rollout, Jobs/CronJobs, HPA/PDB |
| Сеть | EndpointSlice readiness, Ingress, CNI, ошибки/потери интерфейсов при наличии метрик |
| Сигналы | Warning events, firing alerts, источник доказательства и время |

Логи и describe могут содержать внутренние данные/значения окружения. Перед отправкой выберите нужные строки и удалите credentials; не отправляйте kubeconfig, Secret manifests, ключи или полный необработанный архив кластера. В этой инструкции нет apply/delete/patch/restart/drain/exec/port-forward и нет изменения ресурсов.
