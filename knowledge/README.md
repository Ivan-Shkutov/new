# Официальная документация Kubernetes

`kubernetes-1.30-en-ru.jsonl.gz` — 1478 Markdown-документов из официального `kubernetes/website`, ветка `release-1.30`, commit `c28dcd8393562ddc6adf89b920ad3c396166a31b`.

Авторы: Kubernetes Authors. Лицензия CC BY 4.0: [полный текст](KUBERNETES-LICENSE.txt). Каждый документ сохраняет URL исходника с фиксированным commit, язык, относительное имя и версию. Текст экспортирован из Markdown без исполнения Hugo, shortcode или команд. Оригинал: https://github.com/kubernetes/website. Изменение представления: экспорт в JSONL и gzip для индексации в Pulse.

Включены все `.md` из `content/en/docs` (1343) и `content/ru/docs` (135). Это полный **текстовый снимок этих каталогов**, не весь сайт Kubernetes: изображения, внешние генерируемые справочники API, связанные файлы примеров и другие языки не включены. В русской части нет полного перевода английского корпуса. Manifest хранит SHA256, commit и границы снимка.

Загрузка в Qdrant после установки модели:

```bash
docker exec pulse-clusters-8090-pulse-1 python -m pulse_app.import_knowledge \
  --jsonl /app/knowledge/kubernetes-1.30-en-ru.jsonl.gz \
  --report /runtime/kubernetes-import-report.json
```

Для обновления корпуса используйте официальный checkout нужной ветки и `scripts/export-kubernetes-docs.py`. При смене модели embeddings требуется новая revision и переиндексация. Устаревшие версии документов не подменяют выбранную minor-версию кластера.

VK Workspace SaaS и VK Teams on-premises загружаются отдельно с официального сайта либо из официальной выгрузки; они не включены в данный файл. См. [настройку AI и Qdrant](../docs/AI-KNOWLEDGE-RU.md).
