"""UX-DOCX-01: structural checks, not an Office layout/printing claim."""
import copy,hashlib,io,json,zipfile
from pathlib import Path
import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from test_m2 import service,request,finish
from test_m3 import upload,index
from test_m3b import project,state,confirm
from yuriy.legal_drafts import SCHEMAS,validate,assemble,DEFAULT_PROFILE


def payload(tid='pretension'):
    return {'title':'Претензия — образец','templateId':tid,'fields':{k:'[УТОЧНИТЬ: '+label+']' for k,label in SCHEMAS[tid]['fields']}}
def plan(c,args):return c.post('/api/projects/'+project(c)['id']+'/results/docx-plan',json=args)
def body(raw):
    d=Document(io.BytesIO(raw));return '\n'.join([p.text for p in d.paragraphs]+[c.text for t in d.tables for r in t.rows for c in r.cells])


@pytest.mark.parametrize('tid',list(SCHEMAS))
def test_templates_are_distinct_data_only_and_profile(tid):
    args=payload(tid);args['fields']['circumstances']='# Факты\n**Сумма** и *обстоятельства*\n{{7*7}} {% include "secret" %} <xml> & \\n'
    data=validate(args);out=assemble(data,[]);doc=Document(io.BytesIO(out['compact']));s=doc.sections[0]
    assert '{{7*7}}' in body(out['compact']) and '{% include "secret" %}' in body(out['compact'])
    assert '**' not in body(out['compact']) and '# Факты' not in body(out['compact']) and '<xml> &' in body(out['compact'])
    assert abs(s.page_width.mm-210)<.1 and abs(s.page_height.mm-297)<.1
    for attr,mm in [('left_margin',30),('right_margin',15),('top_margin',20),('bottom_margin',20)]:assert abs(getattr(s,attr).mm-mm)<.1
    for name in ['Normal','LegalBody','LegalHeading','LegalRequisites','LegalDemand','LegalAttachment','LegalSignature','LegalService']:
        st=doc.styles[name];assert st.font.name=='Times New Roman' and st.font.size.pt==14 and st.paragraph_format.line_spacing==1.5
    st=doc.styles['LegalBody'];assert st.paragraph_format.alignment==WD_ALIGN_PARAGRAPH.JUSTIFY and abs(st.paragraph_format.first_line_indent.mm-12.5)<.1
    assert len(doc.tables)==1 and len(doc.tables[0].columns)==2
    assert all(x.get(qn('w:val'))=='nil' for x in doc.tables[0]._tbl.tblPr.find(qn('w:tblBorders')))
    assert 'ЧЕРНОВИК' in doc.sections[0].header.paragraphs[0].text
    text=body(out['compact']);assert '[УТОЧНИТЬ:' in text and 'SHA-256' not in text and 'Чек-лист' in text
    assert doc.element.xpath('.//w:br[@w:type="page"]')
    assert doc.styles['LegalDemand'].element.pPr.numPr.numId.val!=doc.styles['LegalAttachment'].element.pPr.numPr.numId.val
    if tid=='claim_arbitration':assert 'арбитражный суд' in text.lower()
    if tid=='claim_general':assert 'В суд:' in text


@pytest.mark.parametrize('key',['recipient_info','signature','grounds','document_date'])
def test_empty_named_field_blocks_before_template_loading(service,key,monkeypatch):
    c,provider,_=service;args=payload();args['fields'][key]=' '
    def no_assembly(*a,**k):raise AssertionError('Assembly called with missing field')
    monkeypatch.setattr('yuriy.operations.assemble',no_assembly)
    r=plan(c,args);assert r.status_code==400 and dict(SCHEMAS['pretension']['fields'])[key] in r.text
    assert not project(c)['results'] and not provider.calls


