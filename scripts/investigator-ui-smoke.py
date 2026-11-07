#!/usr/bin/env python3
"""Real browser/API/read-only TLS Kubernetes fixtures; no production model claims."""
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

from playwright.sync_api import sync_playwright,expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pulse_app.store import Store

spec=importlib.util.spec_from_file_location('scenario_fixtures',ROOT/'scripts/scenarios-ui-smoke.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
original=fixture.handler_for
ANSWER='''## Что произошло
Pod выселен: EmptyDir data превысил 1Gi [E01].
## Анализ инцидента
Статус завершения подтверждён. Внешняя доступность не проверена.
## Причины
Источник роста файлов пока не установлен.
## Как исправить
Сопоставьте назначение файлов и правила их очистки.
## Последствия
Влияние на весь сервис не установлено.
## Профилактика
Настройте наблюдение за временным хранилищем после проверки приложения.'''


def handler_for(state):
    class Handler(original(state)):
        def do_POST(self):
            if self.path!='/v1/chat/completions':return self.respond({},405)
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            with state.lock:state.models.append(body);behavior=state.behavior
            if behavior=='slow':
                state.slow_started.set();state.slow_release.wait(20)
            if behavior=='error':return self.respond({'error':'fixture'},503)
            reason='length' if behavior=='truncated' else 'stop'
            return self.respond({'choices':[{'message':{'content':'<think>PRIVATE-REASONING</think>'+ANSWER},'finish_reason':reason}], 'usage':{'prompt_tokens':1200,'completion_tokens':180,'total_tokens':1380}})
    return Handler


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--base-url',default='http://127.0.0.1:18162')
    p.add_argument('--runtime',type=Path,required=True)
    p.add_argument('--screenshots',type=Path,required=True)
    a=p.parse_args();a.screenshots.mkdir(parents=True,exist_ok=True)
    state=fixture.Fixtures();fixture.handler_for=handler_for
    cid='chat-ui-'+uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='pulse-chat-fixture-') as temp:
        kube,model,threads,config=fixture.fixture_servers(state,Path(temp))
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=shutil.which('chromium'),headless=True,args=['--no-sandbox'])
                context=browser.new_context(http_credentials={'username':'pulse','password':(a.runtime/'secrets/portal-password').read_text().strip()},viewport={'width':1440,'height':1000})
                api=context.request;headers=fixture.HEADERS
                assert api.put(a.base_url+'/api/settings',headers=headers,data={'llm':{'url':f'http://127.0.0.1:{model.server_port}/v1','model':'qwen-fixture'}}).ok
                assert api.post(a.base_url+'/api/clusters',headers=headers,data={'id':cid,'name':'Peoplehub · тест','enabled':False,'kubeconfig':json.dumps(config)}).status==201
                snap={'observed_at':fixture.datetime.now(fixture.timezone.utc).isoformat(),'complete':True,'resources':{'pods':{'items':[fixture.POD],'error':None,'truncated':False}}}
                Store(a.runtime).execute('INSERT INTO snapshots VALUES (?,?)',(cid,json.dumps(snap)))
                page=context.new_page();errors=[];sessions=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('response',lambda r:sessions.append(r.json()['id']) if r.url.endswith('/api/sessions') and r.status==201 else None)
                page.goto(a.base_url+'/#overview');page.locator('#cluster-select').select_option(cid)
                expect(page.locator('.cluster-caption')).to_have_text('Peoplehub · тест')
                expect(page.locator('#navigation [data-page=diagnostics]')).to_have_count(0)
                page.get_by_role('button',name='Анализ: bad',exact=True).click()
                expect(page.locator('#ai-dialog')).to_be_visible()
                expect(page.locator('#ai-form [type=submit]')).to_be_enabled()
                expect(page.locator('.chat-turn')).to_have_count(0)
                page.screenshot(path=str(a.screenshots/'empty.png'))
                page.locator('[data-analysis=extended]').click()
                expect(page.locator('#ai-status')).to_contain_text('Ответ получен',timeout=30000)
                expect(page.locator('.chat-answer')).to_contain_text('Что произошло')
                expect(page.locator('.chat-answer')).to_contain_text('Профилактика')
                assert 'PRIVATE-REASONING' not in page.locator('.chat-answer').inner_text()
                assert not page.locator('.analysis-sources').evaluate('(e)=>e.open')
                page.screenshot(path=str(a.screenshots/'incident.png'))
                page.locator('#ai-form textarea').fill('Что проверить первым?');page.locator('#ai-form textarea').press('Enter')
                expect(page.locator('.chat-turn')).to_have_count(2)
                expect(page.locator('#ai-status')).to_contain_text('Ответ получен',timeout=30000)
                assert any(m['role']=='assistant' for m in state.models[-1]['messages'])
                assert 'PRIVATE-REASONING' not in json.dumps(state.models[-1]['messages'])
                old=sessions[-1];page.get_by_role('button',name='Закрыть анализ',exact=True).click()
                page.get_by_role('button',name='Анализ: bad',exact=True).click()
                expect(page.locator('#ai-form [type=submit]')).to_be_enabled()
                expect(page.locator('.chat-turn')).to_have_count(0)
                assert sessions[-1]!=old
                state.mode('truncated');page.locator('[data-analysis=brief]').click()
                expect(page.locator('#ai-status')).to_contain_text('Анализ не завершён',timeout=30000)
                expect(page.locator('#ai-conversation .notice.error').first).to_contain_text('length')
                expect(page.locator('#ai-conversation')).to_contain_text('Кратко по данным Pulse')
                page.screenshot(path=str(a.screenshots/'truncated.png'))
                page.get_by_role('button',name='Закрыть анализ',exact=True).click()
                state.mode('slow');page.get_by_role('button',name='Анализ: bad',exact=True).click()
                expect(page.locator('#ai-form [type=submit]')).to_be_enabled();page.locator('[data-analysis=extended]').click()
                assert state.slow_started.wait(5)
                page.get_by_role('button',name='Закрыть анализ',exact=True).click();state.mode('valid')
                page.get_by_role('button',name='Анализ: bad',exact=True).click()
                expect(page.locator('#ai-form [type=submit]')).to_be_enabled();state.slow_release.set()
                expect(page.locator('.chat-turn')).to_have_count(0)
                page.set_viewport_size({'width':390,'height':844})
                page.locator('[data-analysis=extended]').click();expect(page.locator('#ai-status')).to_contain_text('Ответ получен',timeout=30000)
                assert page.locator('#ai-dialog').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
                page.screenshot(path=str(a.screenshots/'mobile.png'))
                assert api.get(a.base_url+'/api/investigations').json()['items']==[]
                assert not errors,errors
                assert not state.errors,state.errors
                assert state.kubernetes and all(method=='GET' for method,_ in state.kubernetes)
                assert all(r['max_tokens']==8192 and r['stream'] is False for r in state.models)
                assert all('ТОЛЬКО JSON' not in r['messages'][0]['content'] for r in state.models)
                browser.close()
                print('PASS: overview modal, six-section text, follow-up, clean reopen, partial/error visibility, late response isolation, mobile, read-only TLS collection, bounded inference, no durable history.')
        finally:
            state.slow_release.set()
            for server in (kube,model):server.shutdown();server.server_close()
            for thread in threads:thread.join(timeout=3)


if __name__=='__main__':main()
