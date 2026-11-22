import hashlib
import io
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import docx
import pytest
from fastapi.testclient import TestClient

from yuriy.app import create_app
from yuriy.event_intents import is_event_instruction, propose
from yuriy.legal_drafts import assemble, validate
from yuriy.storage import Workspace


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def test_draft_calculation_select_exposes_all_three_options():
    js = (Path(__file__).resolve().parents[1] / 'yuriy/static/legal-drafts.js').read_text(encoding='utf-8')
    m = re.search(r'<select id="draftCalculation">(.*?)</select>', js)
    assert m is not None
    options = re.findall(r'<option value="([^"]+)">([^<]+)</option>', m.group(1))
    assert options == [
        ('none', 'Без автоматического расчёта'),
        ('interest395', 'Проценты по ст. 395 ГК РФ'),
        ('contract_penalty', 'Договорная неустойка'),
    ]


@pytest.mark.parametrize('question', [
    'Напомни статьи ГК РФ о неустойке',
    'Напомни про срок исковой давности',
    'Напомни порядок подачи апелляционной жалобы',
    'Юрий, напомни условия расторжения договора поставки',
    'Напомни завтрашнее заседание?',
    'Напомни мне что такое астрент',
])
def test_legal_questions_with_napomni_are_not_hijacked_by_secretary(question):
    assert not is_event_instruction(question)


@pytest.mark.parametrize('instruction', [
    'Напомни завтра в 15:00 отправить претензию',
    'Напомни 25 ноября в 11:00 позвонить доверителю',
    'Юрий, запиши заседание на 01.12.2026 в 10-30',
    'Добавь в календарь проверку ответа контрагента',
])
def test_real_event_instructions_route_to_local_secretary(instruction):
    assert is_event_instruction(instruction)


def test_hyphenated_numbers_do_not_crash_or_clobber_clock_time():
    p = propose('Напомни завтра в 14:30 позвонить в офис 45-67 по договору 12-34', 'Europe/Samara', NOW)
    assert p['date'] == '2026-11-20'
    assert p['time'] == '14:30'
    assert '45-67' in p['title'] and '12-34' in p['title']

    with pytest.raises(ValueError, match='Неверное время'):
        propose('Напомни завтра в 29-00 заседание', 'Europe/Samara', NOW)


def test_invalid_calendar_date_raises_russian_value_error():
    with pytest.raises(ValueError, match='Некорректная календарная дата'):
        propose('Напомни 31.02.2026 в 10:00 заседание', 'Europe/Samara', NOW)
    with pytest.raises(ValueError, match='Некорректная календарная дата'):
        propose('Напомни 31 ноября 2026 в 10:00 заседание', 'Europe/Samara', NOW)


def test_russian_month_names_and_relative_day_candidates():
    exact = propose('Запиши 25 ноября 2026 в 11:15 заседание в суде', 'Europe/Samara', NOW)
    assert exact['date'] == '2026-11-25'
    assert exact['time'] == '11:15'
    assert 'ноября' not in exact['title'].lower()

    partial = propose('Напомни 25 ноября в 11:15 направить отзыв', 'Europe/Samara', NOW)
    assert partial['date'] == ''
    assert '2026-11-25' in partial['dateCandidates']
    assert any('без года' in w for w in partial['warnings'])

    rel = propose('Напомни через 10 дней проверить почту', 'Europe/Samara', NOW)
    assert rel['date'] == ''
    assert '2026-11-29' in rel['dateCandidates']


def test_chat_handles_invalid_secretary_date_gracefully_without_http_400(tmp_path):
    class NeverCalledProvider:
        async def stream(self, cfg, messages):
            raise AssertionError('External provider must not be called for local secretary command')
            yield {}

    app = create_app(config_path=tmp_path / 'config.json', workspace=tmp_path / 'space', provider=NeverCalledProvider())
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token'] = app.state.test_token
        c.post('/api/secretary/settings', json={'timezone': 'Europe/Samara', 'notificationsEnabled': False, 'confirmed': True})
        st = c.get('/api/state').json()
        p = st['state']['projects'][0]
        t = p['threads'][0]
        r = c.post('/api/chat', json={
            'requestId': 'req-bad-date-1',
            'projectId': p['id'],
            'threadId': t['id'],
            'text': 'Напомни 31.02.2026 в 10:00 отправить письмо',
            'deep': False,
            'revision': st['revision'],
            'documentIds': [],
            'documentsConsent': False,
        })
        assert r.status_code == 200, r.text
        assert r.json()['route'] == 'local-secretary-v2'
        saved = c.get('/api/state').json()['state']['projects'][0]['threads'][0]['messages'][-1]
        assert saved['kind'] == 'secretaryProposal'
        assert 'Некорректная календарная дата' in saved['text']
        assert saved['contextSummary']['eventProposal'] is None


