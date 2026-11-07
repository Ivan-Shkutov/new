# Holmes, Qdrant и библиотека Markdown

Сначала добейтесь законченного ответа Pulse → Qwen без базы знаний. Затем подключайте библиотеку. Pulse управляет сбором, поиском и формированием ответа. Qdrant хранит векторы; индексатор обращается к API Pulse, чтобы каталог и версия документа оставались согласованными. Holmes выполняет дополнительные разрешённые проверки; автоматические исправления кластера не выполняются.

## Запустить Qdrant и CPU embeddings

Выпуск Pulse уже должен быть 0.16.0. Не запускайте второй экземпляр, если эти порты/каталоги уже заняты действующим сервисом. Никакие GPU этому Compose не назначены: две A30 остаются у vLLM. Начальные пределы RAM: Qdrant 4 GiB, embeddings 8 GiB, индексатор 2 GiB; их можно изменить в переменных Compose.

```bash
set -euo pipefail
release="$(readlink -f /opt/services/pulse/current)"
runtime=/opt/services/pulse/runtime
install -d /opt/services/qdrant/storage /opt/services/rag/embedding-models /opt/services/rag/inbox/kubernetes /opt/services/rag/inbox/vk
install -d -o "$(stat -c %u "$runtime")" -g "$(stat -c %g "$runtime")" -m 0700 "$runtime/library-reports"
export PULSE_RUNTIME_HOST="$runtime"
docker compose --env-file "$runtime/.env" --project-name pulse-knowledge \
  -f "$release/deploy/compose.ai.yaml" up -d qdrant embeddings
curl -fsS http://127.0.0.1:16333/healthz
docker compose --env-file "$runtime/.env" --project-name pulse-knowledge \
  -f "$release/deploy/compose.ai.yaml" exec embeddings ollama pull bge-m3
```

Загрузка embedding-модели требует доступа сервера к реестру Ollama. Образы закреплены digest; веса embeddings скачиваются отдельно. Реальную семантическую точность BGE-M3 и диагнозов Qwen проверяют на сервере, она не доказывается тестовыми векторами.

Добавьте подключения в существующий runtime `.env`, сохранив порт, UID/GID и прочие строки:

```bash
python3 - <<'PY'
from pathlib import Path
p=Path('/opt/services/pulse/runtime/.env')
updates={'PULSE_QDRANT_URL':'http://127.0.0.1:16333','PULSE_EMBEDDINGS_URL':'http://127.0.0.1:11435','PULSE_EMBEDDINGS_MODEL':'bge-m3','PULSE_EMBEDDINGS_PROVIDER':'ollama','PULSE_EMBEDDINGS_DIMENSION':'1024'}
lines=p.read_text().splitlines()
lines=[line for line in lines if line.split('=',1)[0] not in updates]
p.write_text('\n'.join(lines+[k+'='+v for k,v in updates.items()])+'\n')
PY
/opt/services/pulse/current/pulse --runtime "$runtime" start-offline
/opt/services/pulse/current/pulse --runtime "$runtime" check
```

Для воспроизводимости закрепите digest embedding-модели: `ollama list`/`/api/tags`, затем `PULSE_EMBEDDINGS_REVISION` в runtime `.env`. Изменение модели/dimension/revision/алгоритма чанков создаёт другую коллекцию. Старый каталог станет `needs_reindex`; после переиндексации новые публикации доступны поиску. Не удаляйте каталог SQLite: это исходные тексты и метаданные.

## Holmes: подготовка, обновление, проверка

В Настройки → кластер → Holmes сохраните рабочий vLLM в общих настройках, выберите свободный порт, нажмите «Подготовить конфигурацию». Генератор создаст `runtime/holmes/peoplehub` с отдельными ключами. Он рассчитан на проверенный API Holmes 0.45.0; установленную другую версию сначала сверяют с контрактом. Образ Holmes не включён в Pulse, требуется доступ к его реестру либо одобренный локальный образ. Не подбирайте tag/parser по названию модели.

Для существующего сгенерированного bundle нажмите «Обновить skills и лимиты». Значения 8/4096/180 можно менять в форме; максимумы — 16/32768/900. Ожидание запроса Pulse к Holmes отдельно, по умолчанию 600 секунд. Это ожидание HTTP, а не гарантия остановки удалённого процесса: клиентский timeout не отменяет уже начатую генерацию. Больший лимит не гарантирует лучшего диагноза; повторения инструментов и лишний контекст ухудшают результат.

Сначала «Проверить ответ Holmes», затем **«Проверить инструменты»**: тест требует неизвестный заранее nonce из настоящего HTTP tool. Только после этого включайте Holmes. `--reasoning-parser qwen3` не доказывает поддержку tool calling. На ваших весах, chat template и vLLM нужно проверить запрос инструмента, исполнение и возврат результата.

Запуск отдельно, используя **тот же project**, которым этот bundle был запущен ранее:

```bash
docker compose --project-name pulse-holmes-peoplehub \
  --project-directory /opt/services/pulse/runtime/holmes/peoplehub \
  -f /opt/services/pulse/runtime/holmes/peoplehub/compose.yaml \
  up -d --force-recreate --wait
```

