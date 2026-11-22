"""Conservative DOCX operations. No macros, external fetches or arbitrary templates."""
from __future__ import annotations
import io
import zipfile
from lxml import etree

W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'


def xml_text(value):
    if any(not (ord(c) in (9,10,13) or 32<=ord(c)<=0xD7FF or 0xE000<=ord(c)<=0xFFFD or 0x10000<=ord(c)<=0x10FFFF) for c in value):raise ValueError('Текст содержит символы, недопустимые для XML/DOCX.')
    return value


def edit_docx(raw,old,new):
    xml_text(old);xml_text(new)
    if not old or len(old)>4000 or len(new)>4000 or any(c in old+new for c in '\r\n\t\x00'):
        raise ValueError('DOCX: непустой искомый текст и замена до 4000 символов, без переводов строк и табуляции.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist();names=z.namelist()
            if len(names)>3000 or len(set(names))!=len(names) or sum(x.file_size for x in entries)>64*1024*1024:raise ValueError('Слишком сложный/большой DOCX.')
            if any(n.startswith(('word/embeddings/','customUI/')) for n in names):raise ValueError('Вложения и custom UI не поддерживаются.')
            if any(n.lower().endswith('.bin') or 'vbaproject' in n.lower() for n in names):raise ValueError('Бинарные вложения и макросы не поддерживаются.')
            if any(n.startswith(('word/header','word/footer','word/footnotes','word/endnotes','word/comments','_xmlsignatures/')) for n in names):raise ValueError('DOCX с колонтитулами, сносками, комментариями или подписями не правится этой версией. Сохраните упрощённую копию.')
            if b'macroEnabled' in z.read('[Content_Types].xml'):raise ValueError('Макросовместимый контейнер запрещён.')
            for rel in entries:
                if not rel.filename.endswith('.rels'):continue
                if rel.file_size>1024*1024:raise ValueError('Слишком большой файл relationships.')
                relxml=z.read(rel)
                if b'\x00' in relxml or b'<!DOCTYPE' in relxml.upper() or b'<!ENTITY' in relxml.upper():raise ValueError('Неподдерживаемый XML relationships.')
                relations=etree.fromstring(relxml,etree.XMLParser(resolve_entities=False,no_network=True,load_dtd=False))
                for relationship in relations:
                    target=relationship.get('Target','')
                    if relationship.get('TargetMode','').lower()=='external' or target.startswith(('//','\\\\')) or __import__('re').match(r'^[A-Za-z][A-Za-z0-9+.-]*:',target):raise ValueError('DOCX с внешними relationships/шаблонами отклонён.')
            item=z.getinfo('word/document.xml')
            if item.file_size>16*1024*1024:raise ValueError('XML DOCX превышает 16 МБ.')
            xml=z.read(item)
            if b'\x00' in xml or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():raise ValueError('Неподдерживаемое или небезопасное кодирование XML.')
            tree=etree.fromstring(xml,etree.XMLParser(resolve_entities=False,no_network=True,load_dtd=False,huge_tree=False))
            forbidden={'ins','del','moveFrom','moveTo','fldChar','instrText','hyperlink','drawing','pict','object','altChunk','sdt','txbxContent'}
            for node in tree.iter():
                if not isinstance(node.tag,str):continue
                if etree.QName(node).localname in forbidden or etree.QName(node).localname=='AlternateContent':raise ValueError('DOCX содержит поля, правки, графику, ссылки или сложную разметку. Правка отклонена; используйте редактор.')
            paragraphs=[''.join(p.itertext('{'+W+'}t')) for p in tree.iter('{'+W+'}p')]
            # lxml itertext(tag) includes only that tag's textual descendants.
            total=sum(p.count(old) for p in paragraphs)
            nodes=[n for n in tree.iter('{'+W+'}t') if old in (n.text or '')]
            if total!=1 or len(nodes)!=1 or (nodes[0].text or '').count(old)!=1:
                raise ValueError('Замена DOCX должна однозначно находиться внутри одного текстового run. Совпадений нет, несколько или текст разбит форматированием.')
            node=nodes[0];before=node.text;node.text=before.replace(old,new,1)
            if node.text and (node.text[0].isspace() or node.text[-1].isspace()):node.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
            modified=etree.tostring(tree,xml_declaration=True,encoding='UTF-8',standalone=True)
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as dest:
                for entry in entries:dest.writestr(entry,modified if entry.filename=='word/document.xml' else z.read(entry.filename))
            return out.getvalue(),before,node.text
    except (zipfile.BadZipFile,KeyError,etree.XMLSyntaxError,RuntimeError):raise ValueError('Повреждённый или неподдерживаемый DOCX.')


def make_draft(title,body,sources,check_info=None,source_status=None):
    from docx import Document
    from docx.shared import Pt
    xml_text(title);xml_text(body)
    if len(body.splitlines())>2000:raise ValueError('DOCX ограничен 2000 абзацами основного текста.')
    if len(sources)>8 or sum(len(s.get('chunks',[])) for s in sources)>64 or sum(len(c['text']) for s in sources for c in s.get('chunks',[]))>12000:raise ValueError('Состав источников превышает бюджет DOCX.')
    document=Document()
    from datetime import datetime,timezone
    document.core_properties.created=datetime.now(timezone.utc)
    document.core_properties.modified=datetime.now(timezone.utc)
    document.core_properties.last_modified_by='Юрий'
    document.core_properties.author='Юрий'
    document.core_properties.comments='Черновик. Правовые основания и источники требуют ручной проверки.'
    normal=document.styles['Normal'];normal.font.name='Times New Roman';normal.font.size=Pt(12)
    document.sections[0].header.paragraphs[0].text='Юрий · ЧЕРНОВИК · требуется проверка юристом'
    document.add_heading('ЧЕРНОВИК',0);document.add_heading(title,1)
    document.add_paragraph('Не готов к подаче или отправке. Факты, реквизиты, правовые основания и редакции норм подлежат ручной проверке.')
    if source_status and source_status!='complete':document.add_paragraph('Исходный ответ имеет статус '+source_status+'. Его текст может быть неполным; проверьте завершённость самостоятельно.')
    for line in body.splitlines():document.add_paragraph(line)
    document.add_heading('Источники и охват',1)
    if not sources:document.add_paragraph('Подтверждённые первоисточники не приложены. Не использовать ссылки и нормы только по памяти модели.')
    for source in sources:
        document.add_heading(source.get('sourceId','')+' · '+source['name'],2)
        document.add_paragraph('SHA-256: '+source['sha256']+'\nВерсия текста: '+source['revisionId']+'\nПодготовлено фрагментов: '+str(source['sentChunks'])+' из '+str(source['totalChunks'])+'. Это не подтверждение полноты извлечения и не юридическая проверка.')
        for chunk in source.get('chunks',[]):document.add_paragraph(chunk['ref']+' · '+chunk['location']+'\n'+chunk['text'])
        for warning in source.get('warnings',[]):document.add_paragraph('Ограничение: '+warning)
    document.add_heading('Дополнительная проверка C',1)
    document.add_paragraph(check_info or 'Отдельная проверка C не приложена. Ни наличие модели C, ни её ответ не заменяют сверку с первоисточниками.')
    document.add_heading('Чек-лист перед использованием',1)
    for line in ['Заполнить незаданные реквизиты и поля [УТОЧНИТЬ].','Сверить обстоятельства, даты, суммы и расчёты с материалами.','Проверить актуальные редакции норм и применимость судебных актов по первоисточникам.','Сверить каждую цитату, координату и ссылку.','Проверить подсудность, порядок направления, сроки и комплект приложений.','Проверить оформление в установленном редакторе. Удалить пометку «Черновик» только после самостоятельной проверки.']:
        document.add_paragraph('☐ '+line)
    out=io.BytesIO();document.save(out);return out.getvalue()


def check_word_handoff(raw):
    """Lightweight container preflight, not a guarantee about external editor safety."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist()
            if len(entries)>3000 or len({e.filename for e in entries})!=len(entries) or sum(e.file_size for e in entries)>64*1024*1024:raise ValueError('DOCX превышает лимиты открытия.')
            if b'macroEnabled' in z.read('[Content_Types].xml') or any(e.filename.lower().endswith('.bin') for e in entries):raise ValueError('Макросы и бинарные вложения не передаются редактору. Сохраните и проверяйте файл самостоятельно.')
            for e in entries:
                if not e.filename.endswith('.rels'):continue
                if e.file_size>1024*1024:raise ValueError('Слишком большой relationships.')
                xml=z.read(e)
                if b'\x00' in xml or b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():raise ValueError('Небезопасный relationships.')
                root=etree.fromstring(xml,etree.XMLParser(resolve_entities=False,no_network=True,load_dtd=False))
                if any(r.get('TargetMode','').lower()=='external' or __import__('re').match(r'^(?:[A-Za-z][A-Za-z0-9+.-]*:|//|\\\\)',r.get('Target','')) for r in root):raise ValueError('DOCX с внешними relationships не открывается автоматически. Сохраните файл для самостоятельной проверки.')
    except (zipfile.BadZipFile,KeyError,etree.XMLSyntaxError):raise ValueError('Повреждённый DOCX.')
