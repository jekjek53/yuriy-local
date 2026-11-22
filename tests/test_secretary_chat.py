"""M4-SEC-01: ordinary chat -> local proposal -> explicit event confirmation."""
import copy,uuid
from datetime import datetime,timezone
import pytest
from test_m2 import service,request,finish
from test_m3b import project,state
from test_m4 import configure
from yuriy.event_intents import propose,is_event_instruction

NOW=datetime(2026,11,19,7,tzinfo=timezone.utc)  # Thursday, Europe/Samara.


@pytest.mark.parametrize('command,title,day,tm',[
 ('Напомни завтра в 15-00 отправить письмо','Отправить письмо','2026-11-20','15:00'),
 ('Юрий, напомни мне в пятницу в 15-00 подать заявление в суд','Подать заявление в суд','','15:00'),
 ('Запиши на этой неделе в пятницу в 15-00 судебное заседание по делу № 1','Судебное заседание по делу № 1','2026-11-20','15:00'),
 ('Напомни на следующей неделе во вторник в 10-30 о встрече','О встрече','2026-11-24','10:30'),
 ('В пятницу в 15-00 заседание, напомни','Заседание','','15:00'),
 ('Напомни 20.11.2026 в пятницу в 15-00 заседание','Заседание','2026-11-20','15:00'),
 ('Добавь в календарь 2026-11-20 в 09:00 встречу','Встречу','2026-11-20','09:00'),
 ('Пожалуйста, напомни послезавтра в 8:05 подать иск','Подать иск','2026-11-21','08:05'),
])
def test_russian_commands(command,title,day,tm):
    assert is_event_instruction(command)
    p=propose(command,'Europe/Samara',NOW);assert (p['title'],p['date'],p['time'])==(title,day,tm)
    assert p['referenceDate']=='2026-11-19' and p['referenceInstant']==NOW.isoformat()


@pytest.mark.parametrize('command',[
 'Что такое иск?', 'Как поставить напоминание?', 'Напомни, что такое исковая давность',
 'Проанализируй фразу: напомни завтра в 15-00', '«Напомни завтра в 15-00»',
 'В договоре написано: напомни завтра в 15-00', 'Не напоминай завтра о встрече',
])
def test_non_event_questions_not_intercepted(command):assert not is_event_instruction(command)


@pytest.mark.parametrize('command',[
 'Напомни в пятницу в 15-00 о встрече',
 'Напомни в следующую пятницу в 15-00 о встрече',
 'Напомни в понедельник 20.11.2026 в 15-00 о встрече',
 'Напомни 20.11.2026 или 21.11.2026 в 15-00 о встрече',
 'Напомни 20.11 в 15-00 о встрече',
 'Напомни каждую пятницу в 15-00 о встрече',
])
def test_ambiguous_date_is_never_guessed(command):
    p=propose(command,'Europe/Samara',NOW);assert p['date']=='' and p['warnings']


def test_weekday_candidates_and_missing_time():
    p=propose('Напомни в пятницу о встрече','Europe/Samara',NOW)
    assert p['dateCandidates']==['2026-11-20','2026-11-27'] and p['needsTimeChoice'] and p['time']==''
    p=propose('Напомни в ближайший четверг о встрече','Europe/Samara',NOW)
    assert p['date']=='' and p['dateCandidates']==['2026-11-19','2026-11-26']
    p=propose('Напомни в ближайшую пятницу о встрече','Europe/Samara',NOW);assert p['date']=='2026-11-20'
    p=propose('Напомни завтра весь день о встрече','Europe/Samara',NOW);assert p['allDay'] and not p['needsTimeChoice']
    p=propose('Напомни завтра с 15-00 до 16-00 о встрече','Europe/Samara',NOW);assert p['time']=='' and p['needsTimeChoice']
    with pytest.raises(ValueError):propose('Напомни завтра в 29-00 о встрече','Europe/Samara',NOW)


def test_explicit_timezone_does_not_silently_use_default():
    p=propose('Напомни завтра в 15-00 МСК о встрече','Europe/Samara',NOW)
    assert p['timezone']=='Europe/Moscow' and 'МСК' not in p['title']
    p=propose('Напомни завтра в 15-00 Europe/Moscow о встрече','Europe/Samara',NOW);assert p['timezone']=='Europe/Moscow'
    p=propose('Напомни завтра в 15-00 Europe/Moscow и Europe/Samara о встрече','Europe/Samara',NOW);assert not p['time'] and p['needsTimeChoice']


