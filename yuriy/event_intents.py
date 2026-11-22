"""Conservative Russian event intent/date extraction, no model and no calendar writes."""
from __future__ import annotations
import re
from datetime import date,timedelta
from zoneinfo import ZoneInfo
from .storage import text,strict_date

DAY_PATTERNS=[r'понедельник(?:а|у|ом|е)?',r'вторник(?:а|у|ом|е)?',r'сред(?:а|у|ы|е|ой)',r'четверг(?:а|у|ом|е)?',r'пятниц(?:а|у|ы|е|ей)',r'суббот(?:а|у|ы|е|ой)',r'воскресень(?:е|я|ю|ем|и)']
DAY_NAMES=['понедельник','вторник','среда','четверг','пятница','суббота','воскресенье']
MONTH_NAMES={
    'январ':1,'феврал':2,'март':3,'апрел':4,'ма':5,'июн':6,
    'июл':7,'август':8,'сентябр':9,'октябр':10,'ноябр':11,'декабр':12,
}
MONTH_PATTERN=r'(?:январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[йяе]|июн[ьяе]|июл[ьяе]|август[ае]?|сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе])'
COMMAND=r'(?:напомни(?:те)?|запиши(?:те)?|запланируй(?:те)?|поставь(?:те)?|добавь(?:те)?|внеси(?:те)?|создай(?:те)?)'
TEMPORAL=(
    r'\b(?:сегодня|завтра|послезавтра|утром|дн[её]м|вечером|весь\s+день|на\s+(?:этой|следующей|прошлой)\s+неделе|кажд(?:ый|ую|ое)|еженедельн|ежедневн)\b'
    r'|\b(?:'+('|'.join(DAY_PATTERNS))+r')\b'
    r'|(?<![\d./:-])\d{1,2}[:-]\d{2}(?![\d./:-])'
    r'|(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)(?!\d)'
    r'|\b\d{1,2}\s+'+MONTH_PATTERN+r'\b'
    r'|\bчерез\s+\d+\s*(?:минут|час|дн|сут|недел|месяц)'
    r'|\b(?:календар|расписани|событи|напоминани|встреч|заседани|созвон|звонок)'
)
QUESTION_TAIL=(
    r'(?:мне\s+|нам\s+)?(?:'
    r'что\s+такое|как(?:ой|ая|ое|ие|им|их)?\b|почему\b|зачем\b|когда\s+наступает|где\s+найти|сколько\b|определение\b'
    r'|(?:про\s+|о\s+|об\s+)?(?:стать[юиейя]|норм[уыае]|закон|кодекс|гк\b|апк\b|гпк\b|упк\b|кас\b|нк\b|тк\b|коап\b|правил[ао]|порядок|услови[яйе]|основани[яйе]|признак|поняти[ея]|практик[уиа]|пленум|обзор|срок\s+исковой\s+давност|исков(?:ую|ой)\s+давност|подсудност|госпошлин|ответственност|обязательств|расторжени|неустойк|реквизит|позици[юия])'
    r')'
)
COLON_TIME=re.compile(r'(?<![\d./:-])(\d{1,2}):(\d{2})(?![\d./:-])')
DASH_TIME=re.compile(r'(?<![\d./:-])(?:(\b(?:в|во|к|с|до|около|на)\s+)(\d{1,2})-(\d{2})|(\d{1,2})-(00|05|10|15|20|25|30|35|40|45|50|55))(?![\d./:-])',re.I)


def _month_num(word):
    w=word.lower()
    for prefix,num in MONTH_NAMES.items():
        if w.startswith(prefix):return num
    return None


def is_event_instruction(value):
    """Route direct imperatives, not legal questions, quoted instructions or examples."""
    value=value.strip()
    if not value or value.endswith('?'):return False
    if re.search(r'^[«"“`]|(?:^|\n)\s*>',value) or re.match(r'(?:не\s+|что\s+|как\s+|почему\s+|зачем\s+|объясни|проанализируй|пример|составь|в договоре)',value,re.I):return False
    value=re.sub(r'^юрий\b[\s,!:\-]*','',value,flags=re.I)
    value=re.sub(r'^пожалуйста[\s,]*','',value,flags=re.I)
    cmd=re.match(COMMAND+r'\b',value,re.I)
    if cmd:
        tail=value[cmd.end():].strip(' ,:—-')
        if re.match(QUESTION_TAIL,tail,re.I):return False
        return bool(re.search(TEMPORAL,tail,re.I))
    # A short factual preamble followed by an explicit reminder command.
    return bool(len(value)<=1000 and re.match(r'(?:у меня\b|завтра\b|сегодня\b|послезавтра\b|в\s+(?:'+('|'.join(DAY_PATTERNS))+r')\b)',value,re.I) and re.search(r'\bнапомни(?:те)?\b',value,re.I))


