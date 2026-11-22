"""Russian legal search & local reference library (codes, Supreme Court plenum rulings, judicial practice).

Combines:
1. Local workspace legal library (`.yuriy/legal-kb.sqlite3` FTS5) — user-imported codes, plenums, court rulings.
2. Optional search across official / open Russian legal domains over HTTPS with strict host allowlist and public DNS check.
"""
from __future__ import annotations
import html
import json
import re
import threading
import urllib.parse
import uuid
from pathlib import Path
import httpx
from .models import check_public_dns
from .storage import db_connect, ident, now, text

ALLOWED_LEGAL_DOMAINS = (
    'pravo.gov.ru',
    'publication.pravo.gov.ru',
    'vsrf.ru',
    'supcourt.ru',
    'sudact.ru',
    'consultant.ru',
    'garant.ru',
    'arbitr.ru',
    'kad.arbitr.ru',
    'ksrf.ru',
)

SEARCH_ENDPOINT = 'https://html.duckduckgo.com/html/'

BUILTIN_NOTES = [
    {
        'id': 'builtin-gk-309-310',
        'title': 'ГК РФ ст. 309, 310 — Общие положения об исполнении обязательств',
        'category': 'Кодексы РФ',
        'citation': 'Гражданский кодекс РФ (часть первая), ст. 309, 310',
        'body': (
            'Статья 309 ГК РФ: Обязательства должны исполняться надлежащим образом в соответствии с условиями обязательства '
            'и требованиями закона, иных правовых актов, а при отсутствии таких условий и требований — в соответствии с обычаями '
            'или иными обычно предъявляемыми требованиями.\n'
            'Статья 310 ГК РФ: Односторонний отказ от исполнения обязательства и одностороннее изменение его условий не допускаются, '
            'за исключением случаев, предусмотренных ГК РФ, другими законами или иными правовыми актами, либо договором в предпринимательских отношениях.'
        ),
    },
    {
        'id': 'builtin-gk-330-333',
        'title': 'ГК РФ ст. 330, 333 и Пленум ВС РФ № 7 от 24.03.2016 — Неустойка и её снижение',
        'category': 'Пленумы ВС РФ и ГК РФ',
        'citation': 'ГК РФ ст. 330, 333; Постановление Пленума ВС РФ от 24.03.2016 № 7 (п. 69–81)',
        'body': (
            'Статья 330 ГК РФ: Неустойкой (штрафом, пеней) признается определенная законом или договором денежная сумма, '
            'которую должник обязан уплатить кредитору в случае неисполнения или ненадлежащего исполнения обязательства. '
            'Соглашение о неустойке должно быть совершено в письменной форме (ст. 331 ГК РФ).\n'
            'Статья 333 ГК РФ и Пленум ВС РФ № 7: Если подлежащая уплате неустойка явно несоразмерна последствиям нарушения обязательства, '
            'суд вправе уменьшить неустойку; для коммерческих субъектов снижение допускается по обоснованному заявлению должника в исключительных случаях.'
        ),
    },
    {
        'id': 'builtin-gk-395',
        'title': 'ГК РФ ст. 395 — Проценты за пользование чужими денежными средствами',
        'category': 'Пленумы ВС РФ и ГК РФ',
        'citation': 'ГК РФ ст. 395; Постановление Пленума ВС РФ от 24.03.2016 № 7 (п. 37–57)',
        'body': (
            'Статья 395 ГК РФ: В случаях неправомерного удержания денежных средств, уклонения от их возврата, иной просрочки в их уплате '
            'подлежат уплате проценты на сумму долга. Размер процентов определяется ключевой ставкой Банка России, действовавшей в соответствующие периоды, '
            'если иной размер процентов не установлен законом или договором. Если договором предусмотрена зачётная неустойка за просрочку платежа (п. 4 ст. 395 ГК РФ), '
            'проценты по ст. 395 ГК РФ за тот же период по общему правилу одновременно не взыскиваются, если иное прямо не предусмотрено законом или договором.'
        ),
    },
    {
        'id': 'builtin-gk-196-200',
        'title': 'ГК РФ ст. 196, 199, 200, 202 — Исковая давность и претензионный порядок',
        'category': 'Пленумы ВС РФ и ГК РФ',
        'citation': 'ГК РФ ст. 196, 199, 200, 202; Постановление Пленума ВС РФ от 29.09.2015 № 43',
        'body': (
            'Общий срок исковой давности составляет три года со дня, определяемого в соответствии со ст. 200 ГК РФ (п. 1 ст. 196 ГК РФ), '
            'но не может превышать десять лет со дня нарушения права (п. 2 ст. 196 ГК РФ). '
            'При соблюдении обязательного досудебного (претензионного) порядка течение срока исковой давности приостанавливается на срок фактического соблюдения такого порядка (п. 3 ст. 202 ГК РФ).'
        ),
    },
    {
        'id': 'builtin-apk-pretrial',
        'title': 'АПК РФ ч. 5 ст. 4 и Постановление Пленума ВС РФ № 18 от 22.06.2021 — Досудебный порядок в арбитраже',
        'category': 'Процессуальное право и Пленумы ВС РФ',
        'citation': 'АПК РФ ч. 5 ст. 4, ст. 125–126; Постановление Пленума ВС РФ от 22.06.2021 № 18',
        'body': (
            'Гражданско-правовые споры о взыскании денежных средств по требованиям, возникшим из договоров, других сделок, вследствие неосновательного обогащения, '
            'могут быть переданы на разрешение арбитражного суда после принятия сторонами мер по досудебному урегулированию по истечении 30 календарных дней '
            'со дня направления претензии (требования), если иные срок и (или) порядок не установлены законом или договором.'
        ),
    },
    {
        'id': 'builtin-gk-432-450',
        'title': 'ГК РФ ст. 432, 431.2, 450, 450.1 — Существенные условия, заверения и расторжение договора',
        'category': 'Кодексы РФ',
        'citation': 'ГК РФ ст. 431.2, 432, 450, 450.1; Постановление Пленума ВС РФ от 25.12.2018 № 49',
        'body': (
            'Договор считается заключенным, если между сторонами достигнуто соглашение по всем существенным условиям договора (ст. 432 ГК РФ): '
            'предмет договора, условия, названные в законе как существенные (например, срок в договоре подряда ст. 708 ГК РФ, цена в продаже недвижимости ст. 555 ГК РФ), '
            'а также условия, относительно которых по заявлению одной из сторон должно быть достигнуто соглашение. '
            'Различаются расторжение договора в судебном порядке (ст. 450 ГК РФ) и односторонний внесудебный отказ от договора (ст. 450.1 ГК РФ).'
        ),
    },
]