def test_chat_routes_local_and_confirms_exact_event(service):
    c,provider,app=service;configure(c);cmd='Юрий, напомни мне в пятницу в 15-00 подать заявление в суд';req=request(c,cmd)
    assert c.post('/api/chat',json=req).status_code==200;m=finish(c,req['requestId'])
    assert m['kind']=='secretaryProposal' and m['status']=='complete' and not provider.calls
    assert 'не записано' in m['text'] and 'не могу' not in m['text']
    p=m['contextSummary']['eventProposal'];assert p['title']=='Подать заявление в суд' and not p['date'] and p['time']=='15:00'
    assert not project(c)['events'];count=len(project(c)['threads'][0]['messages'])
    assert c.post('/api/chat',json=req).json()['reused'] and len(project(c)['threads'][0]['messages'])==count
    source=next(x for x in project(c)['threads'][0]['messages'] if x['role']=='user');assert p['sourceArgs']['messageId']==source['id']
    fields={**p,'date':p['dateCandidates'][0],'allDay':False,'status':'planned'};pid=project(c)['id']
    res=c.post(f'/api/projects/{pid}/event-plan',json={'revision':state(c)['revision'],'event':fields,'source':p['sourceArgs']});assert res.status_code==200,res.text
    assert not project(c)['events'];plan=res.json();url='/api/secretary/plans/'+plan['planId']+'/confirm'
    assert c.post(url,json={'confirmed':False}).status_code==400 and not project(c)['events']
    assert c.post(url,json={'confirmed':True}).status_code==200
    evt=project(c)['events'][0];assert evt['title']=='Подать заявление в суд' and evt['timezone']=='Europe/Samara' and evt['startsAt'].endswith('11:00:00+00:00')
    assert evt['source']['messageId']==source['id'] and evt['source']['text']==cmd and evt['source']['sha256']
    assert c.post(url,json={'confirmed':True}).status_code==409 and len(project(c)['events'])==1
    assert c.post('/api/chat',json={**req,'text':cmd+'!'}).status_code==409
    # Frozen snapshot survives backup/restore, rather than reinterpreting "Friday" later.
    backup=c.post('/api/backup');assert backup.status_code==200
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    restored=next(x for x in project(c)['threads'][0]['messages'] if x['id']==m['id']);assert restored['contextSummary']['eventProposal']==p


def test_local_chat_needs_no_model_key_and_no_automatic_material_transfer(service):
    c,provider,_=service;configure(c);c.post('/api/models/settings',json={'clearKey':True,'consent':False})
    cmd='Напомни завтра в 15-00 подать иск';assert c.post('/api/chat/route',json={'text':cmd}).json()['local']
    req=request(c,cmd,deep=True);r=c.post('/api/chat',json=req);assert r.status_code==200,r.text;m=finish(c,req['requestId'])
    assert m['tier']=='local' and m['requestedDeep'] and not provider.calls
    assert c.post('/api/chat',json=req).json()['reused']
    assert c.post('/api/chat',json={**req,'deep':False}).status_code==409
    assert not c.post('/api/chat/route',json={'text':'Что такое иск?'}).json()['local']


def test_unconfirmed_zone_local_notice_not_false_model_refusal(service):
    c,provider,_=service;req=request(c,'Напомни завтра в 15-00 отправить письмо');assert c.post('/api/chat',json=req).status_code==200
    m=finish(c,req['requestId']);assert m['kind']=='secretaryProposal' and m['contextSummary']['eventProposal'] is None
    assert 'часовой пояс' in m['text'] and not provider.calls and not project(c)['events']


def test_regular_chat_still_calls_model_and_local_proposal_is_not_c_target(service):
    c,provider,_=service;configure(c);req=request(c,'Что такое иск?');assert c.post('/api/chat',json=req).status_code==200;finish(c,req['requestId']);assert len(provider.calls)==1
    req=request(c,'Напомни завтра в 15-00 подать иск');c.post('/api/chat',json=req);m=finish(c,req['requestId']);assert len(provider.calls)==1
    c.post('/api/models/settings',json={'modelC':'test-c','verificationEnabled':True})
    b={'requestId':str(uuid.uuid4()),'projectId':project(c)['id'],'threadId':project(c)['threads'][0]['id'],'messageId':m['id'],'excerpt':m['text'],'revision':state(c)['revision'],'confirmed':True}
    assert c.post('/api/check',json=b).status_code==400 and len(provider.calls)==1


def test_long_instruction_not_silently_truncated(service):
    c,provider,_=service;configure(c);req=request(c,'Напомни завтра в 15-00 '+('текст '*1000));assert c.post('/api/chat',json=req).status_code==200
    m=finish(c,req['requestId']);assert 'длиннее 5000' in m['text'] and m['contextSummary']['eventProposal'] is None and not provider.calls


def test_new_proposal_does_not_repair_or_touch_old_bad_title(service):
    c,provider,_=service;configure(c);data=state(c);old={'id':str(uuid.uuid4()),'title':'Я не могу записывать события в календарь','date':'2035-03-01','time':'12:00','type':'meeting','note':'старое ошибочное название','done':False}
    data['state']['projects'][0]['events'].append(old);assert c.put('/api/state',json=data).status_code==200;old=copy.deepcopy(project(c)['events'][0])
    req=request(c,'Напомни завтра в 15-00 подать иск');assert c.post('/api/chat',json=req).status_code==200;finish(c,req['requestId'])
    assert project(c)['events']==[old] and not provider.calls
