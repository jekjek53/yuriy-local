import copy
import io
import json
import time
import uuid
import zipfile
from pathlib import Path
import pytest
from docx import Document
from test_m2 import service,request,finish
from test_m3 import upload,index


def state(c):return c.get('/api/state').json()
def project(c):return state(c)['state']['projects'][0]
def edit_plan(c,pid,fid,body):return c.post(f'/api/projects/{pid}/documents/{fid}/edit-plan',json=body)
def confirm(c,plan):return c.post('/api/operations/'+plan['planId']+'/confirm',json={'confirmed':True})
def result_plan(c,pid,**body):
    from yuriy.legal_drafts import SCHEMAS
    if 'text' in body:
        content=body.pop('text');body.update(templateId='pretension',fields={k:'[УТОЧНИТЬ: '+v+']' for k,v in SCHEMAS['pretension']['fields']});body['fields']['circumstances']=content
    return c.post(f'/api/projects/{pid}/results/docx-plan',json=body)

def word_file(text='Оплата через десять дней.',complex=False):
    doc=Document();p=doc.add_paragraph();run=p.add_run(text);run.bold=True
    doc.add_table(rows=1,cols=1).cell(0,0).text='Условие договора'
    if complex:doc.sections[0].header.paragraphs[0].text='Колонтитул'
    out=io.BytesIO();doc.save(out);return out.getvalue()


def check_body(c,source,**kwargs):
    p=project(c);return dict(requestId=str(uuid.uuid4()),projectId=p['id'],threadId=p['threads'][0]['id'],messageId=source['id'],excerpt=source['text'],revision=state(c)['revision'],confirmed=True,**kwargs)

def answer(c):
    b=request(c);assert c.post('/api/chat',json=b).status_code==200;return finish(c,b['requestId'])


def test_txt_edit_preview_confirm_versions_backup_restore(service,tmp_path):
    c,_,_=service;pid,fid=upload(c,'договор.txt','Срок десять дней.\n'.encode());index(c,pid,fid)
    r=edit_plan(c,pid,fid,{'text':'Срок пятнадцать дней.\n'});assert r.status_code==200,r.text
    plan=r.json();assert '-Срок десять' in plan['diff'] and plan['oldSha256']!=plan['newSha256']
    assert project(c)['documents'][0]['id']==fid
    assert c.post('/api/operations/'+plan['planId']+'/confirm',json={}).status_code==400
    result=confirm(c,plan);assert result.status_code==200,result.text;op=result.json()['operation']
    assert confirm(c,plan).status_code==409
    d=project(c)['documents'][0];assert d['id']!=fid
    assert c.get(f'/api/projects/{pid}/files/{d["id"]}').content.decode()=='Срок пятнадцать дней.\n'
    old=c.get(f'/api/projects/{pid}/operations/{op["id"]}/revision-file?which=before');assert old.content.decode()=='Срок десять дней.\n'
    assert (tmp_path/'space'/'projects'/pid/'materials'/(fid+'.blob')).read_bytes()==old.content
    assert c.get(f'/api/projects/{pid}/documents').json()['documents'][0]['status']=='added'
    assert op['preflight'] and list((tmp_path/'space'/'.yuriy/backups').glob('*.zip'))
    data=state(c);data['state']['projects'][0]['operations']=[];assert c.put('/api/state',json=data).status_code==400
    backup=c.post('/api/backup');assert backup.status_code==200
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert c.get(f'/api/projects/{pid}/operations/{op["id"]}/revision-file?which=before').content==old.content


def test_edit_rechecks_revision_hash_ttl_and_archive(service,tmp_path):
    c,_,_=service;pid,fid=upload(c,'test.md',b'old');plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    data=state(c);data['state']['projects'][0]['name']='changed';assert c.put('/api/state',json=data).status_code==200
    assert confirm(c,plan).status_code==409
    plan=edit_plan(c,pid,fid,{'text':'new'}).json();path=tmp_path/'space'/'projects'/pid/'materials'/(fid+'.blob');path.write_bytes(b'outside')
    assert confirm(c,plan).status_code==409
    path.write_bytes(b'old');plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    app=c.app;app.state.runtime['operations'].plans[plan['planId']]['expires']=time.monotonic()-1
    assert confirm(c,plan).status_code==409
    data=state(c);data['state']['projects'][0]['archived']=True;assert c.put('/api/state',json=data).status_code==200
    assert edit_plan(c,pid,fid,{'text':'new'}).status_code==400