def propose(value,tz,instant):
    value=text(value,5000).strip();days=set();warnings=[];remove=[];invalid=False;candidates=[];partial_date_seen=False
    zones=set()
    for pattern,zt in [(r'\b(?:мск|московск(?:ое|ому|ого)\s+врем(?:я|ени))\b','Europe/Moscow'),(r'\bсамарск(?:ое|ому|ого)\s+врем(?:я|ени)\b','Europe/Samara')]:
        for match in re.finditer(pattern,value,re.I):zones.add(zt);remove.append(match.span())
    for match in re.finditer(r'\b[A-Z][A-Za-z_]+/[A-Za-z_]+(?:/[A-Za-z_]+)?\b',value):
        try:ZoneInfo(match.group())
        except ValueError:continue
        except KeyError:continue
        zones.add(match.group());remove.append(match.span())
    if len(zones)==1:tz=next(iter(zones))
    if len(zones)>1:warnings.append('Указано несколько часовых поясов. Уточните зону и время вручную.')
    today=instant.astimezone(ZoneInfo(tz)).date()
    for m in re.finditer(r'(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{1,2}\.\d{1,2}\.\d{4}(?!\d)',value):
        raw=m.group()
        try:
            day=strict_date(raw) if '-' in raw else date(*map(int,raw.split('.')[::-1]))
        except ValueError:
            raise ValueError('Некорректная календарная дата в исходном тексте.')
        days.add(day.isoformat());remove.append(m.span())
    for m in re.finditer(r'(?<!\d)(\d{1,2})\s+('+MONTH_PATTERN+r')(?:\s+(\d{4})(?:\s*(?:г\.?|года))?)?\b',value,re.I):
        d_num=int(m.group(1));m_num=_month_num(m.group(2));y_str=m.group(3)
        if y_str:
            try:day=date(int(y_str),m_num,d_num)
            except ValueError:raise ValueError('Некорректная календарная дата в исходном тексте.')
            days.add(day.isoformat());remove.append(m.span())
        else:
            partial_date_seen=True
            for cand_year in (today.year,today.year+1):
                try:
                    cand=date(cand_year,m_num,d_num)
                    if cand.isoformat() not in candidates:candidates.append(cand.isoformat())
                except ValueError:pass
            if not candidates:raise ValueError('Некорректная календарная дата в исходном тексте.')
    for pattern,offset in [(r'\bсегодня\b',0),(r'\bзавтра\b',1),(r'\bпослезавтра\b',2)]:
        for m in re.finditer(pattern,value,re.I):days.add((today+timedelta(days=offset)).isoformat());remove.append(m.span())
    for m in re.finditer(r'\bчерез\s+(\d{1,3})\s*(?:календарн\w+\s+|рабоч\w+\s+)?(дн(?:я|ей)|сут(?:ки|ок)|недел(?:ю|и|ь))\b',value,re.I):
        qty=int(m.group(1));unit=m.group(2).lower()
        delta_days=qty*7 if unit.startswith('недел') else qty
        cand=(today+timedelta(days=delta_days)).isoformat()
        if cand not in candidates:candidates.append(cand)
        warnings.append(f'Относительный срок «{m.group(0)}» не вычисляется автоматически: выберите {cand} или уточните дату по правилам календаря.')
    week=list(re.finditer(r'\bна\s+(этой|следующей|прошлой)\s+неделе\b',value,re.I))
    weekdays=[]
    for wd,pattern in enumerate(DAY_PATTERNS):
        for m in re.finditer(r'\b(?:'+pattern+r')\b',value,re.I):weekdays.append((wd,m));remove.append(m.span())
    for m in week:remove.append(m.span())
    if weekdays:
        distinct={wd for wd,_ in weekdays}
        if days:
            if len(days)!=1 or any(date.fromisoformat(d).weekday() not in distinct for d in days) or len(distinct)>1:
                invalid=True;warnings.append('День недели не совпал с датой или указано несколько дней. Уточните дату вручную.')
        elif len(distinct)==1 and len(week)==1:
            wd=next(iter(distinct));offset={'этой':0,'следующей':7,'прошлой':-7}[week[0].group(1).lower()]
            days.add((today-timedelta(days=today.weekday())+timedelta(days=wd+offset)).isoformat())
        elif len(distinct)==1 and re.search(r'\bближайш(?:ий|ую|ее)\b',value,re.I):
            wd=next(iter(distinct));delta=(wd-today.weekday())%7
            if delta==0:
                invalid=True;candidates=[today.isoformat(),(today+timedelta(days=7)).isoformat()];warnings.append('Сегодня тот же день недели. Выберите сегодня или следующую неделю явно.')
            else:days.add((today+timedelta(days=delta)).isoformat())
        else:
            invalid=True
            if len(distinct)==1:
                wd=next(iter(distinct));day=today+timedelta(days=(wd-today.weekday())%7);candidates=[day.isoformat(),(day+timedelta(days=7)).isoformat()]
            warnings.append('День недели без однозначной календарной даты: выберите дату. «Следующая пятница» также требует уточнения; дата не угадана.')
    if re.search(r'\bкажд(?:ый|ую|ое|ую неделю)|еженедельн|ежедневн',value,re.I):
        invalid=True;warnings.append('Повторяющиеся события не поддерживаются в V1. Укажите одну конкретную дату.')
    times=set()
    for m in COLON_TIME.finditer(value):
        h,minute=map(int,m.groups())
        if h>23 or minute>59:raise ValueError('Неверное время в исходном тексте.')
        times.add(f'{h:02}:{minute:02}');remove.append(m.span())
    for m in DASH_TIME.finditer(value):
        if m.group(2) is not None:
            h,minute=int(m.group(2)),int(m.group(3))
            if h>23 or minute>59:raise ValueError('Неверное время в исходном тексте.')
            times.add(f'{h:02}:{minute:02}');remove.append((m.start(2),m.end(3)))
        else:
            h,minute=int(m.group(4)),int(m.group(5))
            if h>23:continue  # Non-prepositional NN-MM > 23 (e.g. office/case 45-00) is not a clock time.
            times.add(f'{h:02}:{minute:02}');remove.append(m.span())
    if len(days)!=1 or invalid:warnings.append('Дата не определена однозначно. Укажите её вручную; год и процессуальный срок не вычисляются автоматически.')
    if len(zones)>1:times=set()
    if len(times)>1:warnings.append('Несколько значений времени: уточните начало и окончание.')
    if not times and not re.search(r'\bвесь день\b',value,re.I):warnings.append('Время не указано. Введите время или явно выберите событие «весь день».')
    if (re.search(r'(?<![\d.])\d{1,2}[./]\d{1,2}(?![.\d])',value) or partial_date_seen) and not days:warnings.append('Дата без года не дополнена предположением.')
    kind='other'
    for pattern,k in [(r'заседани|судебн','hearing'),(r'претензи|процессуальн|обжалован|апелляц|срок','deadline'),(r'отправ|письм','letter'),(r'контроль.*ответ','response'),(r'встреч|звонок','meeting'),(r'задач','task')]:
        if re.search(pattern,value,re.I):kind=k;break
    procedural=bool(re.search(r'процессуальн|обжалован|апелляц|исков.*давност|срок|\d+\s*(?:дней|дня|суток)',value,re.I)) or kind=='deadline'
    if procedural:warnings.append('Черновик срока: проверьте основание, начальное событие, календарь и применимые правила. Соблюдение процессуального срока не гарантируется.')
    # Remove scheduling wrappers, not the activity itself. Full instruction stays in provenance.
    chars=list(value)
    for a,b in remove:chars[a:b]=' '*(b-a)
    title=''.join(chars)
    title=re.sub(r'^\s*юрий\b[\s,!:\-]*','',title,flags=re.I)
    title=re.sub(r'^\s*у\s+меня\b','',title,flags=re.I)
    title=re.sub(r'\b(?:пожалуйста|мне|меня|ближайш(?:ий|ую|ее)|следующ(?:ий|ую|ее)|весь день)\b',' ',title,flags=re.I)
    title=re.sub(COMMAND+r'(?:\s+(?:это\s+)?(?:в\s+календарь|в\s+расписание))?', ' ',title,flags=re.I)
    title=re.sub(r'^\s*(?:событие|напоминание|задачу)\s*[:—-]?','',title,flags=re.I)
    title=re.sub(r'\b(?:в|во|на|к)\s+(?=\s|[,.;:]|$)',' ',title,flags=re.I)
    title=re.sub(r'\s+',' ',title).strip(' ,.;:!—-')
    if len(title)<3 or re.fullmatch(r'(?:на|в|и|о|у|к|напоминание|событие|задача)(?:\s+(?:на|в|и|о|у|к))*',title,re.I):
        title='';warnings.append('Укажите содержательное название события.')
    title=(title[:1].upper()+title[1:])[:200]
    day=next(iter(days)) if len(days)==1 and not invalid else '';tm=next(iter(times)) if len(times)==1 else ''
    if day and day<today.isoformat():warnings.append('Дата находится в прошлом. Проверьте год и дату перед подтверждением.')
    return {'title':title,'date':day,'dateCandidates':candidates,'weekday':DAY_NAMES[date.fromisoformat(day).weekday()] if day else '', 'time':tm,'timezone':tz,'allDay':not bool(tm),'needsTimeChoice':len(times)!=1 and not re.search(r'\bвесь день\b',value,re.I),'type':kind,'note':'','reminders':[],'warnings':warnings,'proceduralDraft':procedural,'referenceDate':today.isoformat(),'referenceInstant':instant.isoformat(),'method':'local-dates-weekdays-v2'}


def proposal_text(proposal):
    p=proposal
    value='Предложение события: '+(p['title'] or '[уточните название]')+'\nДата: '+(p['date']+(' ('+p['weekday']+')' if p['weekday'] else '') if p['date'] else '[уточните дату]')+'\nВремя: '+(p['time'] or ('[уточните время или выберите весь день]' if p['needsTimeChoice'] else 'весь день'))+' · '+p['timezone']
    if p['warnings']:value+='\n'+'\n'.join(p['warnings'])
    return value+'\nЛокальный разбор; внешний API не вызывался. Событие ещё не записано. Проверьте поля и отдельно подтвердите сохранение.'
