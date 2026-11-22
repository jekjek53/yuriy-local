"""User-controlled provider settings; Windows DPAPI secrets never enter workspace backups."""
from __future__ import annotations
import ctypes
import ipaddress
import json
import os
import socket
import threading
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = 'https://sprutdock.ru/v1'

class DpapiSecrets:
    persistent = os.name == 'nt'
    def __init__(self, path):
        self.path = Path(path)
        self._memory = ''

    def _crypt(self, data, decrypt=False):
        if os.name != 'nt': raise ValueError('DPAPI доступен только в Windows.')
        from ctypes import wintypes
        class Blob(ctypes.Structure):
            _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]
        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        target = Blob()
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        if decrypt:
            f = crypt32.CryptUnprotectData
            f.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = f(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
        else:
            f = crypt32.CryptProtectData
            f.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
            ok = f(ctypes.byref(source), 'Yuriy API credential', None, None, None, 1, ctypes.byref(target))
        if not ok: raise ValueError('Не удалось обработать ключ через Windows DPAPI. Повторно введите ключ в этой учётной записи.')
        try: return ctypes.string_at(target.pbData, target.cbData)
        finally: kernel32.LocalFree(ctypes.cast(target.pbData, ctypes.c_void_p))

    def get(self):
        if not self.persistent: return self._memory
        if not self.path.exists(): return ''
        try: return self._crypt(self.path.read_bytes(), decrypt=True).decode('utf-8')
        except (OSError, UnicodeError, ValueError): raise ValueError('Сохранённый ключ недоступен этой учётной записи Windows. Удалите его и введите заново.')

    def set(self, value):
        if not self.persistent:
            self._memory = value
            return
        encrypted = self._crypt(value.encode('utf-8'))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        try:
            temp.write_bytes(encrypted)
            os.replace(temp, self.path)
        finally:
            if temp.exists(): temp.unlink()

    def delete(self):
        self._memory = ''
        self.path.unlink(missing_ok=True)


def endpoint_valid(value):
    if not isinstance(value, str) or len(value) > 1000 or any(c in value for c in '\r\n\t\\'):
        raise ValueError('Некорректный адрес API.')
    value = value.strip().rstrip('/')
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError: raise ValueError('Некорректный адрес API.')
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or port not in (None, 443):
        raise ValueError('API должен использовать HTTPS, порт 443, без логина, пароля и query-параметров в адресе.')
    host = u.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')) or '.' not in host:
        raise ValueError('Локальные адреса API запрещены.')
    try:
        if not ipaddress.ip_address(host).is_global: raise ValueError('Частные IP-адреса API запрещены.')
    except ValueError as exc:
        if 'запрещены' in str(exc): raise
    return value


def check_public_dns(endpoint):
    host = urlsplit(endpoint).hostname
    try: addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError: raise ValueError('Не удалось определить адрес API. Проверьте интернет и endpoint.')
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('API разрешён только на публичных IP-адресах.')


class ModelSettings:
    def __init__(self, directory, secrets=None):
        self.directory = Path(directory)
        self.path = self.directory/'models.json'
        self.secrets = secrets or DpapiSecrets(self.directory/'api-key.dpapi')
        self.lock = threading.RLock()
        self.values = {'endpoint': DEFAULT_ENDPOINT, 'modelB': '', 'modelD': '', 'modelC': '', 'verificationEnabled': False, 'consent': False, 'maxOutputTokens': 1800, 'maxOutputTokensD': None, 'maxOutputTokensC': None}
        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text('utf-8'))
                self.values.update(self._validate(stored))
            except (OSError, ValueError, TypeError):
                raise ValueError('Не удалось прочитать настройки моделей. Сохраните копию models.json и проверьте его содержимое.')

    def _validate(self, data):
        if not isinstance(data, dict): raise ValueError('Ожидается объект настроек.')
        out = dict(self.values)
        if 'endpoint' in data: out['endpoint'] = endpoint_valid(data['endpoint'])
        for name in ('modelB', 'modelD', 'modelC'):
            if name in data:
                v = data[name]
                if not isinstance(v, str) or len(v) > 200 or any(ord(c) < 32 for c in v): raise ValueError('Некорректный ID модели.')
                out[name] = v.strip()
        if 'verificationEnabled' in data:
            if type(data['verificationEnabled']) is not bool:raise ValueError('Неверное разрешение проверки C.')
            out['verificationEnabled']=data['verificationEnabled']
        if 'consent' in data:
            if type(data['consent']) is not bool: raise ValueError('Неверное согласие на передачу данных.')
            out['consent'] = data['consent']
        if 'maxOutputTokens' in data:
            v = data['maxOutputTokens']
            if type(v) is not int or not 128 <= v <= 8192: raise ValueError('Лимит ответа: от 128 до 8192 токенов.')
            out['maxOutputTokens'] = v
        if 'maxOutputTokensD' in data:
            v=data['maxOutputTokensD']
            if v is not None and (type(v) is not int or not 128<=v<=8192):raise ValueError('Лимит D: от 128 до 8192 токенов или пусто для общего лимита.')
            out['maxOutputTokensD']=v
        if 'maxOutputTokensC' in data:
            v=data['maxOutputTokensC']
            if v is not None and (type(v) is not int or not 128<=v<=8192):raise ValueError('Лимит C: 128–8192 или пусто для общего.')
            out['maxOutputTokensC']=v
        return out

    def public(self):
        with self.lock:
            error = None
            try: has_key = bool(self.secrets.get())
            except ValueError as exc: has_key = False; error = str(exc)
            return {**self.values, 'hasKey': has_key, 'keyError': error, 'keyStorage': 'windows-dpapi' if self.secrets.persistent else 'session-only'}

    def save(self, data):
        with self.lock:
            result = self._validate(data)
            changed_host = urlsplit(result['endpoint']).netloc != urlsplit(self.values['endpoint']).netloc
            if changed_host and data.get('confirmEndpoint') is not True:
                raise ValueError('Подтвердите новый хост API: ему будут передаваться ключ и выбранный текст.')
            key = data.get('key', '')
            if not isinstance(key, str) or len(key) > 1000 or any(c.isspace() for c in key): raise ValueError('Некорректный ключ API.')
            self.directory.mkdir(parents=True, exist_ok=True)
            # Only encrypted or in-memory secrets, never plaintext config.
            if data.get('clearKey') is True: self.secrets.delete()
            elif key: self.secrets.set(key)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(result, ensure_ascii=False), 'utf-8')
            os.replace(temp, self.path)
            self.values = result
            return self.public()

    def for_request(self, deep=False, require_model=True, role=None):
        with self.lock:
            if not self.values['consent']: raise ValueError('Разрешите передачу текста переписки и инструкции проекта выбранному API.')
            key = self.secrets.get()
            if not key: raise ValueError('Введите API-ключ в настройках моделей.')
            tier=role or ('D' if deep else 'B')
            if tier not in ('B','C','D'):raise ValueError('Неизвестный маршрут модели.')
            if tier=='C' and not self.values['verificationEnabled']:raise ValueError('Отдельная проверка C не разрешена в настройках.')
            model = self.values['model'+tier]
            if require_model and not model: raise ValueError('Не задана модель '+tier+'.')
            return {**self.values, 'key': key, 'model': model, 'maxOutputTokens': (self.values.get('maxOutputTokens'+tier) if self.values.get('maxOutputTokens'+tier) is not None else self.values['maxOutputTokens'])}