@pytest.mark.parametrize('patch',[{'templateId':'universal'},{'fields':{'body':'text'}},{'profile':{'fontSize':100}},{'profile':{'font':'Unknown'}},{'profile':{'extra':0}},{'calculation':{'method':'mixed'}}])
def test_bad_schema_rejected(service,patch):
    c,_,_=service;assert plan(c,{**payload(),**patch}).status_code==400


def test_legacy_unstructured_endpoint_is_rejected(service):
    c,_,_=service;assert plan(c,{'title':'legacy','text':'a complete body'}).status_code==400


def test_profile_is_per_document():
    a=payload();a['profile']={'font':'Arial','fontSize':12,'leftMm':25,'lineSpacing':1.25};out=assemble(validate(a),[]);d=Document(io.BytesIO(out['compact']))
    assert d.styles['LegalBody'].font.name=='Arial' and d.styles['LegalBody'].font.size.pt==12
    assert abs(d.sections[0].left_margin.mm-25)<.1
    assert validate(payload())['profile']==DEFAULT_PROFILE


def test_trusted_template_hash_checked(monkeypatch,tmp_path):
    import yuriy.legal_drafts as ld
    root=ld.TEMPLATE_DIR
    for path in root.iterdir():
        if path.is_file():(tmp_path/path.name).write_bytes(path.read_bytes())
    (tmp_path/SCHEMAS['pretension']['file']).write_bytes(b'user content is not a template')
    monkeypatch.setattr(ld,'TEMPLATE_DIR',tmp_path)
    with pytest.raises(ValueError,match='Контрольная сумма'):assemble(validate(payload()),[])


def test_compact_review_provenance_and_backup_exact(service,tmp_path):
    c,provider,_=service;quote='Уникальная цитата договора {{ secret }}. Полная выдержка.'
    pid,fid=upload(c,'Договор.txt',quote.encode());index(c,pid,fid)
    req=request(c);req.update(documentIds=[fid],documentsConsent=True);c.post('/api/chat',json=req);m=finish(c,req['requestId'])
    args=payload();args['sourceMessageId']=m['id'];args['fields']['circumstances']=m['text'];r=plan(c,args);assert r.status_code==200,r.text;p=r.json()
    assert 'bytes' not in p and 'reviewBytes' not in p
    base='/api/operations/'+p['planId']+'/preview-file'
    compact=c.get(base).content;review=c.get(base+'?format=review').content
    assert quote not in body(compact) and quote in body(review)
    assert 'Договор.txt' in body(compact) and fid not in body(compact)
    assert 'SHA-256' in body(review) and fid in body(review) and 'абз. 1' in body(review)
    assert confirm(c,p).status_code==200 and confirm(c,p).status_code==409
    result=project(c)['results'][0];assert result['jurisdiction']=='Досудебный порядок' and result['role']=='Заявитель'
    assert result['fieldProvenance']['circumstances']['kind']=='source-answer'
    assert result['fieldProvenance']['grounds']['containsPlaceholder']
    url=f'/api/projects/{pid}/results/{result["id"]}/file'
    assert c.get(url).content==compact and c.get(url+'?format=review').content==review
    backup=c.post('/api/backup');assert backup.status_code==200
    with zipfile.ZipFile(io.BytesIO(backup.content)) as z:assert z.read(f'results/{pid}/{result["reviewArtifactId"]}.docx')==review
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert c.get(url+'?format=review').content==review
    changed=state(c);changed['state']['projects'][0]['results'][0]['fieldProvenance']={};assert c.put('/api/state',json=changed).status_code==400
    path=tmp_path/'space'/'projects'/pid/'results'/(result['reviewArtifactId']+'.docx');path.write_bytes(b'tampered')
    assert c.get(url+'?format=review').status_code==409 and c.post('/api/backup').status_code==400


def calc(method):
    return {'method':method,'principal':'10000','basis':'[УТОЧНИТЬ: применимость и основание]','periods':[{'start':'2024-01-01','end':'2024-01-10',**({'annualRatePercent':'10','yearDays':366} if method=='interest395' else {'dailyRatePercent':'0.1'})}]}


