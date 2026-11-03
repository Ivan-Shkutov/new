#!/usr/bin/env python3
"""Disposable runtime only: real browser/API; local HTTP fixture replaces LLM reasoning."""
import argparse,json,threading,subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--base-url',default='http://127.0.0.1:18090');p.add_argument('--password-file',type=Path,required=True);p.add_argument('--screenshots',type=Path,default=Path('/tmp/pulse-0130-sessions'));a=p.parse_args();a.screenshots.mkdir(parents=True,exist_ok=True)
requests=[]
class Model(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
  assert 'Senior DevOps/SRE' in body['messages'][0]['content']
  payload=json.dumps({'choices':[{'message':{'content':'<think>INTERNAL-FIXTURE</think>Тестовый ответ. История только в текущем диалоге.'},'finish_reason':'stop'}]},ensure_ascii=False).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(payload)
server=ThreadingHTTPServer(('127.0.0.1',0),Model);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
with sync_playwright() as p:
 browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox'])
 context=browser.new_context(http_credentials={'username':'pulse','password':a.password_file.read_text().strip()},viewport={'width':1366,'height':900})
 before=context.request.get(a.base_url+'/api/settings').json()
 assert not before.get('llm',{}).get('has_key'),'Use a disposable runtime without a real model credential'
 headers={'X-Pulse-Request':'1'}
 assert context.request.put(a.base_url+'/api/settings',data={'llm':{'url':f'http://127.0.0.1:{server.server_port}/v1','model':'qwen'}},headers=headers).ok
 try:
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));sids=[];jids=[]
  page.on('response',lambda r:jids.append(r.json()['id']) if '/api/sessions/' in r.url and r.url.endswith('/messages') and r.status==202 else None)
  page.on('response',lambda r:sids.append(r.json()['id']) if r.url.endswith('/api/sessions') and r.status==201 else None)
  history=context.request.get(a.base_url+'/api/investigations').json()
  page.goto(a.base_url+'/#chat');expect(page.locator('#ai-dialog')).to_be_visible()
  expect(page.locator('#ai-form button[type=submit]')).to_be_enabled()
  page.locator('#ai-form textarea').fill('Почему небо синее?');page.locator('#ai-form button[type=submit]').click()
  expect(page.locator('.chat-answer')).to_have_count(1)
  assert 'INTERNAL-FIXTURE' not in page.locator('.chat-answer').inner_text()
  page.locator('#ai-form textarea').fill('Объясни короче');page.locator('#ai-form button[type=submit]').click()
  expect(page.locator('.chat-answer')).to_have_count(2)
  assert any(m['role']=='assistant' for m in requests[-1]['messages'])
  assert 'INTERNAL-FIXTURE' not in json.dumps(requests[-1]['messages'])
  old=sids[-1]
  # Reopen the compatibility chat explicitly: it has no main navigation item.
  page.get_by_role('button',name='Закрыть анализ',exact=True).click()
  page.locator('#new-chat').click()
  expect(page.locator('#ai-form button[type=submit]')).to_be_enabled()
  expect(page.locator('.chat-turn')).to_have_count(0)
  assert sids[-1]!=old
  page.locator('#ai-form textarea').fill('Новый вход в чат');page.locator('#ai-form button[type=submit]').click()
  expect(page.locator('.chat-answer')).to_have_count(1)
  assert not any(m['role']=='assistant' for m in requests[-1]['messages'])
  # Close and reopen in one task, before the old queued close event fires.
  page.evaluate("document.querySelector('#ai-dialog').close(); document.querySelector('#new-chat').click()")
  expect(page.locator('#ai-dialog')).to_be_visible()
  expect(page.locator('#ai-form button[type=submit]')).to_be_enabled()
  expect(page.locator('.chat-turn')).to_have_count(0)
  page.screenshot(path=str(a.screenshots/'general-chat.png'))
  old=sids[-1];page.get_by_role('button',name='Закрыть анализ',exact=True).click();expect(page.locator('#ai-conversation')).to_be_empty()
  for _ in range(50):
   # Idempotent cleanup probe: tolerate one reset of an idle keep-alive socket.
   if context.request.get(a.base_url+f'/api/sessions/{old}/jobs/{jids[-1]}',max_retries=1).status==404:break
   page.wait_for_timeout(50)
  assert context.request.get(a.base_url+'/api/investigations').json()==history
  page.get_by_role('button',name='Новый диалог',exact=True).click();expect(page.locator('#ai-form button[type=submit]')).to_be_enabled()
  expect(page.locator('.chat-turn')).to_have_count(0)
  page.locator('#ai-form textarea').fill('Другой вопрос');page.locator('#ai-form button[type=submit]').click();expect(page.locator('.chat-answer')).to_have_count(1)
  assert not any(m['role']=='assistant' for m in requests[-1]['messages'])
  page.keyboard.press('Escape');expect(page.locator('#ai-conversation')).to_be_empty()
  assert sids[-1]!=old and not errors,errors
  before_check=len(requests)
  checked=subprocess.run([str(ROOT/'pulse'),'--runtime',str(a.password_file.parent.parent),'llm-check'],capture_output=True,text=True)
  assert checked.returncode==0,checked.stdout+checked.stderr
  report=json.loads((a.password_file.parent.parent/'llm-check-report.json').read_text())
  assert report['generation_ok'] and report['quality_verified'] is False and len(report['cases'])==4
  assert len(requests)==before_check+4 and all(r['policy_applied'] for r in report['cases'])
  assert 'INTERNAL-FIXTURE' not in json.dumps(report)
  assert context.request.get(a.base_url+'/api/investigations').json()==history
  print('PASS: real browser/API/HTTP-model fixture; general chat; multi-turn; local engine; close+Escape clear memory; reopen starts empty; no durable history written.')
  print('PASS: shared DevOps policy; think excluded from visible answer/history; actual llm-check CLI through Docker/API with four fixture completions and a manual quality rubric.')
 finally:
  old=before.get('llm',{})
  if old.get('url') and old.get('model'):context.request.put(a.base_url+'/api/settings',data={'llm':{'url':old['url'],'model':old['model']}},headers=headers)
  browser.close();server.shutdown();thread.join()
