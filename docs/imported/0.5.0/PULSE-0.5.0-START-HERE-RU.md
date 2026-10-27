# С чего начать: Pulse Clusters 0.5.0

**Один архив:** `pulse-clusters-0.5.0-complete.tar.gz`. Внутри приложение, интерфейс, Compose, установочные команды, офлайн wheels, тесты и документация.

Новый каталог — `/opt/pulse-clusters-0.5.0`, порт 8090. Старый `/root/pulse-clusters` и порт 8089 не перезаписываются. Установщик не удаляет модели, vLLM, NVIDIA, Astra и другие проекты.

## 1. Передать с Windows

Сохраните архив в Downloads. PowerShell:

```powershell
scp "$env:USERPROFILE\Downloads\pulse-clusters-0.5.0-complete.tar.gz" dito@10.129.160.10:/home/dito/
```

`10.129.160.10` — ранее указанный адрес p0-app5000; замените, если изменился. Имя `p0-app5000` требует DNS/SSH alias.

## 2. Подготовить на сервере, от root

```bash
mkdir -p /opt
tar -xzf /home/dito/pulse-clusters-0.5.0-complete.tar.gz -C /opt
cd /opt/pulse-clusters-0.5.0
./pulse verify
./pulse init --port 8090
```

Если каталог этого выпуска уже содержит ваши изменения, распакуйте в другой пустой каталог. Повторный `init` сохраняет пароль, `.env` и документы.

Для переноса истории и подключений 0.4.1 выполните **до первого запуска**:

```bash
./pulse import-legacy /root/pulse-clusters/runtime-v3
```

Для согласованной копии документов остановите старую панель после её backup на время переноса. Полная последовательность — INSTALL-RU.md. Источник не изменяется, прерванные задания автоматически повторно не запускаются.

## 3. Запустить

```bash
docker version
docker compose version
docker image inspect python:3.12-slim
```

Если базового образа нет и сервер имеет интернет:

```bash
docker pull --platform linux/amd64 python:3.12-slim
```

Затем:

```bash
./pulse start
./pulse status
curl --fail --silent --show-error http://127.0.0.1:8090/healthz
cat runtime/secrets/portal-password
```

Python-зависимости устанавливаются из wheelhouse без интернета, с проверкой хешей. Для закрытого сервера перенесите собранный образ по разделу «Офлайн» полного руководства.

## 4. Открыть веб

В новом PowerShell оставьте работать SSH-туннель:

```powershell
ssh -N -L 8090:127.0.0.1:8090 dito@10.129.160.10
```

Браузер: **http://127.0.0.1:8090**. Логин `pulse`, пароль из предыдущего шага. После переноса могут действовать старые параметры авторизации из `.env`.

## 5. Настроить в Pulse

1. **Настройки → Локальная LLM.** Режим «Встроенный агент → vLLM», URL `http://127.0.0.1:8002/v1`, model ID из `/v1/models` вашего сервера (ранее `qwen`). Сохранить → «Проверить ответ и tool calling». Контекст соответствует `--max-model-len` vLLM.
2. **Настройки → Добавить кластер.** ID `peoplehub`, название PeopleHub, автономный kubeconfig только для чтения. RBAC и экспорт — в INSTALL-RU.md.
3. Указать доступный URL Prometheus/VictoriaMetrics, реальные метки кластера и credentials. ClusterIP из другой сети обычно недоступен.
4. Указать URL Astra, доступ для чтения, поля и значение кластера в логах/инцидентах. Сохранить → «Проверить источники».
5. **База знаний.** Загрузить инструкции и описание сервиса. Дождаться «Готов к поиску», проверить поиск по названию приложения.
6. **Обзор.** Открыть Pod и задать вопрос; у ошибки нажать «! Почему?». После фактического исправления сохранить и опубликовать подтверждённое решение.

```bash
./pulse check --cluster peoplehub
./pulse accept --cluster peoplehub
```

`check` проверяет источники; `accept` дополнительно вызывает модель с инструментом. Для встроенного агента это контролируемый tool-call тест. Для Holmes — чтение namespaces через Pulse. Качество диагностики проверяется отдельно на известной причине сбоя.

Holmes необязателен для встроенного анализа. Есть команда `./pulse holmes peoplehub`; подготовка и запуск описаны в INSTALL-RU.md. Его образ и vLLM не вложены.

Основные файлы: `INSTALL-RU.md`, `docs/PROJECT-SPEC-RU.md`, `docs/QA-RU.md`. Официальные документы: `docs/official-catalog/index.html` и `DOWNLOAD-RU.md` рядом с ним.
