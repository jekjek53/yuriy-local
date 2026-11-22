from __future__ import annotations
import hmac
import asyncio
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit, quote
import re
import sqlite3
import zipfile

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from .models import ModelSettings
from .provider import OpenAIProvider, ProviderError
from .chat import ChatEngine
from .documents import Documents
from .operations import Operations
from .secretary import Secretary
from .notifications import notify_windows
from .desktop import open_word_copy
from .storage import Workspace, Conflict, MAX_STATE, MAX_UPLOAD, no_link, text

VERSION = '0.6.0-m4'
STATIC = Path(__file__).parent/'static'/'index.html'

def config_dir():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'.config')))/'YuriyLocal'

async def limited_body(request, limit):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit: raise HTTPException(413, 'Превышен размер запроса.')
    return bytes(data)

def create_app(config_path=None, workspace=None, preview=False, provider=None, secrets_store=None):
    config_path = Path(config_path or config_dir()/'config.json')
    token = secrets.token_urlsafe(32)
    context = {'workspace': None, 'startup_error': None, 'shutdown': None}
    config_lock = threading.Lock()
    if workspace:
        context['workspace'] = Workspace(workspace)
    elif config_path.exists():
        try:
            cfg = json.loads(config_path.read_text('utf-8'))
            context['workspace'] = Workspace(cfg['workspace'])
        except Exception:
            context['startup_error'] = 'Не удалось открыть выбранное пространство. Проверьте наличие папки, права доступа и отсутствие другой службы. Не выбирайте другую папку, если хотите восстановить доступ к прежним данным.'

    settings = ModelSettings(config_path.parent, secrets=secrets_store)
    provider = provider or OpenAIProvider()
    chat = ChatEngine(lambda: ws(), settings, provider, documents_getter=lambda: docstore(), secretary_getter=lambda: secretary())
    if context['workspace']: context['workspace'].recover_generations()

    stop_scheduler=asyncio.Event()
    async def scheduler_loop():
        while not stop_scheduler.is_set():
            if context['workspace']:
                try:await asyncio.to_thread(secretary().tick)
                except Exception:
                    if context.get('secretary'):context['secretary'].last_error='Не удалось обновить напоминания. Проверьте доступ к диску; служба повторит попытку.'
            try:await asyncio.wait_for(stop_scheduler.wait(),timeout=5)
            except asyncio.TimeoutError:pass

    @asynccontextmanager
    async def lifespan(app):
        stop_scheduler.clear();task=asyncio.create_task(scheduler_loop())
        try:yield
        finally:
            stop_scheduler.set();await task
            await chat.close()
            if context['workspace']: context['workspace'].close()

    app = FastAPI(title='Юрий — секретарь M4', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runtime = context
    app.state.chat = chat
    app.state.models = settings
    app.state.test_token = token

    @app.middleware('http')
    async def local_boundary(request: Request, call_next):
        host = request.headers.get('host', '')
        try: hostname = urlsplit('http://' + host).hostname
        except ValueError: hostname = None
        allowed = hostname in ('127.0.0.1', 'localhost', '[::1]', '::1', 'testserver')
        if preview and hostname and re.fullmatch(r'[a-zA-Z0-9-]+\.e2b\.app', hostname): allowed = True
        if not allowed: return JSONResponse({'detail': 'Недопустимый Host.'}, status_code=403)
        origin = request.headers.get('origin')
        expected = f'{request.url.scheme}://{host}'
        # Proxy HTTPS is permitted only in explicit development-preview mode.
        valid_origins = {expected}
        if preview: valid_origins.add('https://' + host)
        if origin and origin not in valid_origins:
            return JSONResponse({'detail': 'Чужой Origin запрещён.'}, status_code=403)
        if request.url.path.startswith('/api/') and request.url.path != '/api/health':
            supplied = request.headers.get('x-yuriy-token', '')
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({'detail': 'Сессия устарела. Обновите страницу.'}, status_code=403)
        length = request.headers.get('content-length')
        if length and (not length.isdigit() or int(length) > 128 * 1024 * 1024):
            return JSONResponse({'detail': 'Слишком большой запрос.'}, status_code=413)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        if not preview: response.headers['X-Frame-Options'] = 'DENY'
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc): return JSONResponse({'detail': str(exc)}, status_code=400)
    @app.exception_handler(Conflict)
    async def conflict_error(request, exc): return JSONResponse({'detail': str(exc)}, status_code=409)
    @app.exception_handler(KeyError)
    async def bad_key(request, exc): return JSONResponse({'detail': 'Не хватает обязательного поля.'}, status_code=400)
    @app.exception_handler(TypeError)
    async def bad_type(request, exc): return JSONResponse({'detail': 'Неверный тип данных.'}, status_code=400)
    @app.exception_handler(OSError)
    async def disk_error(request, exc): return JSONResponse({'detail': 'Ошибка диска или доступа к файлу. Данные не подтверждены как сохранённые; проверьте место и права.'}, status_code=500)
    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc): return JSONResponse({'detail': 'Ошибка базы данных. Сохранение не подтверждено. Создайте копию пространства и обратитесь к журналу запуска.'}, status_code=500)
    @app.exception_handler(zipfile.BadZipFile)
    async def archive_error(request, exc): return JSONResponse({'detail': 'Архив ZIP повреждён.'}, status_code=400)

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc): return JSONResponse({'detail': str(exc), 'code': exc.code}, status_code=502)

    def ws():
        if not context['workspace']: raise HTTPException(409, 'Сначала выберите рабочее пространство.')
        return context['workspace']

    def docstore():
        if context.get('documents') is None: context['documents'] = Documents(ws())
        return context['documents']

    def opstore():
        if context.get('operations') is None:context['operations']=Operations(ws(),docstore())
        return context['operations']

    def secretary():
        if context.get('secretary') is None:context['secretary']=Secretary(ws(),docstore(),notify_windows if not preview else None)
        return context['secretary']

    @app.get('/api/secretary')
    async def secretary_center():return await asyncio.to_thread(secretary().center)

    @app.post('/api/secretary/settings')
    async def secretary_settings(request: Request):
        body=json.loads(await limited_body(request,4096))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(secretary().configure,body)

    @app.post('/api/secretary/acknowledge')
    async def secretary_ack(request: Request):
        body=json.loads(await limited_body(request,4096))
        return await asyncio.to_thread(secretary().acknowledge,body['id'])

    @app.post('/api/projects/{pid}/event-suggestion')
    async def event_suggestion(pid: str,request: Request):
        body=json.loads(await limited_body(request,40000))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(secretary().suggestion,pid,body)

    @app.post('/api/projects/{pid}/event-plan')
    async def event_plan(pid: str,request: Request):
        body=json.loads(await limited_body(request,40000))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(secretary().plan,pid,body)

    @app.post('/api/projects/{pid}/archive-plan')
    async def archive_plan(pid: str,request: Request):
        body=json.loads(await limited_body(request,4096))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(secretary().archive_plan,pid,body)

    @app.post('/api/secretary/plans/{key}/confirm')
    async def confirm_event(key: str,request: Request):
        body=json.loads(await limited_body(request,4096))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(secretary().confirm,key,body.get('confirmed'))

    @app.post('/api/secretary/plans/{key}/discard')
    async def discard_event(key: str):
        secretary().plans.pop(key,None);return {'discarded':True}

    @app.get('/', response_class=HTMLResponse)
    def index():
        nonce = secrets.token_urlsafe(18)
        html = STATIC.read_text('utf-8').replace('// __M4_MODULE__', (STATIC.parent/'m4.js').read_text('utf-8')).replace('// __LEGAL_DRAFTS__', (STATIC.parent/'legal-drafts.js').read_text('utf-8')).replace('// __CHAT_LAYOUT__', (STATIC.parent/'chat-layout.js').read_text('utf-8')).replace('/* __CHAT_LAYOUT_CSS__ */', (STATIC.parent/'chat-layout.css').read_text('utf-8')).replace('__LOCAL_TOKEN__', token)
        html = html.replace('<script>', '<script nonce="'+nonce+'">')
        response = HTMLResponse(html)
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'nonce-"+nonce+"'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'" + ("; frame-ancestors 'none'" if not preview else '')
        return response

    @app.get('/api/health')
    def health(): return {'app': 'yuriy-local', 'version': VERSION}

    @app.get('/api/workspace')
    def get_workspace():
        w = context['workspace']
        return {'configured': bool(w), 'path': str(w.root) if w else None, 'suggested': str(Path.home()/'Documents'/'Yuriy'), 'version': VERSION, 'error': context['startup_error'], 'timezone': 'Europe/Samara', 'preview': preview}

    @app.post('/api/workspace')
    async def set_workspace(request: Request):
        body = json.loads(await limited_body(request, 4096))
        if body.get('confirmed') is not True: raise ValueError('Подтвердите выбор папки.')
        with config_lock:
            if context['workspace']: raise Conflict('Пространство уже выбрано. Для смены остановите службу и измените config.json согласно инструкции.')
            w = Workspace(body['path'])
            try:
                config_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = config_path.with_suffix('.tmp')
                tmp.write_text(json.dumps({'workspace': str(w.root)}, ensure_ascii=False), 'utf-8')
                os.replace(tmp, config_path)
            except Exception:
                w.close(); raise
            w.recover_generations()
            context['workspace'] = w
            context['startup_error'] = None
        return {'path': str(w.root)}

    @app.get('/api/state')
    def get_state(): return ws().read()

    @app.put('/api/state')
    async def put_state(request: Request):
        body = json.loads(await limited_body(request, MAX_STATE + 1024))
        if type(body.get('revision')) is not int: raise ValueError('Требуется номер редакции.')
        async with chat.lock:
            chat.guard()
            revision = await asyncio.to_thread(ws().save, body['state'], body['revision'])
            return {'revision': revision, 'projects': {p['id']: {'updatedAt': p.get('updatedAt'), 'archivedAt': p.get('archivedAt')} for p in ws().read()['state']['projects']}}

    @app.post('/api/projects/{pid}/files')
    async def upload(pid: str, request: Request, name: str):
        data = await limited_body(request, MAX_UPLOAD)
        async with chat.lock:
            chat.guard()
            return await asyncio.to_thread(ws().upload, pid, name, data)

    @app.get('/api/projects/{pid}/files/{fid}')
    def download(pid: str, fid: str):
        name, data = ws().download(pid, fid)
        return Response(data, media_type='application/octet-stream', headers={'Content-Disposition': "attachment; filename*=UTF-8''" + quote(name, safe='')})

    @app.post('/api/backup')
    async def backup():
        async with chat.lock:
            chat.guard()
            p = await asyncio.to_thread(ws().backup)
            return Response(p.read_bytes(), media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="yuriy-{p.name}"', 'X-Backup-Name': p.name})

    @app.post('/api/restore')
    async def restore(request: Request):
        if request.headers.get('x-confirm-restore') != 'yes': raise ValueError('Требуется подтверждение замены пространства.')
        data = await limited_body(request, 128 * 1024 * 1024)
        async with chat.lock:
            chat.guard()
            def restore_data():
                with ws().lock:
                    result=ws().restore(data)
                    if context.get('secretary'):secretary().plans.clear();secretary().last_tick=None
                    return result
            result = await asyncio.to_thread(restore_data)
            await asyncio.to_thread(ws().recover_generations)
            if context.get('documents'): await asyncio.to_thread(docstore().clear)
            if context.get('operations'):opstore().plans.clear()
            if context.get('secretary'):secretary().plans.clear();secretary().last_tick=None
            chat.jobs.clear(); chat.current = None
            return result


    @app.get('/api/projects/{pid}/documents')
    async def document_status(pid: str):
        return {'documents': await asyncio.to_thread(docstore().listing, pid)}

    @app.post('/api/projects/{pid}/documents/{fid}/index')
    async def document_index(pid: str, fid: str):
        async with chat.lock:
            chat.guard()
            return await asyncio.to_thread(docstore().index, pid, fid)

    @app.get('/api/projects/{pid}/documents/{fid}/preview')
    async def document_preview(pid: str, fid: str, offset: int = 0):
        return await asyncio.to_thread(docstore().preview, pid, fid, offset)

    @app.get('/api/projects/{pid}/document-search')
    async def document_search(pid: str, q: str):
        return {'hits': await asyncio.to_thread(docstore().search, pid, q)}

    @app.get('/api/projects/{pid}/documents/{fid}/edit-source')
    async def edit_source(pid: str, fid: str):
        def read():
            opstore().project(pid)
            _,_,source,path=docstore().source(pid,fid)
            ext=Path(source['name']).suffix.lower()
            if ext not in ('.txt','.md','.docx'):raise ValueError('Этот формат не редактируется.')
            if ext=='.docx':return {**source,'format':'docx'}
            if path.stat().st_size>400003:raise ValueError('Редактор ограничен 100000 символов UTF-8.')
            raw=path.read_bytes()
            import hashlib
            if hashlib.sha256(raw).hexdigest()!=source['sha256']:raise Conflict('Исходник изменён вне приложения.')
            try:value=raw.decode('utf-8-sig')
            except UnicodeError:raise ValueError('Поддерживается только UTF-8.')
            if len(value)>100000:raise ValueError('Редактор ограничен 100000 символов.')
            return {**source,'format':'text','text':value}
        return await asyncio.to_thread(read)

    @app.post('/api/projects/{pid}/documents/{fid}/edit-plan')
    async def edit_plan(pid: str, fid: str, request: Request):
        body=json.loads(await limited_body(request,450000))
        async with chat.lock:
            chat.guard()
            return await asyncio.to_thread(opstore().prepare_edit,pid,fid,body)

    @app.get('/api/document-templates')
    def document_templates():
        from .legal_drafts import catalog
        return catalog()

    @app.post('/api/projects/{pid}/results/docx-plan')
    async def docx_plan(pid: str, request: Request):
        body=json.loads(await limited_body(request,450000))
        async with chat.lock:
            chat.guard()
            return await asyncio.to_thread(opstore().prepare_result,pid,body)

    @app.get('/api/operations/{plan_id}/preview-file')
    async def plan_file(plan_id: str,format: str='docx'):
        p=opstore().plan(plan_id)
        if format not in ('docx','review') or (format=='review' and 'reviewBytes' not in p):raise ValueError('Такой формы предпросмотра нет.')
        return Response(p['reviewBytes'] if format=='review' else p['bytes'],media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote('Предпросмотр — '+p['name'],safe='')})

    @app.post('/api/operations/{plan_id}/discard')
    async def discard_plan(plan_id: str):
        opstore().plans.pop(plan_id,None)
        return {'discarded':True}

    @app.post('/api/operations/{plan_id}/confirm')
    async def operation_confirm(plan_id: str, request: Request):
        body=json.loads(await limited_body(request,4096))
        if body.get('confirmed') is not True:raise ValueError('Нужно подтверждение пользователя.')
        async with chat.lock:
            chat.guard()
            return await asyncio.to_thread(opstore().confirm,plan_id)

    @app.get('/api/projects/{pid}/results/{rid}/file')
    async def result_file(pid: str, rid: str, format: str='docx'):
        name,raw=await asyncio.to_thread(opstore().result_file,pid,rid,format)
        return Response(raw,media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document' if format in ('docx','review') else 'text/plain; charset=utf-8',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name,safe='')})

    @app.get('/api/projects/{pid}/operations/{oid}/revision-file')
    async def revision_file(pid: str,oid: str,which: str='before'):
        name,raw=await asyncio.to_thread(opstore().revision_file,pid,oid,which)
        return Response(raw,media_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(name,safe='')})

    @app.post('/api/projects/{pid}/results/{rid}/open-editor')
    async def open_editor(pid: str,rid: str,request: Request):
        body=json.loads(await limited_body(request,4096))
        if body.get('confirmed') is not True:raise ValueError('Подтвердите передачу DOCX установленному редактору.')
        async with chat.lock:
            chat.guard()
            def launch():
                form=body.get('format','docx')
                if form not in ('docx','review'):raise ValueError('Редактору передаётся DOCX.')
                filename,raw=opstore().result_file(pid,rid,form)
                return open_word_copy(ws(),pid,raw,body.get('sha256'),preview,filename=filename)
            return await asyncio.to_thread(launch)

    @app.post('/api/projects/{pid}/documents/{fid}/open-editor')
    async def open_material_editor(pid: str,fid: str,request: Request):
        body=json.loads(await limited_body(request,4096))
        if body.get('confirmed') is not True:raise ValueError('Подтвердите открытие отдельной копии DOCX.')
        async with chat.lock:
            chat.guard()
            def launch():
                _,_,source,path=docstore().source(pid,fid)
                if path.stat().st_size>25*1024*1024:raise ValueError('Материал превышает 25 МБ.')
                name,raw=ws().download(pid,fid)
                if Path(name).suffix.lower()!='.docx':raise ValueError('Редактор вызывается только для DOCX.')
                # Reject macro/external containers before handing imported DOCX to another app.
                from .wordfiles import check_word_handoff
                check_word_handoff(raw)
                return open_word_copy(ws(),pid,raw,body.get('sha256'),preview,filename=name)
            return await asyncio.to_thread(launch)

    @app.get('/api/projects/{pid}/operations/{oid}/revision-preview')
    async def revision_preview(pid: str,oid: str,which: str='before',offset: int=0):
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(opstore().revision_preview,pid,oid,which,offset)

    @app.post('/api/projects/{pid}/operations/{oid}/restore-plan')
    async def restore_revision(pid: str,oid: str,request: Request):
        body=json.loads(await limited_body(request,4096))
        async with chat.lock:
            chat.guard();return await asyncio.to_thread(opstore().prepare_restore,pid,oid,body.get('which','before'))

    @app.post('/api/check')
    async def check_answer(request: Request):
        body=json.loads(await limited_body(request,250000))
        return await chat.check(body)

    @app.get('/api/models/settings')
    def get_models_settings(): return settings.public()

    @app.post('/api/models/settings')
    async def save_models_settings(request: Request):
        body = json.loads(await limited_body(request, 8192))
        async with chat.lock:
            chat.guard()
            return settings.save(body)

    @app.get('/api/models/list')
    async def list_models():
        chat.guard()
        cfg = settings.for_request(require_model=False)
        return {'models': await provider.models(cfg)}

    @app.post('/api/chat/route')
    async def chat_route(request: Request):
        from .event_intents import is_event_instruction
        body=json.loads(await limited_body(request,64000))
        return {'local':is_event_instruction(text(body.get('text',''),12000))}

    @app.post('/api/chat')
    async def start_chat(request: Request):
        body = json.loads(await limited_body(request, 64000))
        return await chat.start(body)

    @app.get('/api/chat/active')
    def active_chat(): return {'active':chat.status()}

    @app.get('/api/chat/{request_id}/events')
    async def chat_events(request_id: str):
        await chat.get(request_id)
        async def stream():
            async for event in chat.events(request_id):
                yield 'event: snapshot\ndata: '+json.dumps(event, ensure_ascii=False)+'\n\n'
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering':'no'})

    @app.post('/api/chat/{request_id}/cancel')
    async def cancel_chat(request_id: str): return await chat.cancel(request_id)

    @app.post('/api/shutdown')
    async def shutdown():
        callback = context.get('shutdown')
        if not callback: raise HTTPException(409, 'Остановка через API недоступна в этом режиме запуска.')
        await chat.close()
        threading.Timer(0.4, callback).start()
        return {'stopping': True}

    return app
