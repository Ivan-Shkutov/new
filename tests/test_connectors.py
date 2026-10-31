import json

import httpx
import pytest
import yaml

from pulse_app import connectors as c


def kube(**changes):
    value = {"apiVersion": "v1", "kind": "Config", "current-context": "read",
             "contexts": [{"name": "read", "context": {"cluster": "cluster", "user": "reader"}}],
             "clusters": [{"name": "cluster", "cluster": {"server": "https://cluster.example"}}],
             "users": [{"name": "reader", "user": {"token": "test-token"}}]}
    value.update(changes)
    return value


def install_transport(monkeypatch, handler):
    actual = httpx.Client
    def factory(**kwargs):
        return actual(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(c.httpx, "Client", factory)


def test_valid_kubeconfig():
    result = c.validate_kubeconfig(yaml.safe_dump(kube()))
    assert result["server"] == "https://cluster.example"
    assert result["token"] == "test-token"
    assert result["context"] == "read"
    json.dumps(result)


@pytest.mark.parametrize("override", [
    {"exec": {"command": "sh"}}, {"auth-provider": {"name": "gcp"}},
    {"tokenFile": "/secrets/token"}, {"client-key": "/private.key"},
    {"as": "admin"},
])
def test_reject_unsafe_credentials(override):
    value = kube()
    value["users"][0]["user"].update(override)
    with pytest.raises(c.ConnectorError):
        c.validate_kubeconfig(yaml.safe_dump(value))


@pytest.mark.parametrize("override", [
    {"insecure-skip-tls-verify": True}, {"certificate-authority": "/some.pem"},
    {"server": "http://cluster.example"}, {"server": "https://user:pass@cluster.example"},
    {"certificate-authority-data": "not-base64"},
])
def test_reject_unsafe_tls(override):
    value = kube()
    value["clusters"][0]["cluster"].update(override)
    with pytest.raises(c.ConnectorError):
        c.validate_kubeconfig(yaml.safe_dump(value))


def test_collection_paginates_redacts_and_marks_forbidden(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.url)
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path.endswith("nodes"):
            return httpx.Response(403, text="token=test-token upstream detail")
        if request.url.path.endswith("pods"):
            first = "continue" not in request.url.params
            return httpx.Response(200, json={"items": [{"metadata": {"name": "a" if first else "b", "annotations": {"token": "hidden"}},
                 "spec": {"containers": [{"env": [{"name": "PASSWORD", "value": "secret"}], "args": ["--password=secret"]}]}}],
                 "metadata": {"continue": "next" if first else ""}})
        if request.url.path.endswith("configmaps"):
            return httpx.Response(200, json={"items": [{"metadata": {"name": "cfg"}, "data": {"password": "secret"}, "binaryData": {"blob": "secret"}}]})
        return httpx.Response(200, json={"items": []})
    install_transport(monkeypatch, handler)
    result = c.collect_cluster(c.validate_kubeconfig(yaml.safe_dump(kube())))
    assert not result["complete"]
    assert len(result["resources"]["pods"]["items"]) == 2
    serialized = json.dumps(result)
    assert "hidden" not in serialized and "secret" not in serialized and "test-token" not in serialized
    assert result["resources"]["configmaps"]["items"][0]["keys"] == ["blob", "password"]
    assert result["resources"]["nodes"]["error"] == "Upstream HTTP 403"
    assert all("secrets" not in url.path and "exec" not in url.path for url in seen)


def test_collection_stops_pagination(monkeypatch):
    monkeypatch.setattr(c, "RESOURCE_PATHS", {"pods": "/api/v1/pods"})
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [], "metadata": {"continue": "never-ending"}})
    install_transport(monkeypatch, handler)
    result = c.collect_cluster({"server": "https://example.test", "token": "test"})
    assert len([r for r in requests if r.url.path=="/api/v1/pods"]) == c.MAX_PAGES
    assert result["resources"]["pods"]["truncated"]
    assert not result["complete"]


