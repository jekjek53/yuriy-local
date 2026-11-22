"""Structured legal drafts: trusted DOCX templates, data-only context, no provider calls."""
from __future__ import annotations
import calendar
import hashlib
import io
import json
import math
import re
from datetime import datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from pathlib import Path
from docx import Document
from docx.shared import Mm,Pt
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docxtpl import DocxTemplate
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from .storage import text,strict_date
from .wordfiles import xml_text

TEMPLATE_DIR=Path(__file__).parent/'templates'
VERSION='legal-templates-1'
COMMON=[('circumstances','Обстоятельства'),('grounds','Основания требований'),('demands','Требования'),('attachments','Приложения'),('document_date','Дата документа'),('signature','Подпись / подписант')]
SCHEMAS={
 'pretension':{'name':'Досудебная претензия','jurisdiction':'Досудебный порядок','role':'Заявитель','file':'pretension-v1.docx','fields':[('recipient_info','Адресат и адрес'),('sender_info','Заявитель и адрес'),('subject','Предмет претензии')]+COMMON},
 'claim_general':{'name':'Исковое заявление — общая юрисдикция','jurisdiction':'Общая юрисдикция','role':'Истец','file':'claim-general-v1.docx','fields':[('court_name','Наименование суда'),('court_address','Адрес суда'),('plaintiff_info','Сведения об истце'),('defendant_info','Сведения об ответчике'),('claim_price','Цена иска / неимущественный характер'),('state_duty','Госпошлина / основание льготы'),('subject','Предмет иска')]+COMMON[:3]+[('pretrial_info','Досудебный порядок / применимость')]+COMMON[3:]},
 'claim_arbitration':{'name':'Исковое заявление — арбитраж','jurisdiction':'Арбитраж','role':'Истец','file':'claim-arbitration-v1.docx','fields':[('court_name','Наименование арбитражного суда'),('court_address','Адрес суда'),('plaintiff_info','Истец: наименование / ФИО, реквизиты, адрес'),('defendant_info','Ответчик: наименование / ФИО, реквизиты, адрес'),('claim_price','Цена иска / неимущественный характер'),('state_duty','Госпошлина / основание льготы'),('subject','Предмет иска')]+COMMON[:3]+[('pretrial_info','Досудебный порядок и подтверждения')]+COMMON[3:]},
}
DEFAULT_PROFILE={'font':'Times New Roman','fontSize':14,'lineSpacing':1.5,'leftMm':30,'rightMm':15,'topMm':20,'bottomMm':20,'firstLineMm':12.5}
LABELS=dict(COMMON+[('subject','Предмет'),('pretrial_info','Досудебный порядок')])


def catalog():
    return {'version':VERSION,'defaultProfile':DEFAULT_PROFILE,'templates':[{'id':k,**{x:v[x] for x in ('name','jurisdiction','role')},'fields':[{'id':a,'label':b,'required':True} for a,b in v['fields']]} for k,v in SCHEMAS.items()]}


def profile(args):
    if args is None:args={}
    if not isinstance(args,dict) or set(args)-set(DEFAULT_PROFILE):raise ValueError('Неизвестные параметры оформления.')
    out={**DEFAULT_PROFILE,**args}
    if out['font'] not in ('Times New Roman','Arial','Calibri'):raise ValueError('Шрифт: Times New Roman, Arial или Calibri.')
    limits={'fontSize':(10,16),'lineSpacing':(1,2),'leftMm':(10,40),'rightMm':(10,40),'topMm':(10,40),'bottomMm':(10,40),'firstLineMm':(0,20)}
    for k,(lo,hi) in limits.items():
        v=out[k]
        if type(v) not in (int,float) or not math.isfinite(v) or not lo<=v<=hi:raise ValueError('Недопустимое оформление: '+k)
    return out


