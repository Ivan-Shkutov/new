#!/usr/bin/env python3
"""Browser smoke against a disposable Pulse instance; cleans up its own test document.

Requires Playwright and a Chromium binary. Reads the password from a file, never logs it.
Use a test runtime: the test briefly saves local model settings, then restores URL/model.
It does not call a model or connect a Kubernetes cluster.
"""
import argparse
import json
import uuid
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8091')
    parser.add_argument('--username', default='pulse')
    parser.add_argument('--password-file', type=Path, required=True)
    parser.add_argument('--chromium', default='/usr/bin/chromium')
    parser.add_argument('--screenshot-dir', type=Path)
    args = parser.parse_args()
    password = args.password_file.read_text().strip()
    token = f'pulseuitest{uuid.uuid4().hex}'
    filename = f'{token}.md'
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.chromium, headless=True,
                                              args=['--no-sandbox'])
        context = browser.new_context(base_url=args.base_url,
                                      http_credentials={'username': args.username, 'password': password},
                                      viewport={'width': 1440, 'height': 1000})
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        previous = context.request.get('/api/settings')
        assert previous.ok, f'Cannot read settings: HTTP {previous.status}'
        llm = previous.json().get('llm', {})
        restore = {key: llm[key] for key in ('url', 'model') if key in llm}
        doc_id = None
        try:
            page.goto('/')
            page.get_by_role('heading', name='Обзор кластеров', exact=True).wait_for()
            page.wait_for_function("document.querySelector('#connection-state').textContent === 'Сервер доступен'")
            assert page.locator('#global-error').is_hidden(), 'Initial API error'
            page.get_by_role('link', name='Ресурсы', exact=True).click()
            page.get_by_role('heading', name='Выберите кластер', exact=True).wait_for()
            page.goto('/#investigations')  # Compatibility archive has no main navigation item.
            page.get_by_role('heading', name='Выберите кластер', exact=True).wait_for()
            page.get_by_role('link', name='База знаний', exact=True).click()
            page.locator('#document-form select[name=scope]').select_option('global')
            page.locator('#document-form input[type=file]').set_input_files({
                'name': filename, 'mimeType': 'text/markdown',
                'buffer': f'# {token}\nДокумент браузерной проверки. <script>alert(1)</script>\n'.encode()})
            page.get_by_role('button', name='Загрузить документ', exact=True).click()
            page.get_by_role('heading', name=filename, exact=True).wait_for()
            documents = context.request.get('/api/documents').json()['items']
            doc_id = next(item['id'] for item in documents if item['name'] == filename)
            page.locator('#document-search-form input[name=q]').fill(token)
            page.get_by_role('button', name='Найти', exact=True).click()
            page.get_by_role('heading', name=filename, exact=True).wait_for()
            assert page.locator('#document-list script').count() == 0, 'Unsafe search snippet rendering'
            card = page.locator('#document-list .item-card').filter(has=page.get_by_role('heading', name=filename, exact=True))
            page.once('dialog', lambda dialog: dialog.accept())
            card.get_by_role('button', name='Удалить документ', exact=True).click()
            page.get_by_text('Документ удалён', exact=True).wait_for()
            remaining = context.request.get('/api/documents').json()['items']
            assert all(item['id'] != doc_id for item in remaining), 'Document was not removed'
            doc_id = None
            page.get_by_role('link', name='Настройки', exact=True).click()
            page.locator('#llm-form input[name=url]').fill('http://127.0.0.1:8002/v1')
            page.locator('#llm-form input[name=model]').fill('smoke-test-not-a-real-model')
            page.locator('#llm-form').get_by_role('button', name='Сохранить', exact=True).click()
            page.get_by_text('Настройки сохранены.', exact=True).wait_for()
            saved = context.request.get('/api/settings').json()
            assert saved['llm']['model'] == 'smoke-test-not-a-real-model'
            page.get_by_role('button', name='+ Добавить', exact=True).click()
            page.locator('#cluster-form input[name=id]').fill('Invalid_ID')
            assert not page.locator('#cluster-form input[name=id]').evaluate('(element) => element.checkValidity()')
            page.locator('#cluster-dialog').get_by_role('button', name='Отмена', exact=True).click()
            page.get_by_role('link', name='Обзор', exact=True).click()
            if args.screenshot_dir:
                args.screenshot_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(args.screenshot_dir/'pulse-desktop.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile layout overflow'
            if args.screenshot_dir:
                page.screenshot(path=str(args.screenshot_dir/'pulse-mobile.png'), full_page=True)
            assert not errors, f'Browser JavaScript errors: {errors}'
            print('PASS: real authenticated API; navigation; document upload/search/delete; safe snippet rendering;')
            print('LLM settings save; cluster form validation; desktop/mobile; no JavaScript errors.')
        finally:
            if doc_id is not None:
                result = context.request.delete(f'/api/documents/{doc_id}', headers={'X-Pulse-Request': '1'})
                assert result.ok, 'Failed to remove smoke-test document'
            result = context.request.put('/api/settings', data={'llm': restore}, headers={'X-Pulse-Request': '1'})
            assert result.ok, 'Failed to restore model settings'
            browser.close()


if __name__ == '__main__':
    main()