@pytest.mark.parametrize('method,expected',[('interest395','27.32'),('contract_penalty','100.00')])
def test_separate_exact_calculations(method,expected):
    args=payload();args['calculation']=calc(method);data=validate(args);assert data['calculation']['total']==expected
    out=body(assemble(data,[])['compact']);assert ('ст. 395' in out)==(method=='interest395')
    assert ('Договорная неустойка' in out)==(method=='contract_penalty')


@pytest.mark.parametrize('change',[{'basis':''},{'periods':[]},{'principal':'NaN'},{'periods':[{'start':'2024-01-01','end':'2024-01-10','dailyRatePercent':'0.1'}]},{'periods':[{'start':'2024-01-01','end':'2024-01-10','annualRatePercent':'10','yearDays':365}]}])
def test_calculations_never_infer_missing_data(change):
    args=payload();args['calculation']={**calc('interest395'),**change}
    with pytest.raises(ValueError):validate(args)


def test_editor_copy_keeps_human_name_not_artifact_name(service,monkeypatch):
    c,_,app=service;p=plan(c,payload()).json();assert confirm(c,p).status_code==200;r=project(c)['results'][0];seen=[]
    def handoff(ws,pid,raw,sha,preview=False,filename=None):seen.append((filename,hashlib.sha256(raw).hexdigest()));return {'requested':False,'reason':'mock'}
    monkeypatch.setattr('yuriy.app.open_word_copy',handoff)
    url=f'/api/projects/{project(c)["id"]}/results/{r["id"]}/open-editor'
    assert c.post(url,json={'confirmed':True,'sha256':r['artifactSha256']}).status_code==200
    assert seen[-1]==(r['filename'],r['artifactSha256'])
    assert c.post(url,json={'confirmed':True,'sha256':r['reviewSha256'],'format':'review'}).status_code==200
    assert seen[-1][0].endswith(' — для сверки.docx') and 'Претензия' in seen[-1][0]


def test_actual_editor_temp_filename(service,monkeypatch):
    # Exercise Windows branch without changing global os.name / pathlib semantics.
    from types import SimpleNamespace
    from yuriy.desktop import open_word_copy
    c,_,app=service;seen=[];raw=b'mock';sha=hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr('yuriy.desktop.os',SimpleNamespace(name='nt',startfile=lambda path,op:seen.append(path)))
    result=open_word_copy(app.state.runtime['workspace'],project(c)['id'],raw,sha,False,'Черновик — Претензия.docx')
    assert result['requested'] and Path(seen[0]).name=='Черновик — Претензия.docx'
    assert Path(seen[0]).read_bytes()==raw


def test_saved_legacy_docx_is_read_without_relayout(service):
    from yuriy.wordfiles import make_draft
    from yuriy.storage import now
    from yuriy.operations import write_new
    c,_,app=service;ws=app.state.runtime['workspace'];data=ws.read();p=data['state']['projects'][0];pid=p['id']
    import uuid
    rid=str(uuid.uuid4());raw=make_draft('Старый результат','Старый универсальный текст',[])
    r={'id':rid,'name':'Старый результат','text':'Старый универсальный текст','created':now(),'format':'docx','filename':'Черновик — Старый результат.docx','artifactSha256':hashlib.sha256(raw).hexdigest(),'generatedBy':'server-m3b'}
    ws._result_file(pid,r);write_new(ws.safe(f'projects/{pid}/results/{rid}.docx'),raw);p['results'].append(r)
    with ws.connect() as db:ws._write(db,data['state'],data['revision']+1)
    url=f'/api/projects/{pid}/results/{rid}/file';assert c.get(url).content==raw
    assert c.get(url+'?format=review').status_code==400
    backup=c.post('/api/backup');assert backup.status_code==200
    assert c.post('/api/restore',content=backup.content,headers={'X-Confirm-Restore':'yes'}).status_code==200
    assert c.get(url).content==raw