def test_logs_are_bounded_and_not_path_injected(monkeypatch):
    def handler(request):
        assert request.url.path == "/api/v1/namespaces/default/pods/pod-1/log"
        assert request.url.params["limitBytes"] == "262144"
        assert request.url.params["previous"] == "true"
        return httpx.Response(200, text="one log line")
    install_transport(monkeypatch, handler)
    config = {"server": "https://example.test"}
    assert c.resource_logs(config, "default", "pod-1", previous=True) == "one log line"
    with pytest.raises(c.ConnectorError):
        c.resource_logs(config, "../../secrets", "pod-1")


@pytest.mark.parametrize('uids,accepted,read_log', [
    (['replacement'], False, False),
    (['original', 'replacement'], False, True),
    (['original', 'original'], True, True),
])
def test_investigation_logs_reject_replaced_pod_uid(monkeypatch, uids, accepted, read_log):
    identities = iter(uids)
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith('/log'):
            return httpx.Response(200, text='log body')
        return httpx.Response(200, json={'metadata': {'uid': next(identities)}})
    install_transport(monkeypatch, handler)
    if accepted:
        assert c.resource_logs({'server': 'https://cluster.test'}, 'default', 'pod-1', expected_uid='original') == 'log body'
    else:
        with pytest.raises(c.ConnectorError, match='UID'):
            c.resource_logs({'server': 'https://cluster.test'}, 'default', 'pod-1', expected_uid='original')
    assert any(p.endswith('/log') for p in paths) == read_log


def test_metrics_require_scope_and_reject_promql(monkeypatch):
    with pytest.raises(c.ConnectorError, match="labels"):
        c.query_metrics({"url": "https://example.test"}, "cpu")
    with pytest.raises(c.ConnectorError, match="arbitrary"):
        c.query_metrics({}, "up{} or secret{}")
    def handler(request):
        query = request.url.params["query"]
        assert 'cluster="peoplehub"' in query
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": []}})
    install_transport(monkeypatch, handler)
    result = c.query_metrics({"url": "https://example.test/select/0/prometheus", "labels": {"cluster": "peoplehub"}}, "cpu")
    assert result["empty"]


@pytest.mark.parametrize('case',['invalid_data','invalid_result','invalid_labels','invalid_point','nan','stale','warnings'])
def test_query_metrics_rejects_invalid_or_partial_evidence(monkeypatch,case):
    import time
    row={'metric':{'instance':'node:9100'},'value':[time.time(),'25']}
    payload={'status':'success','data':{'resultType':'vector','result':[row]}}
    if case=='invalid_data':payload['data']=[]
    elif case=='invalid_result':payload['data']['result']={}
    elif case=='invalid_labels':row['metric']={'instance':12}
    elif case=='invalid_point':row['value']=[]
    elif case=='nan':row['value'][1]='NaN'
    elif case=='stale':row['value'][0]-=3600
    elif case=='warnings':payload['warnings']=['private upstream details']
    install_transport(monkeypatch,lambda request:httpx.Response(200,json=payload))
    with pytest.raises(c.ConnectorError) as caught:
        c.query_metrics({'url':'https://metrics.test','mode':'dedicated'},'node_memory_total')
    assert 'private upstream' not in str(caught.value)


def test_upstream_redirect_does_not_forward_credentials(monkeypatch):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://evil.example"})
    install_transport(monkeypatch, handler)
    with pytest.raises(c.ConnectorError, match="302"):
        c.query_metrics({"url": "https://example.test", "token": "secret", "mode": "dedicated"}, "memory")
    assert len(seen) == 1


