"""SQLite-backed single-user workspace. Provider access is owned by chat.py, not by storage."""
from __future__ import annotations
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import uuid
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePath, PurePosixPath


def workspace_relative(value):
    """Validate the input BEFORE conversion to native Windows separators.

    Internal callers use forward-slash strings. PurePath objects are normalized
    with as_posix(), never str(), so WindowsPath does not introduce false input.
    This helper is testable with PureWindowsPath even on a POSIX test host.
    """
    if isinstance(value, str):
        raw = value
    elif isinstance(value, PurePath):
        raw = value.as_posix()
    else:
        raise ValueError('Недопустимый тип пути.')
    rel = PurePosixPath(raw)
    if (not raw or '\x00' in raw or rel.is_absolute() or '..' in rel.parts
            or ':' in raw or '\\' in raw or not rel.parts):
        raise ValueError('Недопустимый путь.')
    return Path(*rel.parts)


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, typ, value, traceback):
        try:
            return super().__exit__(typ, value, traceback)
        finally:
            self.close()

def db_connect(path, **kwargs):
    return sqlite3.connect(path, factory=ManagedConnection, **kwargs)

MAX_UPLOAD = 25 * 1024 * 1024
MAX_STATE = 12 * 1024 * 1024
MAX_BACKUP = 512 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

def now():
    return datetime.now(timezone.utc).isoformat()

def ident(value):
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError('Недопустимый идентификатор.')
    return value

def text(value, limit=1000):
    if not isinstance(value, str) or len(value) > limit or '\x00' in value:
        raise ValueError('Недопустимая строка или превышение размера.')
    return value

def no_link(path: Path):
    """Reject symlinks and Windows junctions/reparse points in every existing component."""
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        if part.is_symlink():
            raise ValueError('Символические ссылки в рабочем пространстве запрещены.')
        try:
            if getattr(part.lstat(), 'st_file_attributes', 0) & 0x400:
                raise ValueError('Reparse point / junction в рабочем пространстве запрещён.')
        except FileNotFoundError:
            continue
    return path

def strict_date(value):
    text(value, 10)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Дата должна быть YYYY-MM-DD.')
    return date.fromisoformat(value)