def clean(value):
    """Normalize known Markdown as text, never interpret Jinja/HTML from data."""
    value=xml_text(value).replace('\r\n','\n').replace('\r','\n')
    value=re.sub(r'^\s{0,3}#{1,6}\s+','',value,flags=re.M)
    value=re.sub(r'\*\*(.+?)\*\*',r'\1',value,flags=re.S)
    value=re.sub(r'__(.+?)__',r'\1',value,flags=re.S)
    value=re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)',r'\1',value)
    value=re.sub(r'(?<!\w)_([^_\n]+)_(?!\w)',r'\1',value)
    value=re.sub(r'~~(.+?)~~',r'\1',value)
    value=re.sub(r'^\s*>\s?', '',value,flags=re.M)
    value=re.sub(r'`([^`\n]+)`',r'\1',value)
    value=re.sub(r'\[([^\]\n]+)\]\(([^\s)]+)\)',r'\1 (\2)',value)
    value=re.sub(r'^\s*[-*+]\s+', '• ',value,flags=re.M)
    if re.search(r'^\s*```|^\s*\|.*\|\s*$|^\s*---+\s*$',value,re.M):raise ValueError('Уберите Markdown-таблицы, разделители и блоки кода из полей документа; используйте обычные абзацы.')
    return value.strip()


def dec(value,label,upper='1000000000000',allow_zero=False):
    if not isinstance(value,(str,int)) or isinstance(value,bool):raise ValueError('Укажите точное число строкой: '+label)
    try:d=Decimal(str(value).replace(',','.'))
    except InvalidOperation:raise ValueError('Неверное число: '+label)
    if not d.is_finite() or d<0 or (not allow_zero and d==0) or d>Decimal(upper):raise ValueError('Недопустимое число: '+label)
    return d


def calculation(args):
    if not isinstance(args,dict):raise ValueError('Расчёт должен быть структурированным объектом.')
    method=args.get('method','none')
    if method=='none':
        if any(k not in ('method',) for k in args):raise ValueError('При «без расчёта» уберите расчётные данные.')
        return {'method':'none','lines':[]}
    if method not in ('interest395','contract_penalty'):raise ValueError('Выберите один отдельный вид расчёта.')
    if set(args)-{'method','principal','basis','periods'}:raise ValueError('Неизвестное поле расчёта; проценты и неустойка не смешиваются.')
    basis=clean(text(args.get('basis',''),2000))
    if not basis:raise ValueError('Заполните основание расчёта.')
    principal=dec(args.get('principal'),'сумма долга');periods=args.get('periods')
    if not isinstance(periods,list) or not 1<=len(periods)<=50:raise ValueError('Расчёт: от 1 до 50 периодов.')
    label='Проценты по ст. 395 ГК РФ' if method=='interest395' else 'Договорная неустойка'
    lines=[label+' — проект расчёта по указанным пользователем данным.','Основание: '+basis,'Сумма для расчёта: '+format(principal,'f')+' руб. Даты начала и окончания каждого периода включены.'];total=Decimal('0');previous=None;normalized=[]
    for i,p in enumerate(periods,1):
        if not isinstance(p,dict):raise ValueError('Неверный период расчёта.')
        allowed={'start','end','annualRatePercent','yearDays'} if method=='interest395' else {'start','end','dailyRatePercent'}
        if set(p)!=allowed:raise ValueError('Поля периода не соответствуют выбранному виду расчёта.')
        a=strict_date(p['start']);b=strict_date(p['end']);days=(b-a).days+1
        if days<1 or days>3660 or (previous is not None and a<=previous):raise ValueError('Периоды должны идти по возрастанию без пересечения; один период не более 3660 дней.')
        previous=b
        rate=dec(p.get('annualRatePercent' if method=='interest395' else 'dailyRatePercent'),'ставка','1000',True)
        denominator=Decimal('100')
        if method=='interest395':
            if a.year!=b.year or type(p['yearDays']) is not int or p['yearDays'] not in (365,366):raise ValueError('Для ст. 395 разделите периоды по календарным годам и укажите базу 365/366 явно.')
            if p['yearDays']!=(366 if calendar.isleap(a.year) else 365):raise ValueError('База 365/366 не совпала с календарным годом; проверьте исходные данные.')
            denominator*=p['yearDays'];formula=f'{principal} × {rate}% / {p["yearDays"]} × {days}'
        else:formula=f'{principal} × {rate}% в день × {days}'
        amount=(principal*rate*days/denominator).quantize(Decimal('.01'),rounding=ROUND_HALF_UP);total+=amount
        lines.append(f'{i}. {a.isoformat()} — {b.isoformat()}: {formula} = {amount} руб.')
        normalized.append({**p,'days':days,'amount':str(amount)})
    lines+=['Итого: '+str(total)+' руб. Каждый период округлён до копеек по правилу HALF_UP.','Ставки, границы периодов и применимость основания вводятся и проверяются пользователем. Автозагрузки ставок, зачёта платежей, применения ограничений или снижения неустойки нет.']
    return {'method':method,'principal':str(principal),'basis':basis,'periods':normalized,'total':str(total),'lines':lines}


