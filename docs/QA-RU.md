# Проверки Pulse Clusters 0.16.0

Версия **0.16.0 / build b6c7a21b723d8cad**. Проверки выполнялись в отдельной рабочей среде и одноразовых runtime, не на сервере пользователя.

- Полный pytest: **550 passed**, без skips. Один warning Starlette TestClient о будущей замене зависимости; это не ошибка теста.
- Docker Linux amd64 собран из закреплённого Python base и hash-locked wheelhouse. Запущены `/healthz`, защищённый `/api/overview` и `pulse check`; version/build совпадают с исходниками. Сервис работает без root, с read-only filesystem и host network.
- Новый Chromium smoke на Docker/API: Обзор → пустое окно анализа; обычный законченный текст с шестью разделами; follow-up; очистка при новом открытии; скрытие reasoning; неполный finish_reason=length; сохранение фактов; поздний ответ закрытой сессии не попадает в новую; desktop/mobile; реальные HTTP/TLS read-only запросы Kubernetes fixture. Модель — явный HTTP fixture, не Qwen.
- Отдельный Chromium smoke на Docker/API: Astra 401, точный текст ошибки и сохранение EmptyDir; явный повтор напрямую локальной моделью; отсутствие скрытого fallback/смены сохранённого провайдера; неполный текст не показан как законченный анализ.
- Дополнительные browser checks: настройки метрик с обратным чтением, базовый UI/документы/XSS/navigation/settings, общий временный чат и девять вызовов `llm-check` через Docker/API; компактный Обзор 1920/1366/mobile и состояния stale/critical. Fixtures не доказывают работу доступа production.
- Настоящий Qdrant 1.15.5: create/upsert/query, фильтры scope/version, reindex после удаления коллекции и удаление. Отдельно браузер/API: загрузка, каталог, полный текст, поиск, удаление.
- Новый watcher: настоящий ZIP → MD-only импорт → Pulse API → SQLite publication catalogue → настоящий Qdrant → поиск; отчёт в API и повтор без пересчёта embeddings. Численные embeddings — HTTP fixture, семантическая релевантность не измерялась.
- 24 настоящих PromQL-вычисления pinned Prometheus/promtool: вес узлов, duplicate scrape, cluster scope, свежесть raw samples.
- Настоящее Docker-обновление с 0.15.1: backup → restore → новый image/build → переключение current → сгенерированный rollback. Сохранены ключ шифрования, пароль, подключения и история; Caddy/TLS продолжает работать.
- Проверены node --check, compileall, git diff --check и Compose-конфигурации, включая объединённый Holmes/knowledge проект. Complete-архив проверяется внешней SHA256 и каждым файлом MANIFEST, RELEASE.json соответствует source/image.
- В CI добавлена браузерная проверка цепочки инцидента; удалённая GitHub Actions в этой сессии отдельно не выдаётся за выполненную.

Фото контролируемого стенда: [ответ](images/investigator-0160/incident.png), [пустое окно](images/investigator-0160/empty.png), [мобильный экран](images/investigator-0160/mobile.png), [ошибка усечения](images/investigator-0160/truncated.png).

## Что проверяют новые регрессии

Нормальный text completion больше не обязан быть JSON сценария; точный UID из Ресурсов; запрет подмены цели/конфигурации в диалоге; настройки токенов/timeout доходят до провайдера; неверные типы/границы отвергаются без утечки ключей; Holmes refresh сохраняет credentials/Compose с приватным backup; Markdown preflight до HTTP-вызовов, traversal/links/не-MD; headings/fences; idempotence/FTS scope; новый или пустой backup и переключение current с откатом.

## Границы

Peoplehub, настоящий Astra/vLLM/Qwen/Holmes пользователя здесь не запускались. Реальные веса BGE-M3 не загружены. Успех mocks не равен качеству диагноза или проверке tool calling; для этого нужны nonce-тест Holmes и реальные известные инциденты на сервере. Историческая причина общего резервного экрана 0.15.1 не доказана одними скриншотами. Исправлено подтверждённое несоответствие главного UI/JSON-контракта и добавлена диагностика отказа, но внешние права, proxy deadlines и поведение модели проверяются отдельно.

[Установка/проверка/откат](UPDATE-0.16.0-RU.md) · [аудит и ограничения](AUDIT-0.16.0-RU.md) · [AI-стек](AI-STACK-RU.md).