def test_docx_demands_and_attachments_strip_blank_lines_and_bare_markers():
    fields = {
        'recipient_info': 'ООО «Бета»',
        'sender_info': 'ООО «Альфа»',
        'subject': 'Договор поставки № 5 от 01.02.2026',
        'circumstances': 'Товар поставлен, оплата не поступила.',
        'grounds': '[УТОЧНИТЬ: нормы права]',
        'demands': '1. Оплатить задолженность 100 000 руб.\n\n   \n2.\n•   \n3) Уплатить проценты.',
        'attachments': '\n1. Копия договора\n\n2. Акт сверки\n',
        'document_date': '22.11.2026',
        'signature': 'Директор И. И. Иванов',
    }
    spec = validate({'templateId': 'pretension', 'fields': fields})
    assert spec['fields']['demands'] == 'Оплатить задолженность 100 000 руб.\nУплатить проценты.'
    assert spec['fields']['attachments'] == 'Копия договора\nАкт сверки'

    out = assemble(spec, [])
    assert '\n2. \n' not in out['text']
    doc = docx.Document(io.BytesIO(out['compact']))
    demand_paragraphs = [p.text for p in doc.paragraphs if p.style and p.style.name == 'LegalDemand']
    assert demand_paragraphs == ['Оплатить задолженность 100 000 руб.', 'Уплатить проценты.']

    empty_only = dict(fields, demands='1.\n   \n• ')
    with pytest.raises(ValueError, match='Пустые обязательные поля'):
        validate({'templateId': 'pretension', 'fields': empty_only})


def test_cancelled_events_ui_distinguishes_cancelled_from_completed():
    html = (Path(__file__).resolve().parents[1] / 'yuriy/static/index.html').read_text(encoding='utf-8')
    m4 = (Path(__file__).resolve().parents[1] / 'yuriy/static/m4.js').read_text(encoding='utf-8')
    assert 'Прошедшие, выполненные и отменённые' in html
    assert "cancelled?'отменено':'выполнено'" in html
    assert 'typetag.cancelled' in html
    assert "e.status!=='cancelled'" in html
    assert "e.status!=='cancelled'" in m4


def test_workspace_startup_cleans_stale_temporary_editor_copies(tmp_path):
    ws = Workspace(tmp_path / 'space')
    pid = ws.read()['state']['projects'][0]['id']
    stale_dir = ws.safe(f'projects/{pid}/temporary/oldcopy')
    fresh_dir = ws.safe(f'projects/{pid}/temporary/newcopy')
    stale_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)
    stale_file = stale_dir / 'Черновик.docx'
    fresh_file = fresh_dir / 'Свежий.docx'
    stale_file.write_bytes(b'old')
    fresh_file.write_bytes(b'new')
    old_ts = time.time() - 90000
    os.utime(stale_file, (old_ts, old_ts))
    os.utime(stale_dir, (old_ts, old_ts))
    ws.close()

    ws2 = Workspace(tmp_path / 'space')
    try:
        assert not stale_dir.exists()
        assert fresh_file.exists()
    finally:
        ws2.close()


def test_bat_crlf_and_release_manifest_integrity():
    root = Path(__file__).resolve().parents[1]
    for name in ('install.bat', 'start.bat', 'stop.bat'):
        raw = (root / name).read_bytes()
        assert b'\r\n' in raw
        assert b'\n' not in raw.replace(b'\r\n', b''), f'{name} contains bare LF'

    manifest = root / 'RELEASE-MANIFEST.sha256'
    entries = [line.strip().split('  ', 1) for line in manifest.read_text(encoding='utf-8').splitlines() if line.strip()]
    assert entries
    for expected_sha, rel_path in entries:
        target = root / rel_path
        assert target.is_file(), f'Missing file from manifest: {rel_path}'
        actual_sha = hashlib.sha256(target.read_bytes()).hexdigest()
        assert actual_sha == expected_sha, f'SHA mismatch for {rel_path}'
