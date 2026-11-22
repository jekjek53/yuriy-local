"""Explicit handoff to an OS-associated DOCX editor; immutable artifact stays intact."""
import hashlib
import os
import uuid
import re
from .storage import Conflict,ident
from .operations import write_new


def open_word_copy(ws,pid,raw,expected_sha,preview=False,filename="Черновик.docx"):
    ident(pid)
    if hashlib.sha256(raw).hexdigest()!=expected_sha:raise Conflict('SHA-256 изменился после предпросмотра.')
    if os.name!='nt' or preview:return {'requested':False,'reason':'Открытие редактора доступно только в локальной Windows-службе. Сохраните DOCX.','downloadAvailable':True}
    filename=re.sub(r'[\\/:*?"<>|\x00-\x1f]','_',str(filename))
    stem=filename[:-5] if filename.lower().endswith('.docx') else filename
    basename=(stem[:120].strip(' .') or 'Черновик')+'.docx'
    if basename.split('.')[0].upper() in {'CON','PRN','AUX','NUL',*('COM'+str(i) for i in range(1,10)),*('LPT'+str(i) for i in range(1,10))}:basename='Документ — '+basename
    target=ws.safe(f'projects/{pid}/temporary/{uuid.uuid4().hex}/{basename}')
    write_new(target,raw)
    try:os.startfile(str(target),'open')
    except OSError:
        target.unlink(missing_ok=True)
        return {'requested':False,'reason':'Не удалось открыть ассоциированное приложение. Возможно, редактор DOCX не установлен. Сохраните файл и откройте вручную.','downloadAvailable':True}
    return {'requested':True,'reason':'Windows получила запрос открыть отдельную копию DOCX. Используйте предпросмотр и печать редактора. Факты открытия и печати не подтверждены.','copyPath':str(target),'downloadAvailable':True}
