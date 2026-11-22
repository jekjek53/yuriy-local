"""Development-only mock API chat; never used by the release launcher."""
import asyncio
import os
from pathlib import Path
from yuriy.app import create_app
from yuriy.provider import ProviderError

class MockProvider:
    async def models(self,cfg): return ['mock-b','mock-d']
    async def stream(self,cfg,messages):
        content=messages[-1]['content']
        if content=='FAIL':raise ProviderError('unauthorized','Тест: API не принял ключ (401).')
        for part in ['Тестовый ', 'потоковый ', 'ответ. ', '<script>window.pwned=1</script>']:
            yield {'text':part}
            await asyncio.sleep(15 if content=='SLOW' else .25)
        yield {'usage':{'total_tokens':25}}
        yield {'finishReason':'stop'}

def factory():
    base=Path(os.environ.get('YURIY_TEST_RUNTIME', str(Path.home()/'yuriy-m2-browser-runtime')))
    return create_app(config_path=base/'config.json',workspace=base/'space',provider=MockProvider(),preview=True)
