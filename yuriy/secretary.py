"""Local, conservative event proposals and durable reminders. No implicit model calls."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import secrets
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .storage import Conflict, ident, text, strict_date, now, validate_state

UTC=timezone.utc
TYPES=('hearing','letter','meeting','deadline','response','task','other')
META='secretary-v1'


def zone(value):
    text(value,100)
    try:return ZoneInfo(value)
    except (ZoneInfoNotFoundError,ValueError):raise ValueError('Неизвестный часовой пояс IANA.')


def local_instant(day,clock,tz,fold=None):
    strict_date(day)
    if not isinstance(clock,str) or not re.fullmatch(r'\d{2}:\d{2}',clock):raise ValueError('Укажите время HH:MM.')
    try:naive=datetime.fromisoformat(day+'T'+clock)
    except ValueError:raise ValueError('Неверное время.')
    z=zone(tz);options=[]
    for f in (0,1):
        local=naive.replace(tzinfo=z,fold=f);utc=local.astimezone(UTC)
        if utc.astimezone(z).replace(tzinfo=None)==naive and utc not in [v[1] for v in options]:options.append((f,utc))
    if not options:raise ValueError('Это местное время не существует из-за перехода часового пояса.')
    if len(options)>1 and (type(fold) is not int or fold not in (0,1)):raise ValueError('Неоднозначное местное время: выберите первое или второе вхождение (fold).')
    return next((v for f,v in options if f==fold),options[0][1]).isoformat()


def normalize_event(args,old=None):
    title=text(args['title'],200).strip()
    if not title:raise ValueError('Название события пусто.')
    day=args['date'];strict_date(day);tm=text(args.get('time',''),5);tz=args['timezone'];zone(tz)
    all_day=args.get('allDay',not bool(tm))
    if type(all_day) is not bool or all_day!= (not bool(tm)):raise ValueError('Без времени выберите «весь день»; для точного времени снимите этот флаг.')
    status=args.get('status','planned')
    if status not in ('planned','completed','cancelled'):raise ValueError('Неверный статус события.')
    kind=args.get('type','other')
    if kind not in TYPES:raise ValueError('Неверный тип события.')
    offsets=args.get('reminders',[])
    if not isinstance(offsets,list) or len(offsets)>5 or any(type(n) is not int or n<0 or n>43200 for n in offsets) or len(set(offsets))!=len(offsets):raise ValueError('До пяти разных напоминаний за 0–43200 минут.')
    reminder_time=text(args.get('reminderTime',''),5)
    start=local_instant(day,tm,tz,args.get('fold')) if tm else None
    if all_day and offsets:
        if not reminder_time:raise ValueError('Для напоминаний события «весь день» явно укажите местное время напоминания.')
        local_instant(day,reminder_time,tz,args.get('fold'))
    end_day=args.get('endDate','');end_time=args.get('endTime','');end=None
    if end_day:
        strict_date(end_day)
        if all_day:
            if end_time or end_day<day:raise ValueError('Проверьте окончание события на весь день.')
        else:
            end=local_instant(end_day,end_time,tz,args.get('endFold'))
            if datetime.fromisoformat(end)<=datetime.fromisoformat(start):raise ValueError('Окончание должно быть позже начала.')
    elif end_time:raise ValueError('Укажите дату окончания.')
    event={'id':old['id'] if old else str(uuid.uuid4()),'title':title,'date':day,'time':tm,'timezone':tz,'allDay':all_day,'startsAt':start,'endDate':end_day,'endTime':end_time,'endsAt':end,'fold':args.get('fold'),'endFold':args.get('endFold'),'type':kind,'note':text(args.get('note',''),1000),'place':text(args.get('place',''),500),'caseNumber':text(args.get('caseNumber',''),200),'status':status,'done':status!='planned','reminders':sorted(offsets),'reminderTime':reminder_time if all_day else '', 'managedBy':'secretary-m4','sequence':(old.get('sequence',0)+1) if old else 1,'confirmed':True,'createdAt':old.get('createdAt',now()) if old else now(),'updatedAt':now()}
    fields=('date','time','timezone','startsAt','allDay','fold','reminders','reminderTime','status')
    unchanged=bool(old and old.get('managedBy')=='secretary-m4' and all(event[k]==old.get(k) for k in fields))
    event['scheduleVersion']=old.get('scheduleVersion',old.get('sequence',1)) if unchanged else ((old.get('scheduleVersion',old.get('sequence',0))+1) if old else 1)
    return event


def validate_managed(e):
    n=normalize_event(e,e)
    for k in ('title','date','time','timezone','allDay','startsAt','endsAt','status','done','reminders','reminderTime'):
        if e.get(k)!=n[k]:raise ValueError('Неверные сохранённые поля события: '+k)
    if e.get('confirmed') is not True or type(e.get('sequence')) is not int or e['sequence']<1 or type(e.get('scheduleVersion')) is not int or e['scheduleVersion']<1:raise ValueError('Неверное подтверждение события.')
    if not isinstance(e.get('source'),dict):raise ValueError('Нет происхождения события.')


from .event_intents import propose


def event_future(e,instant=None):
    instant=instant or datetime.now(UTC)
    if e.get('done'):return False
    if e.get('startsAt'):return datetime.fromisoformat(e['startsAt'])>=instant
    return e['date']>=instant.astimezone(zone(e.get('timezone','Europe/Samara'))).date().isoformat()


class Secretary:
    def __init__(self,ws,documents=None,notifier=None):
        self.ws=ws;self.documents=documents;self.notifier=notifier;self.plans={};self.last_tick=None;self.last_error=None

    def _meta(self,db):
        row=db.execute('SELECT value FROM meta WHERE key=?',(META,)).fetchone()
        return json.loads(row[0]) if row else {'timezone':'Europe/Samara','timezoneConfirmed':False,'notificationsEnabled':False,'receipts':{}}

    def _put(self,db,m):db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)',(META,json.dumps(m,ensure_ascii=False)))

    def settings(self):
        with self.ws.lock,self.ws.connect() as db:m=self._meta(db)
        return {k:v for k,v in m.items() if k!='receipts'}

    def configure(self,args):
        tz=args['timezone'];zone(tz)
        if args.get('confirmed') is not True:raise ValueError('Подтвердите часовой пояс.')
        if type(args.get('notificationsEnabled')) is not bool:raise ValueError('Укажите разрешение уведомлений.')
        with self.ws.lock,self.ws.connect() as db:
            m=self._meta(db);m.update(timezone=tz,timezoneConfirmed=True,notificationsEnabled=args['notificationsEnabled'],confirmedAt=now());self._put(db,m)
        return self.settings()

    def source(self,p,args):
        origin=args.get('kind','manual')
        if origin in ('manual','text'):
            return {'kind':origin,'text':text(args.get('text',''),5000),'label':'Введено пользователем' if origin=='manual' else 'Предложено локальным разбором поручения; требует проверки'}
        if origin=='message':
            mid=ident(args['messageId']);m=next((m for t in p['threads'] for m in t['messages'] if m['id']==mid),None)
            if not m:raise ValueError('Сообщение не найдено в проекте.')
            excerpt=text(args['text'],5000)
            if not excerpt or excerpt not in m['text']:raise ValueError('Фрагмент не соответствует сообщению.')
            return {'kind':origin,'messageId':mid,'text':excerpt,'sha256':hashlib.sha256(m['text'].encode()).hexdigest(),'label':'Предложение из сообщения '+mid+'; не подтверждённый факт'}
        if origin=='document':
            fid=ident(args['fileId']);offset=args.get('offset',0)
            if type(offset) is not int or offset<0:raise ValueError('Неверный номер фрагмента.')
            d=self.documents.preview(p['id'],fid,offset);chunks=d['chunks']
            if not chunks:raise ValueError('Нет фрагмента.')
            c=chunks[0];excerpt=text(args.get('text',c['text']),5000)
            if not excerpt or excerpt not in c['text']:raise ValueError('Фрагмент не соответствует документу.')
            return {'kind':origin,'fileId':fid,'revisionId':d['revisionId'],'sha256':d['sha256'],'offset':offset,'location':{k:c[k] for k in ('kind','number','segment')},'text':excerpt,'label':'Извлечено из '+d['name']+'; сведения требуют проверки'}
        raise ValueError('Неверный источник события.')

    def project(self,pid):
        ident(pid);state=self.ws.read();p=next((p for p in state['state']['projects'] if p['id']==pid),None)
        if not p:raise ValueError('Проект не найден.')
        return state,p

    def suggestion(self,pid,args,instant=None):
        _,p=self.project(pid)
        if p['archived']:raise ValueError('Архив доступен только для чтения.')
        cfg=self.settings()
        if not cfg['timezoneConfirmed']:raise ValueError('Сначала подтвердите часовой пояс секретаря.')
        src=self.source(p,args['source']);proposal=propose(src['text'],cfg['timezone'],instant or datetime.now(UTC))
        return {**proposal,'source':src,'projectId':pid,'externalApiCalled':False}

    def plan(self,pid,args):
        state,p=self.project(pid)
        if p['archived']:raise ValueError('Архив доступен только для чтения.')
        if args.get('revision')!=state['revision']:raise Conflict('Состояние изменилось. Перечитайте событие.')
        cfg=self.settings()
        if not cfg['timezoneConfirmed']:raise ValueError('Подтвердите часовой пояс секретаря.')
        eid=args.get('eventId');old=None
        if eid:
            ident(eid);old=next((e for e in p['events'] if e['id']==eid),None)
            if not old:raise ValueError('Событие не найдено.')
        e=normalize_event(args['event'],old)
        e['source']=copy.deepcopy(old['source']) if old and old.get('source') else self.source(p,args.get('source',{'kind':'manual'}))
        e['proceduralDraft']=bool(args.get('proceduralDraft')) or e['type']=='deadline' or bool(re.search(r'срок|обжалован|апелляц|процессуальн',e['title']+' '+e['source'].get('text',''),re.I))
        return self.add_plan({'kind':'event','projectId':pid,'projectName':p['name'],'revision':state['revision'],'event':e,'before':old,'warning':'Все поля должны быть проверены пользователем. '+('Старое расписание будет отменено.' if old else 'Будет создано новое событие; существующие записи не изменятся.')+(' Черновик срока: проверьте основания и правила; соблюдение срока не гарантируется.' if e['proceduralDraft'] else '')})

    def archive_plan(self,pid,args):
        state,p=self.project(pid)
        if type(args.get('archived')) is not bool or args.get('revision')!=state['revision']:raise Conflict('Нужны актуальная ревизия и статус архива.')
        choice=args.get('eventsChoice','keep')
        if choice not in ('keep','cancel'):raise ValueError('Выберите оставить события или отменить.')
        future=[e['id'] for e in p['events'] if event_future(e)]
        future_events=[{k:e.get(k) for k in ('id','title','date','time','timezone')} for e in p['events'] if e['id'] in future]
        return self.add_plan({'futureEvents':future_events,'kind':'archive','projectId':pid,'projectName':p['name'],'revision':state['revision'],'archived':args['archived'],'eventsChoice':choice,'futureIds':future,'warning':'Будущие события остаются действующими и видимыми.' if choice=='keep' else 'Перечисленные будущие события будут отменены, прежнее расписание аннулируется.'})

    def add_plan(self,p):
        self.plans={k:v for k,v in self.plans.items() if v['expires']>time.monotonic()}
        if len(self.plans)>=10:raise ValueError('Слишком много предпросмотров; дождитесь истечения 10 минут.')
        key=secrets.token_urlsafe(24);p['expires']=time.monotonic()+600;self.plans[key]=p
        return {**{k:v for k,v in p.items() if k!='expires'},'planId':key,'expiresInSeconds':600}

    def confirm(self,key,confirmed):
        if confirmed is not True:raise ValueError('Нужно отдельное подтверждение полей пользователем.')
        p=self.plans.get(key)
        if not p or p['expires']<time.monotonic():self.plans.pop(key,None);raise Conflict('Предпросмотр истёк или использован.')
        with self.ws.lock,self.ws.connect() as db:
            db.execute('BEGIN IMMEDIATE');state=self.ws._read(db);revision=int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            if revision!=p['revision']:raise Conflict('Состояние изменилось; создайте новую карточку подтверждения.')
            project=next(x for x in state['projects'] if x['id']==p['projectId'])
            if p['kind']=='event':
                if project['archived']:raise ValueError('Архив доступен только для чтения.')
                event=copy.deepcopy(p['event']);event['confirmationId']=key
                project['events']=[e for e in project['events'] if e['id']!=event['id']]+[event]
                label='Подтверждено событие: '+event['title']
            else:
                project['archived']=p['archived'];project['archivedAt']=now() if p['archived'] else None
                if p['archived'] and p['eventsChoice']=='cancel':
                    for e in project['events']:
                        if e['id'] in p['futureIds']:
                            if e.get('managedBy')=='secretary-m4':e.update(status='cancelled',done=True,sequence=e['sequence']+1,scheduleVersion=e['scheduleVersion']+1,updatedAt=now())
                            else:e['done']=True
                label='Архив: '+str(p['archived'])+'; будущие события: '+p['eventsChoice']
                project['archiveConfirmationId']=key
            project['updatedAt']=now();project['activity'].append({'text':label,'at':now()});project['activity']=project['activity'][-150:]
            validate_state(state);self.ws._write(db,state,revision+1);del self.plans[key]
        self.tick(notify=False,collect_due=False)
        return {'revision':revision+1,'confirmationId':key,'eventId':p.get('event',{}).get('id')}

    def tick(self,instant=None,notify=True,collect_due=True):
        instant=instant or datetime.now(UTC)
        notifications=[]
        with self.ws.lock,self.ws.connect() as db:
            # Check baseline under the workspace lock so restore cannot race it.
            live=self.last_tick is not None and 0<=(instant-self.last_tick).total_seconds()<=45
            if collect_due:self.last_tick=instant
            m=self._meta(db);original=json.dumps(m,sort_keys=True,ensure_ascii=False);receipts=m['receipts'];active={}
            for pid,pdata,edata in db.execute('SELECT p.id,p.data,e.data FROM events e JOIN projects p ON p.id=e.project_id'):
                e=json.loads(edata);p=json.loads(pdata)
                if e.get('managedBy')!='secretary-m4' or not e.get('confirmed') or e.get('status')!='planned':continue
                base=e['startsAt']
                if e['allDay'] and e['reminders']:base=local_instant(e['date'],e['reminderTime'],e['timezone'],e.get('fold'))
                for offset in e['reminders']:
                    due=datetime.fromisoformat(base)-timedelta(minutes=offset)
                    key=e['id']+':'+str(e['scheduleVersion'])+':'+str(offset);active[key]=True
                    if key in receipts:
                        receipts[key].update(title=e['title'],projectName=p['name'],archived=p['archived'])
                        continue
                    if not collect_due or due>instant:continue
                    missed=not live or (instant-due).total_seconds()>45
                    row={'id':key,'projectId':pid,'eventId':e['id'],'title':e['title'],'projectName':p['name'],'archived':p['archived'],'date':e['date'],'time':e['time'],'timezone':e['timezone'],'dueAt':due.isoformat(),'recordedAt':instant.isoformat(),'missed':missed,'acknowledged':False,'superseded':False,'notification':'not-requested'}
                    if notify and live and not missed and m['notificationsEnabled']:
                        row['notification']='attempt-recorded';notifications.append(key)
                    receipts[key]=row
            for k,r in receipts.items():
                if k not in active:r['superseded']=True
            # Bounded retention: never silently drop an outstanding reminder.
            disposable=sorted((k for k,r in receipts.items() if r['acknowledged'] or r['superseded']),key=lambda k:receipts[k]['recordedAt'])
            for k in disposable[:max(0,len(receipts)-10000)]:del receipts[k]
            if json.dumps(m,sort_keys=True,ensure_ascii=False)!=original:self._put(db,m)
        if notifications:
            try:result=self.notifier(len(notifications)) if self.notifier else 'unavailable'
            except Exception:result='failed'
            with self.ws.lock,self.ws.connect() as db:
                m=self._meta(db)
                for k in notifications:
                    if k in m['receipts']:m['receipts'][k]['notification']=result
                self._put(db,m)
        self.last_error=None

    def center(self):
        with self.ws.lock,self.ws.connect() as db:m=self._meta(db)
        rows=sorted(m['receipts'].values(),key=lambda r:r['dueAt'],reverse=True)
        outstanding=[r for r in rows if not r['acknowledged'] and not r['superseded']]
        return {'settings':{k:v for k,v in m.items() if k!='receipts'},'items':(outstanding+[r for r in rows if r not in outstanding])[:500],'outstanding':len(outstanding),'total':len(rows),'lastTick':self.last_tick.isoformat() if self.last_tick else None,'schedulerError':self.last_error,'serviceRequired':True}

    def acknowledge(self,key):
        text(key,200)
        with self.ws.lock,self.ws.connect() as db:
            m=self._meta(db)
            if key not in m['receipts']:raise ValueError('Напоминание не найдено.')
            m['receipts'][key]['acknowledged']=True;self._put(db,m)
        return {'acknowledged':True}
