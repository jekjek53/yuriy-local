import copy
import hashlib
import io
import json
import sqlite3
import uuid
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from yuriy.app import create_app
from yuriy.storage import Workspace, Conflict, validate_state

@pytest.fixture
def client(tmp_path):
    app=create_app(config_path=tmp_path/'config.json')
    with TestClient(app) as c:
        c.headers['X-Yuriy-Token']=app.state.test_token
        yield c

def configure(c,tmp_path):
    r=c.post('/api/workspace',json={'path':str(tmp_path/'Рабочая папка'),'confirmed':True})
    assert r.status_code==200,r.text
    return c.get('/api/state').json()

def save(c,payload):
    return c.put('/api/state',json=payload)

def test_setup_and_persistence(client,tmp_path):
    assert client.get('/api/workspace').json()['configured'] is False
    assert client.post('/api/workspace',json={'path':str(tmp_path/'x')}).status_code==400
    data=configure(client,tmp_path)
    p=data['state']['projects'][0];p['name']='Договор № 1';t=p['threads'][0]
    t['draft']='Незавершённая мысль';t['messages'].append({'id':str(uuid.uuid4()),'role':'user','text':'Вопрос','at':'2026-11-19T10:00:00+00:00'})
    assert save(client,data).status_code==200
    reloaded=client.get('/api/state').json()
    assert reloaded['state']['projects'][0]['threads'][0]['draft']=='Незавершённая мысль'
    assert save(client,data).status_code==409
    assert client.post('/api/workspace',json={'path':str(tmp_path/'second'),'confirmed':True}).status_code==409

def test_boundary(client,tmp_path):
    assert client.get('/api/state',headers={'X-Yuriy-Token':''}).status_code==403
    assert client.get('/api/workspace',headers={'Origin':'https://evil.test'}).status_code==403
    assert client.get('/api/workspace',headers={'Host':'evil.test'}).status_code==403
    assert client.options('/api/state',headers={'Origin':'https://evil.test'}).status_code==403
    assert client.get('/api/health').json()['app']=='yuriy-local'
    assert 'nonce-' in client.get('/').headers['Content-Security-Policy']

def test_upload_download_restore(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0]
    content=b'contract\x00\xff';pid=p['id']
    r=client.post(f'/api/projects/{pid}/files',params={'name':'Договор.pdf'},content=content)
    assert r.status_code==200,r.text
    d=r.json();assert d['sha256']==hashlib.sha256(content).hexdigest();p['documents'].append(d)
    assert save(client,data).status_code==200
    url=f'/api/projects/{pid}/files/{d["id"]}'
    assert client.get(url).content==content
    assert client.post(f'/api/projects/{pid}/files',params={'name':'../bad'},content=b'x').status_code==400
    backup=client.post('/api/backup');assert backup.status_code==200,backup.text
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:
        assert 'manifest.json' in z.namelist()
    current=client.get('/api/state').json();current['state']['projects'][0]['name']='Новое имя'
    assert save(client,current).status_code==200
    r=client.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'})
    assert r.status_code==200,r.text
    assert client.get('/api/state').json()['state']['projects'][0]['name']!='Новое имя'
    assert client.get(url).content==content
    assert list((tmp_path/'Рабочая папка'/'.yuriy'/'backups').glob('*.zip'))
    assert client.post('/api/restore',content=b'bad',headers={'X-Confirm-Restore':'yes'}).status_code==400

def test_calendar_archive_and_results(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0]
    e={'id':str(uuid.uuid4()),'title':'Заседание','date':'2026-11-25','time':'10:00','type':'hearing','note':'Зал 3','done':False}
    p['events'].append(e);p['results'].append({'id':str(uuid.uuid4()),'name':'Заметка','text':'Мой текст','created':'today'})
    assert save(client,data).status_code==200
    assert list((tmp_path/'Рабочая папка'/'projects'/p['id']/'results').glob('*.txt'))[0].read_text('utf-8')=='Мой текст'
    data=client.get('/api/state').json();p=data['state']['projects'][0];p['archived']=True
    assert save(client,data).status_code==200
    data=client.get('/api/state').json();data['state']['projects'][0]['events'][0]['title']='Изменено'
    assert save(client,data).status_code==400
    data=client.get('/api/state').json();p=data['state']['projects'][0];p['archived']=False
    assert save(client,data).status_code==200
    data=client.get('/api/state').json();data['state']['projects'][0]['events'][0]['date']='2026-02-31'
    assert save(client,data).status_code==400