def validate_state(s):
    if not isinstance(s, dict) or s.get('version') != 2:
        raise ValueError('Неподдерживаемый формат данных.')
    projects = s.get('projects')
    if not isinstance(projects, list) or not 1 <= len(projects) <= 2000:
        raise ValueError('Требуется от 1 до 2000 проектов.')
    seen = set()
    def check_id(v):
        ident(v)
        if v in seen:
            raise ValueError('Повторяющийся идентификатор.')
        seen.add(v)
    for p in projects:
        check_id(p['id']); text(p['name'], 300)
        if not p['name'].strip(): raise ValueError('Название проекта пусто.')
        text(p.get('instruction', ''), 20000); strict_date(p['createdAt'])
        if p.get('projectDate'): strict_date(p['projectDate'])
        if type(p.get('archived')) is not bool: raise ValueError('Недопустимый статус архива.')
        if p.get('tab', 'chat') not in ('chat', 'documents', 'results', 'events'):
            raise ValueError('Неизвестная вкладка.')
        threads = p['threads']
        if not isinstance(threads, list) or not 1 <= len(threads) <= 500:
            raise ValueError('Недопустимое количество обсуждений.')
        for t in threads:
            check_id(t['id']); text(t['name'], 300); text(t.get('draft', ''), 100000)
            messages = t['messages']
            if not isinstance(messages, list) or len(messages) > 10000: raise ValueError('Слишком много сообщений.')
            for m in messages:
                check_id(m['id']); text(m.get('text', ''), 100000)
                if m['role'] not in ('user', 'assistant'): raise ValueError('Неизвестная роль.')
                if m['role'] == 'assistant' and (m.get('generatedBy') != 'server-m2' or m.get('status') not in ('running','complete','cancelled','error','interrupted','limited')): raise ValueError('Неверные метаданные ответа модели.')
        if p['threadId'] not in [t['id'] for t in threads]: raise ValueError('Обсуждение не найдено.')
        for key in ('documents', 'results', 'events', 'activity'):
            if not isinstance(p[key], list) or len(p[key]) > 10000: raise ValueError('Превышен лимит списка.')
        for d in p['documents']:
            check_id(d['id']); text(d['name'], 255)
            if type(d.get('pinned')) is not bool: raise ValueError('Недопустимый флаг контекста.')
        for op in p.get('operations',[]):
            ident(op['id'])
            if op.get('kind') not in ('edit','result'):raise ValueError('Недопустимая операция.')
            if op['kind']=='edit':ident(op['oldFileId']);ident(op['newFileId'])
        for r in p['results']:
            if r.get('format')=='docx' and (r.get('generatedBy')!='server-m3b' or not re.fullmatch('[a-f0-9]{64}',r.get('artifactSha256',''))):raise ValueError('Неверные метаданные DOCX.')
            if r.get('reviewArtifactId'):
                check_id(r['reviewArtifactId'])
                if not re.fullmatch('[a-f0-9]{64}',r.get('reviewSha256','')):raise ValueError('Неверный SHA сверочной формы.')
            check_id(r['id']); text(r['name'], 255); text(r['text'], 1_000_000)
        for e in p['events']:
            check_id(e['id'])
            if e.get('managedBy')=='secretary-m4':
                from .secretary import validate_managed
                validate_managed(e);continue
            if e.get('reminders'):raise ValueError('Подтвердите событие через карточку M4, прежде чем включать напоминания.')
            text(e['title'], 200)
            if not e['title'].strip(): raise ValueError('Название события пусто.')
            strict_date(e['date'])
            tm = e.get('time', '')
            if tm:
                if not re.fullmatch(r'\d{2}:\d{2}', tm): raise ValueError('Неверное время.')
                datetime.strptime(tm, '%H:%M')
            if e.get('type') not in ('hearing', 'letter', 'meeting', 'deadline', 'other'): raise ValueError('Неверный тип события.')
            text(e.get('note', ''), 1000)
            e['timezone'] = 'Europe/Samara'
            e['allDay'] = not bool(tm)
            e['startsAt'] = datetime.fromisoformat(e['date']+'T'+tm+':00+04:00').astimezone(timezone.utc).isoformat() if tm else None
            if type(e.get('done')) is not bool: raise ValueError('Неверный статус события.')
        for a in p['activity']:
            text(a['text'], 2000); text(a['at'], 100)
    if s['projectId'] not in [p['id'] for p in projects]: raise ValueError('Текущий проект не найден.')
    if len(json.dumps(s, ensure_ascii=False).encode()) > MAX_STATE:
        raise ValueError('Превышен размер базы интерфейса (12 МБ).')
    return s

class Conflict(Exception):
    pass

