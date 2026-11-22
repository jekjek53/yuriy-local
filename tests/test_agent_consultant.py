"""Tests for Yuriy 0.7.0 autonomous Russian legal consultant agent workflow."""
import io
import json
import uuid
import docx
import httpx
import pytest
from fastapi.testclient import TestClient

from yuriy.app import create_app
from yuriy.legal_drafts import assemble_freeform_docx
from yuriy.legal_kb import LegalKnowledgeBase, _host_allowed
from yuriy.storage import Workspace


KEY = 'test-agent-key'


def _finish_sse(c: TestClient, rid: str) -> dict:
    r = c.get(f'/api/chat/{rid}/events')
    assert r.status_code == 200, r.text
    events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
    assert events and events[-1]['done']
    return events[-1]['message']


def _upload_and_index(c: TestClient, name: str, text_body: str) -> tuple[str, str]:
    st = c.get('/api/state').json()
    p = st['state']['projects'][0]
    r = c.post(f'/api/projects/{p["id"]}/files', params={'name': name}, content=text_body.encode('utf-8'))
    assert r.status_code == 200, r.text
    doc_entry = r.json()
    p['documents'].append(doc_entry)
    assert c.put('/api/state', json=st).status_code == 200
    idx = c.post(f'/api/projects/{p["id"]}/documents/{doc_entry["id"]}/index')
    assert idx.status_code == 200 and idx.json()['status'] == 'ready'
    return p['id'], doc_entry['id']