def validate(args):
    if not isinstance(args,dict) or set(args)-{'title','templateId','fields','profile','calculation','sourceMessageId'}:raise ValueError('Используйте только структурированные поля DOCX; неизвестные параметры не отбрасываются молча.')
    tid=args.get('templateId')
    if tid not in SCHEMAS:raise ValueError('Выберите доверенный шаблон: претензия, иск общей юрисдикции или арбитражный иск.')
    schema=SCHEMAS[tid];values=args.get('fields')
    if not isinstance(values,dict):raise ValueError('Нужны структурированные поля документа, а не один готовый текст.')
    unknown=set(values)-{k for k,_ in schema['fields']}
    if unknown:raise ValueError('Неизвестные поля: '+', '.join(sorted(unknown)))
    missing=[label for key,label in schema['fields'] if not isinstance(values.get(key),str) or not values[key].strip()]
    if missing:raise ValueError('Пустые обязательные поля: '+'; '.join(missing))
    out={k:clean(text(values[k],20000)) for k,_ in schema['fields']}
    empty=[label for k,label in schema['fields'] if not out[k]]
    if empty:raise ValueError('Пустые обязательные поля: '+'; '.join(empty))
    if sum(map(len,out.values()))>100000 or sum(len(v.splitlines()) for v in out.values())>1500:raise ValueError('Документ ограничен 100000 символов и 1500 строками полей.')
    for key in ('demands','attachments'):
        items=[re.sub(r'^\s*(?:\d+[.)]|[•])\s*','',line).strip() for line in out[key].splitlines()]
        items=[line for line in items if line]
        if not items:
            label=dict(schema['fields'])[key]
            raise ValueError('Пустые обязательные поля: '+label)
        out[key]='\n'.join(items)
    return {'templateId':tid,'fields':out,'profile':profile(args.get('profile')),'calculation':calculation(args.get('calculation',{'method':'none'}))}


def set_profile(doc,p):
    from docx.oxml.ns import qn
    for section in doc.sections:
        section.page_width=Mm(210);section.page_height=Mm(297)
        for attr,key in [('left_margin','leftMm'),('right_margin','rightMm'),('top_margin','topMm'),('bottom_margin','bottomMm')]:setattr(section,attr,Mm(p[key]))
    for name in ('Normal','LegalBody','LegalRequisites','LegalHeading','LegalDemand','LegalAttachment','LegalSignature','LegalService'):
        style=doc.styles[name];style.font.name=p['font'];style.font.size=Pt(p['fontSize'])
        fonts=style.element.get_or_add_rPr().find(qn('w:rFonts'))
        if fonts is not None:
            for attr in ('ascii','hAnsi','eastAsia','cs'):fonts.set(qn('w:'+attr),p['font'])
        pf=style.paragraph_format;pf.line_spacing=p['lineSpacing'];pf.space_after=Pt(0);pf.space_before=Pt(0)
        pf.first_line_indent=Mm(p['firstLineMm'] if name in ('Normal','LegalBody') else 0)
        pf.alignment=WD_ALIGN_PARAGRAPH.JUSTIFY if name in ('Normal','LegalBody') else WD_ALIGN_PARAGRAPH.LEFT
        if name=='LegalHeading':pf.alignment=WD_ALIGN_PARAGRAPH.CENTER;style.font.bold=True;pf.space_before=Pt(10);pf.space_after=Pt(6);pf.keep_with_next=True
    for table in doc.tables:
        table.autofit=False;table.columns[0].width=Mm((210-p['leftMm']-p['rightMm'])/2);table.columns[1].width=table.columns[0].width
        for row in table.rows:
            for cell in row.cells:cell.width=table.columns[0].width


