"""Bounded OpenAI-compatible streaming client. No automatic paid retries."""
from __future__ import annotations
import asyncio
import json
import httpx
from .models import check_public_dns

class ProviderError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def http_error(status):
    # Never return raw upstream bodies: they can echo credentials or user input.
    messages = {
        400: ('bad_request', 'API отклонил параметры запроса. Проверьте ID модели и поддерживаемый формат.'),
        401: ('unauthorized', 'API не принял ключ (401). Проверьте ключ в настройках.'),
        402: ('balance', 'Недостаточно средств или требуется оплата (402).'),
        403: ('forbidden', 'API запретил доступ (403). Проверьте права ключа и модели.'),
        404: ('not_found', 'Модель или endpoint не найдены (404).'),
        429: ('rate_limit', 'Лимит запросов или квота API исчерпаны (429). Автоматического повтора не было.'),
    }
    code, msg = messages.get(status, ('upstream', f'Сервис модели вернул HTTP {status}. Автоматического повтора не было.'))
    return ProviderError(code, msg)


class OpenAIProvider:
    def __init__(self, transport=None, dns_checker=check_public_dns):
        self.transport = transport
        self.dns_checker = dns_checker

    async def _check(self, cfg):
        await asyncio.to_thread(self.dns_checker, cfg['endpoint'])

    def _client(self):
        return httpx.AsyncClient(timeout=httpx.Timeout(connect=12, read=60, write=30, pool=12), follow_redirects=False, trust_env=False, transport=self.transport)

    async def models(self, cfg):
        await self._check(cfg)
        try:
            async with self._client() as client:
                async with client.stream('GET', cfg['endpoint']+'/models', headers={'Authorization':'Bearer '+cfg['key']}) as response:
                    if not response.is_success: raise http_error(response.status_code)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 2*1024*1024: raise ProviderError('protocol', 'Список моделей превышает ограничение 2 МБ.')
                    data = json.loads(body)
                    if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                        raise ProviderError('protocol', 'API не вернул стандартный список моделей. Можно задать ID вручную.')
                    ids = []
                    for item in data['data'][:1000]:
                        if isinstance(item, dict) and isinstance(item.get('id'), str) and len(item['id']) <= 200:
                            ids.append(item['id'])
                    return sorted(set(ids))
        except httpx.TimeoutException: raise ProviderError('timeout', 'API не ответил вовремя.')
        except httpx.HTTPError: raise ProviderError('network', 'Ошибка соединения с API. Проверьте интернет и адрес сервиса.')
        except (ValueError, UnicodeError): raise ProviderError('protocol', 'Не удалось разобрать список моделей.')

    async def stream(self, cfg, messages):
        await self._check(cfg)
        payload = {'model': cfg['model'], 'messages': messages, 'stream': True, 'max_tokens': cfg['maxOutputTokens'], 'stream_options': {'include_usage': True}}
        try:
            async with self._client() as client:
                async with client.stream('POST', cfg['endpoint']+'/chat/completions', headers={'Authorization':'Bearer '+cfg['key'], 'Content-Type':'application/json'}, json=payload) as response:
                    if not response.is_success: raise http_error(response.status_code)
                    # Parse bytes incrementally with bounded lines; providers use data: JSON frames.
                    buffer = b''; total = 0; finished = False; emitted_chars=0
                    stats={'frames':0,'contentPartsSeen':False,'terminalMessageSeen':False,'unsupportedContent':False,'reasoningSeen':False,'refusalSeen':False}
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > 4*1024*1024: raise ProviderError('limit', 'Поток API превысил ограничение 4 МБ.')
                        buffer += chunk
                        if len(buffer) > 256*1024: raise ProviderError('protocol', 'Слишком длинный фрагмент потока API.')
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            if not line.startswith(b'data:'): continue
                            raw = line[5:].strip()
                            if raw == b'[DONE]':
                                finished = True
                                break
                            if not raw: continue
                            try: frame = json.loads(raw)
                            except (ValueError, UnicodeError): raise ProviderError('protocol', 'Повреждённый JSON-фрагмент потока API.')
                            if not isinstance(frame, dict): raise ProviderError('protocol', 'Неверный формат потока API.')
                            if frame.get('error'):
                                raise ProviderError('upstream', 'API сообщил об ошибке в потоке. Частичный ответ сохранён.')
                            event={}
                            usage = frame.get('usage')
                            if isinstance(usage, dict):
                                clean = {k: usage[k] for k in ('prompt_tokens','completion_tokens','total_tokens') if type(usage.get(k)) is int and 0 <= usage[k] < 10**9}
                                if clean: event['usage']=clean
                            choices = frame.get('choices')
                            if choices and isinstance(choices,list) and isinstance(choices[0],dict):
                                c=choices[0];delta=c.get('delta')
                                if not isinstance(delta,dict):delta={}
                                content=delta.get('content')
                                if content is None and emitted_chars==0 and isinstance(c.get('message'),dict):
                                    content=c['message'].get('content')
                                    if content:stats['terminalMessageSeen']=True
                                if isinstance(content,list):
                                    stats['contentPartsSeen']=True
                                    texts=[]
                                    for part in content:
                                        if isinstance(part,dict) and part.get('type') in ('text','output_text') and isinstance(part.get('text'),str):texts.append(part['text'])
                                        else:stats['unsupportedContent']=True
                                    content=''.join(texts)
                                elif content is not None and not isinstance(content,str):stats['unsupportedContent']=True
                                if isinstance(content,str) and content:
                                    event['text']=content;emitted_chars+=len(content)
                                if delta.get('reasoning_content') or delta.get('reasoning'):stats['reasoningSeen']=True
                                if delta.get('refusal'):stats['refusalSeen']=True
                                reason=c.get('finish_reason')
                                if reason:event['finishReason']=str(reason)[:40]
                            stats['frames']+=1
                            event['streamDiagnostics']=dict(stats)
                            yield event
                        if finished: break
                    if not finished:
                        raise ProviderError('interrupted', 'Поток завершился без маркера окончания. Ответ отмечен как неполный.')
        except httpx.TimeoutException: raise ProviderError('timeout', 'Таймаут API. Частичный ответ сохранён; автоматического повтора не было.')
        except httpx.HTTPError: raise ProviderError('network', 'Связь с API прервалась. Частичный ответ сохранён; автоматического повтора не было.')
