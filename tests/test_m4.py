import asyncio
import copy
import io
import json
import os
import time
import uuid
from datetime import datetime,timedelta,timezone
import pytest
from docx import Document
from test_m2 import service,request,finish
from test_m3 import upload,index
from test_m3b import edit_plan,confirm,result_plan,word_file
from yuriy.secretary import Secretary,propose,local_instant,normalize_event
from yuriy.chat import Job

UTC=timezone.utc

def state(c):return c.get('/api/state').json()
def project(c):return state(c)['state']['projects'][0]
def configure(c,**extra):
    r=c.post('/api/secretary/settings',json={'timezone':'Europe/Samara','notificationsEnabled':False,'confirmed':True,**extra});assert r.status_code==200,r.text
    return r.json()
def event_args(**extra):return dict(title='Заседание',date='2035-03-02',time='10:00',timezone='Europe/Samara',allDay=False,type='hearing',status='planned',reminders=[0],**extra)
def plan(c,pid=None,evt=None,**extra):
    return c.post(f'/api/projects/{pid or project(c)["id"]}/event-plan',json={'revision':state(c)['revision'],'event':evt or event_args(),'source':{'kind':'manual'},**extra})
def accept(c,p):return c.post('/api/secretary/plans/'+p['planId']+'/confirm',json={'confirmed':True})
def create_event(c,evt=None,**extra):
    configure(c);r=plan(c,evt=evt,**extra);assert r.status_code==200,r.text
    p=r.json();r=accept(c,p);assert r.status_code==200,r.text
    return next(e for e in project(c)['events'] if e['confirmationId']==p['planId'])


def test_proposal_absolute_dates_relative_zone_and_no_guessed_time():
    t=datetime(2030,1,1,21,30,tzinfo=UTC)
    p=propose('Завтра заседание в 09:15','Europe/Samara',t)
    assert p['date']=='2030-01-03' and p['time']=='09:15' and not p['allDay'] and p['referenceDate']=='2030-01-02'
    p=propose('Послезавтра отправить письмо','Europe/Samara',t)
    assert p['date']=='2030-01-04' and p['allDay'] and p['time']==''
    p=propose('Встреча 02.04.2030 в 11:20','UTC',t);assert p['date']=='2030-04-02'
    p=propose('Завтра или 2030-01-07 в 10:00 и 12:00','UTC',t);assert p['date']==p['time']=='' and p['warnings']
    p=propose('Подать апелляцию через 15 дней','UTC',t);assert not p['date'] and p['proceduralDraft'] and p['warnings']
    p=propose('Заседание 12.05','UTC',t);assert not p['date']
    with pytest.raises(ValueError):propose('2030-02-31 в 10:00','UTC',t)


def test_iana_utc_and_dst_gap_ambiguity():
    assert local_instant('2030-01-02','10:30','Europe/Samara')=='2030-01-02T06:30:00+00:00'
    with pytest.raises(ValueError,match='не существует'):local_instant('2026-03-29','02:30','Europe/Berlin')
    with pytest.raises(ValueError,match='Неоднозначное'):local_instant('2026-10-25','02:30','Europe/Berlin')
    a=local_instant('2026-10-25','02:30','Europe/Berlin',0);b=local_instant('2026-10-25','02:30','Europe/Berlin',1)
    assert datetime.fromisoformat(b)-datetime.fromisoformat(a)==timedelta(hours=1)
    with pytest.raises(ValueError):local_instant('2030-01-01','25:00','Europe/Samara')
    with pytest.raises(ValueError):local_instant('2030-01-01','10:00','Bad/Zone')


def test_event_card_confirmation_protection_and_edit(service):
    c,_,_=service;assert plan(c).status_code==400;configure(c)
    p=plan(c).json();assert p['event']['startsAt']=='2035-03-02T06:00:00+00:00' and project(c)['events']==[]
    assert c.post('/api/secretary/plans/'+p['planId']+'/confirm',json={'confirmed':False}).status_code==400
    assert accept(c,p).status_code==200 and accept(c,p).status_code==409
    e=project(c)['events'][0];assert e['confirmed'] and e['sequence']==1 and e['source']['kind']=='manual'
    s=state(c);s['state']['projects'][0]['events'][0]['time']='11:00';assert c.put('/api/state',json=s).status_code==400
    e2={**e,'time':'11:00','reminders':[60]};p2=plan(c,evt=e2,eventId=e['id']).json();assert accept(c,p2).status_code==200
    e=project(c)['events'][0];assert e['time']=='11:00' and e['sequence']==2
    s=state(c);assert c.put('/api/state',json=s).status_code==200


