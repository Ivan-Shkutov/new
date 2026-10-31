"""Bounded, read-only upstream adapters. Never expose upstream bodies in errors."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
import ssl
import tempfile
import time
from urllib.parse import quote, urlsplit

import httpx
import yaml


class ConnectorError(ValueError):
    """Safe operator-facing integration failure."""


RESOURCE_PATHS = {
    "nodes": "/api/v1/nodes", "pods": "/api/v1/pods", "namespaces": "/api/v1/namespaces",
    "services": "/api/v1/services", "endpoints": "/api/v1/endpoints",
    "configmaps": "/api/v1/configmaps", "persistentvolumeclaims": "/api/v1/persistentvolumeclaims",
    "persistentvolumes": "/api/v1/persistentvolumes", "events": "/api/v1/events",
    "serviceaccounts": "/api/v1/serviceaccounts",
    "deployments": "/apis/apps/v1/deployments", "statefulsets": "/apis/apps/v1/statefulsets",
    "daemonsets": "/apis/apps/v1/daemonsets", "replicasets": "/apis/apps/v1/replicasets",
    "jobs": "/apis/batch/v1/jobs", "cronjobs": "/apis/batch/v1/cronjobs",
    "ingresses": "/apis/networking.k8s.io/v1/ingresses",
    "networkpolicies": "/apis/networking.k8s.io/v1/networkpolicies",
    "storageclasses": "/apis/storage.k8s.io/v1/storageclasses",
    "horizontalpodautoscalers": "/apis/autoscaling/v2/horizontalpodautoscalers",
    "replicationcontrollers": "/api/v1/replicationcontrollers",
    "poddisruptionbudgets": "/apis/policy/v1/poddisruptionbudgets",
}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_PAGES = 4


def _text(value, name, maximum=1024 * 1024):
    if not isinstance(value, str) or len(value) > maximum:
        raise ConnectorError(f"Invalid {name}")
    return value


def _url(value, *, https_only=False):
    value = _text(value, "endpoint", 4096).rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        raise ConnectorError("Invalid endpoint port") from None
    if (parsed.scheme not in (("https",) if https_only else ("https", "http"))
            or not parsed.hostname or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or any(ch.isspace() for ch in value)):
        raise ConnectorError("Endpoint must be an HTTP(S) URL without credentials, query or fragment")
    return value


def _pem(data, name):
    if data is None:
        return ""
    try:
        result = base64.b64decode(_text(data, name), validate=True).decode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        raise ConnectorError(f"Invalid base64 {name}") from None
    if "-----BEGIN " not in result:
        raise ConnectorError(f"Invalid PEM {name}")
    return result


def validate_kubeconfig(text: str, context: str | None = None) -> dict:
    """Accept self-contained credentials only; credentials must be encrypted at rest."""
    try:
        raw = yaml.safe_load(_text(text, "kubeconfig"))
    except yaml.YAMLError:
        raise ConnectorError("Invalid kubeconfig YAML") from None
    if not isinstance(raw, dict):
        raise ConnectorError("Kubeconfig must be an object")
    context = context or raw.get("current-context")
    if not isinstance(context, str) or not context:
        raise ConnectorError("Kubeconfig requires a current-context")

    def entry(group, name, key):
        rows = raw.get(group)
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ConnectorError(f"Invalid kubeconfig {group}")
        matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
        if len(matches) != 1 or not isinstance(matches[0].get(key), dict):
            raise ConnectorError(f"Missing or ambiguous kubeconfig {key}")
        return matches[0][key]

    selected = entry("contexts", context, "context")
    cluster = entry("clusters", selected.get("cluster"), "cluster")
    user = entry("users", selected.get("user"), "user")
    if cluster.get("insecure-skip-tls-verify"):
        raise ConnectorError("Disabling Kubernetes TLS verification is forbidden")
    if any(name in cluster for name in ("certificate-authority", "proxy-url", "tls-server-name")):
        raise ConnectorError("External CA paths and proxy/TLS overrides are unsupported")
    if any(name in user for name in ("exec", "auth-provider", "tokenFile", "token-file", "client-certificate", "client-key", "username", "password", "as", "as-groups", "as-user-extra")):
        raise ConnectorError("Use self-contained token or certificate credentials; plugins and file references are forbidden")
    token = _text(user.get("token", ""), "token", 65536)
    if any(ch in token for ch in "\r\n"):
        raise ConnectorError("Invalid token")
    ca = _pem(cluster.get("certificate-authority-data"), "certificate-authority-data")
    cert = _pem(user.get("client-certificate-data"), "client-certificate-data")
    key = _pem(user.get("client-key-data"), "client-key-data")
    if bool(cert) != bool(key) or not (token or (cert and key)):
        raise ConnectorError("A token or complete client certificate/key pair is required")
    config = {"server": _url(cluster.get("server"), https_only=True), "token": token,
              "ca_pem": ca, "client_certificate": cert, "client_key": key, "context": context}
    # Validate the trust material locally, before saving a broken connection.
    with _tls(config):
        pass
    return config


@contextmanager
def _tls(settings):
    try:
        ca = settings.get("ca_pem") or None
        context = ssl.create_default_context(cadata=ca)
        cert, key = settings.get("client_certificate"), settings.get("client_key")
        if cert or key:
            if not cert or not key:
                raise ConnectorError("Incomplete TLS client identity")
            with tempfile.TemporaryDirectory(prefix="pulse-tls-") as directory:
                paths = []
                for name, content in (("cert.pem", cert), ("key.pem", key)):
                    path = os.path.join(directory, name)
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w") as handle:
                        handle.write(content)
                    paths.append(path)
                context.load_cert_chain(*paths)
                yield context
        else:
            yield context
    except (ssl.SSLError, OSError, TypeError):
        raise ConnectorError("Invalid or inaccessible TLS certificate material") from None


@contextmanager
def _client(settings, *, timeout=10):
    headers = {"Accept": "application/json"}
    token = settings.get("token") or settings.get("api_key")
    if token:
        token = _text(token, "token", 65536)
        if any(c in token for c in "\r\n"):
            raise ConnectorError("Invalid authentication token")
        headers["Authorization"] = f"Bearer {token}"
    auth = None
    if settings.get("username") and not token:
        auth = httpx.BasicAuth(settings["username"], settings.get("password", ""))
    with _tls(settings) as tls:
        # trust_env=False avoids unintentional credential routing via ambient proxies.
        with httpx.Client(verify=tls, timeout=timeout, follow_redirects=False,
                          trust_env=False, headers=headers, auth=auth) as client:
            yield client


def _request(client, method, url, *, as_json=True, deadline=None, **kwargs):
    try:
        with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise ConnectorError(f"Upstream HTTP {response.status_code}")
            chunks, total = [], 0
            for chunk in response.iter_bytes():
                if deadline is not None and time.monotonic() >= deadline:
                    raise ConnectorError("Collection time budget exceeded")
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise ConnectorError("Upstream response exceeds 16 MiB limit")
                chunks.append(chunk)
            body = b"".join(chunks)
        if not as_json:
            return body.decode("utf-8", errors="replace")
        return json.loads(body)
    except httpx.HTTPError:
        raise ConnectorError("Upstream network/TLS request failed; check endpoint, trust and connectivity") from None
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, ConnectorError):
            raise
        raise ConnectorError("Upstream returned invalid JSON") from None


def _sanitize(value, *, depth=0):
    if depth > 40:
        return "[depth limit]"
    if isinstance(value, list):
        return [_sanitize(item, depth=depth + 1) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in ("managedFields", "annotations"):
            continue
        if key in ("command", "args") and isinstance(item, list):
            result[key] = ["[redacted]"]
        elif key == "env" and isinstance(item, list):
            result[key] = [{**_sanitize(v, depth=depth + 1), **({"value": "[redacted]"} if "value" in v else {})}
                           for v in item if isinstance(v, dict)]
        elif key == "httpHeaders" and isinstance(item,list):
            result[key] = [{"name":v.get("name",""),"value":"[redacted]"} for v in item if isinstance(v,dict)]
        else:
            result[key] = _sanitize(item, depth=depth + 1)
    return result


def collect_cluster(config: dict) -> dict:
    base = _url(config.get("server"), https_only=True)
    result = {"observed_at": datetime.now(timezone.utc).isoformat(), "resources": {}, "errors": [], "complete": True}
    deadline = time.monotonic() + 45
    with _client(config) as client:
        for kind, path in RESOURCE_PATHS.items():
            record = {"items": [], "error": None, "truncated": False}
            result["resources"][kind] = record
            cursor = ""
            try:
                for page in range(MAX_PAGES):
                    if time.monotonic() >= deadline:
                        raise ConnectorError("Collection time budget exceeded")
                    params = {"limit": 500}
                    if cursor:
                        params["continue"] = cursor
                    data = _request(client, "GET", base + path, params=params, deadline=deadline)
                    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                        raise ConnectorError("Invalid Kubernetes list response")
                    for item in data["items"]:
                        if not isinstance(item, dict):
                            raise ConnectorError("Invalid Kubernetes object")
                        if kind == "configmaps":
                            if any(not isinstance(item.get(k) or {}, dict) for k in ("data", "binaryData")):
                                raise ConnectorError("Invalid ConfigMap data")
                            item = {"apiVersion": item.get("apiVersion"), "kind": "ConfigMap",
                                    "metadata": item.get("metadata", {}),
                                    "keys": sorted(set((item.get("data") or {}).keys()) | set((item.get("binaryData") or {}).keys()))}
                        record["items"].append(_sanitize(item))
                    metadata = data.get("metadata") or {}
                    if not isinstance(metadata, dict) or not isinstance(metadata.get("continue", ""), str):
                        raise ConnectorError("Invalid Kubernetes pagination metadata")
                    cursor = metadata.get("continue", "")
                    if not cursor:
                        break
                record["truncated"] = bool(cursor)
            except ConnectorError as exc:
                record["error"] = str(exc)
                result["errors"].append(f"{kind}: {exc}")
            if record["error"] or record["truncated"]:
                result["complete"] = False
        # Optional metadata: an unavailable /version must not erase resource data.
        result['cluster_info']={'server_version':None,'error':None}
        try:
            if time.monotonic()>=deadline:raise ConnectorError('Collection time budget exceeded')
            version=_request(client,'GET',base+'/version',deadline=deadline)
            value=version.get('gitVersion') if isinstance(version,dict) else None
            if not isinstance(value,str) or len(value)>100 or not re.fullmatch(r'v\d+\.\d+(?:\.\d+)?[A-Za-z0-9.+-]*',value):
                raise ConnectorError('API did not return a valid Kubernetes server version')
            result['cluster_info']['server_version']=value
        except ConnectorError as exc:
            result['cluster_info']['error']=str(exc)
    return result


def _name(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", value):
        raise ConnectorError(f"Invalid Kubernetes {field}")
    return quote(value, safe="")



def redact_sensitive_text(text: str) -> str:
    """Best-effort obvious credential redaction, not comprehensive DLP."""
    text = re.sub(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----",
                  "[private key redacted]", text, flags=re.DOTALL)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", text)
    text = re.sub(r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b([\"']?\s*[:=]\s*)"
                  r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)", r"\1\2[redacted]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[JWT redacted]", text)
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)


def _redact_log_row(value):
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, list):
        return [_redact_log_row(item) for item in value]
    if isinstance(value, dict):
        return {key: "[redacted]" if re.fullmatch(r"(?i)(?:password|passwd|token|secret|api[_-]?key|authorization)", str(key))
                else _redact_log_row(item) for key, item in value.items()}
    return value


def resource_logs(config, namespace, pod, container=None, previous=False, *, expected_uid=None) -> str:
    namespace, pod = _name(namespace, "namespace"), _name(pod, "pod")
    params = {"tailLines": 500, "limitBytes": 262144, "timestamps": "true", "previous": "true" if previous else "false"}
    if container:
        params["container"] = _name(container, "container")
    with _client(config) as client:
        pod_url = _url(config.get("server"), https_only=True) + f"/api/v1/namespaces/{namespace}/pods/{pod}"
        def check_identity():
            if expected_uid:
                current = _request(client, "GET", pod_url)
                if not isinstance(current, dict) or current.get("metadata", {}).get("uid") != expected_uid:
                    raise ConnectorError("UID Pod изменился; логи другого объекта исключены")
        # Kubernetes logs are addressed by name, not UID. Check both sides of
        # the read so replacement during the request cannot contaminate a case.
        check_identity()
        text = _request(client, "GET", pod_url + "/log", params=params, as_json=False)
        check_identity()
        return redact_sensitive_text(text)


METRIC_EXPRESSIONS = {
    "cpu": "sum by (namespace,pod) (rate(container_cpu_usage_seconds_total{scope}[5m]))",
    "memory": "sum by (namespace,pod) (container_memory_working_set_bytes{scope})",
    "node_cpu": "sum by (instance) (rate(node_cpu_seconds_total{scope}[5m]))",
    "node_memory": "node_memory_MemAvailable_bytes{scope}",
    "node_memory_total": "node_memory_MemTotal_bytes{scope}",
    "filesystem_size": "node_filesystem_size_bytes{scope}",
    "filesystem": "node_filesystem_avail_bytes{scope}",
    "pvc_used": "kubelet_volume_stats_used_bytes{scope}",
    "pvc_capacity": "kubelet_volume_stats_capacity_bytes{scope}",
}


def metrics_config(config):
    source = dict(config.get('metrics', {}))
    if source.get('transport') == 'kubernetes':
        source['_kube'] = config['kube']
    return source


def metrics_configured(source):
    return bool(source.get('url') or source.get('transport') == 'kubernetes')


@contextmanager
def metric_client(source):
    if source.get('transport') == 'kubernetes':
        ns = _name(source.get('namespace', ''), 'namespace')
        service = _name(source.get('service', ''), 'service')
        port = source.get('port', 9090)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConnectorError('Некорректный порт сервиса метрик')
        kube = source.get('_kube') or {}
        base = _url(kube.get('server'), https_only=True) + f'/api/v1/namespaces/{ns}/services/http:{service}:{port}/proxy'
        with _client(kube) as client:
            yield client, base
    else:
        with _client(source) as client:
            yield client, _url(source.get('url'))


def query_metrics(settings: dict, query: str) -> dict:
    if query not in METRIC_EXPRESSIONS:
        raise ConnectorError("Unknown metric; arbitrary PromQL is not permitted")
    labels = settings.get("labels") or {}
    if not isinstance(labels, dict) or len(labels) > 16:
        raise ConnectorError("Invalid metric cluster labels")
    if not labels and settings.get("mode") != "dedicated":
        raise ConnectorError("Shared metrics require explicit cluster labels")
    selectors = []
    for name, value in labels.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", name) or not isinstance(value, str):
            raise ConnectorError("Invalid metric label")
        selectors.append(f"{name}={json.dumps(value, ensure_ascii=True)}")
    if query == "node_cpu":
        if "mode" in labels:
            raise ConnectorError("The mode label is reserved for node CPU selection")
        selectors.append('mode!="idle"')
    expr = METRIC_EXPRESSIONS[query].replace("{scope}", "{" + ",".join(selectors) + "}")
    with metric_client(settings) as (client, base):
        data = _request(client, "GET", base + "/api/v1/query", params={"query": expr})
    series = _metric_series(data, 'vector')
    now = time.time()
    for row in series:
        _metric_point(row.get('value'), earliest=now-120, latest=now+30)
    return {"metric": query, "data": data["data"], "empty": not bool(series), "query": expr}


def _metric_series(reply, expected):
    """Validate the Prometheus envelope before any caller treats it as evidence."""
    if not isinstance(reply, dict) or reply.get('status') != 'success':
        raise ConnectorError('Invalid metrics API response')
    data = reply.get('data')
    if not isinstance(data, dict) or data.get('resultType') != expected or not isinstance(data.get('result'), list):
        raise ConnectorError('Invalid metrics API response')
    # Prometheus may return success alongside warnings about partial data.
    # Do not echo upstream annotations, which can contain private endpoints.
    if reply.get('warnings'):
        raise ConnectorError('Metrics API returned warnings; completeness is not confirmed')
    for row in data['result']:
        if not isinstance(row, dict) or not isinstance(row.get('metric', {}), dict):
            raise ConnectorError('Invalid metrics API series')
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in row.get('metric', {}).items()):
            raise ConnectorError('Invalid metrics API labels')
    return data['result']


def _metric_point(value, *, earliest, latest, percentage=False):
    import math
    if not isinstance(value, (list, tuple)) or len(value) != 2 or any(isinstance(v, bool) for v in value):
        raise ConnectorError('Источник вернул некорректную метрику')
    try:
        timestamp, number = float(value[0]), float(value[1])
    except (ValueError, TypeError, OverflowError):
        raise ConnectorError('Источник вернул некорректную метрику') from None
    if not math.isfinite(timestamp) or not math.isfinite(number) or number < 0 or (percentage and number > 100):
        raise ConnectorError('Источник вернул некорректную метрику')
    if not earliest <= timestamp <= latest:
        raise ConnectorError('Время метрики вне допустимого интервала; актуальность не подтверждена')
    return [timestamp, round(number, 2) if percentage else number]


def _field(row, path):
    if path in row:
        return row[path]
    value = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def astra_logs(settings: dict, namespace: str, pod: str, start: str | None = None, end: str | None = None) -> dict:
    """Astra HTTP adapter; validate payload against the installed Astra Swagger."""
    _name(namespace, "namespace")
    _name(pod, "pod")
    fields = [settings.get("cluster_field", "labels.cluster"),
              settings.get("namespace_field", "labels.k8s.namespace.name"),
              settings.get("pod_field", "labels.k8s.pod.name")]
    if any(not isinstance(f, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", f) for f in fields):
        raise ConnectorError("Invalid Astra field mapping")
    cluster = settings.get("cluster_value")
    if not isinstance(cluster, str) or not cluster:
        raise ConnectorError("Astra requires an explicit cluster value")
    if not settings.get("token"):
        raise ConnectorError("Astra adapter requires a service bearer token; password login is not implemented")
    now = datetime.now(timezone.utc)
    if start is not None:
        try:
            parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ConnectorError("Astra start must be an ISO timestamp with timezone") from None
    else:
        start = (now - timedelta(hours=1)).isoformat()
    if end is None:end = now.isoformat()
    if end is not None:
        try:
            end_time = datetime.fromisoformat(end.replace('Z','+00:00'))
            if end_time.tzinfo is None or end_time <= datetime.fromisoformat(start.replace('Z','+00:00')):
                raise ValueError()
        except (ValueError, AttributeError):
            raise ConnectorError('Astra end must follow start and include timezone') from None
    expected = [cluster, namespace, pod]
    rows, scanned, truncated = [], 0, False
    with _client(settings) as client:
        for page in range(MAX_PAGES):
            payload = {"time": {"from": start, "to": end},
                       "query": " AND ".join(f"{field}:{json.dumps(value)}" for field, value in zip(fields, expected)),
                       "paging": {"page": page, "size": 250}}
            data = _request(client, "POST", _url(settings.get("url")) + "/api/v1/logs/search/monitorLogs", json=payload)
            source = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(source, dict):
                source = next((source[k] for k in ("items", "logs", "records", "content") if isinstance(source.get(k), list)), None)
            if not isinstance(source, list):
                raise ConnectorError("Unsupported Astra response; compare the installed Swagger contract")
            scanned += len(source)
            rows.extend(row for row in source if isinstance(row, dict)
                        and all(_field(row, field) == value for field, value in zip(fields, expected)))
            if len(source) < 250:
                break
            truncated = page == MAX_PAGES - 1
    return {"items": [_redact_log_row(row) for row in rows[:1000]], "scanned": scanned, "truncated": truncated or len(rows)>1000,
            "notice": "Astra adapter contract must be validated for your server version"}


def llm_chat(settings: dict, messages: list) -> str:
    if not isinstance(messages, list) or not messages or len(messages) > 60:
        raise ConnectorError("Invalid LLM conversation")
    clean = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant"):
            raise ConnectorError("Unsupported LLM message")
        clean.append({"role": message["role"], "content": _text(message.get("content"), "message", 100000)})
    if sum(len(m["content"]) for m in clean) > 150000:
        raise ConnectorError("LLM context exceeds the configured application limit")
    model = _text(settings.get("model"), "model", 200)
    if not model:
        raise ConnectorError("LLM model is required")
    base = _url(settings.get("url"))
    if not base.endswith("/v1"):
        base += "/v1"
    with _client(settings, timeout=httpx.Timeout(180, connect=10)) as client:
        response = _request(client, "POST", base + "/chat/completions", json={
            "model": model, "messages": clean, "max_tokens": 6000, "temperature": 0.1,
            "stream": False,
        })
    try:
        choice = response["choices"][0]
        message = choice["message"]
        result = message["content"]
    except (KeyError, IndexError, TypeError):
        raise ConnectorError("LLM returned an unsupported completion response") from None
    if not isinstance(result, str) or not result.strip():
        raise ConnectorError("LLM returned no text; tools and autonomous actions are not enabled")
    if message.get('tool_calls') or message.get('function_call'):
        raise ConnectorError('Local LLM requested tools; native mode never executes model commands')
    if choice.get('finish_reason')!='stop':
        from .astra_ai import IncompleteAnswer
        raise IncompleteAnswer('Локальная модель не подтвердила завершение ответа: '+str(choice.get('finish_reason'))[:40],result)
    return redact_sensitive_text(result)


# Ratios are computed by the source across series, never by averaging node percentages.
# max by(instance, ...) removes duplicate scrape jobs while retaining physical identities.
def utilization_expressions(settings, per_node=False):
    labels = settings.get('labels') or {}
    if not isinstance(labels, dict) or len(labels) > 16:
        raise ConnectorError('Invalid metric cluster labels')
    if not labels and settings.get('mode') != 'dedicated':
        raise ConnectorError('Shared metrics require explicit cluster labels')
    selectors = []
    for name, value in labels.items():
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) or not isinstance(value, str):
            raise ConnectorError('Invalid metric label')
        if name in ('mode', 'cpu', 'instance', 'mountpoint', 'fstype'):
            raise ConnectorError('Cluster scope labels must not select individual CPUs, nodes or filesystems')
        selectors.append(f'{name}={json.dumps(value)}')
    def scope(*extra):
        return '{' + ','.join(selectors + list(extra)) + '}'
    def fresh(selector, rate=False):
        # PromQL timestamps of arithmetic results are evaluation times. Gate
        # raw samples before aggregation, including when rate uses a 5m range.
        values = f'rate({selector}[5m])' if rate else selector
        return f'({values} and (timestamp({selector}) >= time() - 120))'
    cpu = 'max by(instance,cpu,mode)(' + fresh('node_cpu_seconds_total'+scope('mode!~"guest.*"'),True) + ')'
    idle = 'max by(instance,cpu,mode)(' + fresh('node_cpu_seconds_total'+scope('mode="idle"'),True) + ')'
    mem_total = 'max by(instance)(' + fresh('node_memory_MemTotal_bytes'+scope()) + ')'
    mem_avail = 'max by(instance)(' + fresh('node_memory_MemAvailable_bytes'+scope()) + ')'
    fs = scope('mountpoint="/"', 'fstype!~"tmpfs|overlay|squashfs|ramfs"')
    disk_total = 'max by(instance)(' + fresh('node_filesystem_size_bytes'+fs) + ')'
    disk_avail = 'max by(instance)(' + fresh('node_filesystem_avail_bytes'+fs) + ')'
    if per_node:
        return {'cpu':f'100 * (1 - sum by(instance)({idle}) / sum by(instance)({cpu}))',
                'ram':f'100 * (1 - ({mem_avail} and on(instance) {mem_total}) / ({mem_total} and on(instance) {mem_avail}))',
                'disk':f'100 * (1 - ({disk_avail} and on(instance) {disk_total}) / ({disk_total} and on(instance) {disk_avail}))'}
    return {
        'cpu': f'100 * (1 - sum({idle}) / sum({cpu}))',
        'ram': f'100 * (1 - sum({mem_avail} and on(instance) {mem_total}) / sum({mem_total} and on(instance) {mem_avail}))',
        'disk': f'100 * (1 - sum({disk_avail} and on(instance) {disk_total}) / sum({disk_total} and on(instance) {disk_avail}))',
    }


def dashboard_metrics(settings, window='1h'):
    """Three cluster ratios and bounded history; source timestamps are preserved."""
    from concurrent.futures import ThreadPoolExecutor
    windows = {'1h':3600, '6h':21600, '24h':86400}
    if window not in windows:
        raise ConnectorError('Unsupported history window')
    expressions = utilization_expressions(settings)
    node_expressions=utilization_expressions(settings,per_node=True)
    end = int(time.time())
    start, step = end - windows[window], max(30, windows[window] // 120)
    def read(metric):
        base_result = {'percent':None,'observed_at':None,'history':[],'error':None,'history_error':None,'nodes':[],'nodes_error':None}
        with metric_client(settings) as (client, base):
            endpoint = base + '/api/v1/'
            for history in (False, True):
                params = {'query':expressions[metric]}
                if history:
                    params.update(start=start, end=end, step=step)
                try:
                    reply = _request(client,'GET',endpoint+('query_range' if history else 'query'),params=params)
                    series = _metric_series(reply, 'matrix' if history else 'vector')
                    if len(series) > 1:
                        raise ConnectorError('Expected one cluster aggregate')
                    if not series:
                        raise ConnectorError('Пустая выборка: проверьте node_exporter, scrape и labels')
                    values = series[0].get('values') if history else [series[0].get('value')]
                    if not isinstance(values, list) or not values:
                        raise ConnectorError('Источник вернул некорректную метрику')
                    if history and len(values) > 122:
                        raise ConnectorError('История превышает лимит 122 точек; полнота не подтверждена')
                    points = []
                    for value in values:
                        try:
                            points.append(_metric_point(value, earliest=start-step if history else end-120,
                                                        latest=end+30, percentage=True))
                        except ConnectorError as exc:
                            if not history:
                                raise
                            base_result['history_error'] = str(exc)
                    if history:
                        base_result['history'] = points
                    elif points:
                        base_result['observed_at'], base_result['percent'] = points[-1]
                except ConnectorError as exc:
                    base_result['history_error' if history else 'error'] = str(exc)
            try:
                reply=_request(client,'GET',endpoint+'query',params={'query':node_expressions[metric]})
                series = _metric_series(reply, 'vector')
                if len(series) > 500:
                    raise ConnectorError('Детализация превышает лимит 500 узлов; полнота не подтверждена')
                nodes, instances = [], set()
                for row in series:
                    timestamp, number = _metric_point(row.get('value'), earliest=end-120, latest=end+30, percentage=True)
                    instance = row.get('metric', {}).get('instance')
                    if not isinstance(instance, str) or not instance or instance in instances:
                        raise ConnectorError('Некорректная или повторная идентичность узла метрик')
                    instances.add(instance)
                    nodes.append({'instance':instance,'percent':number,'observed_at':timestamp})
                if not nodes:
                    raise ConnectorError('Нет детализации по узлам')
                base_result['nodes'] = nodes
            except ConnectorError as exc:
                base_result['nodes_error'] = str(exc)
        return metric, base_result
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = dict(executor.map(read, ('cpu','ram','disk')))
    return {'window':window,'start':start,'end':end,'step':step,'metrics':results,
            'note':'Node exporter: CPU non-idle; RAM = total − available; диски = корневая ФС /, total − available. Покрытие зависит от scrape и меток.'}


def holmes_chat(settings, messages):
    """HolmesGPT 0.45.0 /api/chat contract; no silent fallback to a different engine."""
    ask = messages[-1]['content']
    with _client(settings, timeout=httpx.Timeout(180,connect=10)) as client:
        info=_request(client,'GET',_url(settings.get('url'))+'/api/info',params={'detail':'full'})
        if not isinstance(info,dict) or not isinstance(info.get('toolsets'),list) or info.get('auth_enabled') is not True or any(not isinstance(t,dict) or not isinstance(t.get('name'),str) for t in info['toolsets']):
            raise ConnectorError('Holmes не подтвердил защищённый контракт /api/info?detail=full')
        active={t.get('name') for t in info['toolsets'] if t.get('enabled') or t.get('status')=='enabled'}
        ready=any(t.get('name')=='pulse-readonly' and t.get('enabled') is True and t.get('status')=='enabled' for t in info['toolsets'])
        if not ready or active-{'pulse-readonly','skills'}:
            raise ConnectorError('Holmes tools не прошли проверку: разрешены только pulse-readonly и skills')
        data = _request(client,'POST',_url(settings.get('url'))+'/api/chat',json={
            'ask':ask,'model':settings.get('model') or None,'stream':False,
            'additional_system_prompt':messages[0]['content'],
            'conversation_history':[m for m in messages[1:-1] if m['role'] in ('user','assistant')],
            'enable_tool_approval':False})
    if not isinstance(data,dict) or not isinstance(data.get('analysis'),str) or not data['analysis'].strip():
        raise ConnectorError('HolmesGPT вернул неподдерживаемый ответ /api/chat')
    if data.get('error') or data.get('status') in ('error','failed','cancelled','incomplete') or data.get('finish_reason') not in (None,'stop'):
        from .astra_ai import IncompleteAnswer
        raise IncompleteAnswer('HolmesGPT сообщил об ошибке или незавершённом ответе',data['analysis'])
    if data.get('pending_approvals'):
        raise ConnectorError('Holmes запросил неподдерживаемое действие. Проверьте разрешённые read-only tools.')
    calls=data.get('tool_calls') or []
    if not isinstance(calls,list) or any(not isinstance(call,dict) for call in calls):
        raise ConnectorError('HolmesGPT вернул неподдерживаемый журнал tools')
    # Store names/status only, not raw tool arguments, bodies, headers or credentials.
    trace=[{'tool':str(c.get('tool_name') or c.get('name') or 'Holmes tool')[:120],
            'status':str(c.get('status') or 'reported')[:40]} for c in calls[:30] if isinstance(c,dict)]
    return redact_sensitive_text(data['analysis']),trace


def redact_structure(value):
    """Redact strings and credential fields before JSON encoding, preserving JSON shape."""
    return _redact_log_row(value)
