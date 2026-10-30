#!/usr/bin/env python3
"""Astra AI settings browser test on a disposable instance. No external AI calls."""
import argparse,json,uuid
from pathlib import Path
from playwright.sync_api import sync_playwright,expect

p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:18090');p.add_argument('--password-file',type=Path,required=True);a=p.parse_args()
cid='ai-ui-'+uuid.uuid4().hex[:8];secret='test-only-'+uuid.uuid4().hex
kube={'apiVersion':'v1','kind':'Config','current-context':'test','contexts':[{'name':'test','context':{'cluster':'test','user':'reader'}}],'clusters':[{'name':'test','cluster':{'server':'https://unused.test'}}],'users':[{'name':'reader','user':{'token':'not-a-real-token'}}]}
with sync_playwright() as pw:
    browser=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
    context=browser.new_context(http_credentials={'username':'pulse','password':a.password_file.read_text().strip()})
    created=context.request.post(a.base_url+'/api/clusters',data={'id':cid,'name':cid,'enabled':False,'kubeconfig':json.dumps(kube)},headers={'X-Pulse-Request':'1'});assert created.status==201
    try:
        page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.route('**/api/clusters/*/astra-ai-test',lambda r:r.fulfill(json={'ok':True,'answer':'Тестовый ответ','stream_completed':True}))
        page.goto(a.base_url+'/#settings')
        def open_settings():
            page.locator('.setting-item').filter(has_text=cid).get_by_role('button',name='Настроить',exact=True).click()
            summary=page.get_by_text('ИИ Astra · анализ через её API',exact=True)
            if summary.locator('..').get_attribute('open') is None:summary.click()
        open_settings();form=page.locator('#cluster-form')
        form.locator('[name=astra_ai_enabled]').check();form.locator('[name=astra_ai_url]').fill('http://astra.test/api/v1/settings/ai/request/chat/completions');form.locator('[name=astra_ai_api_key]').fill(secret)
        form.get_by_role('button',name='Сохранить кластер',exact=True).click();page.locator('#cluster-dialog').wait_for(state='hidden')
        data=context.request.get(a.base_url+'/api/clusters').json();row=next(r for r in data['items'] if r['id']==cid)
        assert row['astra_ai']['enabled'] and row['astra_ai']['has_key'] and secret not in json.dumps(data)
        open_settings();assert form.locator('[name=astra_ai_api_key]').input_value()==''
        page.get_by_role('button',name='Проверить поток Astra AI',exact=True).click();expect(page.locator('#astra-ai-message')).to_contain_text('Поток завершён')
        page.get_by_text('HolmesGPT · отдельный read-only агент',exact=True).click()
        form.locator('[name=holmes_enabled]').check();assert not form.locator('[name=astra_ai_enabled]').is_checked()
        form.locator('[name=astra_ai_enabled]').check();assert not form.locator('[name=holmes_enabled]').is_checked()
        page.get_by_role('button',name='Отмена',exact=True).click()
        assert not errors,errors
        print('PASS: Astra AI settings save; encrypted credential not returned; empty token field on reopen; stream probe UI; exclusive engine selection')
    finally:
        assert context.request.delete(a.base_url+'/api/clusters/'+cid,headers={'X-Pulse-Request':'1'}).ok
        browser.close()
