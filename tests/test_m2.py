import asyncio
import copy
import io
import json
import os
import uuid
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from yuriy.app import create_app
from yuriy.models import DpapiSecrets, ModelSettings, endpoint_valid
from yuriy.provider import OpenAIProvider, ProviderError
from yuriy.storage import Workspace

KEY='test-only-credential-not-real'

class FakeProvider:
    def __init__(self):self.calls=[]
    async def models(self,cfg):return ['test-b','test-d']
    async def stream(self,cfg,messages):
        self.calls.append((dict(cfg),copy.deepcopy(messages)))
        if messages[-1]['content']=='FAIL':raise ProviderError('rate_limit','Тестовая квота исчерпана')
        yield {'text':'Первый фрагмент. '}
        if messages[-1]['content']=='SLOW':await asyncio.sleep(60)
        else:await asyncio.sleep(.03)
        yield {'text':'<script>window.pwned=1</script> Ответ.'}
        yield {'usage':{'prompt_tokens':10,'completion_tokens':12,'total_tokens':22}}
        yield {'finishReason':'stop'}

@pytest.fixture
def service(tmp_path):
    provider=FakeProvider()
    app=create_app(config_path=tmp_path/'runtime'/'config.json',workspace=tmp_path/'space',provider=provider)
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        r=c.post('/api/models/settings',json={'key':KEY,'modelB':'test-b','modelD':'test-d','consent':True})
        assert r.status_code==200,r.text
        yield c,provider,app

def request(c,content='Вопрос',**kwargs):
    data=c.get('/api/state').json();p=data['state']['projects'][0];t=p['threads'][0]
    return dict(requestId=str(uuid.uuid4()),projectId=p['id'],threadId=t['id'],text=content,revision=data['revision'],**kwargs)

def finish(c,rid):
    r=c.get(f'/api/chat/{rid}/events');assert r.status_code==200,r.text
    events=[json.loads(line[6:]) for line in r.text.splitlines() if line.startswith('data: ')]
    assert events[-1]['done']
    return events[-1]['message']

def test_complete_idempotency_and_provenance(service):
    c,p,app=service;body=request(c)
    assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId']);assert m['status']=='complete' and m['usage']['total_tokens']==22
    assert m['model']=='test-b' and m['sourceStatus']=='not-verified'
    assert c.post('/api/chat',json=body).json()['reused'] is True
    assert len(p.calls)==1
    assert c.post('/api/chat',json={**body,'text':'другое'}).status_code==409
    data=c.get('/api/state').json()
    assert c.put('/api/state',json=data).status_code==200
    data=c.get('/api/state').json();data['state']['projects'][0]['threads'][0]['messages'][-1]['text']='Подмена'
    assert c.put('/api/state',json=data).status_code==400
    data=c.get('/api/state').json();data['state']['projects'][0]['threads'][0]['messages'].pop()
    assert c.put('/api/state',json=data).status_code==400

def test_cancel_guards_and_other_window(service):
    c,p,app=service;body=request(c,'SLOW')
    assert c.post('/api/chat',json=body).status_code==200
    assert c.get('/api/chat/active').json()['active']['requestId']==body['requestId']
    assert c.put('/api/state',json=c.get('/api/state').json()).status_code==409
    assert c.post('/api/backup').status_code==409
    assert c.post('/api/models/settings',json={'modelB':'changed'}).status_code==409
    assert c.post('/api/chat',json=request(c)).status_code==409
    assert c.post('/api/chat/'+body['requestId']+'/cancel').status_code==200
    assert finish(c,body['requestId'])['status']=='cancelled'
    assert c.get('/api/chat/active').json()['active'] is None
    assert len(p.calls)<=1

