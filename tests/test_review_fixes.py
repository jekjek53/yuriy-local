import asyncio
import copy
import json
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from test_m2 import service,request,finish,KEY
from yuriy.app import create_app
from yuriy.provider import OpenAIProvider
from yuriy.storage import Workspace


def test_cancel_preserves_observed_prefix(service):
    c,provider,app=service;body=request(c,'SLOW')
    assert c.post('/api/chat',json=body).status_code==200
    # Wait on a real synchronization condition, not on an assumed number of seconds.
    async def observed():
        for _ in range(1000):
            job=app.state.chat.jobs[body['requestId']]
            if job.message['text']:return job.message['text']
            await asyncio.sleep(.001)
        raise AssertionError('No text received')
    prefix=c.portal.call(observed)
    assert c.post('/api/chat/'+body['requestId']+'/cancel').status_code==200
    m=finish(c,body['requestId']);assert m['status']=='cancelled' and m['text']==prefix
    assert m['streamDiagnostics']['receivedTextChars']==len(prefix)
    assert m['streamDiagnostics']['firstTextMs'] is not None
    data=c.get('/api/state').json();saved=data['state']['projects'][0]['threads'][0]['messages'][-1]
    assert saved['text']==prefix and saved['status']=='cancelled'


def test_cancel_before_first_text(tmp_path):
    class Delayed:
        async def stream(self,cfg,messages):
            await asyncio.sleep(60)
            yield {'text':'later'}
    app=create_app(config_path=tmp_path/'config.json',workspace=tmp_path/'space',provider=Delayed())
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        c.post('/api/models/settings',json={'key':KEY,'modelB':'x','consent':True})
        body=request(c);c.post('/api/chat',json=body);c.post('/api/chat/'+body['requestId']+'/cancel')
        m=finish(c,body['requestId']);assert m['status']=='cancelled' and m['text']==''
        assert 'первого текстового' in m['error'] and m['streamDiagnostics']['receivedTextChars']==0
        assert m['streamDiagnostics']['firstTextMs'] is None


def test_late_checkpoint_cannot_roll_final_status_or_text_back(tmp_path):
    ws=Workspace(tmp_path/'space')
    try:
        data=ws.read();p=data['state']['projects'][0];t=p['threads'][0];rid=str(uuid.uuid4())
        ws.start_generation(p['id'],t['id'],rid,'test','b','B',data['revision'],{})
        ws.update_generation(rid,{'text':'prefix-more','status':'running'})
        m=ws.update_generation(rid,{'text':'prefix','status':'cancelled'})
        assert m['text']=='prefix-more'
        ws.update_generation(rid,{'text':'','status':'running'})
        m=ws.generation(rid)['message'];assert m['status']=='cancelled' and m['text']=='prefix-more'
    finally:ws.close()


def test_failed_user_tasks_excluded_but_complete_turn_and_notes_kept(service):
    c,provider,app=service
    failed=request(c,'FAIL');c.post('/api/chat',json=failed);finish(c,failed['requestId'])
    cancelled=request(c,'SLOW');c.post('/api/chat',json=cancelled);c.post('/api/chat/'+cancelled['requestId']+'/cancel');finish(c,cancelled['requestId'])
    good=request(c,'успешный вопрос');c.post('/api/chat',json=good);finish(c,good['requestId'])
    current=request(c,'текущая задача');c.post('/api/chat',json=current);m=finish(c,current['requestId'])
    messages=provider.calls[-1][1]
    assert not any(x['content'] in ('FAIL','SLOW') for x in messages)
    assert any(x['content']=='успешный вопрос' for x in messages)
    assert messages[-1]['content']=='текущая задача'
    assert m['contextSummary']['excludedUnsuccessfulRequests']==2
    saved=c.get('/api/state').json()['state']['projects'][0]['threads'][0]['messages']
    assert any(x['text']=='FAIL' for x in saved) and any(x['text']=='SLOW' for x in saved)


def test_context_budget_never_includes_orphan_assistant(service):
    _,_,app=service
    thread={'messages':[{'role':'user','requestId':'r','text':'x'*39000},{'role':'assistant','requestId':'r','status':'complete','text':'short answer'}]}
    sent,summary=app.state.chat.context({'instruction':''},thread,'question'*1000)
    assert not any(x['role']=='assistant' for x in sent)
    assert summary['historyTruncated']


