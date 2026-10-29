#!/usr/bin/env python3
"""Browser regression scenarios built from real health calculations and explicit fixtures."""
import argparse
from datetime import datetime,timedelta,timezone
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pulse_app.dashboard import summarize,combine
from pulse_app.health import aggregate
from playwright.sync_api import sync_playwright


def payload(case):
    ts=(datetime.now(timezone.utc)-timedelta(hours=1 if case=='stale' else 0)).isoformat()
    kinds=('nodes','pods','daemonsets','deployments','replicasets','statefulsets','jobs','cronjobs','events','persistentvolumeclaims')
    snap={'observed_at':ts,'complete':True,'errors':[],'resources':{k:{'items':[],'error':None,'truncated':False} for k in kinds}}
    for i in range(6):
        bad=case=='critical' and i==4
        snap['resources']['nodes']['items'].append({'metadata':{'name':f'peoplehub-node-0{i+1}'},'status':{'conditions':[{'type':'Ready','status':'False' if bad else 'True','reason':'KubeletNotReady' if bad else 'KubeletReady'},{'type':'DiskPressure','status':'False'}],'nodeInfo':{'kubeletVersion':'v1.30.14'},'addresses':[{'type':'InternalIP','address':f'10.128.73.{i+5}'}]}})
    for i in range(163):
        bad=case=='critical' and i==0
        snap['resources']['pods']['items'].append({'metadata':{'name':'web-api' if bad else f'pod-{i:03d}','namespace':'application','creationTimestamp':ts},'spec':{'nodeName':f'peoplehub-node-0{i%6+1}'},'status':{'phase':'Running','conditions':[{'type':'Ready','status':'False' if bad else 'True','lastTransitionTime':ts}],'containerStatuses':[{'restartCount':2 if bad else 0,'state':{'waiting':{'reason':'CrashLoopBackOff'}} if bad else {'running':{}}}]}})
    for kind,n in [('daemonsets',4),('deployments',14),('replicasets',20),('statefulsets',4)]:
        snap['resources'][kind]['items']=[{'metadata':{'name':f'{kind}-{i}'},'spec':{'replicas':2},'status':{'readyReplicas':2,'desiredNumberScheduled':6,'numberReady':6}} for i in range(n)]
    snap['resources']['jobs']['items']=[{'status':{'active':1}},{'status':{'conditions':[{'type':'Complete','status':'True'}]}}]
    snap['resources']['cronjobs']['items']=[{'spec':{'suspend':False}},{'spec':{'suspend':True}}]
    summary=summarize(snap,'peoplehub','PeopleHub (тест)')
    row={'id':'peoplehub','name':'PeopleHub (тест)','enabled':True,'observed_at':ts,'status':'ok','counts':{'nodes':6,'pods':163,'namespaces':14},'sources':{'metrics':True,'astra':False},'error':'',**summary}
    return {'version':'0.8.0','clusters':[row],'workloads':combine([summary]),'issues':summary['issues'],'issues_total':summary['issues_total'],'health':aggregate([summary['health']]),'llm_configured':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:18090')
    parser.add_argument('--password-file',type=Path,required=True)
    parser.add_argument('--screenshots',type=Path,default=Path('/tmp/pulse-health-ui'))
    args=parser.parse_args();args.screenshots.mkdir(parents=True,exist_ok=True)
    case=['healthy'];errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        context=browser.new_context(http_credentials={'username':'pulse','password':args.password_file.read_text().strip()},viewport={'width':1600,'height':1120})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/clusters',lambda r:r.fulfill(json={'items':[{'id':'peoplehub','name':'PeopleHub (тест)','enabled':True}]}))
        page.route('**/api/dashboard*',lambda r:r.fulfill(json=payload(case[0])))
        page.route('**/api/investigations?*',lambda r:r.fulfill(json={'items':[]}))
        def metrics(route):
            now=time.time();result={'configured':True,'start':now-3600,'end':now,'step':30,'note':'ТЕСТОВЫЕ ДАННЫЕ — не состояние сервера пользователя','metrics':{}}
            for k,n in [('cpu',38),('ram',64),('disk',72)]:result['metrics'][k]={'percent':n,'observed_at':now,'history':[[now-3600+i*30,n+(i%7)-3] for i in range(121)]}
            route.fulfill(json=result)
        page.route('**/api/clusters/*/dashboard-metrics?*',metrics)
        page.goto(args.base_url)
        page.locator('[data-testid=nodes-ready].healthy').wait_for()
        assert page.locator('[data-testid=nodes-ready] strong').inner_text()=='6/6'
        assert page.locator('[data-testid=node-row]').count()==6
        assert page.locator('[data-testid=cluster-health]').evaluate('(e)=>e.classList.contains("healthy")')
        assert page.locator('[data-testid=workload-jobs]').evaluate('(e)=>e.classList.contains("workload-healthy")')
        page.locator('.history-chart').first.wait_for()
        page.locator('#page-title').evaluate('(e)=>e.textContent="Pulse Dark · здоровый кластер · тест"')
        page.screenshot(path=str(args.screenshots/'healthy.png'),full_page=True)
        case[0]='critical';page.get_by_role('button',name='↻ Обновить').click()
        page.locator('[data-testid=nodes-ready].critical').wait_for()
        assert page.locator('[data-testid=nodes-ready] strong').inner_text()=='5/6'
        assert page.locator('.node-critical').count()==1
        page.locator('[data-testid=nodes-ready]').get_by_role('button').click()
        assert page.locator('.node-panel').evaluate('(e)=>e.open')
        page.get_by_label('Фильтр проблем').select_option('critical')
        assert page.locator('.severity-critical').count()==2
        page.get_by_role('button',name='↻ Обновить').click()
        page.locator('[data-testid=nodes-ready].critical').wait_for()
        assert page.get_by_label('Фильтр проблем').input_value()=='critical'
        assert page.locator('.node-panel').evaluate('(e)=>e.open')
        page.locator('#page-title').evaluate('(e)=>e.textContent="Pulse Dark · отказ узла и CrashLoop · тест"')
        page.screenshot(path=str(args.screenshots/'critical.png'),full_page=True)
        page.locator('#problem-list').get_by_role('button',name='Почему?',exact=True).click()
        page.wait_for_url('**/#investigations')
        assert page.locator('#investigation-form input[name=pod]').input_value()=='web-api'
        page.get_by_role('link',name='Обзор',exact=True).click()
        case[0]='stale';page.get_by_role('button',name='↻ Обновить').click()
        page.locator('[data-testid=cluster-health].stale').wait_for()
        assert page.locator('[data-testid=nodes-ready]').evaluate('(e)=>e.classList.contains("unknown")')
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert not errors,errors
        browser.close()
    print('PASS: 6/6 green; 5/6 red; node identity; priority/filter; CrashLoop investigation; active Job neutral; stale not green; mobile and no JS errors')


if __name__=='__main__':main()