def test_deep_errors_revision_consent_archive(service):
    c,p,app=service
    body=request(c,'FAIL',deep=True);assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId']);assert m['status']=='error' and m['errorCode']=='rate_limit' and m['model']=='test-d'
    body=request(c);body['revision']-=1;assert c.post('/api/chat',json=body).status_code==409
    c.post('/api/models/settings',json={'consent':False});assert c.post('/api/chat',json=request(c)).status_code==400
    c.post('/api/models/settings',json={'consent':True,'modelD':''});assert c.post('/api/chat',json=request(c,deep=True)).status_code==400
    data=c.get('/api/state').json();data['state']['projects'][0]['archived']=True
    assert c.put('/api/state',json=data).status_code==200
    assert c.post('/api/chat',json=request(c)).status_code==400
    assert len(p.calls)==1

def test_settings_no_secret_in_browser_config_backup(service,tmp_path):
    c,p,app=service
    assert KEY not in c.get('/api/models/settings').text
    assert KEY not in c.get('/').text
    assert c.get('/api/models/list').json()['models']==['test-b','test-d']
    for f in (tmp_path/'runtime').glob('*.json'): assert KEY not in f.read_text('utf-8')
    backup=c.post('/api/backup');assert backup.status_code==200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
        assert all(KEY.encode() not in z.read(name) for name in z.namelist())
        assert all('dpapi' not in name and 'models.json' not in name for name in z.namelist())
    assert c.post('/api/models/settings',json={'endpoint':'https://example.com/v1'}).status_code==400
    assert c.post('/api/models/settings',json={'clearKey':True}).json()['hasKey'] is False
    assert c.get('/api/models/settings',headers={'X-Yuriy-Token':''}).status_code==403

@pytest.mark.parametrize('value',['http://example.com/v1','https://localhost/v1','https://127.0.0.1/v1','https://192.168.1.2/v1','https://a:b@example.com/v1','https://example.com:444/v1','https://example.com/v1?key=secret','https://example.com/v1#x','https://example.local/v1'])
def test_endpoints(value):
    with pytest.raises(ValueError):endpoint_valid(value)

def test_context_isolation_and_budget(service):
    c,p,app=service
    data=c.get('/api/state').json();project=data['state']['projects'][0];thread=project['threads'][0]
    project['instruction']='ИНСТРУКЦИЯ ТЕКУЩЕГО ПРОЕКТА'
    for i in range(45):thread['messages'].append({'id':str(uuid.uuid4()),'role':'user','text':str(i)+'x'*1500})
    other=copy.deepcopy(project);other['id']=str(uuid.uuid4());other['threads'][0]['id']=str(uuid.uuid4());other['threadId']=other['threads'][0]['id'];other['threads'][0]['messages']=[];other['instruction']='СЕКРЕТ ДРУГОГО ПРОЕКТА';data['state']['projects'].append(other)
    assert c.put('/api/state',json=data).status_code==200
    body=request(c);assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId']);sent=p.calls[-1][1];encoded=json.dumps(sent,ensure_ascii=False)
    assert 'ИНСТРУКЦИЯ ТЕКУЩЕГО ПРОЕКТА' in encoded and 'СЕКРЕТ ДРУГОГО ПРОЕКТА' not in encoded
    assert m['contextSummary']['historyTruncated'] and len(sent)<=43
    assert sum(len(x['content']) for x in sent)<43000

def test_restart_recovers_and_idempotency(tmp_path):
    root=tmp_path/'space';w=Workspace(root);data=w.read();p=data['state']['projects'][0];t=p['threads'][0];rid=str(uuid.uuid4())
    w.start_generation(p['id'],t['id'],rid,'вопрос','test-b','B',data['revision'],{})
    w.update_generation(rid,{'text':'Частичный ответ'});w.close()
    provider=FakeProvider();app=create_app(config_path=tmp_path/'runtime'/'config.json',workspace=root,provider=provider)
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        m=finish(c,rid);assert m['status']=='interrupted' and m['text']=='Частичный ответ'
        body=dict(requestId=rid,projectId=p['id'],threadId=t['id'],text='вопрос',revision=0)
        assert c.post('/api/chat',json=body).json()['reused']
        assert not provider.calls

