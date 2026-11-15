"""Optional real browser acceptance. RUN_BROWSER_TESTS=1 enables it (Windows and Linux)."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx
import pytest

pytestmark = pytest.mark.skipif(os.getenv('RUN_BROWSER_TESTS') != '1', reason='Set RUN_BROWSER_TESTS=1 and install requirements-ui.txt')


def test_upload_select_free_question_and_switch_back(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    from test_xlsx import xlsx, ROWS
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'DATABASE_PATH': str(tmp_path / 'browser.sqlite'), 'APP_ACCESS_TOKEN': ''}
    log = (tmp_path / 'server.log').open('w', encoding='utf-8')
    server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'browser_fixture:app', '--app-dir', 'tests',
                               '--host', '127.0.0.1', '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log)
    base = f'http://127.0.0.1:{port}'
    try:
        for _ in range(100):
            try:
                if httpx.get(base + '/api/health', timeout=1).is_success:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None:
                pytest.fail((tmp_path / 'server.log').read_text(encoding='utf-8'))
            time.sleep(.1)
        else:
            pytest.fail('Browser test server did not start')
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.getenv('CHROMIUM_EXECUTABLE') or None, headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base)
            playwright.expect(page.locator('#kpi-count')).not_to_have_text('—')
            builtin_npl = page.locator('#kpi-npl').inner_text()
            # Browser size validation rejects locally without posting the file.
            page.locator('#xlsx-file').set_input_files({'name': 'large.xlsx', 'mimeType': 'application/octet-stream', 'buffer': b'x'*(10*1024*1024+1)})
            page.locator('#upload-button').click()
            playwright.expect(page.locator('#global-error')).to_contain_text('10 МБ')
            # Backend cell validation and import history are visible.
            page.locator('#xlsx-file').set_input_files({'name': 'invalid.xlsx', 'mimeType': 'application/octet-stream', 'buffer': xlsx([ROWS[0], ROWS[0]])})
            page.locator('#upload-button').click()
            playwright.expect(page.locator('#import-result')).to_contain_text('Импорт отклонён')
            assert 'Дубликат' in page.locator('#import-result').inner_text()
            playwright.expect(page.locator('#upload-button')).to_be_enabled()
            page.locator('#xlsx-file').set_input_files({'name': 'portfolio.xlsx', 'mimeType': 'application/octet-stream', 'buffer': xlsx()})
            page.locator('#upload-button').click()
            playwright.expect(page.locator('#import-result')).to_contain_text('Импорт сохранён')
            assert 'Строк: 2' in page.locator('#import-result').inner_text()
            playwright.expect(page.locator('#dataset-filter')).to_be_enabled()
            identifier = page.locator('#dataset-filter option').nth(1).get_attribute('value')
            assert identifier.startswith('upload_')
            page.select_option('#dataset-filter', identifier)
            playwright.expect(page.locator('#kpi-count')).to_have_text('2')
            playwright.expect(page.locator('#send-button')).to_be_enabled()
            assert '75,00%' in page.locator('#kpi-npl').inner_text()
            assert 'PD/LGD' in page.locator('#metric-availability').inner_text()
            assert identifier in page.locator('#npl-calculation').inner_text()
            assert page.locator('#date-filter').input_value() == '2026-10-31'
            page.locator('#question').fill('Сколько просрочки 90+ в загруженном портфеле?')
            page.locator('#send-button').click()
            page.wait_for_selector('.result-block')
            answer = page.locator('.answer').inner_text()
            assert '3000.0 / 4000.0' in answer and '75.00%' in answer and identifier in answer
            page.locator('.source-ref').first.click()
            trace = page.locator('.trace-call').nth(1).inner_text()
            assert 'get_portfolio_summary' in trace and identifier in trace and '3000' in trace
            playwright.expect(page.locator('#dataset-filter')).to_be_enabled()
            page.select_option('#dataset-filter', 'builtin')
            playwright.expect(page.locator('#kpi-count')).to_have_text('100')
            playwright.expect(page.locator('#dataset-filter')).to_be_enabled()
            assert page.locator('#kpi-npl').inner_text() == builtin_npl
            assert page.locator('.result-block').count() == 0
            page.set_viewport_size({'width': 390, 'height': 844})
            assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
            assert not errors
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)
        log.close()
