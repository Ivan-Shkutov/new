#!/usr/bin/env python3
"""Compact overview/drilldown/chat browser checks, with clearly identified fixtures."""
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
    data=fixture.payload('critical');data['version']='0.11.0'
    issues=[]
    for i in range(80):
        row=dict(data['issues'][i%len(data['issues'])]);row['name']+=f'-{i:02d}'
        row['id']=hashlib.sha256(str(i).encode()).hexdigest()[:32];issues.append(row)
    jobs=[];errors=[];last_request=[]
    def problem_route(route):
        q=parse_qs(urlparse(route.request.url).query);rows=issues
        if q.get('q'):rows=[i for i in rows if q['q'][0].lower() in json.dumps(i).lower()]
        if q.get('severity',['all'])[0]!='all':rows=[i for i in rows if i['severity']==q['severity'][0]]
        offset=int(q.get('offset',[0])[0]);limit=int(q.get('limit',[25])[0]);route.fulfill(json={'items':rows[offset:offset+limit],'total':len(rows),'offset':offset,'limit':limit,'coverage':[]})
    def job_route(route):
        if route.request.method=='POST':
            body=route.request.post_data_json;last_request.append(body)
            assert body['cluster_id']=='peoplehub'
            if jobs:assert body['previous_id']==jobs[-1]['id']
            else:assert body['problem_id']==issues[0]['id']
            job={'id':f'job-{len(jobs)}','cluster_id':'peoplehub','question':body['question'],'status':'done','answer':'## Вывод\nПричина пока не установлена.\n- Источник: condition Ready=False.\n<script>alert(1)</script>\n| Проверка | Результат |\n| --- | --- |\n| UID | Тестовые данные |\n## Проверка\nСверьте логи kubelet.','context':json.dumps(issues[0]),'evidence':json.dumps({'records':[{'id':'E01','check':'Pod status (fixture)','status':'ok','method':'GET','path':'/api/v1/namespaces/application/pods/test','equivalent_command':'kubectl -n application get pod test -o json','data':{'reason':'Evicted'}}],'unavailable':0}),'trace':json.dumps([{'tool':'skill:nodes','status':'selected'},{'tool':'snapshot','status':'ok'}])}
            jobs.append(job);route.fulfill(status=202,json={'id':job['id'],'status':'queued'})
        elif '/investigations?' in route.request.url:route.fulfill(json={'items':jobs[::-1]})
        else:route.fulfill(json=jobs[-1])
    def metrics(route):
        now=time.time();result={'configured':True,'window':'1h','start':now-3600,'end':now,'step':30,'note':'Тестовые данные, не состояние сервера пользователя','metrics':{}}
        for k,n in [('cpu',38),('ram',64),('disk',72)]:result['metrics'][k]={'percent':n,'observed_at':now,'history':[[now-3600+i*30,n+(i%7)-3] for i in range(121)]}
        if 'metrics-detail' in route.request.url:route.fulfill(json={'metric':'cpu','summary':result,'series':{'node_cpu':{'data':{'result':[{'metric':{'instance':'node-01'},'value':[now,'2.4']}]}}},'units':{'node_cpu':'cores'}})
        else:route.fulfill(json=result)
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        context=browser.new_context(http_credentials={'username':'pulse','password':args.password_file.read_text().strip()},viewport={'width':1920,'height':1080})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/clusters',lambda r:r.fulfill(json={'items':[{'id':'peoplehub','name':'PeopleHub (тест)','enabled':True}]}))
        page.route('**/api/dashboard*',lambda r:r.fulfill(json=data))
        page.route('**/api/problems?*',problem_route)
        page.route('**/api/investigations**',job_route)
        page.route('**/api/investigations/*',job_route)
        page.route('**/api/clusters/*/dashboard-metrics?*',metrics)
        page.route('**/api/clusters/*/metrics-detail?*',metrics)
        page.goto(args.base_url)
        page.locator('[data-testid=utilization-cpu]').get_by_text('38%',exact=True).wait_for()
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
        page.locator('[data-testid=utilization-cpu]').click()
        page.locator('#dashboard-detail').wait_for(state='visible')
        page.locator('#detail-body').get_by_text('instance=node-01',exact=True).wait_for()
        assert page.locator('#detail-body .history-chart').count()==1
        page.screenshot(path=str(args.screenshots/'cpu-detail.png'),full_page=True)
        page.get_by_role('button',name='Закрыть dashboard',exact=True).click()
        page.locator('.problem-table-body .ai-spark').first.click()
        page.locator('#ai-dialog').wait_for(state='visible')
        page.locator('#ai-form textarea').fill('В чём причина?');page.locator('#ai-form button[type=submit]').click()
        try:page.locator('.chat-answer').first.wait_for(timeout=5000)
        except Exception:
            print('Chat test failed:',page.locator('#ai-status').inner_text(),errors)
            page.screenshot(path='/tmp/pulse-chat-failure.png',full_page=True)
            raise
        assert page.locator('#ai-conversation script').count()==0
        assert '<script>alert(1)</script>' in page.locator('.chat-answer').inner_text()
        page.locator('#ai-form textarea').fill('Что проверить дальше?');page.locator('#ai-form button[type=submit]').click()
        expect(page.locator('.chat-answer')).to_have_count(2)
        assert len(last_request)==2
        page.get_by_role('button',name='Нужные команды',exact=True).click()
        expect(page.locator('.chat-answer')).to_have_count(3)
        assert last_request[-1]['mode']=='commands'
        assert page.locator('.evidence-journal').count()==3
        assert page.locator('.analysis-table').count()==3
        page.screenshot(path=str(args.screenshots/'ai-context.png'),full_page=True)
        page.get_by_role('button',name='Закрыть анализ',exact=True).click()
        page.locator('.problem-pager').get_by_role('button',name='→',exact=True).click()
        expect(page.locator('.problem-pager')).to_contain_text('26–50')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()
    print('PASS: one screen at 1920x1080 and 1366x768; 3/3/7 rows; real-detail contract; 80 paginated problems; context chat/follow-up/presets/evidence; safe rendering; mobile')


if __name__=='__main__':main()
