"""Regression cases for bounded evidence and explicit model completion."""
import json
import httpx
import pytest
from pulse_app import astra_ai, cluster_chat, connectors as c, diagnostics as d
from test_astra_ai import SETTINGS, MESSAGES, event, response
from test_connectors import install_transport
from test_cluster_chat import pods


@pytest.mark.parametrize('limit',[512,2000,30000])
def test_snapshot_budget_includes_node_capacity_and_metadata(limit):
    snap={'complete':True,'errors':['unavailable '*20000],
          'cluster_info':{'server_version':'v1.30','discovery':'x'*100000},
          'resources':{'pods':{'items':pods()},'nodes':{'items':[
              {'metadata':{'name':f'node-{i}'},'status':{'capacity':{'custom':'x'*30000},
               'conditions':[{'type':'Ready','status':'True'}]}} for i in range(30)]}}}
    out=cluster_chat.compact_snapshot(snap,limit=limit)
    assert len(json.dumps(out,ensure_ascii=False))<=limit
    assert out['resources']['pods']['count']==163
    assert out['resources']['pods']['unhealthy']==1
    if limit==30000:assert out['resources']['pods']['items'][0]['reason']=='Evicted'


def test_evidence_budget_counts_queries_and_preserves_all_ids():
    report={'pod':{'reason':'Evicted','message':'EmptyDir data exceeds 1Gi'},
            'findings':[{'source':'E01','fact':'x'*20000}],
            'records':[{'id':f'E{i:02d}','check':'Metric','status':'ok',
                        'query':{'query':'metric{label="'+'x'*10000+'"}'},
                        'data':{'values':['\\"'*10000]}} for i in range(1,56)]}
    output=d.model_evidence(report)
    assert len(json.dumps(output,ensure_ascii=False))<=65000
    assert [r['id'] for r in output['records']]==[r['id'] for r in report['records']]
    assert all(r['status']=='ok' for r in output['records'])
    assert output['pod']==report['pod']
    assert output['context_truncated']
    assert len(json.dumps(d.small({'text':'\\"'*10000},200),ensure_ascii=False))<=200


@pytest.mark.parametrize('result,project',[
    ({'items':[],'metadata':{'continue':'another-page'}},lambda v:{'items':[]}),
    ({'items':[{'message':'x'*10000}]},lambda v:v),
    ({'items':[]},lambda v:{'items':[],'truncated':True}),
])
def test_partial_evidence_is_not_a_complete_success(result,project):
    collector=d.Collector({})
    collector.run('Events','','GET','/events',lambda:result,project=project)
    collector.finish();report=collector.report
    assert report['records'][0]['status']=='partial'
    assert not report['records'][0]['complete'] and not report['complete']
    assert report['partial']==1 and report['unavailable']==0
    assert 'неполные данные: 1' in d.verified_summary(report)


def test_astra_done_without_finish_reason_preserves_partial(monkeypatch):
    install_transport(monkeypatch,lambda request:response(event('Partial answer')+'data: [DONE]\n\n'))
    with pytest.raises(astra_ai.IncompleteAnswer) as exc:astra_ai.chat(SETTINGS,MESSAGES)
    assert exc.value.partial=='Partial answer'
    assert 'finish_reason=stop' in str(exc.value)


@pytest.mark.parametrize('bad_messages',[None,{},['text']])
def test_astra_invalid_context_is_a_connector_error(bad_messages):
    with pytest.raises(c.ConnectorError):astra_ai.chat(SETTINGS,bad_messages)


def test_astra_logs_credentials_cannot_substitute_for_ai_key(monkeypatch):
    install_transport(monkeypatch,lambda request:pytest.fail('No AI credential; do not send a request'))
    with pytest.raises(c.ConnectorError,match='Bearer token Astra AI'):
        astra_ai.chat({**SETTINGS,'api_key':'','token':'logs-only','username':'logs-user'},MESSAGES)


@pytest.mark.parametrize('finish',[None,'length','content_filter'])
def test_local_llm_requires_explicit_success(monkeypatch,finish):
    install_transport(monkeypatch,lambda request:httpx.Response(200,json={'choices':[
        {'message':{'content':'Partial token=private-value'},'finish_reason':finish}]}))
    with pytest.raises(astra_ai.IncompleteAnswer) as exc:
        c.llm_chat({'url':'http://local.test','model':'qwen'},MESSAGES)
    assert exc.value.partial.startswith('Partial') and 'private-value' not in exc.value.partial


def test_astra_future_start_with_implicit_end_rejected_before_network(monkeypatch):
    install_transport(monkeypatch,lambda request:pytest.fail('Invalid time range'))
    with pytest.raises(c.ConnectorError,match='end must follow start'):
        c.astra_logs({'url':'https://astra.test','token':'logs','cluster_value':'one'},'ns','pod','9999-01-01T00:00:00Z')


@pytest.mark.parametrize('reply',[
    {'analysis':'Partial','error':'PRIVATE-UPSTREAM-ERROR'},
    {'analysis':'Partial','status':'incomplete'},
    {'analysis':'Partial','finish_reason':'length'},
])
def test_holmes_explicit_failure_retains_partial(monkeypatch,reply):
    def request(client,method,url,**kwargs):
        if url.endswith('/api/info'):return {'auth_enabled':True,'toolsets':[{'name':'pulse-readonly','enabled':True,'status':'enabled'}]}
        return reply
    monkeypatch.setattr(c,'_request',request)
    with pytest.raises(astra_ai.IncompleteAnswer) as exc:c.holmes_chat({'url':'http://holmes.test'},MESSAGES)
    assert exc.value.partial=='Partial' and 'PRIVATE' not in str(exc.value)


@pytest.mark.parametrize('toolsets',[[None],[{'name':'pulse-readonly','enabled':True,'status':'failed'}],
    [{'name':'pulse-readonly','enabled':True,'status':'enabled'},{'name':'bash','enabled':True,'status':'pending'}]])
def test_holmes_unverified_toolsets_never_reach_chat(monkeypatch,toolsets):
    def request(client,method,url,**kwargs):
        assert url.endswith('/api/info')
        return {'auth_enabled':True,'toolsets':toolsets}
    monkeypatch.setattr(c,'_request',request)
    with pytest.raises(c.ConnectorError):c.holmes_chat({'url':'http://holmes.test'},MESSAGES)