Неподходящий/не полученный образ не обходите отключением auth или tools whitelist. Правила и инструменты загружаются в контейнер Holmes; в каталог весов Qwen их копировать не нужно. Автоматически включены только `skills` и `pulse-readonly` данного кластера. Внешний Holmes с другими toolsets текущий адаптер Pulse отвергнет: интеграцию нужно настраивать явно.

**Один Compose-проект для нового стека:** если этот Holmes ещё не запущен другим project, можно объединить оба файла. Явный `--project-directory` сохраняет корректный путь `.:/bundle` и `.env` Holmes:

```bash
export PULSE_RUNTIME_HOST=/opt/services/pulse/runtime
docker compose --env-file "$PULSE_RUNTIME_HOST/.env" --project-name pulse-ai-peoplehub \
  --project-directory "$PULSE_RUNTIME_HOST/holmes/peoplehub" \
  -f /opt/services/pulse/current/deploy/compose.ai.yaml \
  -f "$PULSE_RUNTIME_HOST/holmes/peoplehub/compose.yaml" \
  up -d qdrant embeddings holmes
```

Не запускайте отдельный и объединённый проекты одновременно на тех же портах/данных. Уже работающий независимый Holmes не переносите вслепую. Сервисы Pulse и vLLM могут сохранять собственные Compose.

## Документы: только Markdown

В каждом каталоге продукта создайте `manifest.json` с **реальной версией** и происхождением архива. Например, для Kubernetes используйте minor-версию вашего кластера; пример 1.30 нельзя оставлять, если у вас другая версия:

```json
{"product":"kubernetes","version":"1.30","source":"https://kubernetes.io/docs/","language":"en","cluster_id":"peoplehub"}
```

Для VK: `product: vk-teams-on-premises` или `vk-mail-on-premises`, установленная версия, действительный источник. Общая документация всех кластеров: `cluster_id: ""`. Не помечайте неизвестную версию как подтверждённую. YAML/HTML/PDF из архива не индексируются. Если в архиве нет MD, индексатор сообщит неуспех — автоматического превращения неизвестных форматов в достоверный Markdown нет.

```text
/opt/services/rag/inbox/
  kubernetes/
    manifest.json
    kubernetes-library.tar.gz
  vk/
    manifest.json
    vk.zip
```

Копируйте большой архив сначала с окончанием `.partial`, затем переименуйте в `.tar.gz`/`.zip`. Watcher дополнительно ждёт неизменный размер/mtime в двух сканированиях. Для обновления отдельного MD кладите его в папку продукта; большой готовый набор MD можно импортировать одноразовым `python -m pulse_app.library --path ...`.

Запустить watcher (для отдельного проекта `pulse-knowledge`, как выше):

```bash
export PULSE_RUNTIME_HOST=/opt/services/pulse/runtime
docker compose --env-file "$PULSE_RUNTIME_HOST/.env" --project-name pulse-knowledge \
  -f /opt/services/pulse/current/deploy/compose.ai.yaml --profile library up -d indexer
```

При объединённом стеке добавляйте тот же `--profile library` к **той же** объединённой команде/project. Индексатор использует пароль администратора Pulse из read-only файла, не передаёт его модели. Это доверенный сервис импорта, а не инструмент Holmes.

Архивы не извлекаются на диск и не исполняются. Traversal, ссылки, шифрованные ZIP, повторы имён, больше 30 000 файлов/2 GiB распакованных данных отвергаются. Один MD до 2 MB. Чанки учитывают заголовки, абзацы и обычные code fences; размер ограничен символами, это не точный tokenizer BGE-M3. Oversized блок делится с перекрытием/признаком continued, а API embeddings требует `truncate:false`.

Повторный неизменённый документ не пересчитывает embeddings, если коллекция существует. Новая ревизия публикуется после upsert; устаревшие/неопубликованные векторы отфильтровываются каталогом. Изменение файла обновляет его документ. Удаление файла из архива само по себе **не удаляет документ**: явно удалите его через UI, чтобы случайно не потерять знания при неполном экспорте. Имена архивов/путей и provenance сохраняйте стабильными при обновлении.

## Как проверить

В Pulse → База знаний видны отчёты импорта, документы, статусы, ошибки и поиск. Найдите точную фразу из тестового MD, затем задайте вопрос по ней и проверьте цитату D## в анализе. Для Kubernetes поиск исключает другую minor-версию; для остальных продуктов version показана в источнике, автоматическая фильтрация всех продуктовых версий пока требует явной настройки контекста.

Qdrant Dashboard: `http://127.0.0.1:16333/dashboard` на сервере. С компьютера используйте SSH tunnel, сохраняя loopback binding:

```powershell
ssh -L 16333:127.0.0.1:16333 dito@10.129.160.10
```

Откройте `http://127.0.0.1:16333/dashboard` на компьютере. Наличие points подтверждает хранение; качество поиска подтверждают ожидаемые документы/версии и ответы с источниками.

Backup runtime Pulse сохраняет тексты/метаданные. Для быстрой полной защиты Qdrant дополнительно сохраняйте snapshots/каталог его данных согласованным способом; embedding-веса можно загрузить заново. Не публикуйте API Qdrant/Ollama без отдельной защиты.
