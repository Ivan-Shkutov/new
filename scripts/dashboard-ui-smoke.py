#!/usr/bin/env python3
"""Test dashboard states with explicit fixtures against a disposable running instance."""
import argparse
import json
import time
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:18090')
    parser.add_argument('--password-file',type=Path,required=True)
    parser.add_argument('--screenshot',type=Path)
    args=parser.parse_args()
    kinds=['daemonsets','deployments','jobs','cronjobs','replicasets','statefulsets','pods']
    workloads={k:{'total':10,'positive':8,'percent':80,'label':'Готовы','states':{'ready':8,'not_ready':2},'partial':False,'stale':False} for k in kinds}
    workloads['jobs'].update(total=0,positive=0,percent=None,label='Завершены',states={})
    cluster={'id':'one','name':'Тестовый PeopleHub','enabled':True,'observed_at':time.strftime('%Y-%m-%dT%H:%M:%S+00:00',time.gmtime()),'status':'ok','counts':{'nodes':6,'pods':163,'namespaces':14},'sources':{'metrics':True,'astra':False},'error':''}
    data={'version':'0.7.0','clusters':[cluster],'workloads':workloads,'issues':[],'issues_total':0,'llm_configured':True}
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
        context=browser.new_context(http_credentials={'username':'pulse','password':args.password_file.read_text().strip()},viewport={'width':1600,'height':1100})
        page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/clusters',lambda r:r.fulfill(json={'items':[{'id':'one','name':'Тестовый PeopleHub','enabled':True}]}))
        page.route('**/api/dashboard*',lambda r:r.fulfill(json=data))
        def metrics(route):
            now=time.time(); result={'configured':True,'start':now-3600,'end':now,'step':30,'note':'ТЕСТОВЫЕ ДАННЫЕ — не производственные измерения','metrics':{}}
            for k,n in [('cpu',42),('ram',67),('disk',81)]:
                result['metrics'][k]={'percent':n,'observed_at':now,'history':[[now-3600+i*30,n+(i%5)-2] for i in range(121)]}
            route.fulfill(json=result)
        page.route('**/api/clusters/*/dashboard-metrics?*',metrics)
        page.goto(args.base_url)
        page.locator('[data-testid=utilization-cpu]').get_by_text('42%',exact=True).wait_for()
        assert page.locator('.workload-card').count()==7
        assert page.locator('[data-testid=workload-jobs] .donut-number').inner_text()=='—'
        assert page.locator('.history-chart').count()==3
        page.get_by_label('Интервал графиков').select_option('6h')
        page.locator('[data-testid=utilization-cpu]').get_by_text('42%',exact=True).wait_for()
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True,exist_ok=True)
            page.locator('#page-title').evaluate('(e)=>e.textContent="Обзор D+B · тестовые данные"')
            page.screenshot(path=str(args.screenshot),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.unroute('**/api/clusters/*/dashboard-metrics?*')
        page.route('**/api/clusters/*/dashboard-metrics?*',lambda r:r.fulfill(json={'configured':False,'metrics':{}}))
        page.get_by_role('button',name='↻ Обновить').click()
        page.locator('[data-testid=utilization-cpu]').get_by_text('Нет данных',exact=True).wait_for()
        assert page.locator('[data-testid=utilization-cpu] .donut-number').inner_text()=='—'
        assert not errors,errors
        browser.close()
    print('PASS: 7 workload rings; 3 utilization rings and charts; interval switch; missing metrics; mobile; no JS errors')


if __name__=='__main__': main()