def _words(value: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r'[\w]{2,40}', value.lower().replace('ё', 'е'), flags=re.UNICODE)))[:16]


def _host_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != 'https' or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip('.')
    return any(host == dom or host.endswith('.' + dom) for dom in ALLOWED_LEGAL_DOMAINS)


def _strip_html(raw_html: str) -> str:
    cleaned = re.sub(r'(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>', ' ', raw_html)
    cleaned = re.sub(r'(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>|</tr>', '\n', cleaned)
    cleaned = re.sub(r'(?is)<[^>]+>', ' ', cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r'[ \t]+', ' ', ln).strip() for ln in cleaned.splitlines()]
    lines = [ln for ln in lines if ln]
    return '\n'.join(lines)


class LegalKnowledgeBase:
    """Workspace-scoped local Russian legal reference DB + optional allowlisted web search."""

    def __init__(self, workspace, transport=None, dns_checker=check_public_dns):
        self.ws = workspace
        self.transport = transport
        self.dns_checker = dns_checker
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS kb_entries(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    citation TEXT NOT NULL,
                    body TEXT NOT NULL,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
                    id UNINDEXED,
                    title,
                    citation,
                    body,
                    tokenize='unicode61'
                );
            ''')
            count = db.execute('SELECT COUNT(*) FROM kb_entries WHERE builtin=1').fetchone()[0]
            if count < len(BUILTIN_NOTES):
                for item in BUILTIN_NOTES:
                    self._upsert(db, item['id'], item['title'], item['category'], item['citation'], item['body'], builtin=1)

    def connect(self):
        path = self.ws.safe('.yuriy/legal-kb-v1.sqlite3')
        return db_connect(path, timeout=15)

    def _upsert(self, db, entry_id: str, title: str, category: str, citation: str, body: str, builtin: int = 0):
        norm = lambda s: s.lower().replace('ё', 'е')
        db.execute(
            'INSERT OR REPLACE INTO kb_entries VALUES(?,?,?,?,?,?,?)',
            (entry_id, title, category, citation, body, builtin, now()),
        )
        db.execute('DELETE FROM kb_fts WHERE id=?', (entry_id,))
        db.execute('INSERT INTO kb_fts VALUES(?,?,?,?)', (entry_id, norm(title), norm(citation), norm(body)))

    def list_entries(self) -> list[dict]:
        with self.lock, self.connect() as db:
            rows = db.execute(
                'SELECT id, title, category, citation, substr(body, 1, 400), length(body), builtin, updated_at '
                'FROM kb_entries ORDER BY builtin ASC, updated_at DESC LIMIT 200'
            ).fetchall()
        return [
            {
                'id': r[0],
                'title': r[1],
                'category': r[2],
                'citation': r[3],
                'preview': r[4],
                'chars': r[5],
                'builtin': bool(r[6]),
                'updatedAt': r[7],
            }
            for r in rows
        ]

    def add_entry(self, args: dict) -> dict:
        if not isinstance(args, dict):
            raise ValueError('Ожидается объект правовой справки.')
        title = text(args.get('title', ''), 300).strip()
        category = text(args.get('category', 'Судебная практика / НПА'), 120).strip() or 'Судебная практика / НПА'
        citation = text(args.get('citation', ''), 300).strip()
        body = text(args.get('body', ''), 100000).strip()
        if not title or not body:
            raise ValueError('Заполните заголовок и текст нормы / судебной практики.')
        entry_id = 'kb-' + uuid.uuid4().hex[:16]
        with self.lock, self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM kb_entries WHERE builtin=0').fetchone()[0]
            if total >= 1000:
                raise ValueError('Локальная правовая база ограничена 1000 пользовательскими записями.')
            self._upsert(db, entry_id, title, category, citation or title, body, builtin=0)
        return {'id': entry_id, 'title': title, 'category': category, 'citation': citation or title, 'chars': len(body)}

    def delete_entry(self, entry_id: str) -> dict:
        ident(entry_id)
        with self.lock, self.connect() as db:
            row = db.execute('SELECT builtin FROM kb_entries WHERE id=?', (entry_id,)).fetchone()
            if not row:
                raise ValueError('Запись в правовой базе не найдена.')
            if row[0]:
                raise ValueError('Встроенные базовые памятки не удаляются.')
            db.execute('DELETE FROM kb_entries WHERE id=?', (entry_id,))
            db.execute('DELETE FROM kb_fts WHERE id=?', (entry_id,))
        return {'deleted': True, 'id': entry_id}

    def search_local(self, query: str, limit: int = 6) -> list[dict]:
        if not isinstance(query, str) or not query.strip():
            return []
        terms = _words(query[:500])
        if not terms:
            return []
        match = ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms)
        limit = max(1, min(int(limit), 12))
        with self.lock, self.connect() as db:
            rows = db.execute(
                'SELECT e.id, e.title, e.category, e.citation, e.body '
                'FROM kb_fts f JOIN kb_entries e ON e.id = f.id '
                'WHERE kb_fts MATCH ? ORDER BY bm25(kb_fts) LIMIT ?',
                (match, limit),
            ).fetchall()
        return [
            {
                'source': 'local_kb',
                'id': r[0],
                'title': r[1],
                'category': r[2],
                'citation': r[3],
                'snippet': r[4][:2500],
            }
            for r in rows
        ]

    async def search_web_ru(self, query: str, limit: int = 5) -> list[dict]:
        """Search open Russian legal resources via HTTPS with strict domain filtering."""
        q = text(query, 300).strip()
        if not q:
            return []
        site_filter = '(site:consultant.ru OR site:garant.ru OR site:sudact.ru OR site:vsrf.ru OR site:pravo.gov.ru)'
        full_q = f'{q} {site_filter}'
        self.dns_checker(SEARCH_ENDPOINT)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 YuriyLegalAgent/0.7',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        }
        hits = []
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=8, read=15, write=10, pool=8),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                resp = await client.get(SEARCH_ENDPOINT, params={'q': full_q}, headers=headers)
                if resp.status_code >= 400:
                    return []
                raw = resp.text[:512000]
                blocks = re.findall(
                    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a[^>]+class="[^"]*result__a|$)',
                    raw,
                    flags=re.I | re.S,
                )
                for href, raw_title, rest in blocks:
                    url = html.unescape(href)
                    if 'uddg=' in url:
                        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                        if qs.get('uddg'):
                            url = qs['uddg'][0]
                    if not _host_allowed(url):
                        continue
                    snip_m = re.search(r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|div|span)>', rest, flags=re.I | re.S)
                    snippet = _strip_html(snip_m.group(1)) if snip_m else ''
                    title_clean = _strip_html(raw_title)
                    if not title_clean:
                        continue
                    hits.append({
                        'source': 'web_ru',
                        'title': title_clean[:240],
                        'url': url[:500],
                        'domain': urllib.parse.urlsplit(url).hostname,
                        'snippet': snippet[:800],
                    })
                    if len(hits) >= max(1, min(int(limit), 8)):
                        break
        except Exception:
            return []
        return hits

    async def fetch_legal_excerpt(self, url: str) -> dict:
        """Fetch an excerpt from an allowlisted open Russian legal URL."""
        if not isinstance(url, str) or not _host_allowed(url):
            raise ValueError('Разрешены только открытые правовые сайты РФ из белого списка (pravo.gov.ru, vsrf.ru, sudact.ru, consultant.ru, garant.ru).')
        self.dns_checker(url)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 YuriyLegalAgent/0.7',
            'Accept-Language': 'ru-RU,ru;q=0.9',
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=8, read=15, write=10, pool=8),
            follow_redirects=True,
            max_redirects=3,
            trust_env=False,
            transport=self.transport,
        ) as client:
            resp = await client.get(url, headers=headers)
            if not _host_allowed(str(resp.url)):
                raise ValueError('Перенаправление вне разрешённых правовых доменов заблокировано.')
            if resp.status_code >= 400:
                return {'url': url, 'ok': False, 'error': f'HTTP {resp.status_code}'}
            body = _strip_html(resp.text[:600000])
            return {'url': str(resp.url), 'ok': True, 'excerpt': body[:5000]}
