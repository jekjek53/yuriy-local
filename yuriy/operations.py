"""Human-confirmed, versioned workspace operations. No arbitrary paths or overwrites."""
from __future__ import annotations
import difflib
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from .storage import Conflict,MAX_STATE,ident,text,now,validate_state
from .legal_drafts import validate as validate_draft, assemble, assemble_freeform_docx, SCHEMAS


def digest(raw):return hashlib.sha256(raw).hexdigest()


def write_new(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True)
    # Fixed immutable destination; finalization is atomic and never overwrites a file.
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    try:
        with temp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        if path.exists():raise ValueError('Файл с этим ID уже существует.')
        os.replace(temp,path)
    finally:temp.unlink(missing_ok=True)


class Operations:
    def __init__(self,ws,documents):self.ws=ws;self.documents=documents;self.plans={}

    def project(self,pid):
        ident(pid);data=self.ws.read();p=next((p for p in data['state']['projects'] if p['id']==pid),None)
        if not p:raise ValueError('Проект не найден.')
        if p['archived']:raise ValueError('Архив доступен только для чтения.')
        return data,p

    def add_plan(self,data):
        self.plans={k:v for k,v in self.plans.items() if v['expires']>time.monotonic()}
        if len(self.plans)>=10 or sum(len(v['bytes'])+len(v.get('reviewBytes',b'')) for v in self.plans.values())+len(data['bytes'])+len(data.get('reviewBytes',b''))>64*1024*1024:raise ValueError('Слишком много открытых предпросмотров. Закройте/подождите истечения 10 минут.')
        pid=secrets.token_urlsafe(24);data['expires']=time.monotonic()+600;self.plans[pid]=data
        return {'planId':pid,'expiresInSeconds':600,**{k:v for k,v in data.items() if k not in ('bytes','reviewBytes','expires','result')}}

    def plan(self,plan_id):
        if not isinstance(plan_id,str) or plan_id not in self.plans:raise Conflict('Предпросмотр недоступен или уже использован. Создайте новый.')
        p=self.plans[plan_id]
        if time.monotonic()>p['expires']:
            del self.plans[plan_id];raise Conflict('Срок подтверждения истёк. Создайте новый предпросмотр.')
        return p

    def prepare_edit(self,pid,fid,args):
        state,p=self.project(pid);_,_,source,path=self.documents.source(pid,fid)
        if path.stat().st_size>25*1024*1024:raise ValueError('Материал превышает 25 МБ.')
        raw=path.read_bytes()
        if digest(raw)!=source['sha256']:raise Conflict('Материал изменён вне приложения. Правка запрещена; импортируйте актуальную копию.')
        ext=Path(source['name']).suffix.lower()
        if ext in ('.txt','.md'):
            try:before=raw.decode('utf-8-sig')
            except UnicodeError:raise ValueError('Правка TXT/MD поддерживает только UTF-8.')
            text(before,100000);after=text(args['text'],100000)
            if '\r\n' in before and '\n' not in before.replace('\r\n',''):
                after=after.replace('\r\n','\n').replace('\n','\r\n')
            if any(ord(c)<32 and c not in '\n\r\t' for c in after):raise ValueError('Недопустимые управляющие символы.')
            candidate=(b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+after.encode('utf-8')
            warning='Правится управляемая копия материала в проекте, не внешний файл. Прежняя версия останется на диске и в backup.'
        elif ext=='.docx':
            find=text(args['find'],4000);replacement=text(args['replace'],4000)
            with tempfile.TemporaryDirectory(dir=self.ws.system,prefix='word-edit-') as td:
                td=Path(td);(td/'source').write_bytes(raw);(td/'args.json').write_text(json.dumps({'find':find,'replace':replacement},ensure_ascii=False),'utf-8')
                flags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
                try:subprocess.run([sys.executable,'-m','yuriy.word_worker',str(td/'source'),str(td/'args.json'),str(td/'candidate'),str(td/'result.json')],timeout=35,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
                except (subprocess.SubprocessError,OSError):raise ValueError('Подготовка DOCX прервана по ошибке/лимиту. Исходник не изменён.')
                result=json.loads((td/'result.json').read_text('utf-8'))
                if not result['ok']:raise ValueError(result['error'])
                if not (td/'candidate').exists() or (td/'candidate').stat().st_size>25*1024*1024:raise ValueError('Некорректный размер новой редакции DOCX.')
                candidate=(td/'candidate').read_bytes();before=result['before'];after=result['after']
            warning='Показан изменяемый текстовый run, не весь DOCX. Сохранены прочие ZIP-части; оформление нужно проверить в редакторе. Внешний оригинал не меняется.'
        else:raise ValueError('Правка поддерживает только TXT/MD UTF-8 и простую однозначную замену в DOCX. PDF не редактируется.')
        if raw==candidate or before==after:raise ValueError('Изменений нет.')
        diff='\n'.join(difflib.unified_diff(before.splitlines(),after.splitlines(),fromfile='До',tofile='После',lineterm=''))
        if not diff:diff='Строки текста совпадают, но меняются переносы/конец файла. Сверьте текст и SHA-256 новой редакции.'
        return self.add_plan({'kind':'edit','projectId':pid,'fileId':fid,'name':source['name'],'revision':state['revision'],'oldSha256':source['sha256'],'newSha256':digest(candidate),'bytes':candidate,'before':before,'after':after,'diff':diff,'warning':warning})

    def prepare_restore(self,pid,operation_id,which):
        data,p=self.project(pid);ident(operation_id)
        op=next((o for o in p.get('operations',[]) if o['id']==operation_id and o['kind']=='edit'),None)
        if not op or which not in ('before','after'):raise ValueError('Редакция не найдена.')
        target_id=op['oldFileId'] if which=='before' else op['newFileId']
        family={target_id}
        for _ in range(len(p.get('operations',[]))+1):
            previous=len(family)
            for o in p.get('operations',[]):
                if o['kind']=='edit' and (o['oldFileId'] in family or o['newFileId'] in family):family.update((o['oldFileId'],o['newFileId']))
            if len(family)==previous:break
        current=[d for d in p['documents'] if d['id'] in family]
        if len(current)!=1:raise ValueError('Нет единственной текущей редакции этой цепочки. Сначала прикрепите материал явно.')
        fid=current[0]['id'];_,_,source,path=self.documents.source(pid,fid)
        _,candidate=self.revision_file(pid,operation_id,which)
        if path.stat().st_size>25*1024*1024:raise ValueError('Слишком большой материал.')
        raw=path.read_bytes()
        if digest(raw)!=source['sha256']:raise Conflict('Текущая редакция изменена вне приложения.')
        if raw==candidate:raise ValueError('Выбранная редакция уже совпадает с текущей.')
        before='Текущая редакция: '+source['sha256'];after='Возврат точных сохранённых байтов: '+digest(candidate)
        if Path(source['name']).suffix.lower() in ('.txt','.md'):
            before=raw.decode('utf-8-sig');after=candidate.decode('utf-8-sig')
        diff='\n'.join(difflib.unified_diff(before.splitlines(),after.splitlines(),fromfile='Текущая',tofile='Выбранная',lineterm=''))
        return self.add_plan({'kind':'edit','projectId':pid,'fileId':fid,'name':source['name'],'revision':data['revision'],'oldSha256':source['sha256'],'newSha256':digest(candidate),'bytes':candidate,'before':before,'after':after,'diff':diff,'restoredFromFileId':target_id,'warning':'Возврат создаёт НОВУЮ текущую редакцию из точных сохранённых байтов. История и внешний оригинал не меняются. Обязательны подтверждение и страховочная копия; оформление DOCX проверьте в редакторе.'})

    def revision_preview(self,pid,operation_id,which,offset=0):
        name,raw=self.revision_file(pid,operation_id,which)
        ext=Path(name).suffix.lower()
        if ext not in ('.txt','.md','.docx'):raise ValueError('Формат не поддерживается для просмотра.')
        with tempfile.TemporaryDirectory(dir=self.ws.system,prefix='version-view-') as td:
            td=Path(td);(td/'source').write_bytes(raw)
            flags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0
            try:subprocess.run([sys.executable,'-m','yuriy.extract_worker',str(td/'source'),ext,str(td/'result.json')],check=True,timeout=35,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=flags)
            except (OSError,subprocess.SubprocessError):raise ValueError('Просмотр версии прерван по ошибке/лимиту. Можно скачать исходные байты.')
            if not (td/'result.json').exists() or (td/'result.json').stat().st_size>8*1024*1024:raise ValueError('Недопустимый результат просмотра.')
            result=json.loads((td/'result.json').read_text('utf-8'))
        if not result.get('ok'):raise ValueError(result.get('error','Не удалось прочитать редакцию.'))
        result=result['result'];offset=max(0,min(offset,5000))
        return {'name':name,'sha256':digest(raw),'chars':result['chars'],'chunks':result['chunks'][offset:offset+20],'offset':offset,'hasMore':offset+20<len(result['chunks']),'warnings':result['warnings']}

    def prepare_result(self,pid,args):
        state,p=self.project(pid);title=text(args.get('title',''),200).strip()
        if not title:raise ValueError('Заполните название результата.')
        data=validate_draft(args)
        source_id=args.get('sourceMessageId');source=None;sources=[];check_info=None
        if source_id:
            ident(source_id)
            source=next((m for t in p['threads'] for m in t['messages'] if m['id']==source_id and m['role']=='assistant'),None)
            if not source or source.get('status') not in ('complete','limited','cancelled','error','interrupted') or not source.get('text'):raise ValueError('Нет сохранённого текста ответа для результата.')
            sources=source.get('contextSummary',{}).get('documentContext',{}).get('sources',[])
            checks=[m for t in p['threads'] for m in t['messages'] if m.get('kind')=='verification' and m.get('verifiesMessageId')==source_id]
            if checks:
                c=checks[-1];coverage=c.get('checkScope',{})
                check_info='Последняя C: '+c.get('model','')+' · '+c.get('status','')+' · '+('весь ответ' if coverage.get('wholeAnswer') else 'только выбранный фрагмент')+'. ID: '+c['id']+'. Это модельная проверка, не подтверждение первоисточников. Сам отчёт C смотрите в истории.'
        artifacts=assemble(data,sources,check_info,source.get('status') if source else None)
        raw=artifacts['compact'];body=artifacts['text'];review=artifacts['review']
        rid=str(uuid.uuid4());review_id=str(uuid.uuid4());filename='Черновик — '+re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',title)[:100]+'.docx'
        provenance={}
        for key,value in data['fields'].items():
            raw_value=args['fields'][key].strip()
            derived=bool(source and raw_value in source['text'])
            provenance[key]={'kind':'source-answer' if derived else 'user-confirmed-input','containsPlaceholder':'[УТОЧНИТЬ' in value}
            if derived:provenance[key].update(messageId=source_id,sha256=digest(source['text'].encode()))
        result={'id':rid,'name':title,'text':body,'created':now(),'format':'docx','filename':filename,'artifactSha256':digest(raw),'generatedBy':'server-m3b','generatedWith':'docxtpl-v1','sourceMessageId':source_id,'sourceAnswerStatus':source.get('status') if source else None,'sourceTextSha256':digest(source['text'].encode()) if source else None,'bodyEdited':True,'sourceStatus':'not-verified','reviewArtifactId':review_id,'reviewSha256':digest(review),'templateData':data,'documentType':SCHEMAS[data['templateId']]['name'],'jurisdiction':SCHEMAS[data['templateId']]['jurisdiction'],'role':SCHEMAS[data['templateId']]['role'],'templateVersion':artifacts['version'],'templateSha256':artifacts['templateSha256'],'fieldProvenance':provenance,'sourceSnapshots':sources}
        return self.add_plan({'kind':'result','projectId':pid,'revision':state['revision'],'name':filename,'bytes':raw,'reviewBytes':review,'newSha256':digest(raw),'reviewSha256':digest(review),'result':result,'before':'','after':body,'sourceCount':len(sources),'templateName':SCHEMAS[data['templateId']]['name'],'profile':data['profile'],'unresolvedFields':artifacts['placeholders'],'warning':'Будут созданы компактный DOCX и отдельный DOCX для сверки из доверенного шаблона. Это черновик, не подтверждение юридической готовности. Исходники не перезаписываются.'})

    def confirm(self,plan_id):
        plan=self.plan(plan_id)
        with self.ws.lock:
            state,p=self.project(plan['projectId'])
            if state['revision']!=plan['revision']:raise Conflict('Состояние изменилось после предпросмотра. Создайте новый предпросмотр.')
            if plan['kind']=='edit':
                _,old,source,path=self.documents.source(plan['projectId'],plan['fileId'])
                if path.stat().st_size>25*1024*1024 or digest(path.read_bytes())!=plan['oldSha256'] or source['sha256']!=plan['oldSha256']:raise Conflict('Файл изменился после предпросмотра. Подтверждение недействительно.')
            # Backup is mandatory before a material revision. Failure prevents commit.
            preflight=self.ws.backup().name if plan['kind']=='edit' else None
            del self.plans[plan_id]  # Single use, including a failed disk/DB attempt.
            operation={'id':str(uuid.uuid4()),'kind':plan['kind'],'at':now(),'confirmation':'explicit-local-confirmation','confirmationId':plan_id,'preflight':preflight,'sha256':plan['newSha256']}
            with self.ws.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
                if current!=plan['revision']:raise Conflict('Редакция изменилась. Нужен новый предпросмотр.')
                if plan['kind']=='edit':
                    _,_,latest,latest_path=self.documents.source(plan['projectId'],plan['fileId'])
                    if latest_path.stat().st_size>25*1024*1024 or digest(latest_path.read_bytes())!=plan['oldSha256'] or latest['sha256']!=plan['oldSha256']:raise Conflict('Материал изменился во время страховочной копии. Нужен новый предпросмотр.')
                    fid=str(uuid.uuid4());target=self.ws.safe(f'projects/{p["id"]}/materials/{fid}.blob')
                    write_new(target,plan['bytes'])
                    db.execute('INSERT INTO files VALUES(?,?,?,?,?)',(fid,p['id'],source['name'],len(plan['bytes']),plan['newSha256']))
                    d={**old,'id':fid,'size':len(plan['bytes']),'sha256':plan['newSha256'],'meta':'Новая редакция; требуется извлечь текст заново.'}
                    p['documents']=[d if x['id']==plan['fileId'] else x for x in p['documents']]
                    operation.update(restoredFromFileId=plan.get('restoredFromFileId'),oldFileId=plan['fileId'],newFileId=fid,name=source['name'],oldSha256=plan['oldSha256'])
                else:
                    r=plan['result'];self.ws._result_file(p['id'],r)
                    write_new(self.ws.safe(f'projects/{p["id"]}/results/{r["id"]}.docx'),plan['bytes'])
                    if r.get('reviewArtifactId'):write_new(self.ws.safe(f'projects/{p["id"]}/results/{r["reviewArtifactId"]}.docx'),plan['reviewBytes'])
                    p['results'].append(r);operation.update(resultId=r['id'],name=r['name'])
                p.setdefault('operations',[]).append(operation);p['updatedAt']=now()
                p['activity'].append({'text':('Сохранена новая редакция материала: ' if plan['kind']=='edit' else 'Создан DOCX-черновик: ')+plan['name'],'at':now()});p['activity']=p['activity'][-150:]
                validate_state(state['state']);self.ws._write(db,state['state'],current+1)
            return {'operation':operation,'revision':current+1}

    def commit_agent_docx(self,pid,args,sources=None):
        """Atomically save an agent-generated DOCX (template or freeform) into project results."""
        ident(pid)
        title=text(args.get('title',''),200).strip()
        if not title:raise ValueError('Укажите название создаваемого документа.')
        sources=sources or []
        if args.get('templateId'):
            clean_args={k:v for k,v in args.items() if k in ('title','templateId','fields','profile','calculation')}
            data=validate_draft(clean_args)
            artifacts=assemble(data,sources)
            doc_type=SCHEMAS[data['templateId']]['name']
            jurisdiction=SCHEMAS[data['templateId']]['jurisdiction']
            role_label=SCHEMAS[data['templateId']]['role']
            gen_with='agent-docxtpl-v1'
        else:
            artifacts=assemble_freeform_docx(args,sources)
            data=None
            doc_type=artifacts['docTypeLabel']
            jurisdiction='РФ'
            role_label='Исполнитель'
            gen_with='agent-freeform-v1'
        raw=artifacts['compact'];review=artifacts['review'];body=artifacts['text']
        rid=str(uuid.uuid4());review_id=str(uuid.uuid4())
        filename='Черновик — '+re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',title)[:100]+'.docx'
        result={
            'id':rid,'name':title,'text':body,'created':now(),'format':'docx','filename':filename,
            'artifactSha256':digest(raw),'generatedBy':'server-m3b','generatedWith':gen_with,
            'sourceMessageId':args.get('sourceMessageId'),'sourceStatus':'not-verified',
            'reviewArtifactId':review_id,'reviewSha256':digest(review),
            'documentType':doc_type,'jurisdiction':jurisdiction,'role':role_label,
            'templateVersion':artifacts['version'],'unresolvedPlaceholders':artifacts.get('placeholders',[]),
        }
        if data:
            result['templateData']=data
            result['templateSha256']=artifacts['templateSha256']
        with self.ws.lock:
            state=self.ws.read()
            p=next((x for x in state['state']['projects'] if x['id']==pid),None)
            if not p or p['archived']:raise ValueError('Проект недоступен для записи результата.')
            operation={'id':str(uuid.uuid4()),'kind':'result','at':now(),'confirmation':'agent-task-execution','confirmationId':'agent-'+uuid.uuid4().hex[:16],'preflight':None,'sha256':digest(raw),'resultId':rid,'name':title}
            with self.ws.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
                latest_state=self.ws._read(db)
                lp=next((x for x in latest_state['projects'] if x['id']==pid),None)
                if not lp or lp['archived']:raise ValueError('Проект недоступен для записи результата.')
                self.ws._result_file(pid,result)
                write_new(self.ws.safe(f'projects/{pid}/results/{rid}.docx'),raw)
                write_new(self.ws.safe(f'projects/{pid}/results/{review_id}.docx'),review)
                lp['results'].append(result)
                lp.setdefault('operations',[]).append(operation)
                lp['updatedAt']=now()
                lp['activity'].append({'text':'Агент Юрий создал DOCX: '+filename,'at':now()});lp['activity']=lp['activity'][-150:]
                validate_state(latest_state);self.ws._write(db,latest_state,current+1)
        return {'id':rid,'name':title,'filename':filename,'documentType':doc_type,'unresolvedPlaceholders':artifacts.get('placeholders',[]),'sha256':result['artifactSha256']}

    def result_file(self,pid,rid,format):
        ident(pid);ident(rid)
        if format not in ('txt','docx','review'):raise ValueError('Недопустимый формат.')
        p=next((p for p in self.ws.read()['state']['projects'] if p['id']==pid),None)
        r=next((r for r in (p or {}).get('results',[]) if r['id']==rid),None)
        if not r or (format in ('docx','review') and r.get('format')!='docx'):raise ValueError('Результат не найден.')
        if format=='review' and not r.get('reviewArtifactId'):raise ValueError('У старого результата нет отдельной сверочной формы. Сохранённый исходный DOCX не изменялся.')
        file_id=r['reviewArtifactId'] if format=='review' else rid
        ext='docx' if format=='review' else format
        path=self.ws.safe(f'projects/{pid}/results/{file_id}.{ext}')
        if path.stat().st_size>25*1024*1024:raise Conflict('Размер результата изменён вне приложения.')
        raw=path.read_bytes()
        expected=r['reviewSha256'] if format=='review' else r['artifactSha256'] if format=='docx' else digest(r['text'].encode())
        if digest(raw)!=expected:raise Conflict('Результат изменён вне приложения; SHA-256 не совпал.')
        filename=r.get('filename',r['name']+'.docx')
        return (filename[:-5]+' — для сверки.docx' if format=='review' else filename if format=='docx' else r['name']+'.txt'),raw

    def revision_file(self,pid,operation_id,which):
        ident(pid);ident(operation_id)
        if which not in ('before','after'):raise ValueError('Укажите before или after.')
        p=next((p for p in self.ws.read()['state']['projects'] if p['id']==pid),None)
        op=next((o for o in (p or {}).get('operations',[]) if o['id']==operation_id and o['kind']=='edit'),None)
        if not op:raise ValueError('Редакция не найдена в проекте.')
        fid=op['oldFileId'] if which=='before' else op['newFileId'];path=self.ws.safe(f'projects/{pid}/materials/{ident(fid)}.blob')
        if path.stat().st_size>25*1024*1024:raise Conflict('Размер редакции изменён вне приложения.')
        raw=path.read_bytes()
        if digest(raw)!=(op['oldSha256'] if which=='before' else op['sha256']):raise Conflict('Редакция изменена вне приложения.')
        return ('До — ' if which=='before' else 'После — ')+op['name'],raw