def test_edit_backup_failure_and_cross_project(service,monkeypatch):
    c,_,app=service;pid,fid=upload(c,'test.txt',b'old');plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    def fail():raise OSError('disk full')
    monkeypatch.setattr(app.state.runtime['workspace'],'backup',fail)
    assert confirm(c,plan).status_code==500
    assert project(c)['documents'][0]['id']==fid
    assert edit_plan(c,'wrong',fid,{'text':'new'}).status_code==400
    assert edit_plan(c,pid,'../x',{'text':'new'}).status_code in (400,404)


def test_docx_edit_is_conservative_and_preserves_other_parts(service):
    c,_,_=service;raw=word_file();pid,fid=upload(c,'текст.docx',raw)
    plan=edit_plan(c,pid,fid,{'find':'десять','replace':'пятнадцать'});assert plan.status_code==200,plan.text
    p=plan.json();assert 'пятнадцать' in p['after']
    candidate=c.get('/api/operations/'+p['planId']+'/preview-file').content
    with zipfile.ZipFile(io.BytesIO(raw)) as before,zipfile.ZipFile(io.BytesIO(candidate)) as after:
        assert before.namelist()==after.namelist()
        assert all(before.read(n)==after.read(n) for n in before.namelist() if n!='word/document.xml')
    doc=Document(io.BytesIO(candidate));assert doc.paragraphs[0].runs[0].bold and 'пятнадцать' in doc.paragraphs[0].text
    assert confirm(c,p).status_code==200
    pid,fid=upload(c,'complex.docx',word_file(complex=True))
    assert edit_plan(c,pid,fid,{'find':'десять','replace':'пятнадцать'}).status_code==400
    pid,fid=upload(c,'ambiguous.docx',word_file('десять десять'))
    assert edit_plan(c,pid,fid,{'find':'десять','replace':'пятнадцать'}).status_code==400
    assert edit_plan(c,pid,fid,{'find':'нет','replace':'да'}).status_code==400


def test_generate_docx_draft_confirm_download_tamper_backup(service,tmp_path):
    c,_,_=service;pid=project(c)['id'];m=answer(c)
    plan=result_plan(c,pid,title='Претензия [УТОЧНИТЬ]',text='Текст черновика\nРеквизиты: [УТОЧНИТЬ]',sourceMessageId=m['id'])
    assert plan.status_code==200,plan.text;p=plan.json();assert project(c)['results']==[]
    raw=c.get('/api/operations/'+p['planId']+'/preview-file').content;doc=Document(io.BytesIO(raw));alltext='\n'.join(x.text for x in doc.paragraphs)
    assert 'ЧЕРНОВИК' in alltext and 'Чек-лист' in alltext and '[УТОЧНИТЬ]' in alltext
    assert confirm(c,p).status_code==200
    r=project(c)['results'][0];assert r['format']=='docx' and r['sourceStatus']=='not-verified' and r['bodyEdited']
    assert c.get(f'/api/projects/{pid}/results/{r["id"]}/file').content==raw
    backup=c.post('/api/backup');assert backup.status_code==200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:assert any(n.endswith('.docx') for n in z.namelist())
    data=state(c);data['state']['projects'][0]['results'][0]['text']='forged';assert c.put('/api/state',json=data).status_code==400
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert c.get(f'/api/projects/{pid}/results/{r["id"]}/file').content==raw
    (tmp_path/'space'/'projects'/pid/'results'/(r['id']+'.docx')).write_bytes(b'tampered')
    assert c.get(f'/api/projects/{pid}/results/{r["id"]}/file').status_code==409
    assert c.post('/api/backup').status_code==400


def test_manual_result_metadata_cannot_forge_docx(service):
    c,_,_=service;data=state(c);data['state']['projects'][0]['results'].append({'id':str(uuid.uuid4()),'name':'fake','text':'x','created':'now','format':'docx','generatedBy':'server-m3b','artifactSha256':'a'*64})
    assert c.put('/api/state',json=data).status_code==400


def test_check_c_settings_scope_idempotency_and_history(service):
    c,p,app=service;m=answer(c)
    b=check_body(c,m);assert c.post('/api/check',json=b).status_code==400
    assert c.post('/api/models/settings',json={'modelC':'test-c','verificationEnabled':True,'maxOutputTokensC':3000}).status_code==200
    b=check_body(c,m);r=c.post('/api/check',json=b);assert r.status_code==200,r.text
    check=finish(c,b['requestId']);assert check['tier']=='C' and check['model']=='test-c' and check['kind']=='verification'
    assert check['verifiesMessageId']==m['id'] and check['checkScope']['wholeAnswer'] and check['sourceStatus']=='not-verified'
    assert p.calls[-1][0]['maxOutputTokens']==3000 and 'answerExcerpt' in p.calls[-1][1][-1]['content']
    count=len(p.calls);assert c.post('/api/check',json=b).json()['reused'] and len(p.calls)==count
    query=request(c,'новая задача');c.post('/api/chat',json=query);finish(c,query['requestId'])
    assert not any('answerExcerpt' in x['content'] for x in p.calls[-1][1])
    b=check_body(c,m);b['excerpt']=m['text'][:8];c.post('/api/check',json=b);partial=finish(c,b['requestId'])
    assert not partial['checkScope']['wholeAnswer'] and partial['checkScope']['checkedChars']==8


