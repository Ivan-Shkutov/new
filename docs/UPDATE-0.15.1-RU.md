# Полный комплект Pulse Clusters 0.15.1

Версия: **0.15.1**, build **253343dad425e1b3**. Проверки и границы: [QA-RU](QA-RU.md).

## Состав

Complete-архив содержит исходники, правила и сценарии, документацию, Kubernetes MD,
wheelhouse с закреплёнными хешами и готовый образ `pulse-clusters:0.15.1` Linux amd64
в `image/pulse-clusters-0.15.1.tar.gz`. Для запуска этого образа registry и сборка
не нужны. Docker Engine, Compose v2+ с `up --wait` и Python 3.12+ должны уже быть
установлены в ОС. Для новой сборки из исходников нужен закреплённый базовый образ.

GPU и веса в комплект не входят: используется существующая Qwen3.8-27B на двух A30.
Astra, Holmes, Prometheus/VictoriaMetrics и Qdrant — внешние источники/сервисы;
подключение к ним сохраняется из runtime, их образы здесь не развёртываются.

## Обновление существующего Pulse на p0-app5000

Скачайте из приватного GitHub оба файла:

- `releases/pulse-clusters-0.15.1-complete.tar.gz`
- `releases/pulse-clusters-0.15.1-complete.tar.gz.sha256`

Перенесите их на сервер в один каталог. Если GitHub-аутентификации на сервере нет,
скачайте в авторизованном браузере и передайте через SCP/WinSCP. Токен не вставляйте
в URL или команды. Откройте root shell в каталоге с этими двумя файлами и выполните:

```bash
(
set -eu
umask 077
sha256sum -c pulse-clusters-0.15.1-complete.tar.gz.sha256
if [ ! -d /opt/pulse-clusters-0.15.1 ]; then
  tar --keep-old-files -xzf pulse-clusters-0.15.1-complete.tar.gz -C /opt
fi
cd /opt/pulse-clusters-0.15.1
python3 scripts/upgrade-existing.py --container pulse-clusters-8090-pulse-1
)
```

Скрипт проверяет весь MANIFEST, определяет действующие каталог и runtime через Docker,
загружает образ, проверяет его version/build ещё до остановки приложения. Затем
создаёт приватный backup, сохраняет пути и rollback.sh, переключает только Pulse и
проверяет новый процесс с авторизацией. При неуспехе после начала backup/остановки
пытается запустить прежний выпуск с тем же runtime. Все ошибки остаются видимыми.

Если имя контейнера отличается, получите его через `docker ps` и укажите в --container.
Нельзя выбирать runtime по старому названию каталога. Не запускайте init поверх
действующих данных. Во время backup и переключения возможен короткий перерыв доступа.
Если каталог релиза уже активен, повторно выполните check вместо upgrade-existing.
Изменённые/неполные файлы в существующем каталоге будут отклонены проверкой MANIFEST.

В выводе сохраните `Backup directory` и `Rollback`. По умолчанию каталог имеет вид
`/root/pulse-backup-0.15.1-ДАТА-ВРЕМЯ`. Он содержит секреты и имеет режим 0700.
Runtime, пароль, registry.key, кластер PeopleHub, настройки метрик, LLM и архив
сохраняются. Caddy на 8091 и vLLM не пересоздаются. Порт Pulse берётся из runtime.

## Проверка после обновления

```bash
cd /opt/pulse-clusters-0.15.1
pulse_runtime="$(docker inspect pulse-clusters-8090-pulse-1 --format '{{range .Mounts}}{{if eq .Destination "/runtime"}}{{.Source}}{{end}}{{end}}')"
./pulse --runtime "$pulse_runtime" check
./pulse --runtime "$pulse_runtime" doctor --cluster peoplehub
```

check должен показать version 0.15.1 / build 253343dad425e1b3 и успешный защищённый API.
doctor проверяет доступ к сохранённым источникам, но не исправляет RBAC/токены/labels.
Ошибка внешнего источника не означает неуспех установки самого Pulse.

Обновите браузер Ctrl+F5, откройте «Диагностика». Проверьте шесть кнопок, краткую
таблицу, закрытые источники, новый результат при каждой кнопке и пустое состояние
при повторном открытии. На известном EmptyDir-инциденте не должно появляться
неподтверждённого вывода о заполнении всего узла. Проверка восстановления использует
новые данные, не LLM. Подробности: [SCENARIOS-RU](SCENARIOS-RU.md).

Для проверки Qwen через новые правила, по желанию:

```bash
./pulse --runtime "$pulse_runtime" llm-check --show-answers
```

Это девять учебных вопросов, несколько минут нагрузки на модель и отчёт
`<runtime>/llm-check-report.json` с критериями ручной проверки. Команда проверяет
совместимый локальный диалог, не заменяет приёмку шести сценариев на настоящем кластере.

## Откат приложения

Запустите созданный скрипт по точному пути, напечатанному при обновлении:

```bash
/root/pulse-backup-0.15.1-ДАТА-ВРЕМЯ/rollback.sh
```

Он останавливает новый Pulse и запускает прежний из сохранённого каталога с тем же
runtime, затем выполняет check. Старый каталог и Docker-образ не удаляются.
У этого выпуска нет новой миграции схемы БД; обычный откат не требует восстановления
backup и не теряет последующие настройки. Возврат самих данных через restore —
отдельная операция. Если автоматический откат сообщил ошибку, сохраните оба каталога
и backup, используйте пути из upgrade.json, не удаляйте runtime.

## Установка на пустом сервере

После проверки внешней SHA256, распаковки в /opt и проверки `sha256sum -c MANIFEST.sha256`:

```bash
cd /opt/pulse-clusters-0.15.1
docker image load --input image/pulse-clusters-0.15.1.tar.gz
./pulse init --port 8090
./pulse start-offline
./pulse check
```

Пароль создаётся в runtime/secrets/portal-password. Открывайте loopback-порт через
SSH-туннель либо настроенный HTTPS. Подключите кластер по [CONNECT-CLUSTER-RU](CONNECT-CLUSTER-RU.md),
модель по [LLM-LOCAL-RU](LLM-LOCAL-RU.md). Установка на пустом сервере не создаёт
демонстрационный кластер и не подставляет секреты из этого чата.

Qdrant — отдельный добровольный этап, а не обязательное условие диагностики.
MD-корпус/чанки/версии описаны в [AI-KNOWLEDGE-RU](AI-KNOWLEDGE-RU.md).
Backup Pulse не включает внешние Docker volumes Qdrant/Ollama/Caddy или веса LLM.
