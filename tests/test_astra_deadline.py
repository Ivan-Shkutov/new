"""Astra pagination must share the selected diagnostic's collection budget."""
import httpx
import pytest

from pulse_app import connectors as c
from test_connectors import install_transport


SETTINGS = {'url': 'https://astra.example', 'token': 'fixture', 'cluster_value': 'one'}


def test_expired_astra_budget_does_not_send_a_request(monkeypatch):
    monkeypatch.setattr(c.time, 'monotonic', lambda: 10)
    install_transport(monkeypatch, lambda request: pytest.fail('No request after the deadline'))
    with pytest.raises(c.ConnectorError, match='time budget'):
        c.astra_logs(SETTINGS, 'default', 'pod', deadline=10)


def test_astra_rechecks_budget_before_next_page(monkeypatch):
    clock = {'now': 0}
    calls = []
    monkeypatch.setattr(c.time, 'monotonic', lambda: clock['now'])

    def request(client, method, url, **kwargs):
        calls.append(kwargs)
        clock['now'] = 2
        return {'items': [{}] * 250}

    monkeypatch.setattr(c, '_request', request)
    with pytest.raises(c.ConnectorError, match='time budget'):
        c.astra_logs(SETTINGS, 'default', 'pod', deadline=1)
    assert len(calls) == 1
    assert calls[0]['deadline'] == 1 and calls[0]['timeout'] == 1


def test_astra_request_timeout_is_bounded_by_remaining_budget(monkeypatch):
    monkeypatch.setattr(c.time, 'monotonic', lambda: 10)

    def handler(request):
        assert set(request.extensions['timeout'].values()) == {0.5}
        return httpx.Response(200, json={'items': []})

    install_transport(monkeypatch, handler)
    result = c.astra_logs(SETTINGS, 'default', 'pod', deadline=10.5)
    assert result['items'] == [] and not result['truncated']