def test_check_uses_historical_sources_requires_reconsent(service):
    c,p,_=service;pid,fid=upload(c,'source.txt','Условие оплаты 17 дней'.encode());index(c,pid,fid)
    b=request(c);b.update(documentIds=[fid],documentsConsent=True);c.post('/api/chat',json=b);m=finish(c,b['requestId'])
    c.post('/api/models/settings',json={'modelC':'test-c','verificationEnabled':True})
    check=check_body(c,m);assert c.post('/api/check',json=check).status_code==400
    check['documentsConsent']=True;assert c.post('/api/check',json=check).status_code==200
    value=finish(c,check['requestId']);assert value['checkScope']['sourceCount']==1
    assert 'Условие оплаты 17 дней' in p.calls[-1][1][-1]['content']
    plan=result_plan(c,pid,title='Проект',text=m['text'],sourceMessageId=m['id']);assert plan.status_code==200
    raw=c.get('/api/operations/'+plan.json()['planId']+'/preview-file').content
    assert 'Условие оплаты 17 дней' not in '\n'.join(p.text for p in Document(io.BytesIO(raw)).paragraphs)
    review=c.get('/api/operations/'+plan.json()['planId']+'/preview-file?format=review').content
    assert 'Условие оплаты 17 дней' in '\n'.join(p.text for p in Document(io.BytesIO(review)).paragraphs)


def test_check_rejects_changed_question_and_false_confirmation(service):
    c,_,_=service;m=answer(c);c.post('/api/models/settings',json={'modelC':'c','verificationEnabled':True})
    b=check_body(c,m);b['confirmed']=False;assert c.post('/api/check',json=b).status_code==400
    data=state(c);data['state']['projects'][0]['threads'][0]['messages'][0]['text']='edited question'
    assert c.put('/api/state',json=data).status_code==200
    assert c.post('/api/check',json=check_body(c,m)).status_code==409


def test_check_over_budget_no_paid_call_then_partial_scope(service,monkeypatch):
    c,provider,_=service
    async def long_stream(cfg,messages):
        provider.calls.append((dict(cfg),copy.deepcopy(messages)))
        yield {'text':'UNIQUE-START '+'x'*35000+' END'}
        yield {'finishReason':'stop'}
    monkeypatch.setattr(provider,'stream',long_stream)
    b=request(c,'q'*6000);c.post('/api/chat',json=b);m=finish(c,b['requestId'])
    c.post('/api/models/settings',json={'modelC':'c','verificationEnabled':True})
    count=len(provider.calls);body=check_body(c,m)
    r=c.post('/api/check',json=body);assert r.status_code==400 and '40000' in r.text
    assert len(provider.calls)==count
    body=check_body(c,m);body['excerpt']=m['text'][:20000]
    assert c.post('/api/check',json=body).status_code==200
    value=finish(c,body['requestId']);assert not value['checkScope']['wholeAnswer'] and value['checkScope']['end']==20000


def test_check_cancel_error_and_no_retry(service,monkeypatch):
    from yuriy.provider import ProviderError
    c,p,app=service;m=answer(c);c.post('/api/models/settings',json={'modelC':'c','verificationEnabled':True})
    async def slow(cfg,messages):
        p.calls.append((dict(cfg),copy.deepcopy(messages)))
        yield {'text':'C prefix'}
        await __import__('asyncio').sleep(60)
    monkeypatch.setattr(p,'stream',slow)
    b=check_body(c,m);assert c.post('/api/check',json=b).status_code==200
    assert c.get('/api/chat/active').json()['active']['message']['kind']=='verification'
    c.post('/api/chat/'+b['requestId']+'/cancel');value=finish(c,b['requestId']);assert value['status']=='cancelled'
    async def failed(cfg,messages):
        p.calls.append((dict(cfg),copy.deepcopy(messages)));raise ProviderError('rate_limit','C quota')
        yield {}
    monkeypatch.setattr(p,'stream',failed)
    b=check_body(c,m);c.post('/api/check',json=b);value=finish(c,b['requestId'])
    assert value['status']=='error' and value['errorCode']=='rate_limit' and value['checkScope']['responseId']==m['id']
    count=len(p.calls);assert c.post('/api/check',json=b).json()['reused'] and len(p.calls)==count