class Workspace:
    def __init__(self, root):
        raw = str(root)
        if not Path(raw).is_absolute() or raw.startswith(('\\\\', '//')):
            raise ValueError('Укажите абсолютный путь к локальной папке.')
        self.root = no_link(Path(raw))
        if self.root == self.root.anchor or self.root == Path(self.root.anchor):
            raise ValueError('Корень диска не может быть рабочим пространством.')
        if os.name == 'nt':
            for var in ('WINDIR', 'ProgramFiles', 'ProgramFiles(x86)'):
                if os.environ.get(var):
                    blocked = Path(os.environ[var]).resolve()
                    if self.root == blocked or blocked in self.root.parents:
                        raise ValueError('Выберите отдельную пользовательскую папку, не системную.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.system = self.safe('.yuriy'); self.system.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self._lock_file = open(self.safe('.yuriy/workspace.lock'), 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self._lock_file.seek(0); self._lock_file.write(b'0'); self._lock_file.flush(); self._lock_file.seek(0)
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            raise ValueError('Это пространство уже открыто другой службой.')
        self.db_path = self.safe('.yuriy/app.sqlite3')
        self.safe('projects').mkdir(exist_ok=True)
        self.safe('.yuriy/backups').mkdir(exist_ok=True)
        self.cleanup_temporary()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id), ordinal INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL);
            ''')
            ver = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if ver and ver[0] != '1': raise ValueError('Версия базы новее приложения.')
            db.execute("INSERT OR IGNORE INTO meta VALUES('schema','1')")
            db.execute("INSERT OR IGNORE INTO meta VALUES('revision','0')")
            if not db.execute('SELECT 1 FROM projects').fetchone():
                p, t = str(uuid.uuid4()), str(uuid.uuid4())
                s = {'version': 2, 'projectId': p, 'projects': [{'id': p, 'name': 'Первый проект', 'instruction': 'Укажите цель проекта и требования к результату.', 'createdAt': date.today().isoformat(), 'updatedAt': now(), 'archived': False, 'archivedAt': None, 'documents': [], 'results': [], 'events': [], 'activity': [], 'tab': 'chat', 'threadId': t, 'threads': [{'id': t, 'name': 'Общее обсуждение', 'draft': '', 'deep': False, 'scroll': None, 'messages': []}]}]}
                self._write(db, s, 1)

    def close(self):
        self._lock_file.close()

    def cleanup_temporary(self, max_age_seconds=86400):
        projects_dir = self.safe('projects')
        if not projects_dir.exists():
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_seconds
        removed = 0
        for pdir in projects_dir.iterdir():
            if pdir.is_symlink() or not pdir.is_dir():
                continue
            temp_dir = pdir / 'temporary'
            if temp_dir.is_symlink() or not temp_dir.is_dir():
                continue
            for item in list(temp_dir.iterdir()):
                try:
                    if item.is_symlink():
                        continue
                    newest = item.stat().st_mtime
                    if item.is_dir():
                        for sub in item.rglob('*'):
                            if not sub.is_symlink():
                                newest = max(newest, sub.stat().st_mtime)
                    if newest <= cutoff:
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink(missing_ok=True)
                        removed += 1
                except OSError:
                    continue
            try:
                if not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
            except OSError:
                pass
        return removed

    def safe(self, relative):
        rel = workspace_relative(relative)
        path = no_link(self.root / rel)
        if self.root not in path.parents: raise ValueError('Выход за рабочее пространство запрещён.')
        if path.exists() and path.is_file() and path.stat().st_nlink > 1:
            raise ValueError('Жёсткие ссылки запрещены.')
        return path

    def connect(self):
        no_link(self.db_path)
        db = db_connect(self.db_path, timeout=15)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def _read(self, db):
        ps = []
        for pid, data in db.execute('SELECT id,data FROM projects ORDER BY rowid'):
            p = json.loads(data); p['threads'] = []; p['events'] = []
            for tid, td in db.execute('SELECT id,data FROM threads WHERE project_id=? ORDER BY rowid', (pid,)):
                t = json.loads(td)
                t['messages'] = [json.loads(r[0]) for r in db.execute('SELECT data FROM messages WHERE thread_id=? ORDER BY ordinal', (tid,))]
                p['threads'].append(t)
            p['events'] = [json.loads(r[0]) for r in db.execute('SELECT data FROM events WHERE project_id=? ORDER BY rowid', (pid,))]
            ps.append(p)
        selected = db.execute("SELECT value FROM meta WHERE key='projectId'").fetchone()
        return {'version': 2, 'projectId': selected[0] if selected else ps[0]['id'], 'projects': ps}

    def read(self):
        with self.lock, self.connect() as db:
            return {'state': self._read(db), 'revision': int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])}

    def _write(self, db, s, revision):
        db.execute('DELETE FROM messages'); db.execute('DELETE FROM threads'); db.execute('DELETE FROM events'); db.execute('DELETE FROM projects')
        for p in s['projects']:
            pdata = {k: v for k, v in p.items() if k not in ('threads', 'events')}
            db.execute('INSERT INTO projects VALUES(?,?)', (p['id'], json.dumps(pdata, ensure_ascii=False)))
            for t in p['threads']:
                td = {k: v for k, v in t.items() if k != 'messages'}
                db.execute('INSERT INTO threads VALUES(?,?,?)', (t['id'], p['id'], json.dumps(td, ensure_ascii=False)))
                for i, m in enumerate(t['messages']):
                    db.execute('INSERT INTO messages VALUES(?,?,?,?)', (m['id'], t['id'], i, json.dumps(m, ensure_ascii=False)))
            for e in p['events']:
                db.execute('INSERT INTO events VALUES(?,?,?)', (e['id'], p['id'], json.dumps(e, ensure_ascii=False)))
        for k, v in [('revision', str(revision)), ('projectId', s['projectId'])]:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (k, v))

    def save(self, s, expected):
        s = validate_state(copy.deepcopy(s))
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision = int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            if expected != revision: raise Conflict('Данные изменились в другом окне. Сохранение остановлено; обновите страницу после копирования несохранённого текста.')
            old = {p['id']: p for p in self._read(db)['projects']}
            def answers(projects):
                return {m['id']: (p['id'],t['id'],m) for p in projects for t in p['threads'] for m in t['messages'] if m['role']=='assistant'}
            if answers(list(old.values())) != answers(s['projects']): raise ValueError('Ответы моделей управляются сервером; их нельзя подменять или удалять через сохранение интерфейса.')
            if set(old) - {p['id'] for p in s['projects']}: raise ValueError('Удаление проектов не поддерживается. Используйте архив.')
            for p in s['projects']:
                before_project=old.get(p['id'],{})
                def managed_events(project):return {e['id']:e for e in project.get('events',[]) if e.get('managedBy')=='secretary-m4'}
                if managed_events(p)!=managed_events(before_project):raise ValueError('События M4 изменяются только через подтверждаемую карточку.')
                if p['archived'] and not before_project.get('archived') and any(not e['done'] for e in managed_events(before_project).values()):raise ValueError('При архивировании выберите судьбу будущих событий через карточку архива.')
                if p.get('archiveConfirmationId')!=before_project.get('archiveConfirmationId'):raise ValueError('Квитанция архива управляется сервером.')
                if p.get('operations',[])!=before_project.get('operations',[]):raise ValueError('Журнал операций управляется сервером.')
                def managed_results(project):return {r['id']:r for r in project.get('results',[]) if r.get('generatedBy')=='server-m3b' or r.get('format')=='docx'}
                if managed_results(p)!=managed_results(before_project):raise ValueError('DOCX-результаты управляются сервером.')
                for d in p['documents']:
                    f = db.execute('SELECT project_id,name,size,sha256 FROM files WHERE id=?', (d['id'],)).fetchone()
                    if not f or f[0] != p['id']: raise ValueError('Файл не импортирован в этот проект.')
                    d.update(name=f[1], size=f[2], sha256=f[3], ext=Path(f[1]).suffix[1:].upper()[:5] or 'FILE', meta=f'{f[2]} байт · сохранён на диске; статус извлечения — в разделе документов')
                before = old.get(p['id'])
                if before:
                    if p['createdAt'] != before['createdAt']: raise ValueError('Дата создания неизменяема; используйте дату проекта.')
                    p['updatedAt'] = before.get('updatedAt', now())
                    if p['archived'] != before['archived']:
                        p['archivedAt'] = now() if p['archived'] else None
                    else: p['archivedAt'] = before.get('archivedAt')
                    if p != before: p['updatedAt'] = now()
                    if before['archived'] and p['archived']:
                        # UI selection may change; archived project content is read-only.
                        keys = ('name', 'instruction', 'createdAt', 'projectDate', 'documents', 'results', 'events')
                        if any(p.get(k) != before.get(k) for k in keys) or [t['messages'] for t in p['threads']] != [t['messages'] for t in before['threads']]:
                            raise ValueError('Архив доступен только для чтения. Сначала восстановите проект.')
                else: p.update(updatedAt=now(), archivedAt=None)
                for r in p['results']:
                    prev = next((x for x in (before or {}).get('results', []) if x['id'] == r['id']), None)
                    if prev and prev != r: raise ValueError('M1 не перезаписывает результаты; создайте новую заметку.')
                    if not prev: self._result_file(p['id'], r)
            self._write(db, s, revision + 1)
            return revision + 1

    def _result_file(self, pid, r):
        directory = self.safe(f'projects/{ident(pid)}/results'); directory.mkdir(parents=True, exist_ok=True)
        path = self.safe(f'projects/{pid}/results/{ident(r["id"])}.txt')
        content = r['text'].encode('utf-8')
        if path.exists():
            if path.read_bytes() != content: raise ValueError('Файл результата уже существует с другим содержимым.')
        else:
            with path.open('xb') as f:
                f.write(content); f.flush(); os.fsync(f.fileno())

    def upload(self, pid, filename, data):
        ident(pid); text(filename, 255)
        if not filename or any(c in filename for c in ('/', '\\', ':', '\x00')) or filename in ('.', '..'):
            raise ValueError('Недопустимое имя файла.')
        if len(data) > MAX_UPLOAD: raise ValueError('Файл превышает 25 МБ.')
        with self.lock, self.connect() as db:
            p = db.execute('SELECT data FROM projects WHERE id=?', (pid,)).fetchone()
            if not p: raise ValueError('Проект не найден.')
            if json.loads(p[0])['archived']: raise ValueError('Сначала верните проект из архива.')
            fid = str(uuid.uuid4()); directory = self.safe(f'projects/{pid}/materials'); directory.mkdir(parents=True, exist_ok=True)
            dest = self.safe(f'projects/{pid}/materials/{fid}.blob')
            with dest.open('xb') as f:
                f.write(data); f.flush(); os.fsync(f.fileno())
            digest = hashlib.sha256(data).hexdigest()
            db.execute('INSERT INTO files VALUES(?,?,?,?,?)', (fid, pid, filename, len(data), digest))
            return {'id': fid, 'name': filename, 'size': len(data), 'sha256': digest, 'pinned': True, 'ext': Path(filename).suffix[1:].upper()[:5] or 'FILE', 'meta': f'{len(data)} байт · сохранён на диске; статус извлечения — в разделе документов'}

    def download(self, pid, fid):
        ident(pid); ident(fid)
        with self.lock, self.connect() as db:
            p = next((p for p in self._read(db)['projects'] if p['id'] == pid), None)
            if not p or not any(d['id'] == fid for d in p['documents']): raise ValueError('Файл не связан с проектом.')
            f = db.execute('SELECT name,sha256 FROM files WHERE id=? AND project_id=?', (fid, pid)).fetchone()
            if not f: raise ValueError('Файл не найден.')
            path = self.safe(f'projects/{pid}/materials/{fid}.blob')
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != f[1]: raise ValueError('Файл изменён вне приложения; контрольная сумма не совпала.')
            return f[0], data

    def backup(self):
        with self.lock:
            out = self.safe('.yuriy/backups/' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.zip')
            with tempfile.TemporaryDirectory(dir=self.system) as td:
                snapshot = Path(td)/'db.sqlite3'
                with self.connect() as source, db_connect(snapshot) as target: source.backup(target)
                items = {'db.sqlite3': snapshot}
                expected_hashes = {}
                with db_connect(snapshot) as db:
                    for fid, pid, digest in db.execute('SELECT id,project_id,sha256 FROM files'):
                        ident(fid); ident(pid)
                        path = self.safe(f'projects/{pid}/materials/{fid}.blob')
                        items[f'files/{pid}/{fid}.blob'] = path
                        expected_hashes[f'files/{pid}/{fid}.blob'] = digest
                    for pid, pdata in db.execute('SELECT id,data FROM projects'):
                        for r in json.loads(pdata)['results']:
                            path = self.safe(f'projects/{pid}/results/{ident(r["id"])}.txt')
                            items[f'results/{pid}/{r["id"]}.txt'] = path
                            expected_hashes[f'results/{pid}/{r["id"]}.txt'] = hashlib.sha256(r['text'].encode()).hexdigest()
                            if r.get('format')=='docx':
                                key=f'results/{pid}/{r["id"]}.docx'
                                items[key]=self.safe(f'projects/{pid}/results/{ident(r["id"])}.docx')
                                expected_hashes[key]=r['artifactSha256']
                                if r.get('reviewArtifactId'):
                                    review_id=ident(r['reviewArtifactId']);key=f'results/{pid}/{review_id}.docx'
                                    items[key]=self.safe(f'projects/{pid}/results/{review_id}.docx');expected_hashes[key]=r['reviewSha256']
                manifest = {'app': 'yuriy-local', 'schema': 1, 'createdAt': now(), 'files': {}}
                total = 0
                partial = out.with_suffix('.partial')
                with zipfile.ZipFile(partial, 'x', zipfile.ZIP_DEFLATED) as z:
                    for name, path in items.items():
                        if path.stat().st_size>MAX_BACKUP-total:raise ValueError('Размер резервной копии превышает 512 МБ.')
                        data = path.read_bytes(); total += len(data)
                        if name in expected_hashes and hashlib.sha256(data).hexdigest() != expected_hashes[name]: raise ValueError('Файл изменён вне приложения. Резервная копия не завершена.')
                        if total > MAX_BACKUP: raise ValueError('Размер резервной копии превышает лимит M1: 512 МБ.')
                        manifest['files'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}; z.writestr(name, data)
                    z.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
                os.replace(partial, out)
            return out

    def restore(self, data):
        """Validated staging, mandatory preflight, monotonic revision; no extractall."""
        with self.lock, tempfile.TemporaryDirectory(dir=self.system) as td:
            td = Path(td); archive = td/'incoming.zip'; archive.write_bytes(data)
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                if len(names) > 12000 or len(set(names)) != len(names) or sum(i.file_size for i in z.infolist()) > MAX_BACKUP:
                    raise ValueError('Архив превышает ограничения или содержит повторы.')
                manifest = json.loads(z.read('manifest.json'))
                if manifest.get('app') not in ('yuriy-local-m1', 'yuriy-local') or manifest.get('schema') != 1: raise ValueError('Это не резервная копия Юрия M1.')
                if set(names) != set(manifest['files']) | {'manifest.json'}: raise ValueError('Состав архива не совпадает с manifest.')
                blobs = {}
                for name, meta in manifest['files'].items():
                    if name != 'db.sqlite3' and not re.fullmatch(r'(files|results)/[A-Za-z0-9_-]{1,80}/[A-Za-z0-9_-]{1,80}\.(blob|txt|docx)', name):
                        raise ValueError('Небезопасный путь в архиве.')
                    blob = z.read(name)
                    if len(blob) != meta['size'] or hashlib.sha256(blob).hexdigest() != meta['sha256']: raise ValueError('Контрольная сумма архива не совпала.')
                    blobs[name] = blob
            source_path = td/'restore.sqlite3'; source_path.write_bytes(blobs['db.sqlite3'])
            with db_connect(source_path) as db:
                db.execute('PRAGMA trusted_schema=OFF')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('База повреждена.')
                schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if not schema or schema[0] != '1': raise ValueError('Неизвестная схема БД.')
                if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone(): raise ValueError('Недопустимые объекты БД.')
                incoming = validate_state(self._read(db))
                records = list(db.execute('SELECT id,project_id,name,size,sha256 FROM files'))
                file_ids = {}
                for fid, pid, name, size, digest in records:
                    ident(fid); ident(pid); text(name,255)
                    key = f'files/{pid}/{fid}.blob'
                    if key not in blobs or len(blobs[key]) != size or hashlib.sha256(blobs[key]).hexdigest() != digest: raise ValueError('Материал отсутствует или повреждён.')
                    file_ids[fid] = pid
                for p in incoming['projects']:
                    for d in p['documents']:
                        if file_ids.get(d['id']) != p['id']: raise ValueError('Нарушена связь документа с проектом.')
                    for r in p['results']:
                        key = f'results/{p["id"]}/{r["id"]}.txt'
                        if blobs.get(key) != r['text'].encode(): raise ValueError('Результат отсутствует или повреждён.')
                        if r.get('format')=='docx':
                            artifact=blobs.get(f'results/{p["id"]}/{r["id"]}.docx')
                            if artifact is None or hashlib.sha256(artifact).hexdigest()!=r['artifactSha256']:raise ValueError('DOCX-результат отсутствует или повреждён.')
                        if r.get('reviewArtifactId'):
                            review=blobs.get(f'results/{p["id"]}/{r["reviewArtifactId"]}.docx')
                            if review is None or hashlib.sha256(review).hexdigest()!=r['reviewSha256']:raise ValueError('Сверочная форма отсутствует или повреждена.')
                    for op in p.get('operations',[]):
                        if op['kind']=='edit' and (file_ids.get(op['oldFileId'])!=p['id'] or file_ids.get(op['newFileId'])!=p['id']):raise ValueError('Повреждены связи редакций материала.')
            old_revision = self.read()['revision']
            preflight = self.backup()  # failure is fatal, never ignore
            # Copy only new immutable files; never overwrite conflicting material.
            for name, content in blobs.items():
                if name == 'db.sqlite3': continue
                category, pid, filename = name.split('/')
                folder = 'materials' if category == 'files' else 'results'
                target = self.safe(f'projects/{pid}/{folder}/{filename}'); target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_bytes() != content: raise ValueError('Конфликт существующего файла; восстановление отменено.')
                else:
                    with target.open('xb') as f: f.write(content)
            with self.connect() as target:
                target.execute('BEGIN IMMEDIATE')
                self._write(target, incoming, old_revision + 1)
                target.execute('DELETE FROM files')
                target.executemany('INSERT INTO files VALUES(?,?,?,?,?)', records)
            return {'preflight': preflight.name, 'revision': old_revision + 1}


    def generation(self, request_id):
        ident(request_id)
        with self.lock, self.connect() as db:
            for data, tid, pid in db.execute("SELECT m.data,t.id,t.project_id FROM messages m JOIN threads t ON m.thread_id=t.id ORDER BY m.rowid DESC"):
                m = json.loads(data)
                if m.get('role') == 'assistant' and m.get('requestId') == request_id:
                    return {'message': m, 'threadId': tid, 'projectId': pid}
        return None

    def start_generation(self, pid, tid, request_id, user_text, model, tier, expected, context_summary, local_response=None):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision = int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            if expected != revision: raise Conflict('Данные изменились. Обновите состояние перед отправкой; API ещё не вызван.')
            s = self._read(db)
            p = next((p for p in s['projects'] if p['id']==pid), None)
            if not p: raise ValueError('Проект не найден.')
            if p['archived']: raise ValueError('Архив доступен только для чтения.')
            t = next((t for t in p['threads'] if t['id']==tid), None)
            if not t: raise ValueError('Обсуждение не найдено.')
            t['messages'].append({'id':str(uuid.uuid4()),'role':'user','text':user_text,'at':now(),'requestId':request_id})
            m = {'id':str(uuid.uuid4()),'role':'assistant','text':'','at':now(),'requestId':request_id,'generatedBy':'server-m2','inputHash':hashlib.sha256(user_text.encode()).hexdigest(),'status':'running','model':model,'tier':tier,'contextSummary':context_summary,'sourceStatus':'not-verified'}
            if local_response is not None:
                m.update(kind='secretaryProposal',status='complete',text=local_response,requestedDeep=tier=='D',model='Локальный секретарь',tier='local')
                proposal=m['contextSummary'].get('eventProposal')
                if proposal is not None:proposal['sourceArgs']={'kind':'message','messageId':t['messages'][-1]['id'],'text':user_text}
            t['messages'].append(m);t['draft']='';t['scroll']=None
            p['updatedAt']=now()
            p['activity'].append({'text':('Локальное предложение события; календарь ещё не изменён' if local_response is not None else 'Запрос к модели '+model+'; источники не проверяются автоматически'),'at':now()})
            p['activity']=p['activity'][-150:]
            validate_state(s)
            if len(json.dumps(s,ensure_ascii=False).encode()) > MAX_STATE-350000:
                raise ValueError('Недостаточно места в лимите истории (12 МБ) для ответа модели. API ещё не вызван. Используйте отдельное рабочее пространство.')
            self._write(db,s,revision+1)
            return m

    def update_generation(self, request_id, updates):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for mid, data, pid in db.execute('SELECT m.id,m.data,t.project_id FROM messages m JOIN threads t ON m.thread_id=t.id ORDER BY m.rowid DESC'):
                m=json.loads(data)
                if m.get('role')!='assistant' or m.get('requestId')!=request_id: continue
                # A delayed checkpoint must never roll a final status/text back.
                if m.get('status')!='running': return m
                updates=dict(updates)
                if not updates.pop('_replaceText',False) and len(updates.get('text',m['text']))<len(m['text']):updates['text']=m['text']
                allowed={'text','status','error','errorCode','usage','finishReason','streamDiagnostics','agentMeta','kind'}
                m.update({k:v for k,v in updates.items() if k in allowed})
                m['updatedAt']=now()
                db.execute('UPDATE messages SET data=? WHERE id=?',(json.dumps(m,ensure_ascii=False),mid))
                pdata=json.loads(db.execute('SELECT data FROM projects WHERE id=?',(pid,)).fetchone()[0]);pdata['updatedAt']=now()
                db.execute('UPDATE projects SET data=? WHERE id=?',(json.dumps(pdata,ensure_ascii=False),pid))
                rev=int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])+1
                db.execute("UPDATE meta SET value=? WHERE key='revision'",(str(rev),))
                return m
            raise ValueError('Ответ модели не найден.')

    def recover_generations(self):
        with self.lock:
            s=self.read()['state']
            for p in s['projects']:
                for t in p['threads']:
                    for m in t['messages']:
                        if m['role']=='assistant' and m.get('status')=='running':
                            self.update_generation(m['requestId'],{'status':'interrupted','errorCode':'service_restart','error':'Служба была перезапущена. Сохранён последний фрагмент; запрос автоматически не повторялся.'})


    def start_check(self,pid,tid,request_id,target_id,model,expected,summary,scope,input_hash):
        with self.lock,self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision=int(db.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0])
            if expected!=revision:raise Conflict('Редакция изменилась. Проверка C ещё не вызвана.')
            s=self._read(db);p=next((p for p in s['projects'] if p['id']==pid),None)
            if not p or p['archived']:raise ValueError('Проект недоступен для проверки.')
            t=next((t for t in p['threads'] if t['id']==tid),None)
            if not t or not any(m['id']==target_id and m['role']=='assistant' and m.get('kind')!='verification' for m in t['messages']):raise ValueError('Проверяемый ответ не найден.')
            m={'id':str(uuid.uuid4()),'role':'assistant','text':'','at':now(),'requestId':request_id,'generatedBy':'server-m2','status':'running','model':model,'tier':'C','kind':'verification','verifiesMessageId':target_id,'inputHash':input_hash,'checkScope':scope,'contextSummary':summary,'sourceStatus':'not-verified'}
            t['messages'].append(m);p['updatedAt']=now()
            p['activity'].append({'text':'Отдельная модельная проверка C: '+model+'; не проверка первоисточников','at':now()});p['activity']=p['activity'][-150:]
            validate_state(s)
            if len(json.dumps(s,ensure_ascii=False).encode())>MAX_STATE-550000:raise ValueError('Недостаточно места в лимите истории для проверки C.')
            self._write(db,s,revision+1)
            return m
