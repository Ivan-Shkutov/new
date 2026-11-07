# Обновление Pulse до 0.16.0

Комплект `releases/pulse-clusters-0.16.0-complete.tar.gz` содержит исходники, wheelhouse и готовый Docker-образ Linux amd64. Веса Qwen, Holmes, Qdrant и embedding-модель в него не входят. Существующий vLLM и Astra этим обновлением не меняются.

## 1. Скачать и передать

В браузере с вашим входом GitHub откройте `Ivan-Shkutov/new` → `releases`. Скачайте архив и соседний `pulse-clusters-0.16.0-complete.tar.gz.sha256`.

PowerShell Windows (именно на компьютере, не в Linux SSH):

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.16.0-complete.tar.gz" "$env:USERPROFILE\Downloads\pulse-clusters-0.16.0-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
```

## 2. Обновить действующий сервер

Войдите на сервер. Если приглашение `dito@`, сначала отдельно выполните `sudo -i` и дождитесь приглашения root. Затем вставьте следующий блок. `init` для существующего runtime не нужен.

```bash
set -euo pipefail
install -d /opt/services/pulse/archive /opt/services/pulse/releases /opt/services/backups/pulse
mv /home/dito/pulse-clusters-0.16.0-complete.tar.gz /opt/services/pulse/archive/
mv /home/dito/pulse-clusters-0.16.0-complete.tar.gz.sha256 /opt/services/pulse/archive/
cd /opt/services/pulse/archive
sha256sum -c pulse-clusters-0.16.0-complete.tar.gz.sha256
# Архив уже содержит верхнюю папку pulse-clusters-0.16.0: не создавайте вложенную копию.
test ! -e /opt/services/pulse/releases/pulse-clusters-0.16.0
tar -xzf pulse-clusters-0.16.0-complete.tar.gz -C /opt/services/pulse/releases
release=/opt/services/pulse/releases/pulse-clusters-0.16.0
backup_dir="/opt/services/backups/pulse/upgrade-0.16.0-$(date +%s%N)"
python3 "$release/scripts/upgrade-existing.py" \
  --backup-dir "$backup_dir" --current-link /opt/services/pulse/current
/opt/services/pulse/current/pulse --runtime /opt/services/pulse/runtime check
docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
```

Helper проверяет MANIFEST и build образа до остановки, находит **фактический runtime** активного контейнера, сохраняет backup и создаёт `rollback.sh`. Прежний образ и source не удаляются. Новый либо пустой каталог backup допустим; непустой каталог и подмена version tag отвергаются. Ссылка `current` меняется только после успешной проверки новой версии. Caddy продолжает проксировать тот же порт.

При неудаче helper пытается вернуть прежнюю версию. Прочитайте его результат; наличие автоматического отката не гарантирует успешность внешних зависимостей.

## 3. Проверить реальный анализ

Обновите браузер с Ctrl+F5. В Обзоре нажмите ✧ у известного инцидента: должно открыться **пустое окно чата**, без страницы «Диагностика». Нажмите «Полный разбор». Проверьте шесть разделов, раскрываемые источники, правильные UID и факты. Новый вход должен очищать переписку. Настройки → локальная модель: `http://127.0.0.1:8002/v1`, `qwen`, по умолчанию 8192 токена / 300 секунд. Это пределы, не просьба писать длинный текст.

Для воспроизведения именно запроса инцидента через backend (вместо 2+2) используйте ID выбранной проблемы из `/api/problems` или Network браузера:

```bash
python3 /opt/services/pulse/current/scripts/check-ai-integration.py \
  --runtime /opt/services/pulse/runtime --cluster peoplehub \
  --problem-id ID_ВЫБРАННОГО_ИНЦИДЕНТА --engine local \
  --output /opt/services/pulse/runtime/ai-integration-report.json
```

Скрипт выполняет read-only сбор, удаляет временную сессию и сохраняет ответ/безопасный trace без raw логов и ключей. `done` подтверждает завершение запроса, но правильность диагноза нужно сверить с фактами. При ошибке её текст теперь виден в окне; `run_id`, время и код — в источниках. Astra и Holmes проверяйте отдельно с `--engine cluster` после выбора провайдера в настройках.

## 4. Holmes и библиотека

Для уже сгенерированного через Pulse Holmes нажмите в настройках кластера «Обновить skills и лимиты». По умолчанию 8 итераций, 4096 токенов и 180 секунд на шаг. Перед изменением сохраняется приватная копия bundle; credentials, URL, образ и Compose не ротируются. Затем пересоздайте **именно его** сервис через тот же Compose/project и повторите «Проверить ответ» + «Проверить инструменты». Произвольный внешний Holmes вне runtime этим действием не меняется.

[Полный порядок Holmes/Qdrant/CPU embeddings/библиотеки](AI-STACK-RU.md).

## 5. Откат

Используйте путь backup, напечатанный helper. Если вы всё ещё в той же оболочке, переменная `backup_dir` сохранена:

```bash
bash "$backup_dir/rollback.sh"
/opt/services/pulse/current/pulse --runtime /opt/services/pulse/runtime check
```

После переподключения замените переменную фактическим абсолютным путём. Откат возвращает прежний source/image и ссылку `current`, сохраняя действующий runtime. Восстановление полного backup поверх runtime — отдельная операция только после остановки и осознанного выбора точки восстановления. Не удаляйте старые образы, исходники или backup до приёмки.
