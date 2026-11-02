import json
from pathlib import Path
import pytest
from pulse_app import astra_ai, connectors, runbooks
from test_api import client, runtime, add, MUTATION
from test_sessions import configure, message


@pytest.mark.parametrize('mode',['chat','problem'])
@pytest.mark.parametrize('engine',['local','cluster'])
def test_shared_devops_rules_reach_both_dialogs_and_selected_engine(client,monkeypatch,mode,engine):
    configure(client)
    add(client,astra_ai={'enabled':True,'url':'http://astra.test/chat/completions','model':'qwen','api_key':'fixture'})
    calls=[]
    def selected(cfg,messages,*args):calls.append(messages);return 'ClusterIP — виртуальный IP сервиса.'
    def wrong(*args):pytest.fail('Unexpected provider')
    monkeypatch.setattr(connectors,'llm_chat',selected if engine=='local' else wrong)
    monkeypatch.setattr(astra_ai,'chat',selected if engine=='cluster' else wrong)
    sid=client.post('/api/sessions',json={'cluster_id':'one','mode':mode,'engine':engine},headers=MUTATION).json()['id']
    job=message(client,sid,'Чем ClusterIP отличается от Pod IP?')
    assert job['status']=='done'
    policy=calls[-1][0]['content']
    for name in ('BASE.md','KUBERNETES.md','NETWORK.md'):
        assert (Path(runbooks.__file__).with_name('ai_rules')/name).read_text() in policy
    trace=json.loads(job['trace'])
    assert any(t['tool']=='policy:senior-devops' and t['status']=='applied' for t in trace)
    assert any(t['tool']=='skill:network' for t in trace)
    if mode=='chat':assert 'Обычный диалог' in policy and 'до 150 слов' in policy


@pytest.mark.parametrize('question,expected',[
    ('Почему CoreDNS отвечает SERVFAIL?','network'),
    ('Как проверить MTU и CNI?','network'),
    ('Как работает NAT для Pod?','network'),
    ('Как проверить сетевую связность?','network'),
    ('Чем отличается TCP timeout от connection refused?','network'),
    ('x509: certificate signed by unknown authority','certificates'),
    ('Pod завис в ImagePullBackOff','images'),
    ('Объясни CrashLoopBackOff','crashloop'),
])
def test_question_selects_relevant_readonly_scenario(question,expected):
    rows=runbooks.select_runbooks({},question)
    assert expected in [r['id'] for r in rows]
    assert len(rows)<=4 and rows[0]['id']=='general'


def test_metadata_keys_and_generic_pressure_notes_are_not_symptoms():
    context={'pod':{'node':'node-network-01','phase':'Running'},'findings':[{'fact':'Текущие Ready/Pressure не исключают прошлого давления.'}]}
    assert [r['id'] for r in runbooks.select_runbooks(context,'Как устроен Deployment?')]==['general','rollout']
    context['pod'].update(reason='Evicted',message='Usage of EmptyDir volume "data" exceeds the limit "1Gi".')
    keys=[r['id'] for r in runbooks.select_runbooks(context,'Как проверить DNS и TLS?')]
    assert 'eviction' in keys and 'network' in keys and len(keys)<=4


def test_packaged_markdown_rules_change_the_build_id(tmp_path,monkeypatch):
    from pulse_app import build
    package=tmp_path/'pulse_app';rules=package/'ai_rules';rules.mkdir(parents=True)
    policy=rules/'BASE.md';policy.write_text('Policy one')
    monkeypatch.setattr(build,'__file__',str(package/'build.py'))
    build.identity.cache_clear()
    try:
        before=build.identity()['build_id']
        policy.write_text('Policy two')
        build.identity.cache_clear()
        assert build.identity()['build_id']!=before
    finally:build.identity.cache_clear()