def test_event_ttl_revision_and_archive_guards(service):
    c,_,app=service;configure(c);p=plan(c).json();s=state(c);assert c.put('/api/state',json=s).status_code==200
    assert accept(c,p).status_code==409
    p=plan(c).json();app.state.runtime['secretary'].plans[p['planId']]['expires']=time.monotonic()-1
    assert accept(c,p).status_code==409
    p=plan(c).json();assert c.post('/api/secretary/plans/'+p['planId']+'/discard').status_code==200;assert accept(c,p).status_code==409
    s=state(c);s['state']['projects'][0]['archived']=True;assert c.put('/api/state',json=s).status_code==200
    assert plan(c).status_code==400


def test_all_day_no_utc_midnight_and_explicit_reminder_time(service):
    c,_,_=service;configure(c)
    evt={**event_args(),'time':'','allDay':True,'reminders':[]}
    p=plan(c,evt=evt).json();assert p['event']['startsAt'] is None
    assert accept(c,p).status_code==200
    assert plan(c,evt={**evt,'reminders':[0]}).status_code==400
    p=plan(c,evt={**evt,'reminders':[0,1440],'reminderTime':'09:00'}).json();assert p['event']['reminderTime']=='09:00'
    for bad in [{'reminders':[0,0]},{'reminders':[-1]},{'reminders':[True]},{'allDay':False},{'endDate':'2035-03-01'}]:
        assert plan(c,evt={**evt,**bad}).status_code==400
    assert plan(c,evt={**event_args(),'endDate':'2035-03-02','endTime':'09:00'}).status_code==400


def test_event_sources_message_and_document_are_snapshots(service):
    c,provider,_=service;configure(c);pid,fid=upload(c,'Суд.txt','Завтра заседание в 14:30.'.encode());index(c,pid,fid)
    source={'kind':'document','fileId':fid,'offset':0,'text':'Завтра заседание в 14:30.'}
    r=c.post(f'/api/projects/{pid}/event-suggestion',json={'source':source});assert r.status_code==200,r.text
    d=r.json();assert not d['externalApiCalled'] and d['source']['revisionId'] and not provider.calls
    p=plan(c,evt={k:v for k,v in d.items() if k not in ('source','warnings')},source=source).json();assert accept(c,p).status_code==200
    assert project(c)['events'][0]['source']['text']=='Завтра заседание в 14:30.'
    b=request(c,'Завтра заседание в 14:00');c.post('/api/chat',json=b);finish(c,b['requestId']);m=project(c)['threads'][0]['messages'][0]
    source={'kind':'message','messageId':m['id'],'text':m['text']}
    assert c.post(f'/api/projects/{pid}/event-suggestion',json={'source':source}).status_code==200
    assert c.post(f'/api/projects/{pid}/event-suggestion',json={'source':{**source,'text':'выдумка'}}).status_code==400


def test_reminder_live_once_sleep_restart_and_acknowledgement(service):
    c,_,app=service;e=create_event(c);ws=app.state.runtime['workspace'];calls=[]
    sec=Secretary(ws,notifier=lambda count:calls.append(count) or 'requested-not-confirmed');sec.configure({'timezone':'Europe/Samara','notificationsEnabled':True,'confirmed':True})
    due=datetime(2035,3,2,6,0,tzinfo=UTC);sec.tick(due-timedelta(seconds=4));sec.tick(due+timedelta(seconds=1));sec.tick(due+timedelta(seconds=3))
    assert calls==[1];center=sec.center();assert center['outstanding']==1
    row=center['items'][0];assert not row['missed'] and row['notification']=='requested-not-confirmed'
    restarted=Secretary(ws,notifier=lambda count:calls.append(count));restarted.tick(due+timedelta(hours=2));assert calls==[1]
    restarted.acknowledge(row['id']);assert restarted.center()['outstanding']==0
    assert c.get('/api/secretary').json()['outstanding']==0


