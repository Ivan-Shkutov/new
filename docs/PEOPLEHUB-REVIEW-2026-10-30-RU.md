# PeopleHub — разбор присланного снимка 30 октября 2026

Источник — пользовательский вывод kubectl/helm/Linux, включая пять приложенных текстовых файлов. Это анализ снимков, а не удалённая проверка из облачной среды. Исходные логи, environment, credentials и полные выгрузки в репозиторий не копировались. Время в тексте ниже — Москва, если не указано иначе. Новые блоки UI до выбора пользователя не внедрены.

## Подтверждённая причина am-agent

Pod `am-agent/am-agent-am-agent-deployment-8d6ddf79-lwzwg`:

- UID `f093cd8b-752d-49ad-92cf-8256d9fad709`.
- `phase=Failed`, `reason=Evicted`.
- Дословный `status.message`: **Usage of EmptyDir volume "data" exceeds the limit "1Gi".**
- Фактический узел: `v0-inf-peoplehub03.novalocal`, IP `10.128.73.7`.
- Том `data` — EmptyDir, medium не задан (обычный disk-backed emptyDir), sizeLimit `1Gi`; контейнер agent монтирует его в `/opt/am/data`.
- У контейнеров `ContainerStatusUnknown`, exitCode 137. Этот код в данном контексте не доказывает OOMKilled; непосредственно указанная причина выселения — превышение лимита тома.
- `finishedAt/startedAt` в статусах контейнеров — null. Отображение `01 Jan 0001` в describe — нулевое значение, не реальное время. Start Time 7 октября — начало Pod, не дата аварии. События отсутствуют. Точное время выселения по этим данным не установлено.
- В списке есть другой Pod **того же ReplicaSet**: `am-agent-am-agent-deployment-8d6ddf79-zlckk`, `2/2 Running`, на том же узле, возраст `2d8h`, 8 накопленных перезапусков (последний указан `2d8h ago`). Это подтверждает наличие готовой реплики в текущем списке, но не успешность доставки данных Astra и не постоянную стабильность. Причины её перезапусков отдельно не установлены.
- Ошибки чтения старых current/previous logs говорят, что завершённый контейнер/прошлый экземпляр недоступен. Они не отменяют явный status.message.

**Установлен непосредственный механизм:** kubelet выселил Pod за превышение 1 GiB данным EmptyDir. **Не установлено:** какие файлы росли и почему — очередь, локальная БД, кэш или другое содержимое. Связывать это с отдельным примером `cannot set state`, ошибкой БД Astra или общим дефицитом RAM/CPU без корреляции нельзя. DiskPressure=False сейчас и свободный диск не исключают превышение отдельного sizeLimit.

Не следует автоматически удалять данные, Failed Pod или увеличивать sizeLimit: это не устраняет причину роста. Сначала проверяют текущую конфигурацию, перезапуски и логи агента; затем выбирают ограничение/retention/буферизацию и ёмкость хранилища с учётом документации агента.

## Версии и ёмкость

| Показатель | Подтверждённое значение |
|---|---|
| API endpoint | `https://10.128.73.5:6443` |
| API-сервер | `v1.30.8` из /version и serverVersion |
| kubectl | `v1.30.14` |
| kubelet | все 6 узлов `v1.30.14` |
| Readiness API | readyz/livez прошли для отвечающего API-сервера |
| Узлы | 6 Ready: один обозначен control-plane, пять без этой роли |
| CPU capacity | 28 vCPU: 8 на control-plane, по 4 на workers |
| RAM capacity | около 93.67 GiB по сумме capacity.memory |
| ОС/runtime | RED OS MUROM 7.3.6, kernel 5.15.178, containerd 1.6.33 |
| Планирование | control-plane taint NoSchedule; cordon не отмечен |

Не смешивать версии API, kubectl и kubelet. Различие patch-версий требует учёта при планировании обновлений, но само по себе не доказывает причину наблюдаемого Evicted. Проверки readyz/livez не равны проверке всех приложений/всех control-plane backend. В Pulse значения следует собирать динамически, а не зашивать из этого отчёта.

## Pod

В первом полном списке — **163 уникальных Pod**: 145 Running, 17 Completed, 1 ContainerStatusUnknown. Последний по настоящему `.status.phase` — Failed/Evicted. Среди Running не найдено неполной готовности в этой таблице. Для строгого счётчика здоровья завершённых требуется также проверка их container exitCode; один текстовый STATUS Completed не является отдельным доказательством всех exitCode.

В выбранной пользователем семантике счётчика ожидается 162/163 после подтверждения успешного завершения 17 Completed. Failed объект следует показывать с пояснением «сохранённый Evicted, другая реплика Ready», чтобы не выдавать его за доказанный текущий отказ всего сервиса. Накопленные restartCount нельзя подписывать как частоту перезапусков за интервал.

