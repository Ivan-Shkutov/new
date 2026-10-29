# Pulse Clusters 0.6.1

Самостоятельная панель наблюдения за Kubernetes: несколько кластеров, 22 типа ресурсов, метрики Prometheus/VictoriaMetrics, логи Kubernetes и адаптер логов Astra, контекстные вопросы локальной LLM, документы с полнотекстовым поиском, история расследований и подтверждённые решения.

**Начните с [пошаговой инструкции для вашего сервера](START-HERE-RU.md).** [Полная установка и эксплуатация](docs/INSTALL-RU.md). [Подключить кластер и проверить работу](docs/CONNECT-CLUSTER-RU.md) · [Проверки выпуска](docs/QA-RU.md) · [Контракты интеграций](docs/CONNECTORS-RU.md).

## Выбор нового обзора

[Четыре макета дашборда: Headlamp / Lens + Grafana / Rancher / Pulse 360](design/overview-variants/README.md). Это варианты для выбора, рабочий интерфейс ими пока не заменён.

[Подключение локальной LLM qwen и проверка расследования](docs/LLM-LOCAL-RU.md).

## Запуск с нуля

Linux, Python 3.12+, Docker Engine и Compose plugin с поддержкой `up --wait` (v2 и новее). Панель по умолчанию доступна только на localhost:8090.

```bash
git clone https://github.com/Ivan-Shkutov/new.git pulse-clusters
cd pulse-clusters
./pulse init --port 8090
./pulse start
./pulse check
cat runtime/secrets/portal-password
```

Логин `pulse`. С рабочей станции: `ssh -N -L 8090:127.0.0.1:8090 USER@SERVER`, затем открыть `http://127.0.0.1:8090`. Замените USER/SERVER реальными значениями. Пароли и kubeconfig не отправляйте в чат или Git.

В Settings добавьте reader kubeconfig; для модели — URL OpenAI-compatible `/v1` и model ID. Без подключений панель пустая: демонстрационных данных нет. Без LLM инвентарь продолжает работать.

## Что входит и где границы

- FastAPI, SQLite WAL/FTS5, зашифрованные подключения, один процесс и один административный аккаунт.
- UI без CDN и npm-сборки; фоновые снимки, ограниченная очередь LLM, продолжение завершённого расследования.
- MD/TXT/RST/PDF с текстом/DOCX, ограничение размера, поиск в общем корпусе и выбранном кластере.
- Docker, CLI init/start/stop/check/backup/restore/export-image, RBAC reader и экспорт краткосрочного kubeconfig.
- Зависимости закреплены с хешами; базовый образ закреплён digest.

Это новая реализация, а не исходники прежнего 0.5.0. Нет многопользовательского RBAC/OIDC, автоматических исправлений, автономного tool-calling агента, Holmes/AURA, PostgreSQL/Qdrant, Astra incidents или автоматического переноса старого runtime. Astra logs требует bearer token; совместимость с реальной версией сервера проверяется отдельно. LLM, модельные веса, Docker Engine и Kubernetes устанавливаются отдельно.

Документы в `docs/imported` и `docs/review` относятся к прежнему описанию 0.5.0 и не являются инструкцией для этого выпуска. Настоящий путь установки — `docs/INSTALL-RU.md`.

## Разработка и проверка

```bash
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install pytest==8.3.5 playwright==1.51.0
.venv/bin/python -m pytest -q
./pulse init
./pulse dev
```

В другом терминале, с установленным Chromium:

```bash
.venv/bin/python scripts/ui-smoke.py --base-url http://127.0.0.1:8090 --password-file runtime/secrets/portal-password --chromium /usr/bin/chromium
```

Browser smoke предназначен только для тестовой установки: временно меняет настройки модели и удаляет свой тестовый документ. Проверки внешних API используют контролируемые ответы и не доказывают доступ к вашей инфраструктуре.