def test_missed_multiple_reminders_no_system_storm(service):
    c,_,app=service;evt={**event_args(),'reminders':[0,60,1440]};create_event(c,evt);calls=[]
    sec=Secretary(app.state.runtime['workspace'],notifier=lambda count:calls.append(count));sec.configure({'timezone':'Europe/Samara','notificationsEnabled':True,'confirmed':True})
    sec.tick(datetime(2035,3,1,5,0,tzinfo=UTC));sec.tick(datetime(2035,3,2,7,0,tzinfo=UTC))
    assert not calls and sec.center()['outstanding']==3 and all(r['missed'] for r in sec.center()['items'])
    sec.tick(datetime(2035,3,2,7,0,5,tzinfo=UTC));assert not calls


def test_changed_schedule_supersedes_and_cancel_stops(service):
    c,_,app=service;e=create_event(c);sec=app.state.runtime['secretary'];sec.tick(datetime(2035,3,2,7,0,tzinfo=UTC));assert sec.center()['outstanding']==1
    p=plan(c,evt={**e,'time':'12:00'},eventId=e['id']).json();assert accept(c,p).status_code==200
    assert sec.center()['outstanding']==0 and sec.center()['items'][0]['superseded']
    e=project(c)['events'][0];p=plan(c,evt={**e,'status':'cancelled'},eventId=e['id']).json();assert accept(c,p).status_code==200
    sec.tick(datetime(2035,3,2,10,0,tzinfo=UTC));assert sec.center()['outstanding']==0


def test_notify_failure_not_retried_and_center_survives(service):
    c,_,app=service;create_event(c);calls=[]
    def fail(n):calls.append(n);raise OSError('blocked')
    sec=Secretary(app.state.runtime['workspace'],notifier=fail);sec.configure({'timezone':'Europe/Samara','notificationsEnabled':True,'confirmed':True});due=datetime(2035,3,2,6,0,tzinfo=UTC)
    sec.tick(due-timedelta(seconds=2));sec.tick(due);sec.tick(due+timedelta(seconds=2))
    assert calls==[1] and sec.center()['items'][0]['notification']=='failed' and sec.center()['outstanding']==1


def test_archive_choice_keeps_or_cancels_future_events(service):
    c,_,_=service;e=create_event(c);pid=project(c)['id']
    s=state(c);s['state']['projects'][0]['archived']=True;assert c.put('/api/state',json=s).status_code==400
    def archive(archived,choice):
        r=c.post(f'/api/projects/{pid}/archive-plan',json={'revision':state(c)['revision'],'archived':archived,'eventsChoice':choice});assert r.status_code==200,r.text
        return accept(c,r.json())
    assert archive(True,'keep').status_code==200 and project(c)['events'][0]['status']=='planned'
    assert archive(False,'keep').status_code==200
    assert archive(True,'cancel').status_code==200 and project(c)['events'][0]['status']=='cancelled'
    assert archive(False,'keep').status_code==200 and project(c)['events'][0]['status']=='cancelled'


