import copy
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path
import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject, DictionaryObject, DecodedStreamObject
from test_m2 import service, request, finish
from yuriy.documents import Documents
from yuriy.extract_worker import extract


def upload(c,name,value):
    data=c.get('/api/state').json();p=data['state']['projects'][0]
    r=c.post(f'/api/projects/{p["id"]}/files',params={'name':name},content=value)
    assert r.status_code==200,r.text
    d=r.json();p['documents'].append(d)
    assert c.put('/api/state',json=data).status_code==200
    return p['id'],d['id']

def index(c,pid,fid):
    r=c.post(f'/api/projects/{pid}/documents/{fid}/index');assert r.status_code==200,r.text
    return r.json()

def docx(value,extra=None):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as z:
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'+value+'</w:body></w:document>')
        for k,v in (extra or {}).items():z.writestr(k,v)
    return out.getvalue()

def pdf(text=True,pages=1,encrypted=False):
    w=PdfWriter()
    for i in range(pages):
        page=w.add_blank_page(width=300,height=300)
        if text:
            font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
            page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):w._add_object(font)})})
            stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 10 250 Td (Contract payment deadline) Tj ET')
            page[NameObject('/Contents')]=w._add_object(stream)
    if encrypted:w.encrypt('secret')
    out=io.BytesIO();w.write(out);return out.getvalue()


def test_text_index_search_preview_and_hash(service,tmp_path):
    c,p,app=service;pid,fid=upload(c,'Договор.txt','Поставка товара.\n\nОплата в течение 10 дней.\n\nЁлка'.encode())
    assert c.get(f'/api/projects/{pid}/documents').json()['documents'][0]['status']=='added'
    result=index(c,pid,fid);assert result['status']=='ready' and result['chunkCount']==3
    assert 'chunks' not in result and result['sha256']
    listing=c.get(f'/api/projects/{pid}/documents').json();assert 'chunks' not in listing['documents'][0]
    preview=c.get(f'/api/projects/{pid}/documents/{fid}/preview').json()
    assert preview['chunks'][1]['number']==2 and preview['chunks'][1]['text']=='Оплата в течение 10 дней.'
    hits=c.get(f'/api/projects/{pid}/document-search',params={'q':'оплата'}).json()['hits']
    assert len(hits)==1 and hits[0]['fileId']==fid and hits[0]['number']==2
    assert c.get(f'/api/projects/{pid}/document-search',params={'q':'елка'}).json()['hits']
    assert not p.calls
    path=tmp_path/'space'/'projects'/pid/'materials'/(fid+'.blob');path.write_bytes('Подмена'.encode())
    assert c.get(f'/api/projects/{pid}/documents').json()['documents'][0]['status']=='stale'
    assert c.get(f'/api/projects/{pid}/documents/{fid}/preview').status_code==400
    assert c.get(f'/api/projects/{pid}/document-search',params={'q':'оплата'}).json()['hits']==[]


def test_docx_main_and_table_and_unsupported_parts(service):
    c,_,_=service;value=docx('<w:p><w:r><w:t>Договор</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Оплата 10 дней</w:t></w:r></w:p></w:tc></w:tr></w:tbl>',{'word/header1.xml':'header'})
    pid,fid=upload(c,'Договор.docx',value);r=index(c,pid,fid)
    assert r['status']=='ready' and r['partialExtraction'] and r['chunkCount']==2
    assert c.get(f'/api/projects/{pid}/documents/{fid}/preview').json()['chunks'][1]['text']=='Оплата 10 дней'


@pytest.mark.parametrize('name,raw', [('old.doc',b'old'),('macro.docm',b'not allowed'),('scan.png',b'png'),('text.txt',b'\xff\xfe\x00'),('broken.docx',b'not a zip'),('bad.pdf',b'not a pdf')])
def test_unsupported_or_malformed(service,name,raw):
    c,_,_=service;pid,fid=upload(c,name,raw);r=index(c,pid,fid)
    assert r['status'] in ('unsupported','error') and r['error']
    assert c.get(f'/api/projects/{pid}/documents/{fid}/preview').status_code==400