def source_context(sources,review):
    if len(sources)>8 or sum(len(s.get('chunks',[])) for s in sources)>64 or sum(len(c['text']) for s in sources for c in s.get('chunks',[]))>12000:raise ValueError('Превышен бюджет источников.')
    rows=[]
    for s in sources:
        locations=list(dict.fromkeys(c['location'] for c in s.get('chunks',[])))
        refs='; '.join(locations[:3])+('; и др.' if len(locations)>3 else '')
        row=[s['name'][:160]+((' · '+refs) if refs else '')]
        # Brief bibliographic coordinates only in the compact service page.
        if review:
            row+=['Источник: '+s.get('sourceId',''),'Файл: '+s.get('fileId',''),'SHA-256: '+s['sha256'],'Версия текста: '+s['revisionId'],'Извлечение: '+s.get('extractorVersion','')+' · '+s.get('processedAt',''),'Охват: '+('частичный' if s.get('partial') else 'все сохранённые фрагменты; полнота исходного документа не подтверждается')]
            for c in s.get('chunks',[]):row+=[c['ref']+' · '+c['location'],c['text']]
            row+=['Ограничение извлечения: '+w for w in s.get('warnings',[])]
        rows.extend(row)
    return rows or ['Сохранённые источники не приложены. Правовые основания требуют самостоятельной сверки.']


def assemble(data,sources,check_info=None,source_status=None):
    schema=SCHEMAS[data['templateId']];registry=json.loads((TEMPLATE_DIR/'registry.json').read_text('utf-8'));path=TEMPLATE_DIR/schema['file']
    raw=path.read_bytes();sha=hashlib.sha256(raw).hexdigest()
    if registry.get(schema['file'])!=sha:raise ValueError('Контрольная сумма доверенного шаблона не совпала. Восстановите файлы программы.')
    if len(raw)>2*1024*1024:raise ValueError('Шаблон превышает лимит.')
    fields=data['fields'];placeholders=[label for key,label in schema['fields'] if '[УТОЧНИТЬ' in fields[key]]
    common={'fields':{k:v.splitlines() for k,v in fields.items()},'jurisdiction':schema['jurisdiction'],'role':schema['role'],'calculation':data['calculation']['lines'],'checklist':['Заполнить поля [УТОЧНИТЬ: …], проверить реквизиты и подпись.','Сверить факты, требования, расчёт, нормы, подсудность и приложения.','Проверить оформление в установленном редакторе. Удалить пометку только после самостоятельной проверки.']}
    warning='Не готов к подаче или отправке. Факты, реквизиты, основания и применимые нормы требуют проверки.'
    if placeholders:warning+=' Незаданные сведения отмечены [УТОЧНИТЬ: …].'
    if source_status and source_status!='complete':warning+=' Исходный ответ имеет статус '+source_status+' и может быть неполным.'
    common['warning']=warning
    outputs=[]
    for review in (False,True):
        tpl=DocxTemplate(io.BytesIO(raw));context={**common,'sources':[xml_text(x) for x in source_context(sources,review)],'review':review,'review_info':(check_info or 'Проверка C не приложена; наличие источников не подтверждает юридическую верность.') if review else ''}
        if review:context['review_info']+=' Шаблон: '+data['templateId']+' · '+VERSION+' · SHA-256: '+sha
        context['review_info']=xml_text(context['review_info'])
        env=SandboxedEnvironment(undefined=StrictUndefined,autoescape=True)
        tpl.render(context,jinja_env=env,autoescape=True)
        doc=tpl.docx;set_profile(doc,data['profile']);doc.core_properties.author='Юрий';doc.core_properties.last_modified_by='Юрий';doc.core_properties.created=datetime.now(timezone.utc);doc.core_properties.modified=datetime.now(timezone.utc);doc.core_properties.comments='Черновик. Не подтверждает юридическую готовность.'
        out=io.BytesIO();tpl.save(out);outputs.append(out.getvalue())
    preview='\n\n'.join(label+'\n'+fields[key] for key,label in schema['fields'])
    if data['calculation']['lines']:preview+='\n\n'+'\n'.join(data['calculation']['lines'])
    return {'compact':outputs[0],'review':outputs[1],'text':preview,'templateSha256':sha,'placeholders':placeholders,'version':VERSION}


