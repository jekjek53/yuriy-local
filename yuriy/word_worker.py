"""Fixed bounded DOCX editing worker, never an agent-supplied command."""
import json
import sys
from pathlib import Path
from .parser_limits import apply_limits
from .wordfiles import edit_docx


def main():
    try:
        apply_limits()
        args=json.loads(Path(sys.argv[2]).read_text('utf-8'))
        raw,before,after=edit_docx(Path(sys.argv[1]).read_bytes(),args['find'],args['replace'])
        Path(sys.argv[3]).write_bytes(raw)
        result={'ok':True,'before':before,'after':after}
    except ValueError as exc:result={'ok':False,'error':str(exc)[:1000]}
    except Exception:result={'ok':False,'error':'Ошибка безопасной подготовки DOCX; исходный файл не изменён.'}
    Path(sys.argv[4]).write_text(json.dumps(result,ensure_ascii=False),'utf-8')

if __name__=='__main__':main()