def test_backup_restore_reminders_settings_and_plan_invalidation(service):
    c,_,app=service;e=create_event(c);sec=app.state.runtime['secretary'];sec.tick(datetime(2035,3,2,7,0,tzinfo=UTC));row=sec.center()['items'][0];sec.acknowledge(row['id'])
    z=c.post('/api/backup').content;p=plan(c,eventId=e['id'],evt={**e,'title':'Позже'}).json()
    assert c.post('/api/restore',content=z,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert accept(c,p).status_code==409
    r=c.get('/api/secretary').json();assert r['settings']['timezoneConfirmed'] and r['items'][0]['acknowledged']
    assert project(c)['events'][0]['id']==e['id']


def test_restore_revision_exact_bytes_and_preview(service):
    c,_,_=service;pid,fid=upload(c,'Срок.txt',b'\xef\xbb\xbfold\r\n');p=edit_plan(c,pid,fid,{'text':'new\n'}).json();assert confirm(c,p).status_code==200
    op=project(c)['operations'][-1];r=c.get(f'/api/projects/{pid}/operations/{op["id"]}/revision-preview');assert r.status_code==200,r.text
    assert r.json()['chunks'][0]['text']=='old'
    r=c.post(f'/api/projects/{pid}/operations/{op["id"]}/restore-plan',json={'which':'before'});assert r.status_code==200,r.text
    restored=r.json();assert c.get(f'/api/operations/{restored["planId"]}/preview-file').content==b'\xef\xbb\xbfold\r\n'
    assert confirm(c,restored).status_code==200
    p=project(c);assert len(p['operations'])==2 and p['documents'][0]['id']!=fid and p['operations'][-1]['restoredFromFileId']==fid
    assert c.get(f'/api/projects/{pid}/files/{p["documents"][0]["id"]}').content==b'\xef\xbb\xbfold\r\n'


def test_docx_revision_restore_byte_exact_and_unsupported_open_rejected(service):
    c,_,_=service;raw=word_file();pid,fid=upload(c,'Договор.docx',raw)
    p=edit_plan(c,pid,fid,{'find':'десять','replace':'двадцать'}).json();assert confirm(c,p).status_code==200
    op=project(c)['operations'][-1];r=c.post(f'/api/projects/{pid}/operations/{op["id"]}/restore-plan',json={'which':'before'});assert r.status_code==200
    p=r.json();assert c.get('/api/operations/'+p['planId']+'/preview-file').content==raw;assert confirm(c,p).status_code==200
    r=c.get(f'/api/projects/{pid}/operations/{op["id"]}/revision-preview?which=after');assert r.status_code==200 and 'двадцать' in str(r.json())


def test_docx_editor_confirm_sha_and_nonwindows_fallback(service):
    c,_,_=service;pid=project(c)['id'];p=result_plan(c,pid,title='Черновик',text='Текст').json();assert confirm(c,p).status_code==200
    r=project(c)['results'][0];url=f'/api/projects/{pid}/results/{r["id"]}/open-editor'
    assert c.post(url,json={'confirmed':False,'sha256':r['artifactSha256']}).status_code==400
    assert c.post(url,json={'confirmed':True,'sha256':'0'*64}).status_code==409
    # Never launch a real editor in an automated test.
    import yuriy.app as module
    from unittest.mock import patch
    with patch.object(module,'open_word_copy',return_value={'requested':False,'reason':'no-editor','downloadAvailable':True}) as launch:
        rr=c.post(url,json={'confirmed':True,'sha256':r['artifactSha256']});assert rr.status_code==200 and rr.json()['downloadAvailable'];assert launch.call_count==1
    pid,fid=upload(c,'НеDOCX.txt',b'plain')
    assert c.post(f'/api/projects/{pid}/documents/{fid}/open-editor',json={'confirmed':True,'sha256':'0'*64}).status_code==400


def test_sse_terminal_frame_not_lost_when_job_finishes_between_yields(service):
    c,_,app=service
    async def run():
        rid=str(uuid.uuid4());job=Job(rid,'pid','tid',{'text':'prefix'});app.state.chat.jobs[rid]=job
        events=app.state.chat.events(rid);first=await anext(events);assert not first['done']
        job.done=True;job.signal()
        terminal=await anext(events);assert terminal['done']
        with pytest.raises(StopAsyncIteration):await anext(events)
    c.portal.call(run)


def test_timezone_change_does_not_move_existing_events_and_legacy_not_scheduled(service):
    c,_,app=service;e=create_event(c);old=e['startsAt'];configure(c,timezone='Europe/Moscow')
    assert project(c)['events'][0]['startsAt']==old and project(c)['events'][0]['timezone']=='Europe/Samara'
    s=state(c);s['state']['projects'][0]['events'].append({'id':str(uuid.uuid4()),'title':'Старое','date':'2035-03-01','time':'12:00','type':'meeting','note':'','done':False})
    assert c.put('/api/state',json=s).status_code==200
    sec=app.state.runtime['secretary'];sec.tick(datetime(2035,3,2,7,0,tzinfo=UTC));assert len(sec.center()['items'])==1


def test_explicit_editor_copy_does_not_mutate_artifact_and_no_editor_fallback(service,monkeypatch):
    from types import SimpleNamespace
    import yuriy.desktop as desktop
    c,_,app=service;pid=project(c)['id'];raw=word_file();seen=[]
    monkeypatch.setattr(desktop,'os',SimpleNamespace(name='nt',startfile=lambda p,verb:seen.append((p,verb))))
    import hashlib
    ws=app.state.runtime['workspace'];r=desktop.open_word_copy(ws,pid,raw,hashlib.sha256(raw).hexdigest());assert r['requested']
    from pathlib import Path
    path=Path(seen[0][0]);assert path.read_bytes()==raw and path.suffix=='.docx' and 'temporary' in path.parts and seen[0][1]=='open'
    def fail(*args):raise OSError('no association')
    monkeypatch.setattr(desktop,'os',SimpleNamespace(name='nt',startfile=fail));r=desktop.open_word_copy(ws,pid,raw,hashlib.sha256(raw).hexdigest());assert not r['requested'] and r['downloadAvailable']
    assert len(list(path.parent.glob('*.docx')))==1


def test_windows_notification_command_is_fixed_and_returns_uncertain_result(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import yuriy.notifications as n
    import base64
    exe=tmp_path/'System32'/'WindowsPowerShell'/'v1.0'/'powershell.exe';exe.parent.mkdir(parents=True);exe.write_bytes(b'test')
    monkeypatch.setattr(n,'os',SimpleNamespace(name='nt',environ={'SystemRoot':str(tmp_path)}));calls=[]
    def run(args,**kwargs):calls.append((args,kwargs));return SimpleNamespace(returncode=0)
    monkeypatch.setattr(n.subprocess,'run',run)
    assert n.notify_windows(2)=='requested-not-confirmed'
    args,kw=calls[0];assert args[0]==str(exe) and '-NoProfile' in args and '-NonInteractive' in args and kw['timeout']==20
    assert base64.b64decode(args[-1]).decode('utf-16le')==n.SCRIPT and not kw.get('shell',False)


def test_background_scheduler_lifecycle_runs_without_browser(tmp_path,monkeypatch):
    from fastapi.testclient import TestClient
    from yuriy.app import create_app
    import threading
    observed=threading.Event();calls=[]
    def tick(self,*args,**kwargs):calls.append(self.ws.root);observed.set()
    monkeypatch.setattr(Secretary,'tick',tick)
    app=create_app(config_path=tmp_path/'cfg.json',workspace=tmp_path/'space')
    with TestClient(app):assert observed.wait(3)
    assert len(calls)>=1


def test_all_day_receipts_use_explicit_local_reminder_clock(service):
    c,_,app=service;evt={**event_args(),'time':'','allDay':True,'reminderTime':'09:00'};create_event(c,evt)
    sec=Secretary(app.state.runtime['workspace']);sec.tick(datetime(2035,3,2,4,59,tzinfo=UTC));assert not sec.center()['items']
    sec.tick(datetime(2035,3,2,5,0,tzinfo=UTC));assert sec.center()['items'][0]['dueAt']=='2035-03-02T05:00:00+00:00'


def test_confirmation_does_not_consume_new_due_reminder_before_scheduler(service):
    c,_,app=service;e=create_event(c);sec=app.state.runtime['secretary'];due=datetime(2035,3,2,6,0,tzinfo=UTC)
    sec.tick(due-timedelta(seconds=5));sec.tick(due,notify=False,collect_due=False)
    assert not sec.center()['items'];sec.tick(due+timedelta(seconds=1));assert len(sec.center()['items'])==1


def test_handoff_rejects_external_docx_links(service):
    c,_,_=service;out=io.BytesIO();doc=Document();doc.add_paragraph('Текст');doc.part.relate_to('https://example.invalid/template','http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate',is_external=True);doc.save(out)
    pid,fid=upload(c,'Внешний.docx',out.getvalue());sha=project(c)['documents'][0]['sha256']
    r=c.post(f'/api/projects/{pid}/documents/{fid}/open-editor',json={'confirmed':True,'sha256':sha});assert r.status_code==400 and 'внешними' in r.json()['detail']


def test_title_edit_does_not_repeat_an_existing_reminder(service):
    c,_,app=service;e=create_event(c);sec=app.state.runtime['secretary'];due=datetime(2035,3,2,6,0,tzinfo=UTC)
    sec.tick(due-timedelta(seconds=2));sec.tick(due);assert sec.center()['outstanding']==1
    p=plan(c,evt={**e,'title':'Уточнённое название'},eventId=e['id']).json();assert accept(c,p).status_code==200
    updated=project(c)['events'][0];assert updated['sequence']==2 and updated['scheduleVersion']==e['scheduleVersion']
    sec.tick(due+timedelta(seconds=3));assert sec.center()['total']==1 and sec.center()['items'][0]['title']=='Уточнённое название'
