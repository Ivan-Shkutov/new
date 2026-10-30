#!/usr/bin/env python3
"""Persist metrics through the real browser/API, including misleading/old API responses."""
import argparse,json,uuid
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:18090');p.add_argument('--password-file',type=Path,required=True);a=p.parse_args()
cid='metrics-ui-'+uuid.uuid4().hex[:8]
kube={'apiVersion':'v1','kind':'Config','current-context':'test','contexts':[{'name':'test','context':{'cluster':'test','user':'reader'}}],'clusters':[{'name':'test','cluster':{'server':'https://unused.test'}}],'users':[{'name':'reader','user':{'token':'not-a-real-token'}}]}
with sync_playwright() as pw:
 browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
 context=browser.new_context(http_credentials={'username':'pulse','password':a.password_file.read_text().strip()})
 assert context.request.post(a.base_url+'/api/clusters',data={'id':cid,'name':cid,'enabled':False,'kubeconfig':json.dumps(kube)},headers={'X-Pulse-Request':'1'}).status==201
 try:
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(a.base_url+'/#settings');form=page.locator('#cluster-form');probe_calls=[]
  page.route('**/api/clusters/*/metrics-test',lambda r:(probe_calls.append(True),r.fulfill(json={'ok':True,'series':6,'note':'Тестовый источник'}))[-1])
  def open_settings():
   page.locator('.setting-item').filter(has_text=cid).get_by_role('button',name='Настроить',exact=True).click()
   summary=page.get_by_text('Метрики Prometheus / VictoriaMetrics',exact=True)
   if summary.locator('..').get_attribute('open') is None:summary.click()
  def current():return next(r for r in context.request.get(a.base_url+'/api/clusters').json()['items'] if r['id']==cid)
  open_settings();assert form.locator('[name=metrics_namespace]').is_hidden()
  form.locator('[name=metrics_transport]').select_option('kubernetes');form.locator('[name=metrics_namespace]').fill('monitoring');form.locator('[name=metrics_service]').fill('kube-prom-kube-prometheus-prometheus');form.locator('[name=metrics_mode]').select_option('dedicated');form.locator('[name=metrics_labels]').fill('{}')
  assert form.locator('[name=metrics_url]').is_hidden()
  form.locator('[name=metrics_transport]').select_option('direct')
  form.get_by_role('button',name='Сохранить кластер',exact=True).click()
  expect(form.locator(':scope > .form-message')).to_contain_text('для HTTP укажите URL')
  assert not current()['has_metrics'] and page.locator('#cluster-dialog').is_visible()
  form.locator('[name=metrics_transport]').select_option('kubernetes');page.get_by_role('button',name='Проверить сохранённый источник',exact=True).click()
  expect(page.locator('#metrics-message')).to_contain_text('несохранённые изменения');assert not probe_calls
  form.get_by_role('button',name='Сохранить кластер',exact=True).click();page.locator('#cluster-dialog').wait_for(state='hidden')
  assert current()['metrics']['transport']=='kubernetes'
  page.reload();open_settings()
  assert {k:form.locator('[name=metrics_'+k+']').input_value() for k in ('transport','namespace','service','port','mode','labels')}=={'transport':'kubernetes','namespace':'monitoring','service':'kube-prom-kube-prometheus-prometheus','port':'9090','mode':'dedicated','labels':'{}'}
  page.get_by_role('button',name='Проверить сохранённый источник',exact=True).click();expect(page.locator('#metrics-message')).to_contain_text('серий 6');assert len(probe_calls)==1
  form.locator('[name=name]').fill(cid+' renamed');form.get_by_role('button',name='Сохранить кластер',exact=True).click();page.locator('#cluster-dialog').wait_for(state='hidden')
  assert current()['metrics']['mode']=='dedicated'
  open_settings();row=current();page.route('**/api/clusters',lambda r:r.fulfill(json={'items':[{**row,'metrics':{}}]}))
  form.get_by_role('button',name='Сохранить кластер',exact=True).click();expect(form.locator(':scope > .form-message')).to_contain_text('не подтвердил сохранение метрик')
  assert page.locator('#cluster-dialog').is_visible();assert not errors,errors
  print('PASS: incomplete metrics never show success; visible validation; correct proxy persists after reload/name edit; unsaved probe blocked; old/mismatched API readback detected')
 finally:
  assert context.request.delete(a.base_url+'/api/clusters/'+cid,headers={'X-Pulse-Request':'1'}).ok
  browser.close()
