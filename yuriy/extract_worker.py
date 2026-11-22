"""Fixed internal parser process; no shell, user code, macros or external references."""
from __future__ import annotations
import json
import logging
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

VERSION = 'extract-1-pypdf-7.1.1'
MAX_CHARS = 1_000_000
MAX_PARTS = 5000


def extract(path, extension):
    parts=[];total=0;warnings=[];info={}
    def add(value,kind,number):
        nonlocal total
        value=value.replace('\x00','').strip()
        total+=len(value)
        if total>MAX_CHARS:raise ValueError('Извлечённый текст превышает 1 млн символов. Документ не индексирован; скрытого обрезания нет.')
        for start in range(0,len(value),1200):
            if len(parts)>=MAX_PARTS:raise ValueError('Превышен лимит 5000 фрагментов. Документ не индексирован.')
            parts.append({'kind':kind,'number':number,'segment':start//1200+1,'text':value[start:start+1200]})
    if extension in ('.txt','.md'):
        try:value=Path(path).read_text('utf-8-sig')
        except UnicodeError:raise ValueError('TXT/MD должен быть в UTF-8. Пересохраните копию в UTF-8.')
        if '\x00' in value:raise ValueError('Файл содержит нулевые байты и не принят как обычный UTF-8 текст.')
        if len(value)>MAX_CHARS:raise ValueError('Текст превышает 1 млн символов. Скрытого обрезания нет.')
        for number,para in enumerate(re.split(r'\n\s*\n',value),1):add(para,'paragraph',number)
    elif extension=='.docx':
        try:
            with zipfile.ZipFile(path) as z:
                names=z.namelist()
                if len(names)>3000 or len(set(names))!=len(names) or sum(i.file_size for i in z.infolist())>64*1024*1024:
                    raise ValueError('DOCX превышает лимит распаковки 64 МБ / 3000 частей или содержит повторы.')
                if any('vbaproject' in name.lower() or name.lower().endswith('.bin') for name in names):
                    raise ValueError('DOCX с бинарными вложениями или макросами не поддерживается.')
                item=z.getinfo('word/document.xml')
                if item.file_size>16*1024*1024:raise ValueError('Основной XML DOCX превышает 16 МБ.')
                xml=z.read(item)
                if b'\x00' in xml:raise ValueError('XML DOCX в UTF-16/32 не поддерживается. Пересохраните копию в современном редакторе.')
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():raise ValueError('DTD и XML-сущности в DOCX запрещены.')
                root=ET.fromstring(xml);ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
                for number,p in enumerate(root.iter(ns+'p'),1):
                    value=''.join((node.text or '') if node.tag==ns+'t' else ('\t' if node.tag==ns+'tab' else '\n') for node in p.iter() if node.tag in (ns+'t',ns+'tab',ns+'br'))
                    add(value,'paragraph',number)
                excluded=[n for n in names if re.match(r'word/(header|footer|footnotes|endnotes|comments)',n)]
                if excluded:info['partialExtraction']=True
                if excluded:warnings.append('Колонтитулы, сноски и комментарии не извлекались: анализ основного текста и таблиц неполный.')
                if any(node.tag in (ns+'ins',ns+'del',ns+'fldChar',ns+'instrText',ns+'drawing') for node in root.iter()):
                    info['partialExtraction']=True
                    warnings.append('Есть правки, поля или графика: их отображение и актуальность не проверены; сверяйте с редактором.')
                warnings.append('Абзацы DOCX нумеруются по XML основного текста, включая таблицы; это не страницы Word.')
        except (zipfile.BadZipFile,KeyError,ET.ParseError,RuntimeError):raise ValueError('Повреждённый или неподдерживаемый DOCX.')
    elif extension=='.pdf':
        from pypdf import PdfReader
        try:
            logging.getLogger('pypdf').setLevel(logging.CRITICAL)
            reader=PdfReader(path,strict=True)
            if reader.is_encrypted:raise ValueError('PDF защищён паролем. Сохраните незашифрованную копию, если имеете право.')
            count=len(reader.pages)
            if count>300:raise ValueError('PDF превышает лимит 300 страниц; разбейте его на части.')
            empty=[]
            for number,page in enumerate(reader.pages,1):
                value=page.extract_text() or ''
                if not value.strip():empty.append(number)
                add(value,'page',number)
            info['pages']=count;info['emptyPages']=empty
            if empty:warnings.append('На страницах '+', '.join(map(str,empty[:30]))+('…' if len(empty)>30 else '')+' не найден текстовый слой. OCR не выполнялся; анализ неполный.')
            warnings.append('PDF: порядок текста и таблиц может отличаться от визуального; сверяйте цитаты с оригиналом.')
        except ValueError:raise
        except Exception:raise ValueError('Не удалось безопасно извлечь текст PDF. Возможно, он повреждён или использует неподдерживаемую структуру.')
    else:raise ValueError('Чтение этого формата не поддерживается. Доступны TXT/MD UTF-8, DOCX без макросов и PDF с текстовым слоем.')
    if not parts:raise ValueError('Текст не найден. Для сканов нужен OCR, который пока не подключён.')
    return {'version':VERSION,'chars':total,'chunks':parts,'warnings':warnings,**info}


def main():
    # Fixed worker only; resource isolation does not change current-user permissions.
    try:
        from .parser_limits import apply_limits
        apply_limits()
        result=extract(sys.argv[1],sys.argv[2])
        answer={'ok':True,'result':result}
    except ValueError as exc:answer={'ok':False,'error':str(exc)[:800]}
    except Exception:answer={'ok':False,'error':'Ошибка извлечения текста. Исходный файл не изменён.'}
    Path(sys.argv[3]).write_text(json.dumps(answer,ensure_ascii=False),'utf-8')

if __name__=='__main__':main()