def test_plan_discard_and_failed_commit_leave_original(service,monkeypatch):
    c,_,app=service;pid,fid=upload(c,'test.txt',b'old');plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    assert c.post('/api/operations/'+plan['planId']+'/discard').status_code==200
    assert confirm(c,plan).status_code==409
    plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    def fail(*a,**k):raise OSError('simulated disk')
    monkeypatch.setattr(app.state.runtime['workspace'],'_write',fail)
    assert confirm(c,plan).status_code==500
    assert project(c)['documents'][0]['id']==fid and not project(c).get('operations')
    assert c.get(f'/api/projects/{pid}/files/{fid}').content==b'old'


def test_docx_external_relationship_and_invalid_xml_rejected(service):
    c,_,_=service;raw=word_file();out=io.BytesIO(raw)
    with zipfile.ZipFile(out,'a') as z:z.writestr('word/_rels/external.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId9" TargetMode="External" Target="https://example.com/template" Type="template"/></Relationships>')
    pid,fid=upload(c,'external.docx',out.getvalue());r=edit_plan(c,pid,fid,{'find':'десять','replace':'двадцать'})
    assert r.status_code==400
    assert result_plan(c,pid,title='t',text='bad\uFFFF').status_code==400
    assert result_plan(c,pid,title='t',text='a\n'*2001).status_code==400


def test_crlf_text_edit_and_check_excerpt_coordinates(service,monkeypatch):
    c,p,_=service;pid,fid=upload(c,'windows.txt',b'old\r\nline\r\n')
    plan=edit_plan(c,pid,fid,{'text':'new\nline\n'}).json();assert confirm(c,plan).status_code==200
    d=project(c)['documents'][0];assert c.get(f'/api/projects/{pid}/files/{d["id"]}').content==b'new\r\nline\r\n'
    async def crlf(cfg,messages):yield {'text':'First\r\nSecond\r\nThird'};yield {'finishReason':'stop'}
    monkeypatch.setattr(p,'stream',crlf);m=answer(c)
    c.post('/api/models/settings',json={'modelC':'c','verificationEnabled':True})
    b=check_body(c,m);b['excerpt']='First\nSecond\nThird';assert c.post('/api/check',json=b).status_code==200
    value=finish(c,b['requestId']);assert value['checkScope']['wholeAnswer'] and value['checkScope']['checkedChars']==len(m['text'])


def test_docx_table_edit_and_split_run_rejection(service):
    c,_,_=service;pid,fid=upload(c,'table.docx',word_file())
    r=edit_plan(c,pid,fid,{'find':'Условие договора','replace':'Новое условие'});assert r.status_code==200,r.text
    candidate=c.get('/api/operations/'+r.json()['planId']+'/preview-file').content
    assert Document(io.BytesIO(candidate)).tables[0].cell(0,0).text=='Новое условие'
    doc=Document();p=doc.add_paragraph();p.add_run('оплата ').bold=True;p.add_run('десять дней')
    out=io.BytesIO();doc.save(out);pid,fid=upload(c,'runs.docx',out.getvalue())
    assert edit_plan(c,pid,fid,{'find':'оплата десять','replace':'оплата пятнадцать'}).status_code==400


def test_source_changed_during_backup_rejected(service,tmp_path,monkeypatch):
    c,_,app=service;pid,fid=upload(c,'test.txt',b'old');plan=edit_plan(c,pid,fid,{'text':'new'}).json()
    ws=app.state.runtime['workspace'];backup=ws.backup
    def race():
        result=backup();(tmp_path/'space'/'projects'/pid/'materials'/(fid+'.blob')).write_bytes(b'outside change');return result
    monkeypatch.setattr(ws,'backup',race)
    r=confirm(c,plan);assert r.status_code==409 and project(c)['documents'][0]['id']==fid


def test_confirmation_receipt_and_artifact_template(service):
    c,_,_=service;pid=project(c)['id'];plan=result_plan(c,pid,title='Проект',text='Текст [УТОЧНИТЬ]').json()
    assert confirm(c,plan).status_code==200
    receipt=project(c)['operations'][-1];assert receipt['confirmationId']==plan['planId']
    r=project(c)['results'][0];raw=c.get(f'/api/projects/{pid}/results/{r["id"]}/file').content
    doc=Document(io.BytesIO(raw));assert doc.core_properties.author=='Юрий' and 'ЧЕРНОВИК' in doc.sections[0].header.paragraphs[0].text
    assert doc.core_properties.created.year>=2026