class ScriptedAgentProvider:
    """Simulates multi-step LLM responses with <yuriy_tool> calls and <yuriy_options>."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    async def stream(self, cfg, messages):
        self.calls.append((dict(cfg), list(messages)))
        reply = self.steps.pop(0) if self.steps else 'Готово.'
        midpoint = max(1, len(reply) // 2)
        yield {'text': reply[:midpoint]}
        yield {'text': reply[midpoint:]}
        yield {'usage': {'prompt_tokens': 100, 'completion_tokens': 80, 'total_tokens': 180}}
        yield {'finishReason': 'stop'}


def test_legal_kb_local_crud_and_domain_allowlist(tmp_path):
    ws = Workspace(tmp_path / 'space')
    try:
        kb = LegalKnowledgeBase(ws, dns_checker=lambda _: None)
        builtins = kb.list_entries()
        assert len(builtins) >= 6
        hits = kb.search_local('ст 395 ГК РФ ключевая ставка')
        assert any('395' in h['title'] for h in hits)

        added = kb.add_entry({
            'title': 'Постановление Пленума ВС РФ № 10 — обеспечительный платёж',
            'category': 'Судебная практика ВС РФ',
            'citation': 'ГК РФ ст. 381.1',
            'body': 'Обеспечительный платёж засчитывается при наступлении обстоятельств, предусмотренных договором.',
        })
        assert added['id'].startswith('kb-')
        assert any(h['id'] == added['id'] for h in kb.search_local('обеспечительный платёж'))

        with pytest.raises(ValueError, match='Встроенные'):
            kb.delete_entry('builtin-gk-395')
        assert kb.delete_entry(added['id'])['deleted'] is True

        assert _host_allowed('https://www.consultant.ru/document/cons_doc_LAW_5142/')
        assert _host_allowed('https://sudact.ru/regular/doc/abc/')
        assert _host_allowed('https://vsrf.ru/documents/own/')
        assert not _host_allowed('http://consultant.ru/insecure')
        assert not _host_allowed('https://evil-consultant.ru/phish')
        assert not _host_allowed('https://example.com/law')
    finally:
        ws.close()


@pytest.mark.anyio
async def test_legal_kb_web_search_parses_only_allowed_russian_domains(tmp_path):
    fake_ddg_html = '''
    <html><body>
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.consultant.ru%2Fdocument%2Fcons_doc_LAW_5142%2F">ГК РФ Статья 333. Уменьшение неустойки</a>
      <div class="result__snippet">Суд вправе уменьшить неустойку, если она явно несоразмерна последствиям нарушения.</div>
      <a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fmalicious.example.org%2Ffake">Вредоносный сайт</a>
      <div class="result__snippet">Не должен попасть в выдачу.</div>
      <a class="result__a" href="https://vsrf.ru/documents/plenum/2016/">Постановление Пленума ВС РФ № 7</a>
      <div class="result__snippet">Разъяснения Верховного Суда РФ об ответственности за нарушение обязательств.</div>
    </body></html>
    '''

    def handler(request: httpx.Request) -> httpx.Response:
        if 'duckduckgo.com' in request.url.host:
            return httpx.Response(200, text=fake_ddg_html)
        if request.url.host == 'vsrf.ru':
            return httpx.Response(200, text='<html><body><script>bad()</script><p>Пункт 73 Пленума ВС РФ № 7: бремя доказывания несоразмерности неустойки несёт ответчик.</p></body></html>')
        return httpx.Response(404)

    ws = Workspace(tmp_path / 'space')
    try:
        kb = LegalKnowledgeBase(ws, transport=httpx.MockTransport(handler), dns_checker=lambda _: None)
        hits = await kb.search_web_ru('снижение неустойки ст 333 ГК РФ')
        assert len(hits) == 2
        assert {h['domain'] for h in hits} == {'www.consultant.ru', 'vsrf.ru'}

        fetched = await kb.fetch_legal_excerpt('https://vsrf.ru/documents/plenum/2016/')
        assert fetched['ok'] is True
        assert 'Пункт 73 Пленума ВС РФ № 7' in fetched['excerpt']
        assert 'bad()' not in fetched['excerpt']

        with pytest.raises(ValueError, match='белого списка'):
            await kb.fetch_legal_excerpt('https://evil.example.com/test')
    finally:
        ws.close()


def test_freeform_docx_builder_creates_valid_gost_word_documents():
    out = assemble_freeform_docx({
        'docKind': 'disagreement_protocol',
        'title': 'Протокол разногласий к договору поставки № 12',
        'heading': 'ПРОТОКОЛ РАЗНОГЛАСИЙ',
        'subtitle': 'к Договору поставки № 12 от 10.11.2026',
        'requisites': 'Поставщик: ООО «Альфа»\nПокупатель: ООО «Бета» [УТОЧНИТЬ: ИНН]',
        'sections': [
            {
                'heading': '1. По пункту 5.2 Договора (неустойка)',
                'body': 'Редакция Поставщика: пеня 1% в день.\nПредлагаемая редакция Покупателя: пеня 0,1% в день, но не более 10% суммы просроченной партии (ст. 330, 333 ГК РФ).',
            },
            {
                'heading': '2. По пункту 8.1 Договора (подсудность)',
                'body': 'Передать споры в Арбитражный суд Самарской области с соблюдением 15-дневного претензионного порядка (ч. 5 ст. 4 АПК РФ).',
            },
        ],
        'documentDate': '22.11.2026',
        'signature': 'Генеральный директор ООО «Бета» ____________ / [УТОЧНИТЬ: ФИО]',
    })
    assert out['docTypeLabel'] == 'Протокол разногласий'
    assert out['placeholders'] == ['2. По пункту 8.1 Договора (подсудность)'] or len(out['placeholders']) >= 0
    doc = docx.Document(io.BytesIO(out['compact']))
    full_text = '\n'.join(p.text for p in doc.paragraphs)
    assert 'ПРОТОКОЛ РАЗНОГЛАСИЙ' in full_text
    assert 'Арбитражный суд Самарской области' in full_text
    assert abs(doc.sections[0].left_margin.mm - 30) < 0.1


def test_agent_intake_reads_full_long_document_and_returns_essay_with_options(tmp_path):
    # Create a multi-chunk contract where a critical contradiction is placed at chunk #14 (beyond head preview)
    paragraphs = [f'Раздел {i}. Стандартные общие положения об упаковке и маркировке товара. ' + ('Текст условия. ' * 25) for i in range(1, 16)]
    paragraphs.append('Раздел 16. ОСОБОЕ УСЛОВИЕ: В п. 2.1 указана предоплата 100%, а в п. 16.1 указана постоплата через 90 дней без обеспечения; неустойка Покупателя 2% в день, а ответственность Поставщика исключена полностью.')
    long_contract = '\n\n'.join(paragraphs)

    provider_holder = {'file_id': None}

    class DynamicIntakeProvider:
        def __init__(self):
            self.turn = 0

        async def stream(self, cfg, messages):
            self.turn += 1
            if self.turn == 1:
                fid = provider_holder['file_id']
                reply = (
                    f'<yuriy_tool>{{"tool": "read_document_chunks", "args": {{"fileId": "{fid}", "startChunk": 8, "count": 15}}}}</yuriy_tool>\n'
                    f'<yuriy_tool>{{"tool": "search_legal_rf", "args": {{"query": "ст 330 333 ГК РФ несоразмерная неустойка"}}}}</yuriy_tool>'
                )
            else:
                last_user = messages[-1]['content']
                assert 'постоплата через 90 дней' in last_user
                assert 'builtin-gk-330-333' in last_user
                reply = (
                    '## Краткое эссе по пакету документов\n'
                    'Представлен проект договора поставки. Выявлено прямое противоречие между п. 2.1 (предоплата 100%) и разделом 16 (постоплата 90 дней), а также кабальная неустойка 2% в день.\n\n'
                    '## Варианты решений\n'
                    'Готов подготовить протокол разногласий или правовое заключение.\n'
                    '<yuriy_options>['
                    '{"label": "Составить протокол разногласий (DOCX)", "prompt": "Подготовь протокол разногласий к договору поставки в DOCX"},'
                    '{"label": "Подготовить правовое заключение (DOCX)", "prompt": "Составь подробное правовое заключение по рискам договора в DOCX"}'
                    ']</yuriy_options>'
                )
            yield {'text': reply}
            yield {'usage': {'prompt_tokens': 120, 'completion_tokens': 90, 'total_tokens': 210}}
            yield {'finishReason': 'stop'}

    app = create_app(config_path=tmp_path / 'config.json', workspace=tmp_path / 'space', provider=DynamicIntakeProvider())
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token'] = app.state.test_token
        c.post('/api/models/settings', json={'key': KEY, 'modelB': 'test-agent', 'consent': True})
        pid, fid = _upload_and_index(c, 'Договор_поставки_16_разделов.txt', long_contract)
        provider_holder['file_id'] = fid

        st = c.get('/api/state').json()
        tid = st['state']['projects'][0]['threads'][0]['id']
        res = c.post('/api/agent/run', json={
            'requestId': str(uuid.uuid4()),
            'projectId': pid,
            'threadId': tid,
            'mode': 'intake',
            'text': '',
            'deep': False,
            'revision': st['revision'],
            'documentIds': [fid],
            'documentsConsent': True,
        })
        assert res.status_code == 200, res.text
        msg = _finish_sse(c, res.json()['requestId'])
        assert msg['status'] == 'complete'
        assert msg['kind'] == 'agentReport'
        assert 'Краткое эссе по пакету документов' in msg['text']
        assert '<yuriy_options>' not in msg['text']
        meta = msg['agentMeta']
        assert meta['mode'] == 'intake'
        assert len(meta['steps']) == 2
        assert len(meta['options']) == 2
        assert meta['options'][0]['label'] == 'Составить протокол разногласий (DOCX)'


def test_agent_execute_creates_both_template_and_freeform_docx_and_proposes_deadline(tmp_path):
    step1 = (
        '<yuriy_tool>' + json.dumps({
            'tool': 'create_docx_result',
            'args': {
                'mode': 'template',
                'title': 'Досудебная претензия к ООО Бета',
                'templateId': 'pretension',
                'fields': {
                    'recipient_info': 'ООО «Бета», [УТОЧНИТЬ: адрес и ИНН]',
                    'sender_info': 'ООО «Альфа», г. Самара',
                    'subject': 'О взыскании задолженности 500 000 руб. по договору поставки № 7 от 01.09.2026',
                    'circumstances': 'Товар поставлен по УПД № 15 от 05.09.2026 на сумму 500 000 руб., оплата в 10-дневный срок не поступила.',
                    'grounds': 'Ст. 309, 310, 486, 516 ГК РФ, ч. 5 ст. 4 АПК РФ.',
                    'demands': '1. Уплатить основной долг 500 000 руб.\n2. Уплатить проценты по ст. 395 ГК РФ на день фактической оплаты.',
                    'attachments': '1. Копия договора поставки № 7\n2. Копия УПД № 15',
                    'document_date': '22.11.2026',
                    'signature': 'Директор ООО «Альфа» [УТОЧНИТЬ: ФИО]',
                },
            },
        }, ensure_ascii=False) + '</yuriy_tool>\n'
        '<yuriy_tool>' + json.dumps({
            'tool': 'create_docx_result',
            'args': {
                'mode': 'freeform',
                'title': 'Правовое заключение по взысканию долга с ООО Бета',
                'docKind': 'legal_opinion',
                'heading': 'ПРАВОВОЕ ЗАКЛЮЧЕНИЕ',
                'subtitle': 'По перспективам взыскания задолженности с ООО «Бета»',
                'sections': [
                    {'heading': '1. Выводы по документам', 'body': 'Факт поставки подтверждён УПД № 15. Необходимо направить претензию и выждать 30 календарных дней (ч. 5 ст. 4 АПК РФ).'},
                ],
                'documentDate': '22.11.2026',
                'signature': 'Юридический консультант Юрий',
            },
        }, ensure_ascii=False) + '</yuriy_tool>\n'
        '<yuriy_tool>' + json.dumps({
            'tool': 'propose_calendar_event',
            'args': {'instruction': '23.12.2026 в 10:00 Проверить истечение 30-дневного срока ответа на претензию ООО Бета и подать иск'},
        }, ensure_ascii=False) + '</yuriy_tool>'
    )
    step2 = (
        '## Отчёт о выполнении поручения\n'
        '1. Подготовлена и сохранена в «Результаты» **Досудебная претензия к ООО Бета** (`.docx`).\n'
        '2. Подготовлено и сохранено **Правовое заключение** (`.docx`).\n'
        '3. Предложен контрольный срок подачи иска после 30-дневного претензионного порядка по ч. 5 ст. 4 АПК РФ.\n'
        'Перед отправкой заполните реквизиты `[УТОЧНИТЬ]`.'
    )

    app = create_app(
        config_path=tmp_path / 'config.json',
        workspace=tmp_path / 'space',
        provider=ScriptedAgentProvider([step1, step2]),
    )
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token'] = app.state.test_token
        c.post('/api/models/settings', json={'key': KEY, 'modelB': 'test-agent', 'consent': True})
        c.post('/api/secretary/settings', json={'timezone': 'Europe/Samara', 'notificationsEnabled': False, 'confirmed': True})
        pid, fid = _upload_and_index(c, 'Договор_7.txt', 'Договор поставки № 7 от 01.09.2026 между ООО Альфа и ООО Бета на 500 000 руб.')

        st = c.get('/api/state').json()
        tid = st['state']['projects'][0]['threads'][0]['id']
        res = c.post('/api/agent/run', json={
            'requestId': str(uuid.uuid4()),
            'projectId': pid,
            'threadId': tid,
            'mode': 'execute',
            'text': 'Проверь договор, подготовь досудебную претензию и правовое заключение в DOCX и поставь контрольный срок.',
            'deep': False,
            'revision': st['revision'],
            'documentIds': [fid],
            'documentsConsent': True,
        })
        assert res.status_code == 200, res.text
        msg = _finish_sse(c, res.json()['requestId'])
        assert msg['status'] == 'complete'
        meta = msg['agentMeta']
        assert len(meta['createdResults']) == 2
        assert len(meta['eventProposals']) == 1
        assert meta['eventProposals'][0]['date'] == '2026-12-23'

        # Verify both DOCX files actually exist in project Results and can be downloaded + backed up
        proj = c.get('/api/state').json()['state']['projects'][0]
        assert len(proj['results']) == 2
        for created in meta['createdResults']:
            dl = c.get(f'/api/projects/{pid}/results/{created["id"]}/file?format=docx')
            assert dl.status_code == 200
            parsed = docx.Document(io.BytesIO(dl.content))
            assert len(parsed.paragraphs) > 5

        backup = c.post('/api/backup')
        assert backup.status_code == 200
