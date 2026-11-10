import http.client
import json
import threading
from types import SimpleNamespace
import pytest
from companion_server import CompanionHandler
from dashboard_server import DashboardServer


@pytest.fixture
def http_server():
    server = DashboardServer(('127.0.0.1', 0), CompanionHandler)
    server.bridge_token = 'test-token'
    server.bridge = SimpleNamespace(submit=lambda p: {'status': 'submitted'}, list=lambda: [])
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    yield server
    server.shutdown(); server.server_close(); thread.join(timeout=2)


def request(server, method='POST', path='/bridge/orders', body='{}', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse(); data = response.read(); code = response.status
    conn.close(); return code, data


def test_missing_token_denied(http_server):
    assert request(http_server)[0] == 403


@pytest.mark.parametrize('headers', [{'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Host': 'evil.example'}])
def test_foreign_origin_or_host_denied(http_server, headers):
    headers.update({'X-Solomon-Bridge': 'test-token', 'Content-Type': 'application/json'})
    assert request(http_server, headers=headers)[0] == 403


def test_authenticated_json_accepted(http_server):
    code, body = request(http_server, headers={'X-Solomon-Bridge': 'test-token', 'Content-Type': 'application/json'})
    assert code == 202 and json.loads(body)['status'] == 'submitted'


def test_wrong_content_type_and_oversize_rejected(http_server):
    assert request(http_server, headers={'X-Solomon-Bridge': 'test-token'})[0] == 400
    assert request(http_server, body=' ' * 32001, headers={'X-Solomon-Bridge': 'test-token', 'Content-Type': 'application/json'})[0] == 400


def test_read_bridge_requires_token(http_server):
    assert request(http_server, 'GET', '/bridge/status')[0] == 403


def test_console_static_served(http_server):
    code, body = request(http_server, 'GET', '/')
    assert code == 200 and 'AI DEV 작업실'.encode() in body
    assert request(http_server, 'GET', '/console.js')[0] == 200


def test_classic_view_offers_a_way_back_to_the_stage(http_server):
    import dashboard_server
    dashboard_server._ensure_schema()                       # the isolated test database starts empty
    code, body = request(http_server, 'GET', '/classic')
    text = body.decode('utf-8')
    assert code == 200 and 'id="main"' in text            # still the original dashboard the workshop embeds
    assert 'id="back-to-stage" href="/"' in text           # full-window view links back to the stage
    assert 'window.top!==window.self' in text              # and hides that link inside the workshop iframe
    assert text.index('id="back-to-stage"') < text.rindex('</body>')


def test_console_has_no_solomon_office_link_or_inbox():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / 'console' / 'console.js').read_text(encoding='utf-8')
    assert '8710' not in script and '솔로몬 집무실' not in script and '솔로몬 작업함' not in script


def test_stage_has_no_broadcast_mode():
    from pathlib import Path
    console = Path(__file__).resolve().parents[1] / 'console'
    for name in ('index.html', 'stage.js', 'stage.css'):
        text = (console / name).read_text(encoding='utf-8')
        assert 'broadcast' not in text and '방송 모드' not in text, name


def test_console_footer_and_about_panel_credit_the_fork():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / 'console' / 'index.html').read_text(encoding='utf-8')
    assert 'id="fork-credit"' in html and 'HanginIs 수정판' in html
    app = (root / 'macos' / 'Sources' / 'DirectorApp' / 'main.swift').read_text(encoding='utf-8')
    assert 'orderFrontStandardAboutPanel' in app and 'HanginIs 수정판' in app