FREEFORM_KINDS={
    'legal_opinion':'Правовое заключение',
    'disagreement_protocol':'Протокол разногласий',
    'addendum':'Дополнительное соглашение',
    'contract':'Проект договора',
    'response_letter':'Ответ на претензию / письмо',
    'procedural_motion':'Ходатайство / отзыв / заявление',
    'memo':'Аналитическая справка',
}


def assemble_freeform_docx(args,sources=None):
    """Build a GOST-styled freeform legal DOCX (opinion, protocol of disagreements, contract, addendum, motion)."""
    from docx.enum.style import WD_STYLE_TYPE
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    if not isinstance(args,dict):raise ValueError('Ожидается структурированное описание документа.')
    doc_kind=args.get('docKind','legal_opinion')
    if doc_kind not in FREEFORM_KINDS:doc_kind='legal_opinion'
    doc_label=FREEFORM_KINDS[doc_kind]
    heading=clean(text(args.get('heading') or args.get('title') or doc_label,300))
    subtitle=clean(text(args.get('subtitle',''),500)) if args.get('subtitle') else ''
    requisites=clean(text(args.get('requisites',''),4000)) if args.get('requisites') else ''
    sections_in=args.get('sections')
    if not isinstance(sections_in,list) or not 1<=len(sections_in)<=40:
        raise ValueError('Документ должен содержать от 1 до 40 разделов.')
    norm_sections=[]
    placeholders=[]
    for idx,sec in enumerate(sections_in,1):
        if not isinstance(sec,dict):raise ValueError('Раздел документа должен быть объектом.')
        sh=clean(text(sec.get('heading',f'Раздел {idx}'),300))
        raw_body=str(sec.get('body',''))
        raw_body=re.sub(r'^\s*\|.*\|\s*$',lambda m:re.sub(r'\s*\|\s*',' — ',m.group(0).strip(' |')),raw_body,flags=re.M)
        raw_body=re.sub(r'^\s*---+\s*$','',raw_body,flags=re.M)
        raw_body=re.sub(r'```[a-zA-Z0-9_-]*','',raw_body)
        sb=clean(text(raw_body,30000))
        if not sb:continue
        if '[УТОЧНИТЬ' in sb or '[УТОЧНИТЬ' in sh:placeholders.append(sh or f'Раздел {idx}')
        norm_sections.append({'heading':sh,'body':sb})
    if not norm_sections:raise ValueError('Заполните содержательный текст разделов документа.')
    signature=clean(text(args.get('signature','[УТОЧНИТЬ: должность, ФИО, подпись]'),1000))
    doc_date=clean(text(args.get('documentDate',''),100)) if args.get('documentDate') else ''
    prof=profile(args.get('profile'))
    sources=sources or []

    outputs=[]
    for review in (False,True):
        d=Document()
        for name in ('LegalBody','LegalRequisites','LegalHeading','LegalDemand','LegalAttachment','LegalSignature','LegalService'):
            st=d.styles.add_style(name,WD_STYLE_TYPE.PARAGRAPH);st.base_style=d.styles['Normal']
        d.sections[0].header.paragraphs[0].text='ЧЕРНОВИК · ПОДГОТОВЛЕНО АГЕНТОМ ЮРИЙ';d.sections[0].header.paragraphs[0].style='LegalService'
        d.add_paragraph('ЧЕРНОВИК','LegalHeading')
        if requisites:
            table=d.add_table(rows=1,cols=2);table.autofit=False
            borders=OxmlElement('w:tblBorders')
            for side in ('top','left','bottom','right','insideH','insideV'):
                el=OxmlElement('w:'+side);el.set(qn('w:val'),'nil');borders.append(el)
            table._tbl.tblPr.append(borders)
            cell=table.cell(0,1);cell.paragraphs[0].style='LegalRequisites'
            first=True
            for line in requisites.splitlines():
                if not line.strip():continue
                if first:cell.paragraphs[0].text=line.strip();first=False
                else:cell.add_paragraph(line.strip(),'LegalRequisites')
        d.add_paragraph(heading.upper(),'LegalHeading')
        if subtitle:d.add_paragraph(subtitle,'LegalHeading')
        for sec in norm_sections:
            if sec['heading']:d.add_paragraph(sec['heading'],'LegalHeading')
            for para in sec['body'].splitlines():
                if para.strip():d.add_paragraph(para.strip(),'LegalBody')
        if doc_date:d.add_paragraph('Дата: '+doc_date,'LegalSignature')
        if signature:
            for line in signature.splitlines():
                if line.strip():d.add_paragraph(line.strip(),'LegalSignature')
        d.add_page_break()
        d.add_paragraph('Источники и чек-лист проверки','LegalHeading')
        warn='Подготовлено агентом Юрием как рабочий черновик ('+doc_label+'). Проверьте факты, реквизиты, актуальную редакцию норм РФ и судебную практику перед подписанием или подачей.'
        if placeholders:warn+=' Содержит места для уточнения [УТОЧНИТЬ]: '+', '.join(placeholders[:8])+'.'
        d.add_paragraph(warn,'LegalService')
        src_lines=source_context(sources[:8],review) if sources else ['Документ сформирован по материалам проекта и правовому анализу агента. Проверьте первоисточники вручную.']
        for line in src_lines:
            d.add_paragraph(xml_text(line),'LegalService')
        d.add_paragraph('Чек-лист юриста','LegalHeading')
        for item in ('Заполнить все поля [УТОЧНИТЬ] и проверить реквизиты сторон.','Сверить ссылки на статьи кодексов РФ, постановления Пленума ВС РФ и судебные акты.','Проверить форматирование в текстовом редакторе и снять пометку «ЧЕРНОВИК» после вычитки.'):
            d.add_paragraph('☐ '+item,'LegalService')
        if review:
            d.add_paragraph('Рабочий след сверки','LegalHeading')
            d.add_paragraph('Свободный юридический документ: '+doc_kind+' · '+VERSION,'LegalService')
        set_profile(d,prof)
        d.core_properties.author='Юрий';d.core_properties.last_modified_by='Юрий'
        d.core_properties.created=datetime.now(timezone.utc);d.core_properties.modified=datetime.now(timezone.utc)
        d.core_properties.comments='Черновик агента Юрия. Требует проверки юристом.'
        buf=io.BytesIO();d.save(buf);outputs.append(buf.getvalue())

    plain_parts=[heading]
    if subtitle:plain_parts.append(subtitle)
    if requisites:plain_parts.append(requisites)
    for sec in norm_sections:
        plain_parts.append((sec['heading']+'\n' if sec['heading'] else '')+sec['body'])
    if doc_date:plain_parts.append('Дата: '+doc_date)
    if signature:plain_parts.append(signature)
    return {'compact':outputs[0],'review':outputs[1],'text':'\n\n'.join(plain_parts),'docKind':doc_kind,'docTypeLabel':doc_label,'placeholders':placeholders,'profile':prof,'version':VERSION}
