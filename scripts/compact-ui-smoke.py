#!/usr/bin/env python3
"""Compact overview/drilldown/scenario browser checks with explicit API fixtures."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse,parse_qs
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright, expect
from importlib.util import spec_from_file_location,module_from_spec
spec=spec_from_file_location('health_fixture',Path(__file__).with_name('health-ui-fixtures.py'))
fixture=module_from_spec(spec);spec.loader.exec_module(fixture)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:18090')
    parser.add_argument('--password-file',type=Path,required=True)
    parser.add_argument('--screenshots',type=Path,default=Path('/tmp/pulse-compact'))
    args=parser.parse_args();args.screenshots.mkdir(parents=True,exist_ok=True)
    data=fixture.payload('overview');data['version']='0.15.0'
    issues=[]
    for i in range(80):
        row=dict(data['issues'][i%len(data['issues'])]);row['name']+=f'-{i:02d}'
        row['id']=hashlib.sha256(str(i).encode()).hexdigest()[:32];issues.append(row)
    all_issues=issues;issues=all_issues[:1]
    jobs=[];errors=[];last_request=[];broken=[False]
    scenarios=[('brief','Краткий разбор'),('deep','Подробный разбор'),('causes','Причины сбоя'),('impact','Последствия сбоя'),('plan','План устранения'),('recovery','Проверка восстановления')]
    def problem_route(route):
        q=parse_qs(urlparse(route.request.url).query);rows=issues
        if q.get('q'):rows=[i for i in rows if q['q'][0].lower() in json.dumps(i).lower()]
        if q.get('severity',['all'])[0]!='all':rows=[i for i in rows if i['severity']==q['severity'][0]]
        offset=int(q.get('offset',[0])[0]);limit=int(q.get('limit',[25])[0]);route.fulfill(json={'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,'coverage':[]})
    def job_route(route):
        if route.request.method=='POST':
            body=route.request.post_data_json;last_request.append(body)
            assert body['problem_id']==issues[0]['id']
            assert body['scenario'] in dict(scenarios)
            assert not {'question','prompt','previous_id'} & body.keys()
            result={'id':body['scenario'],'title':dict(scenarios)[body['scenario']],
                    'status':'partial','origin':'pulse','summary':'Причина пока не установлена. <script>alert(1)</script>',
                    'rows':[{'label':'Объект','value':'Тестовые данные','evidence_ids':['E01']}],
                    'sections':[],'steps':[],'limitations':[],
                    'recovery':{'state':'unconfirmed','checks':[]},
                    'model':{'status':'not_used' if body['scenario']=='recovery' else 'complete','error':''},
                    'observed_at':'2026-11-02T13:45:50Z'}
            if body['scenario']=='plan':
                result['steps']=[{'title':'Проверить исходный Pod','detail':'Сверьте status.message и UID.',
                                  'evidence_ids':['E01'],'commands':[{'command':'kubectl -n application get pod test -o json',
                                  'purpose':'Прочитать статус выбранного Pod','expected':'UID и причина совпадают с выбранным объектом.',
                                  'scope':'application / test'}]}]
            job={'id':f'job-{len(last_request)}','cluster_id':'peoplehub','scenario':body['scenario'],
                 'status':'done','answer':'Неиспользуемый сырой ответ модели.',
                 'scenario_result':result,'context':json.dumps(issues[0]),
                 'evidence':json.dumps({'records':[{'id':'E01','check':'Pod status (fixture)','status':'ok',
                    'complete':True,'method':'GET','path':'/api/v1/namespaces/application/pods/test',
                    'equivalent_command':'kubectl -n application get pod test -o json','data':{'reason':'Evicted'}}],
                    'unavailable':0}),
                 'trace':json.dumps([{'tool':'skill:nodes','status':'selected'},{'tool':'snapshot','status':'ok'}])}
            if broken[0]:
                error='Local model HTTP 503 (fixture)'
                job.update(status='failed',error=error,answer='OOM исключён по коду 137.\nkubectl get deployment unfinished')
                result.update(summary='Подтверждено: превышен лимит EmptyDir data — 1Gi.',
                              model={'status':'unavailable','error':error})
                result['rows']=[{'label':'Причина','value':'Превышен лимит временного тома; заполнение диска узла не доказано.','evidence_ids':['E01']}]
                report=json.loads(job['evidence']);report['pod']={'phase':'Failed','reason':'Evicted','message':'Usage of EmptyDir volume "data" exceeds the limit "1Gi".','source_type':'cached'};report['findings']=[{'source':'E01','fact':'Pod phase=Failed; reason=Evicted.'}];job['evidence']=json.dumps(report)
            jobs.append(job);route.fulfill(status=202,json={'id':job['id'],'status':'queued'})
        else:
            jid=urlparse(route.request.url).path.rsplit('/',1)[-1]
            route.fulfill(json=next(job for job in jobs if job['id']==jid))
    def metrics(route):
        now=time.time();result={'configured':True,'window':'1h','start':now-3600,'end':now,'step':30,'note':'Тестовые данные, не состояние сервера пользователя','metrics':{}}
        for k,n in [('cpu',38),('ram',64),('disk',72)]:result['metrics'][k]={'percent':n,'observed_at':now,'history':[[now-3600+i*30,n+(i%7)-3] for i in range(121)],'nodes':[{'instance':f'10.128.73.{i+5}:9100','percent':(94 if k=='disk' and i==2 else n+i),'observed_at':now} for i in range(6)]}
        result['expected_nodes']=6
        if 'metrics-detail' in route.request.url:route.fulfill(json={'metric':'cpu','summary':result,'series':{'node_cpu':{'data':{'result':[{'metric':{'instance':'node-01'},'value':[now,'2.4']}]}}},'units':{'node_cpu':'cores'}})
        else:route.fulfill(json=result)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        context=browser.new_context(http_credentials={'username':'pulse','password':args.password_file.read_text().strip()},viewport={'width':1920,'height':1080})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/clusters',lambda r:r.fulfill(json={'items':[{'id':'peoplehub','name':'PeopleHub (тест)','enabled':True}]}))
        page.route('**/api/dashboard*',lambda r:r.fulfill(json=data))
        page.route('**/api/problems?*',problem_route)
        page.route('**/api/investigations**',lambda r:r.fulfill(json={'items':[]}))
        page.route('**/api/scenarios',lambda r:r.fulfill(json={'items':[{'id':sid,'title':title,'description':'Тестовый сценарий'} for sid,title in scenarios]}))
        closed=[]
        def sessions_route(route):
            if route.request.method=='POST':
                jobs.clear();route.fulfill(status=201,json={'id':'volatile-'+str(len(closed)),'persistent':False})
            else:closed.append(route.request.url);route.fulfill(json={'ok':True})
        page.route('**/api/sessions',sessions_route)
        page.route('**/api/sessions/*',sessions_route)
        page.route('**/api/sessions/*/scenarios',job_route)
        page.route('**/api/sessions/*/jobs/*',job_route)
        page.route('**/api/clusters/*/dashboard-metrics?*',metrics)
        page.route('**/api/clusters/*/metrics-detail?*',metrics)
        page.goto(args.base_url)
        try:page.locator('[data-testid=utilization-cpu]').get_by_text('38%',exact=True).wait_for(timeout=5000)
        except Exception:
            print('Overview failure:',errors,page.locator('#global-error').inner_text())
            page.screenshot(path='/tmp/overview-0120-failure.png',full_page=True)
            raise
        page.locator('.problem-table-body tr').first.wait_for()
        assert page.locator('.compact-top .signal-tile').count()==3
        assert page.locator('.workload-card').count()==7
        assert len({round(b['y']) for b in page.locator('.workload-card').evaluate_all('(es)=>es.map(e=>e.getBoundingClientRect().toJSON())')})==1
        for width,height in [(1920,1080),(1366,768)]:
            page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollHeight<=innerHeight+1'),(width,height,'vertical overflow',page.evaluate('document.documentElement.scrollHeight'))
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,height,'horizontal overflow')
            footer=page.locator('.problem-footer').bounding_box()
            assert footer and footer['y']+footer['height']<=height+1,(width,height,'problem footer clipped',footer)
            assert page.locator('.problem-scroll').bounding_box()['height']>=70,(width,height,'no usable problem list')
            assert len({round(b['y']) for b in page.locator('.workload-card').evaluate_all('(es)=>es.map(e=>e.getBoundingClientRect().toJSON())')})==1
            page.screenshot(path=str(args.screenshots/f'overview-{width}.png'),full_page=True)
        expect(page.locator('.passport-meta')).to_contain_text('API v1.30.8')
        expect(page.locator('.passport-meta')).to_contain_text('kubelet v1.30.14')
        expect(page.locator('[data-testid=pods-ready]')).to_contain_text('162/163')
        expect(page.locator('[data-testid=nodes-ready]')).to_have_class(__import__('re').compile('healthy'))
        expect(page.locator('[data-testid=utilization-disk]')).to_have_class(__import__('re').compile('metric-critical'))
        page.locator('[data-testid=nodes-ready]').click()
        page.locator('#detail-body .node-dashboard tbody tr').first.wait_for()
        assert page.locator('#detail-body .node-dashboard tbody tr').count()==6
        page.screenshot(path=str(args.screenshots/'nodes-detail.png'),full_page=True)
        page.get_by_role('button',name='Закрыть dashboard',exact=True).click()
        page.get_by_role('button',name='Сведения',exact=True).click()
        expect(page.locator('#detail-body')).to_contain_text('v1.30.8')
        page.get_by_role('button',name='Закрыть dashboard',exact=True).click()
        issues=all_issues
        page.locator('#refresh-page').click()
        page.locator('[data-testid=utilization-cpu]').click()
        page.locator('#dashboard-detail').wait_for(state='visible')
        page.locator('#detail-body').get_by_text('instance=node-01',exact=True).wait_for()
        assert page.locator('#detail-body .history-chart').count()==1
        page.screenshot(path=str(args.screenshots/'cpu-detail.png'),full_page=True)
        page.get_by_role('button',name='Закрыть dashboard',exact=True).click()
        page.locator('.problem-table-body .ai-spark').first.click()
        page.locator('#page-diagnostics').wait_for(state='visible')
        try:page.locator('.scenario-summary').wait_for(timeout=5000)
        except Exception:
            print('Scenario test failed:',page.locator('#scenario-status').inner_text(),errors)
            page.screenshot(path='/tmp/pulse-scenario-failure.png',full_page=True)
            raise
        expect(page.locator('#scenario-buttons [data-scenario]')).to_have_count(6)
        assert page.locator('#page-diagnostics textarea').count()==0
        assert page.locator('#scenario-result-body script').count()==0
        assert '<script>alert(1)</script>' in page.locator('.scenario-summary').inner_text()
        assert 'Неиспользуемый сырой ответ' not in page.locator('#scenario-result-body').inner_text()
        expect(page.locator('.scenario-answer-table')).to_contain_text('Тестовые данные')
        assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
        assert last_request[-1]['scenario']=='brief'
        page.screenshot(path=str(args.screenshots/'analysis-brief.png'),full_page=True)
        page.locator('#scenario-sources>summary').click()
        page.locator('#scenario-evidence-E01>summary').click()
        expect(page.locator('#scenario-evidence-E01')).to_contain_text('kubectl -n application get pod test -o json')
        for scenario,_ in scenarios[1:]:
            page.locator(f'#scenario-buttons [data-scenario={scenario}]').click()
            expect(page.locator('.scenario-summary')).to_have_count(1)
            expect(page.locator('#scenario-result')).to_have_attribute('aria-busy','false')
            expect(page.locator('#scenario-result-body')).to_have_attribute('data-scenario-result',scenario)
            assert last_request[-1]['scenario']==scenario
            assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
            assert page.locator('#scenario-result-body script').count()==0
            if scenario=='plan':
                expect(page.locator('.scenario-command code')).to_have_text('kubectl -n application get pod test -o json')
                expect(page.locator('#scenario-result-body')).to_contain_text('Ожидается: UID и причина совпадают')
                page.screenshot(path=str(args.screenshots/'ai-context.png'),full_page=True)
            if scenario=='recovery':
                expect(page.locator('.scenario-recovery-state')).to_have_attribute('data-recovery-state','unconfirmed')
        assert len(last_request)==6
        assert all('question' not in body and 'previous_id' not in body for body in last_request)
        page.screenshot(path=str(args.screenshots/'ai-context.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        expect(page.locator('#scenario-rerun')).to_be_visible()
        assert page.locator('#page-diagnostics textarea').count()==0
        page.screenshot(path=str(args.screenshots/'ai-mobile.png'),full_page=True)
        broken[0]=True
        requests_before_provider=len(last_request)
        page.locator('#scenario-provider').select_option('local')
        expect(page.locator('.scenario-summary')).to_have_count(0)
        assert len(last_request)==requests_before_provider
        page.locator('#scenario-buttons [data-scenario=brief]').click()
        expect(page.locator('.scenario-summary')).to_have_count(1)
        expect(page.locator('.scenario-summary')).to_contain_text('превышен лимит EmptyDir')
        expect(page.locator('#scenario-status')).to_contain_text('ИИ не завершил разбор')
        assert 'OOM исключён' not in page.locator('#scenario-result-body').inner_text()
        assert 'unfinished' not in page.locator('#scenario-result-body').inner_text()
        assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
        page.locator('#scenario-sources>summary').click()
        expect(page.locator('#scenario-sources')).to_contain_text('Local model HTTP 503')
        assert 'OOM исключён' not in page.locator('#scenario-sources').inner_text()
        expect(page.locator('.scenario-answer-table')).to_contain_text('заполнение диска узла не доказано')
        page.screenshot(path=str(args.screenshots/'interrupted-reply.png'),full_page=True)
        # Leaving the page deletes the volatile session; returning shows an empty state.
        page.locator('a[data-page=overview]').click()
        page.locator('#page-overview').wait_for(state='visible')
        page.locator('a[data-page=diagnostics]').click()
        page.locator('#page-diagnostics').wait_for(state='visible')
        expect(page.locator('.scenario-summary')).to_have_count(0)
        expect(page.locator('#scenario-target')).to_contain_text('Выберите объект')
        expect(page.locator('#scenario-rerun')).to_be_disabled()
        assert closed
        page.locator('a[data-page=overview]').click()
        page.locator('#page-overview').wait_for(state='visible')
        broken[0]=False
        page.locator('.problem-table-body .ai-spark').first.click()
        expect(page.locator('.scenario-summary')).to_have_count(1)
        expect(page.locator('.scenario-summary')).to_contain_text('Причина пока не установлена')
        assert len(jobs)==1
        assert last_request[-1]['scenario']=='brief'
        assert not page.locator('#scenario-sources').evaluate('(e)=>e.open')
        page.locator('a[data-page=overview]').click()
        page.locator('#page-overview').wait_for(state='visible')
        page.locator('.problem-pager').get_by_role('button',name='→',exact=True).click()
        expect(page.locator('.problem-pager')).to_contain_text('26–50')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        data=fixture.payload('critical')
        page.locator('#refresh-page').click()
        expect(page.locator('[data-testid=nodes-ready]')).to_contain_text('5/6')
        expect(page.locator('[data-testid=nodes-ready]')).to_have_class(__import__('re').compile('critical'))
        data=fixture.payload('stale')
        page.locator('#refresh-page').click()
        expect(page.locator('[data-testid=nodes-ready]')).to_have_class(__import__('re').compile('unknown'))
        assert not errors,errors
        browser.close()
    print('PASS: fixture overview at 1920x1080 and 1366x768; 3/3/7 rows; node/metric drilldowns; 80 paginated problems; six scenarios; one result; collapsed evidence; local-model failure fallback; safe DOM; fresh reopen; mobile; stale/critical states')


if __name__=='__main__':main()
