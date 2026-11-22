"""Build bundled trusted templates. Developer utility, never accepts user files."""
from pathlib import Path
import hashlib,json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from docx import Document
from docx.shared import Mm
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from yuriy.legal_drafts import SCHEMAS,DEFAULT_PROFILE,set_profile

root=Path(__file__).resolve().parents[1]/'yuriy'/'templates';root.mkdir(exist_ok=True)
registry={}
for tid,schema in SCHEMAS.items():
    d=Document()
    for name in ('LegalBody','LegalRequisites','LegalHeading','LegalDemand','LegalAttachment','LegalSignature','LegalService'):
        st=d.styles.add_style(name,WD_STYLE_TYPE.PARAGRAPH);st.base_style=d.styles['Normal']
    for ni,name in enumerate(('LegalDemand','LegalAttachment'),40):
        abstract=OxmlElement('w:abstractNum');abstract.set(qn('w:abstractNumId'),str(ni))
        lvl=OxmlElement('w:lvl');lvl.set(qn('w:ilvl'),'0')
        for tag,val in [('start','1'),('numFmt','decimal'),('lvlText','%1.'),('lvlJc','left')]:
            el=OxmlElement('w:'+tag);el.set(qn('w:val'),val);lvl.append(el)
        abstract.append(lvl);d.part.numbering_part.element.append(abstract)
        num=OxmlElement('w:num');num.set(qn('w:numId'),str(ni));ref=OxmlElement('w:abstractNumId');ref.set(qn('w:val'),str(ni));num.append(ref);d.part.numbering_part.element.append(num)
        np=d.styles[name].element.get_or_add_pPr().get_or_add_numPr();np.get_or_add_numId().val=ni;np.get_or_add_ilvl().val=0
    d.sections[0].header.paragraphs[0].text='ЧЕРНОВИК';d.sections[0].header.paragraphs[0].style='LegalService'
    d.add_paragraph('ЧЕРНОВИК','LegalHeading')
    table=d.add_table(rows=1,cols=2);table.autofit=False
    borders=OxmlElement('w:tblBorders')
    for side in ('top','left','bottom','right','insideH','insideV'):
        el=OxmlElement('w:'+side);el.set(qn('w:val'),'nil');borders.append(el)
    table._tbl.tblPr.append(borders)
    cell=table.cell(0,1);cell.paragraphs[0].style='LegalRequisites'
    keys=['recipient_info','sender_info'] if tid=='pretension' else ['court_name','court_address','plaintiff_info','defendant_info','claim_price','state_duty']
    names={'recipient_info':'Кому:','sender_info':'От:','court_name':'В суд:' if tid=='claim_general' else 'В арбитражный суд:','court_address':'Адрес суда:','plaintiff_info':'Истец:','defendant_info':'Ответчик:','claim_price':'Цена иска:','state_duty':'Госпошлина:'}
    for key in keys:
        cell.add_paragraph(names[key],'LegalRequisites')
        cell.add_paragraph('{%p for line in fields.'+key+' %}','LegalRequisites');cell.add_paragraph('{{ line }}','LegalRequisites');cell.add_paragraph('{%p endfor %}','LegalRequisites')
    d.add_paragraph('ПРЕТЕНЗИЯ' if tid=='pretension' else 'ИСКОВОЕ ЗАЯВЛЕНИЕ','LegalHeading')
    d.add_paragraph('{%p for line in fields.subject %}');d.add_paragraph('{{ line }}','LegalHeading');d.add_paragraph('{%p endfor %}')
    labels={'circumstances':'Обстоятельства','grounds':'Основания требований','demands':'Требования','pretrial_info':'Досудебное урегулирование','attachments':'Приложения','document_date':'Дата','signature':'Подпись'}
    for key in [k for k,_ in schema['fields'] if k not in keys and k!='subject']:
        if key=='attachments':
            d.add_paragraph('{%p if calculation %}');d.add_paragraph('Расчёт','LegalHeading');d.add_paragraph('{%p for line in calculation %}');d.add_paragraph('{{ line }}','LegalBody');d.add_paragraph('{%p endfor %}');d.add_paragraph('{%p endif %}')
        d.add_paragraph(labels[key],'LegalHeading')
        style={'demands':'LegalDemand','attachments':'LegalAttachment','signature':'LegalSignature','document_date':'LegalSignature'}.get(key,'LegalBody')
        d.add_paragraph('{%p for line in fields.'+key+' %}');d.add_paragraph('{{ line }}',style);d.add_paragraph('{%p endfor %}')
    d.add_page_break();d.add_paragraph('Источники и чек-лист проверки','LegalHeading');d.add_paragraph('{{ warning }}','LegalService')
    d.add_paragraph('{%p for line in sources %}');d.add_paragraph('{{ line }}','LegalService');d.add_paragraph('{%p endfor %}')
    d.add_paragraph('Чек-лист','LegalHeading');d.add_paragraph('{%p for line in checklist %}');d.add_paragraph('☐ {{ line }}','LegalService');d.add_paragraph('{%p endfor %}')
    d.add_paragraph('{%p if review %}');d.add_paragraph('Рабочий след сверки','LegalHeading');d.add_paragraph('{{ review_info }}','LegalService');d.add_paragraph('{%p endif %}')
    set_profile(d,DEFAULT_PROFILE);path=root/schema['file'];d.save(path);registry[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
(root/'registry.json').write_text(json.dumps(registry,indent=2),'utf-8')
print(registry)