def test_astra_exact_scope_filters_foreign_logs(monkeypatch):
    def handler(request):
        assert request.method == "POST"
        assert request.url.path == "/api/v1/logs/search/monitorLogs"
        return httpx.Response(200, json={"data": {"items": [
            {"labels.cluster": "a", "labels.k8s.namespace.name": "default", "labels.k8s.pod.name": "pod", "message": "ours"},
            {"labels.cluster": "ab", "labels.k8s.namespace.name": "default", "labels.k8s.pod.name": "pod", "message": "foreign"},
            {"message": "unscoped"},
        ]}})
    install_transport(monkeypatch, handler)
    result = c.astra_logs({"url": "https://astra.example", "token": "test", "cluster_value": "a"}, "default", "pod")
    assert len(result["items"]) == 1
    assert result["items"][0]["message"] == "ours"
    assert result["scanned"] == 3


def test_astra_requires_explicit_scope_and_supported_auth():
    with pytest.raises(c.ConnectorError):
        c.astra_logs({"url": "https://astra.example", "token": "test"}, "default", "pod")
    with pytest.raises(c.ConnectorError, match="service bearer"):
        c.astra_logs({"url": "https://astra.example", "cluster_value": "a", "username": "x", "password": "y"}, "default", "pod")


def test_llm_uses_fixed_endpoint_and_never_enables_tools(monkeypatch):
    def handler(request):
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        assert "tools" not in payload
        assert payload["max_tokens"] == 6000
        assert payload["stream"] is False
        return httpx.Response(200, json={"choices": [{"message": {"content": "Observed: pod is pending."}, "finish_reason": "stop"}]})
    install_transport(monkeypatch, handler)
    assert c.llm_chat({"url": "http://127.0.0.1:8002/v1", "model": "qwen"}, [{"role": "user", "content": "why"}]).startswith("Observed")


def test_llm_invalid_or_empty_result_is_failure(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": []}}]}))
    with pytest.raises(c.ConnectorError, match="no text"):
        c.llm_chat({"url": "http://127.0.0.1:8002", "model": "qwen"}, [{"role": "user", "content": "why"}])


def test_common_log_credentials_redacted():
    text = 'Authorization: Bearer abcdefg password="hello world" token=xyz {"api_key":"supersecret"} https://user:pass@example.com/a harmless=42'
    value = c.redact_sensitive_text(text)
    for secret in ("abcdefg", "hello world", "xyz", "supersecret", "user:pass"):
        assert secret not in value
    assert "harmless=42" in value


def test_collection_deadline_marks_all_remaining_resources(monkeypatch):
    clock = iter([0, 46] + [47] * len(c.RESOURCE_PATHS))
    monkeypatch.setattr(c.time, "monotonic", lambda: next(clock))
    install_transport(monkeypatch, lambda request: pytest.fail("Expired budget must not send requests"))
    result = c.collect_cluster({"server": "https://example.test"})
    assert len(result["resources"]) == len(c.RESOURCE_PATHS)
    assert all(row["error"] == "Collection time budget exceeded" for row in result["resources"].values())
    assert not result["complete"]


@pytest.mark.parametrize('tool_call',[False,True])
def test_local_llm_marks_token_limit_as_incomplete_and_never_executes_tools(monkeypatch,tool_call):
    from pulse_app.astra_ai import IncompleteAnswer
    def response(request):
        message={'content':'partial answer'}
        if tool_call:message['tool_calls']=[{'id':'bad'}]
        return httpx.Response(200,json={'choices':[{'message':message,'finish_reason':'tool_calls' if tool_call else 'length'}]})
    install_transport(monkeypatch,response)
    if tool_call:
        with pytest.raises(c.ConnectorError,match='never executes'):
            c.llm_chat({'url':'http://127.0.0.1:8002/v1','model':'qwen'},[{'role':'user','content':'question'}])
    else:
        with pytest.raises(IncompleteAnswer) as caught:
            c.llm_chat({'url':'http://127.0.0.1:8002/v1','model':'qwen'},[{'role':'user','content':'question'}])
        assert caught.value.partial=='partial answer'
