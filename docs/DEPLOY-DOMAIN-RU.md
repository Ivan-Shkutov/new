# Внутренний домен и перенос Docker-комплекта Pulse 0.13.0

## Ваш вариант: внутренняя сеть

Создайте запись во **внутреннем DNS**, используемом рабочими станциями:

| Поле | Значение |
| --- | --- |
| Тип | A |
| Имя в зоне plagate.ru | pulse-clusters |
| Полное имя | pulse-clusters.plagate.ru |
| IPv4 | 10.129.160.10 |
| TTL | 300 |

Публичная A-запись не делает частный адрес доступным из интернета. В этой схеме интернет-доступ не нужен. Саму DNS-запись нужно создать в вашей системе DNS: у этой облачной сессии нет подключения к ней и серверу.

SSH-туннель больше не потребуется. Docker держит HTTPS-сервис в фоне; окно SSH можно закрыть. Но сетевой доступ **TCP 443** от разрешённых внутренних подсетей к серверу всё равно необходим. DNS сопоставляет имя и адрес, не заменяет соединение. Caddy также использует TCP 80 для перенаправления HTTP на HTTPS. Pulse остаётся на localhost:8090; Qdrant и embeddings — только на localhost.

На Windows проверьте:

```powershell
Resolve-DnsName pulse-clusters.plagate.ru
Test-NetConnection pulse-clusters.plagate.ru -Port 443
```

443 станет доступен после запуска Caddy. Если порт фильтруется, согласуйте точное правило с сетевым администратором. Не отключайте firewall целиком. Не меняйте iptables/nftables вслепую.

## Запуск HTTPS

На сервере от root в каталоге обновлённого проекта:

```bash
python3 scripts/pulse-services.py https-init \
  --domain pulse-clusters.plagate.ru --tls internal
python3 scripts/pulse-services.py https-start
python3 scripts/pulse-services.py https-ca
cp runtime/https/pulse-root.crt /home/dito/pulse-root.crt
chmod 644 /home/dito/pulse-root.crt
```

`https-init` не перезаписывает существующий Caddyfile. При повторном применении проверьте `runtime/https/Caddyfile` и пропустите init. Если `runtime` находится отдельно, добавляйте `--runtime /ваш/runtime` перед командой.

Caddy выдаёт сертификат вашей внутренней CA и автоматически обновляет его. Для доверия браузера установите **публичный корневой CRT** на рабочих станциях. Приватный ключ CA не копируйте. Предпочтительный корпоративный вариант — ваш PKI/уже действующий HTTPS reverse proxy; конфигурация Caddy с `tls internal` предназначена для сети, где вы можете настроить доверие клиентов.

В PowerShell рабочей станции:

```powershell
scp dito@10.129.160.10:/home/dito/pulse-root.crt "$env:USERPROFILE\Downloads\pulse-root.crt"
Import-Certificate -FilePath "$env:USERPROFILE\Downloads\pulse-root.crt" -CertStoreLocation Cert:\CurrentUser\Root
```

Это добавляет доверие вашей CA текущему пользователю Windows; корпоративную массовую установку выполняйте через штатную политику. Проверьте источник CRT перед установкой. После этого откройте **https://pulse-clusters.plagate.ru**. Логин/пароль Pulse остаются прежними. Не обходите предупреждение о недоверенном сертификате — установите правильный CA либо корпоративный сертификат.

Если 80/443 заняты, не останавливайте чужие сервисы: добавьте virtual host в существующий reverse proxy. Пример для уже настроенного Nginx с корпоративными сертификатами:

```nginx
server {
    listen 443 ssl;
    server_name pulse-clusters.plagate.ru;
    ssl_certificate /etc/nginx/tls/pulse/fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/pulse/private.key;
    client_max_body_size 11m;
    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }
}
```

Пример предполагает Nginx на том же сервере; Pulse доверяет forwarded headers только loopback-прокси. Внешний reverse proxy требует отдельной сетевой схемы и явно разрешённых доверенных proxy IP. Не ставьте `forwarded-allow-ips=*`. Сертификаты и пути примера подставляются из вашей PKI.

## Проверка после запуска

На сервере:

```bash
./pulse check
python3 scripts/live-acceptance.py \
  --url https://pulse-clusters.plagate.ru \
  --ca-file runtime/https/pulse-root.crt \
  --password-file runtime/secrets/portal-password
```

Проверка использует реальное DNS, проверяет CA/имя, HTTP API, состояние Qdrant и поиск документа по Evicted/EmptyDir. Её успех не доказывает исправность всех кластеров или качество LLM. Для отдельной проверки генерации:

```bash
python3 scripts/live-acceptance.py \
  --url https://pulse-clusters.plagate.ru \
  --ca-file runtime/https/pulse-root.crt \
  --password-file runtime/secrets/portal-password \
  --question 'Кратко объясни разницу между requests и limits в Kubernetes.'
```