def test_real_adapter_with_mock_http():
    async def run():
        calls=[]
        def handler(req):
            calls.append(req)
            if req.url.path.endswith('/models'):return httpx.Response(200,json={'data':[{'id':'test-b'}]})
            return httpx.Response(200,content=b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"total_tokens":4}}\n\ndata: [DONE]\n\n')
        adapter=OpenAIProvider(transport=httpx.MockTransport(handler),dns_checker=lambda _:None)
        cfg={'endpoint':'https://example.com/v1','key':KEY,'model':'test-b','maxOutputTokens':500}
        assert await adapter.models(cfg)==['test-b']
        frames=[f async for f in adapter.stream(cfg,[{'role':'user','content':'test'}])]
        assert frames[0]['text']=='hello' and frames[1]['usage']['total_tokens']==4
        assert calls[-1].headers['authorization']=='Bearer '+KEY
        assert json.loads(calls[-1].content)['stream'] is True
    asyncio.run(run())

@pytest.mark.parametrize('status,code',[(401,'unauthorized'),(402,'balance'),(403,'forbidden'),(404,'not_found'),(429,'rate_limit'),(503,'upstream')])
def test_adapter_http_errors_redact(status,code):
    async def run():
        p=OpenAIProvider(transport=httpx.MockTransport(lambda r:httpx.Response(status,text=KEY)),dns_checker=lambda _:None)
        with pytest.raises(ProviderError) as exc:
            _=[x async for x in p.stream({'endpoint':'https://example.com','key':KEY,'model':'x','maxOutputTokens':128},[])]
        assert exc.value.code==code and KEY not in str(exc.value)
    asyncio.run(run())

def test_adapter_interrupted():
    async def run():
        p=OpenAIProvider(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=b'data: {"choices":[{"delta":{"content":"part"}}]}\n\n')),dns_checker=lambda _:None)
        frames=[]
        with pytest.raises(ProviderError) as exc:
            async for frame in p.stream({'endpoint':'https://example.com','key':KEY,'model':'x','maxOutputTokens':128},[]):frames.append(frame)
        assert ''.join(f.get('text','') for f in frames)=='part' and exc.value.code=='interrupted'
    asyncio.run(run())

@pytest.mark.skipif(os.name!='nt',reason='Requires actual Windows DPAPI')
def test_windows_dpapi(tmp_path):
    store=DpapiSecrets(tmp_path/'key.dpapi');store.set(KEY)
    assert KEY.encode() not in store.path.read_bytes()
    assert DpapiSecrets(store.path).get()==KEY
    store.delete();assert store.get()=='' and not store.path.exists()


def test_full_russian_message_body(service):
    c,p,app=service;body=request(c,'Я'*12000)
    assert c.post('/api/chat',json=body).status_code==200
    assert finish(c,body['requestId'])['status']=='complete'
    assert p.calls[-1][1][-1]['content']=='Я'*12000


def test_immediate_cancel_and_backup_restore(service):
    c,p,app=service;body=request(c,'SLOW')
    assert c.post('/api/chat',json=body).status_code==200
    assert c.post('/api/chat/'+body['requestId']+'/cancel').status_code==200
    assert finish(c,body['requestId'])['status']=='cancelled'
    backup=c.post('/api/backup');assert backup.status_code==200
    newer=request(c);assert c.post('/api/chat',json=newer).status_code==200
    finish(c,newer['requestId'])
    r=c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'});assert r.status_code==200,r.text
    assert c.get('/api/chat/'+newer['requestId']+'/events').status_code==400
    assert finish(c,body['requestId'])['status']=='cancelled'
    assert c.get('/api/models/settings').json()['hasKey']


def test_no_key_or_consent_no_provider_call(tmp_path):
    provider=FakeProvider();app=create_app(config_path=tmp_path/'runtime'/'config.json',workspace=tmp_path/'space',provider=provider)
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        assert c.post('/api/chat',json=request(c)).status_code==400
        c.post('/api/models/settings',json={'modelB':'x','consent':True})
        assert c.post('/api/chat',json=request(c)).status_code==400
        assert not provider.calls