def test_docx_rejects_binary_and_entities(service):
    c,_,_=service
    utf16=io.BytesIO()
    with zipfile.ZipFile(utf16,'w') as z:z.writestr('word/document.xml','<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY y "bad">]><x>&y;</x>'.encode('utf-16'))
    for raw in [docx('<w:p/>',{'word/vbaProject.bin':'macro'}),docx('<!ENTITY x "bad"><w:p/>'),utf16.getvalue()]:
        pid,fid=upload(c,'test.docx',raw);assert index(c,pid,fid)['status']=='error'


def test_pdf_text_encrypted_and_scans(service):
    c,_,_=service
    for raw,expected in [(pdf(), 'ready'),(pdf(text=False),'error'),(pdf(encrypted=True),'error'),(pdf(pages=301),'error')]:
        pid,fid=upload(c,'test.pdf',raw);r=index(c,pid,fid);assert r['status']==expected,r
        if expected=='ready':
            chunks=c.get(f'/api/projects/{pid}/documents/{fid}/preview').json()['chunks']
            assert chunks[0]['kind']=='page' and chunks[0]['number']==1 and 'payment' in chunks[0]['text']


def test_chat_documents_explicit_consent_and_evidence(service):
    c,p,_=service;pid,fid=upload(c,'Контракт.txt',('Вводные.\n\nОплата через десять дней.\n\n'+'Прочие условия. '*1000).encode())
    assert index(c,pid,fid)['status']=='ready'
    body=request(c,'Как предусмотрена оплата?');body['documentIds']=[fid]
    assert c.post('/api/chat',json=body).status_code==400 and not p.calls
    body['documentsConsent']=True;assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId']);ctx=m['contextSummary']['documentContext'];source=ctx['sources'][0]
    assert source['partial'] and source['fileId']==fid and source['sha256']
    assert source['selection']=='lexical' and 'Оплата' in source['chunks'][0]['text']
    assert m['contextSummary']['documentsRead'] is True
    sent=json.dumps(p.calls[-1][1],ensure_ascii=False)
    assert 'Оплата через десять дней.' in sent and '[D1:' in sent
    assert 'недоверенные фрагменты' in sent
    other={**body,'documentIds':[]};assert c.post('/api/chat',json=other).status_code==409
    data=c.get('/api/state').json();data['state']['projects'][0]['threads'][0]['messages'][-1]['contextSummary']['documentContext']['sentChars']=0
    assert c.put('/api/state',json=data).status_code==400


def test_no_automatic_document_transfer(service):
    c,p,_=service;pid,fid=upload(c,'secret.txt','НЕ ПЕРЕДАВАТЬ БЕЗ ФЛАЖКА'.encode());index(c,pid,fid)
    body=request(c);assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId'])
    assert 'НЕ ПЕРЕДАВАТЬ' not in json.dumps(p.calls[-1][1],ensure_ascii=False)
    assert m['contextSummary']['documentsRead'] is False


def test_document_project_boundary_archive_and_pin(service):
    c,p,_=service;pid,fid=upload(c,'test.txt',b'local text');index(c,pid,fid)
    data=c.get('/api/state').json();first=data['state']['projects'][0]
    other=copy.deepcopy(first);other['id']=str(uuid.uuid4());other['documents']=[];other['threads'][0]['id']=str(uuid.uuid4());other['threadId']=other['threads'][0]['id'];data['state']['projects'].append(other)
    assert c.put('/api/state',json=data).status_code==200
    assert c.get(f'/api/projects/{other["id"]}/documents/{fid}/preview').status_code==400
    assert c.get(f'/api/projects/{other["id"]}/document-search',params={'q':'local'}).json()['hits']==[]
    body=request(c);body.update(documentIds=[fid],documentsConsent=True,projectId=other['id'],threadId=other['threadId'])
    assert c.post('/api/chat',json=body).status_code==400
    data=c.get('/api/state').json();data['state']['projects'][0]['documents'][0]['pinned']=False
    assert c.put('/api/state',json=data).status_code==200
    body=request(c);body.update(documentIds=[fid],documentsConsent=True);assert c.post('/api/chat',json=body).status_code==400
    data=c.get('/api/state').json();data['state']['projects'][0]['archived']=True;assert c.put('/api/state',json=data).status_code==200
    assert c.post(f'/api/projects/{pid}/documents/{fid}/index').status_code==400
    assert c.get(f'/api/projects/{pid}/documents/{fid}/preview').status_code==200
    assert not p.calls


