# Проверки Pulse Clusters 0.16.1

Версия **0.16.1 / build 7fe9f145f196dbb5**. Повторная приёмка выполнена в отдельной рабочей среде, не на сервере пользователя.

- **573 pytest passed**, без skips; один warning Starlette TestClient о будущей зависимости.
- Новые регрессии: 110 000 символов в одном сообщении доходят до всех трёх HTTP-адаптеров; сумма сообщений проверяется до сети; неверный контекст Holmes не выполняет HTTP. Модельные ответы — fixtures.
- Ограничение нагрузки: один активный/один ожидающий; третий получает 429; истечение ожидания отражается в API с queue_timeout; закрытый ожидающий запрос освобождает место без генерации. Повторные отмены освобождают payload, а не накапливают его за активной моделью. Слоты возвращаются при завершении/ошибке/отмене.
- Два operator scripts проверены настоящим локальным HTTP-сервером: redirects отвергаются, пароль не отправляется цели перенаправления и не печатается; live-acceptance не использует посторонний HTTP_PROXY.
- Готовый Docker Linux amd64 собран; /healthz и pulse check возвращают совпадающие version/build. Приложение работает без root с read-only filesystem.
- Chromium на Docker/API: единое окно, шесть разделов, follow-up, новое пустое открытие, поздний ответ закрытой сессии, неполный ответ, mobile; отдельные проверки Astra 401/явного повтора native, метрик, основных операций UI и временного чата.
- ZIP watcher → API Pulse → каталог публикаций → настоящий Qdrant 1.15.5 → поиск → повтор без пересчёта. Browser vector upload/search/delete с настоящим Qdrant. Embeddings — численный HTTP fixture, семантическая релевантность не измерена.
- 24 настоящих вычисления PromQL pinned Prometheus/promtool: веса, дубли scrape, scope и свежесть.
- Node syntax, compileall, git diff --check; Compose Pulse, AI stack и объединённый с generated Holmes файл валидируются.
- Настоящее обновление с 0.15.1 и отдельно с 0.16.0: backup/restore → exact image/build → current symlink → generated rollback. Подключения, исходный ключ, пароль и история сохраняются; Caddy/TLS и аутентифицированный API работают после восстановления. Complete-архив проверен SHA256 и каждым файлом MANIFEST; source/image/RELEASE.json совпадают.

[Фото текущего образа](images/investigator-0161/incident.png) · [решения](PROJECT-DECISIONS-RU.md) · [аудит](AUDIT-0.16.1-RU.md) · [установка](UPDATE-0.16.1-RU.md).

Настоящие Qwen, Holmes, BGE-M3, Astra и рабочие кластеры пользователя здесь не доступны. Для проверки диагноза и tool calling на сервере обязательны реальные случаи и nonce инструмента; HTTP fixtures этого не подтверждают. GitHub Actions после push отдельно не выдаётся за выполненную локальную приёмку.

---

Ниже сохранена приёмка предыдущего выпуска 0.16.0; это история, не дополнительное число тестов 0.16.1.

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