## Резервирование и лимиты — отдельно от нагрузки

Проценты взяты из Allocated resources / describe node, то есть относятся к requests/limits, не к текущему использованию.

| Узел | CPU requests | CPU limits | RAM requests | RAM limits |
|---|---:|---:|---:|---:|
| peoplehub01 | 15% | 15% | 5% | 13% |
| peoplehub02 | **99%** | 346% | 33% | 93% |
| peoplehub03 | 65% | 63% | 15% | 26% |
| peoplehub04 | **90%** | 327% | 30% | 89% |
| peoplehub05 | 58% | 321% | 38% | **134%** |
| peoplehub06 | **84%** | 350% | 27% | 94% |

На узле 02 почти исчерпан CPU-бюджет для планирования новых Pod; это не «CPU загружен на 99%». RAM limits на узле 05 превышают allocatable: это риск при одновременном росте потребления, а не подтверждённый текущий OOM. CPU limits >100% сами по себе допустимы при overcommit и не означают перегрузку сейчас.

Узлы неодинаковы по allocatable ephemeral-storage: примерно 237.7 GB на 01; 9.4 GB на 02/04/06; 30.3 GB на 03/05 (десятичные GB, это allocatable, не свободное место). Общий взвешенный дисковый процент может скрыть проблему небольшого узла. Нужны детализация по узлам/mountpoint и худший запас.

## Хостовая проверка касается только peoplehub01

Присланные hostnamectl/free/df/vmstat выполнены на `v0-inf-peoplehub01`:

- `/dev/vda2`, ext4, 246G, занято 17G (7%), доступно 220G; inode занято около 2%.
- available RAM около 12 GiB, swap 0; в коротких vmstat samples CPU idle около 98%.
- В systemctl --failed отмечены cloud-init-local, cloud-init, getty@ttyS0, serial-getty@ttyS0. Это требует отдельной оценки хоста; из этих статусов не следует отказ Kubernetes или необходимость reboot.

Эти показатели нельзя переносить на worker03, где произошло выселение. Много overlay строк в df не следует суммировать как отдельные физические диски.

## Что уже установлено

Helm metadata показывает am-agent chart 1.6.0, Headlamp 0.45.0, Holmes chart/app 0.42.0, kube-prometheus-stack chart 91.8.2, Loki 2.9.5, VictoriaMetrics v1.99.0 и Zabbix 6.0.8. Helm chart/appVersion — не всегда точная версия фактически работающего бинарника; например appVersion kube-prometheus-stack нельзя подписывать как версию Prometheus server. Реальные images/digests проверяют отдельно. В старом Pod agent используется тег latest, точный image ID недоступен; по одному latest нельзя восстановить точную сборку.

## Предложение блоков Pulse для выбора

1. Верхняя строка: API Kubernetes v1.30.8, kubelet v1.30.14, Ready 6/6; различия версий видны отдельно.
2. Таблица узлов: role/IP/OS/kernel/runtime, capacity/allocatable, actual usage, requests/limits, pressure/cordon, число Pod.
3. Ресурсы: общий обзор плюс самый загруженный узел, остаток CPU для планирования, риск overcommit RAM.
4. Хранилища: корень и важные ФС, bytes/inode, PVC отдельно; EmptyDir sizeLimit и его ошибки. Usage EmptyDir только из доступного проверенного источника — node-exporter не гарантирует отдельную серию для каждого тома Pod.
5. Проблемы: точный status.message, объект и UID, фактический узел, состояние владельца/замены, проверенное время или «неизвестно», источники E01….
6. Интеграции: Prometheus/Loki/VictoriaMetrics/ИИ Astra/Holmes с проверкой доступности и версии, а не только наличием Helm release.

## Следующие безопасные проверки агента

```bash
kubectl -n am-agent get pod am-agent-am-agent-deployment-8d6ddf79-zlckk \
  -o jsonpath='{.status.containerStatuses}{"\n"}'

kubectl -n am-agent get pod am-agent-am-agent-deployment-8d6ddf79-zlckk \
  -o jsonpath='{range .spec.volumes[?(@.name=="data")]}{.emptyDir}{"\n"}{end}'

kubectl -n am-agent get rs am-agent-am-agent-deployment-8d6ddf79 \
  -o jsonpath='{.metadata.ownerReferences}{"\n"}'

kubectl -n am-agent logs am-agent-am-agent-deployment-8d6ddf79-zlckk \
  -c agent --since=2h --tail=200 --timestamps
```

Команды только читают. Перед передачей логов удалите credentials/внутренние данные. Для полной причины роста нужны сведения о содержимом/retention `/opt/am/data` и работе доставки агента; сами файлы старого EmptyDir после очистки kubelet могут уже отсутствовать.