def test_backup_drops_rebuildable_cache_keeps_evidence(service):
    c,_,_=service;pid,fid=upload(c,'test.txt',b'payment date');index(c,pid,fid)
    body=request(c);body.update(documentIds=[fid],documentsConsent=True)
    assert c.post('/api/chat',json=body).status_code==200;finish(c,body['requestId'])
    backup=c.post('/api/backup');assert backup.status_code==200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:assert not any('index' in n for n in z.namelist())
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert c.get(f'/api/projects/{pid}/documents').json()['documents'][0]['status']=='added'
    assert finish(c,body['requestId'])['contextSummary']['documentContext']['sources'][0]['chunks'][0]['text']=='payment date'
    assert index(c,pid,fid)['status']=='ready'


def test_limit_no_truncation(service):
    c,_,_=service;pid,fid=upload(c,'big.txt',b'x'*1_000_001);r=index(c,pid,fid)
    assert r['status']=='error' and '1 млн' in r['error']


def test_eight_files_context_budget(service):
    c,p,app=service;ids=[]
    for i in range(8):
        pid,fid=upload(c,f'{i}.txt',('оплата '+'текст '*600).encode());assert index(c,pid,fid)['status']=='ready';ids.append(fid)
    body=request(c,'оплата');body.update(documentIds=ids,documentsConsent=True)
    assert c.post('/api/chat',json=body).status_code==200
    m=finish(c,body['requestId']);ctx=m['contextSummary']['documentContext']
    assert len(ctx['sources'])==8 and ctx['sentChars']<=12000
    assert len(json.dumps(p.calls[-1][1],ensure_ascii=False))<43000


def test_extractor_timeout_and_retry(service,monkeypatch):
    import subprocess
    c,_,_=service;pid,fid=upload(c,'test.txt',b'text')
    original=subprocess.run
    def timeout(*args,**kwargs):raise subprocess.TimeoutExpired(args[0],35)
    monkeypatch.setattr(subprocess,'run',timeout)
    r=index(c,pid,fid);assert r['status']=='error' and '35 секунд' in r['error']
    monkeypatch.setattr(subprocess,'run',original)
    assert index(c,pid,fid)['status']=='ready'


def test_preview_paging_and_fts_operators(service):
    c,_,_=service;pid,fid=upload(c,'test.txt',('\n\n'.join('Оплата '+str(i) for i in range(25))).encode());index(c,pid,fid)
    first=c.get(f'/api/projects/{pid}/documents/{fid}/preview').json();second=c.get(f'/api/projects/{pid}/documents/{fid}/preview?offset=20').json()
    assert len(first['chunks'])==20 and first['hasMore'] and len(second['chunks'])==5 and not second['hasMore']
    assert c.get(f'/api/projects/{pid}/document-search',params={'q':'оплата" OR NOT -*/ "'}).status_code==200


@pytest.mark.skipif(__import__('sys').platform!='win32',reason='Requires actual Windows Job Object')
def test_windows_parser_memory_cap():
    import subprocess,sys
    script='from yuriy.parser_limits import apply_limits\napply_limits()\ntry:\n x=bytearray(800*1024*1024)\nexcept MemoryError:\n print("bounded")\n'
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and 'bounded' in result.stdout,result.stderr
