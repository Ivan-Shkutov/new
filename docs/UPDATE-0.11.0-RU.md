# Pulse 0.11.0 — анализ через ИИ Astra

Поддержан наблюдённый запрос Astra:

```text
POST http://10.128.77.23/api/v1/settings/ai/request/chat/completions
Authorization: Bearer <credential вводится только в настройках>
Content-Type: application/json
{"model":"qwen","stream":true,"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."}]}
```

Ответ разбирается как SSE `chat.completion.chunk`: текст из `choices[0].delta.content`, признак завершения `[DONE]` или `finish_reason=stop`. Служебные reasoning_content не выводятся как ответ. HTTP 200 без корректного завершения не считается успешным анализом. При сетевом обрыве, неверном формате, length/content_filter/tool_calls ответ помечается failed, полученная часть сохраняется с предупреждением. Вызовы инструментов этот адаптер не исполняет. Повторный запрос автоматически не отправляется, чтобы не создавать повторную нагрузку и расходы.

## Обновление

Скачайте `releases/pulse-clusters-0.11.0-complete.tar.gz` и соседний `.sha256` через Download raw file в приватном GitHub. Передайте на сервер:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.11.0-complete.tar.gz" dito@10.129.160.10:/home/dito/
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.11.0-complete.tar.gz.sha256" dito@10.129.160.10:/home/dito/
```

От root на 10.129.160.10, после передачи файлов:

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
sha256sum -c pulse-clusters-0.11.0-complete.tar.gz.sha256
tar --keep-old-files -xzf pulse-clusters-0.11.0-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.11.0
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
cd /opt/pulse-clusters-0.11.0
./pulse restore "${pulse_backup_files[0]}" --confirm-restore
./pulse start-offline
./pulse check
printf 'Backup: %s\n' "$pulse_backup_dir"
)
```

При ошибке остановитесь. Каталоги и runtime не удаляйте. Сохраняются подключения, qwen, метрики, документы, история и доказательства. Новые параметры Astra AI хранятся в прежнем зашифрованном config; новая таблица не требуется. Отдельные vLLM/Holmes не перезапускаются. Откат: остановить новую установку, затем из прежнего каталога запустить `./pulse --runtime ПРЕЖНИЙ_RUNTIME start-offline`. Пути записаны в backup/previous-paths.txt; новые записи обратно автоматически не переносятся.

## Подключить ИИ Astra

1. Обновите страницу Ctrl+F5. Настройки → PeopleHub → Настроить → **ИИ Astra · анализ через её API**.
2. Полный URL: `http://10.128.77.23/api/v1/settings/ai/request/chat/completions`. Model ID: `qwen`. Суффикс `/v1` автоматически не добавляется.
3. В защищённое поле введите действительный Bearer token, имеющий доступ к этому endpoint. Не добавляйте слово `Bearer`, не присылайте значение в чат и не сохраняйте в Git. Используйте поддерживаемый Astra credential с подходящими правами; возможность сервисного токена и его срок жизни пока не подтверждены. Браузерный access token может истечь. Cookie/пароль браузерной сессии Pulse не копирует.
4. Сначала оставьте режим выключенным, сохраните кластер. Заново откройте настройки → **Проверить поток Astra AI**. Нужен завершённый текстовый ответ.
5. Включите **Использовать ИИ Astra для этого кластера**, сохраните. Holmes и Astra AI выбираются взаимоисключающе; настройки локальной модели сохраняются. В списке подключения будет «ИИ Astra AI».
6. Откройте существующую проблему → ✧ → Расширенный анализ или Нужные команды. В источниках должен быть `engine · Astra AI / SSE`. Ответ появляется по мере получения, после завершения статус становится «Ответ готов».

При 401 проверьте истечение/тип credential. При 403 — разрешения API и ограничения сервиса; Pulse не подделывает браузерные Origin/Cookie и не обходит авторизацию. При HTML вместо SSE проверьте URL/login redirect. При 400/413 проверьте поддерживаемый размер контекста/лимиты источника. Подробный upstream body ошибки не отражается браузеру, чтобы не раскрыть внутренние сведения.

Для отключения снимите checkbox и сохраните: будет использоваться ранее настроенная локальная LLM. Для удаления токена API источника поддерживает `clear_credentials: true` при выключенном режиме. Смена URL сбрасывает старый credential; при включённом режиме новый адрес требует нового токена. Токен ИИ Astra отдельный от настройки источника логов Astra и автоматически между ними не переносится.

## Что означает одинаковый анализатор

Кнопка вызывает тот же endpoint Astra, но Pulse передаёт свои инструкции, выбранный вопрос, историю и собранные доказательства. Он не присваивает себе создателя «Астра Мониторинг» и не делает логовые строки доверенными системными инструкциями. Полное совпадение текста ответа не обещается: контекст, промпт и стохастика модели различаются. Название `qwen` не определяет веса и параметры сервера.

По предоставленному request видно анализ логов, включённых в messages, и историю разговора. Автономные команды Kubernetes в этом request не показаны. В Pulse read-only сбор выполняется отдельно до вызова выбранной модели; журнал E01… остаётся источником реально выполненных проверок. Подключение ИИ Astra само по себе не подключает выгрузку её логов: адаптер источника логов настраивается отдельно по реальному API/mapping. В предоставленном образце есть `labels.pod_namespace`/`labels.pod_name`/`labels.pod_uid`; отдельное поле идентификатора кластера не показано, поэтому его нельзя угадывать.

## Обрыв потока в Astra

`ERR_INCOMPLETE_CHUNKED_ENCODING 200` означает, что заголовки успешного ответа пришли, но передача тела не завершилась корректно. Одна эта ошибка не устанавливает, кто оборвал соединение. На реальном пути проверяют коррелирующие логи Astra API, reverse proxy/LB и модельного сервера, их upstream read/idle timeout, перезапуски, отмену запроса и доступность источника. Точный timeout или необходимость restart из скриншота не определяются.

Приложение ограничивает приём временем/объёмом, не принимает HTML login page за ответ и не включает частичный неуспешный анализ в историю успешного follow-up. Уже полученный текст виден с предупреждением. Даже завершённый ответ модели нужно сверять с доказательствами.

## Проверки

166 pytest-тестов прошли, включая UTF-8 по границе chunks, CRLF, SSE completion, error/tool/length, 401/403/redirect/login HTML, отдельное шифрование токена и отказ скрытого fallback. Проверен настоящий локальный HTTP сервер с корректным и намеренно оборванным chunked-потоком. Chromium проверяет сохранение настройки, скрытие токена и выбор движка; запрос проверки провайдера в браузерном тесте задан fixture. Docker и прежний UI проверяются отдельно.

Частная Astra 10.128.77.23 из этой среды недоступна и реальный token не предоставлялся. Протокол реализован по вашему наблюдению и проверен на локальном стенде; живая приёмка выполняется шагами выше на сервере. Работа вашего API с сервисной авторизацией и production-нагрузка здесь не подтверждены.