Без `--cluster` используется локальная модель. Для выбранного кластера добавьте `--cluster peoplehub`: будет применён его движок. Для реального анализа проблемы добавьте `--problem-id` из `/api/problems` и конкретный вопрос. Скрипт читает источники через Pulse, не выполняет изменения Kubernetes; печатает ответ и журнал, затем удаляет временный диалог. Не публикуйте вывод, если он содержит внутренние имена/логи.

## Перенос в другое место

Приложение и официальный текстовый корпус включены в `pulse-clusters:0.13.0`. Сервисная композиция включает отдельные Qdrant, Ollama и Caddy. Веса qwen и GPU/vLLM остаются отдельной зависимостью. Одна копия Pulse использует один runtime, один процесс и одного администратора; это не многопользовательская HA-система с SSO/RBAC.

С доступом в интернет на новом Linux amd64 сервере:

1. Распакуйте complete-архив и проверьте SHA256/MANIFEST.
2. `./pulse init --port 8090`, `./pulse build`.
3. Восстановите штатный backup либо выполните `./pulse start-offline` для пустой установки.
4. Запустите knowledge-start/model/check, затем снова `./pulse start-offline` и импорт/переиндексацию.
5. Настройте адрес локального qwen (на новом хосте 127.0.0.1 относится к новому хосту), сеть до Kubernetes, CA и действительные reader tokens.
6. Подготовьте DNS/HTTPS и доверие CA; выполните live-acceptance.

Для площадки без интернета заранее экспортируйте образы на подготовленной машине:

```bash
./pulse export-image /data/pulse-clusters-0.13.0.tar
docker image save -o /data/pulse-services-0.13.0.tar \
  qdrant/qdrant:v1.15.5 ollama/ollama:0.13.5 caddy:2.10.2
cd /data
sha256sum pulse-services-0.13.0.tar > pulse-services-0.13.0.tar.sha256
```

Образы должны быть заранее загружены (`docker compose ... pull` на подготовленной машине); export-image не скачивает их. На целевой машине проверяйте SHA и используйте `docker load -i ...`. Для офлайн-запуска helper принимает `--offline` и применяет override с тегами и `pull_policy: never`. После docker load digest-ссылки могут отсутствовать, поэтому сначала проверяйте SHA архива и соответствие image ID подготовленной машине, затем используйте этот режим. Веса Ollama и qwen экспортируются отдельно, с их лицензиями/контрольными суммами; image save сам по себе **не сохраняет Docker volumes**.

Копируйте данные модели только при остановленном сервисе или через штатный экспорт. Нужный Ollama volume называется `pulse-knowledge-8090_embedding_models`; его точный путь определяйте через `docker volume inspect`, не удаляйте volume. Для Qdrant можно заново построить индекс из авторитетного каталога SQLite через `--reindex`. [Порядок восстановления](AI-KNOWLEDGE-RU.md).

Backup Pulse содержит credentials: храните его защищённо вместе с `registry.key`, не в Git. Данные Caddy CA лежат в volume `pulse-https-8090_caddy_data`, вне штатного backup Pulse; при смене CA потребуется заново настроить доверие клиентов. При использовании корпоративного сертификата сохраняйте штатные PKI-процедуры.


Пример переноса Ollama-модели (на **подготовленной** машине после загрузки bge-m3):

```bash
python3 scripts/pulse-services.py knowledge-stop
docker run --rm --network none --entrypoint /bin/sh \
  -v pulse-knowledge-8090_embedding_models:/models:ro \
  -v /data:/backup ollama/ollama:0.13.5 \
  -c 'tar -czf /backup/pulse-embedding-models.tar.gz -C /models .'
cd /data
sha256sum pulse-embedding-models.tar.gz > pulse-embedding-models.tar.gz.sha256
```

Передайте model-архив, SHA, образы и полный backup Pulse защищённым способом. На **новой пустой** машине после docker load, проверки SHA и восстановления Pulse:

```bash
docker volume create pulse-knowledge-8090_embedding_models
docker run --rm --network none --entrypoint /bin/sh \
  -v pulse-knowledge-8090_embedding_models:/models \
  -v /data:/backup:ro ollama/ollama:0.13.5 \
  -c 'tar -xzf /backup/pulse-embedding-models.tar.gz -C /models'
python3 scripts/pulse-services.py --offline knowledge-start
python3 scripts/pulse-services.py --offline knowledge-model
python3 scripts/pulse-services.py --offline knowledge-check
./pulse start-offline
```

Затем выполните `--reindex` из [AI-KNOWLEDGE](AI-KNOWLEDGE-RU.md) и HTTPS-init/start с `--offline`. Не распаковывайте архив модели поверх чужого работающего volume. В офлайн-режиме knowledge-model не скачивает веса: проверяет уже перенесённую модель и сохраняет её digest. Перенос весов qwen/vLLM зависит от вашей текущей установки и не заменяется переносом embedding-модели.