def test_symlink_and_lock(tmp_path):
    w=Workspace(tmp_path/'one')
    with pytest.raises(ValueError):Workspace(tmp_path/'one')
    outside=tmp_path/'outside';outside.mkdir()
    link=w.root/'projects'/'link'
    try:link.symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip('symlinks unavailable')
    with pytest.raises(ValueError):w.safe('projects/link/stolen.txt')
    with pytest.raises(ValueError):w.safe('../outside')
    with pytest.raises(ValueError):w.safe('projects/x:stream')
    w.close()

def test_restart(tmp_path):
    root=tmp_path/'restart';w=Workspace(root);payload=w.read();payload['state']['projects'][0]['name']='Сохранилось';w.save(payload['state'],payload['revision']);w.close()
    other=Workspace(root);assert other.read()['state']['projects'][0]['name']=='Сохранилось';other.close()

def test_malicious_backup_no_change(client,tmp_path):
    data=configure(client,tmp_path)
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        z.writestr('manifest.json',json.dumps({'app':'yuriy-local-m1','schema':1,'files':{'../outside.txt':{'size':1,'sha256':hashlib.sha256(b'x').hexdigest()}}}))
        z.writestr('../outside.txt',b'x')
    assert client.post('/api/restore',content=b.getvalue(),headers={'X-Confirm-Restore':'yes'}).status_code==400
    assert client.get('/api/state').json()==data
    assert not (tmp_path/'outside.txt').exists()

def test_real_files_not_claimed_read(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0]
    p['documents'].append({'id':str(uuid.uuid4()),'name':'fake.pdf','pinned':True})
    assert save(client,data).status_code==400
    p['documents']=[];p['threads'][0]['messages'].append({'id':str(uuid.uuid4()),'role':'assistant','text':'Я прочитал','at':'now'})
    assert save(client,data).status_code==400

def test_preflight_failure_does_not_restore(client,tmp_path,monkeypatch):
    configure(client,tmp_path)
    backup=client.post('/api/backup').content
    data=client.get('/api/state').json();data['state']['projects'][0]['name']='Не потерять'
    assert save(client,data).status_code==200
    before=client.get('/api/state').json()
    w=client.app.state.runtime['workspace']
    def fail():raise ValueError('Не удалось создать страховочную копию')
    monkeypatch.setattr(w,'backup',fail)
    r=client.post('/api/restore',content=backup,headers={'X-Confirm-Restore':'yes'})
    assert r.status_code==400
    assert client.get('/api/state').json()==before

def test_multiple_projects_and_500_items(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0]
    for i in range(499):
        other=copy.deepcopy(p);other['id']=str(uuid.uuid4());other['name']=f'Проект {i}'
        t=str(uuid.uuid4());other['threads'][0]['id']=t;other['threadId']=t
        data['state']['projects'].append(other)
    assert save(client,data).status_code==200
    reloaded=client.get('/api/state').json()
    assert len(reloaded['state']['projects'])==500
    assert len({p['id'] for p in reloaded['state']['projects']})==500

def test_same_account_tamper_detected(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0]
    f=client.post(f'/api/projects/{p["id"]}/files',params={'name':'text.txt'},content=b'original').json()
    p['documents'].append(f);assert save(client,data).status_code==200
    path=tmp_path/'Рабочая папка'/'projects'/p['id']/'materials'/(f['id']+'.blob')
    path.write_bytes(b'changed')
    assert client.get(f'/api/projects/{p["id"]}/files/{f["id"]}').status_code==400
    assert client.post('/api/backup').status_code==400

def test_project_date_separate_from_creation(client,tmp_path):
    data=configure(client,tmp_path);p=data['state']['projects'][0];created=p['createdAt']
    p['projectDate']='2030-01-15';assert save(client,data).status_code==200
    data=client.get('/api/state').json();p=data['state']['projects'][0]
    assert p['createdAt']==created and p['projectDate']=='2030-01-15'
    p['createdAt']='2000-01-01';assert save(client,data).status_code==400