def test_independent_d_limit_preserves_old_default(service):
    c,p,app=service
    assert app.state.models.for_request(True)['maxOutputTokens']==1800
    assert c.post('/api/models/settings',json={'maxOutputTokensD':4000}).status_code==200
    assert app.state.models.for_request(True)['maxOutputTokens']==4000
    assert app.state.models.for_request(False)['maxOutputTokens']==1800
    body=request(c,deep=True);c.post('/api/chat',json=body);finish(c,body['requestId'])
    assert p.calls[-1][0]['maxOutputTokens']==4000
    assert c.post('/api/models/settings',json={'maxOutputTokensD':99}).status_code==400
    assert c.post('/api/models/settings',json={'maxOutputTokensD':None}).status_code==200
    assert app.state.models.for_request(True)['maxOutputTokens']==1800


@pytest.mark.parametrize('choice,expected,flag',[
    ({'delta':{'content':[{'type':'text','text':'typed text'}]}},'typed text','contentPartsSeen'),
    ({'message':{'content':'terminal text'},'finish_reason':'stop'},'terminal text','terminalMessageSeen'),
    ({'delta':{'reasoning_content':'private hidden text'},'finish_reason':'stop'},'','reasoningSeen'),
    ({'delta':{'content':[{'type':'image','url':'ignored'}]}},'','unsupportedContent'),
])
def test_stream_variants_and_content_free_diagnostics(choice,expected,flag):
    async def run():
        raw=('data: '+json.dumps({'choices':[choice]})+'\n\ndata: [DONE]\n\n').encode()
        provider=OpenAIProvider(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=raw)),dns_checker=lambda _:None)
        frames=[f async for f in provider.stream({'endpoint':'https://example.com','key':KEY,'model':'x','maxOutputTokens':1800},[])]
        assert ''.join(f.get('text','') for f in frames)==expected
        assert frames[-1]['streamDiagnostics'][flag]
        assert 'private hidden text' not in json.dumps(frames)
    asyncio.run(run())


def test_cancel_real_adapter_closes_stream_and_persists_text(tmp_path):
    class Stream(httpx.AsyncByteStream):
        closed=False
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"visible prefix"}}]}\n\n'
            await asyncio.sleep(60)
        async def aclose(self):self.closed=True
    stream=Stream()
    provider=OpenAIProvider(transport=httpx.MockTransport(lambda r:httpx.Response(200,stream=stream)),dns_checker=lambda _:None)
    app=create_app(config_path=tmp_path/'config.json',workspace=tmp_path/'space',provider=provider)
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        c.post('/api/models/settings',json={'key':KEY,'modelB':'x','consent':True})
        body=request(c);c.post('/api/chat',json=body)
        async def observed():
            for _ in range(1000):
                if app.state.chat.jobs[body['requestId']].message['text']=='visible prefix':return
                await asyncio.sleep(.001)
            raise AssertionError('No SSE prefix')
        c.portal.call(observed);c.post('/api/chat/'+body['requestId']+'/cancel')
        m=finish(c,body['requestId']);assert m['status']=='cancelled' and m['text']=='visible prefix'
        assert m['streamDiagnostics']['frames']==1 and stream.closed
        saved=c.get('/api/state').json()['state']['projects'][0]['threads'][0]['messages'][-1]
        assert saved['text']=='visible prefix'


def test_empty_reasoning_only_response_stays_error_no_retry(tmp_path):
    raw=b'data: {"choices":[{"delta":{"reasoning_content":"never save this"}}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    calls=[]
    def handler(r):calls.append(1);return httpx.Response(200,content=raw)
    provider=OpenAIProvider(transport=httpx.MockTransport(handler),dns_checker=lambda _:None)
    app=create_app(config_path=tmp_path/'config.json',workspace=tmp_path/'space',provider=provider)
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        c.post('/api/models/settings',json={'key':KEY,'modelB':'x','consent':True})
        body=request(c);c.post('/api/chat',json=body);m=finish(c,body['requestId'])
        assert m['errorCode']=='empty' and m['streamDiagnostics']['reasoningSeen'] and len(calls)==1
        assert 'never save this' not in json.dumps(c.get('/api/state').json())
