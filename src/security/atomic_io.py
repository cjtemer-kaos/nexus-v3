import json, tempfile, os
from pathlib import Path
def atomic_write_json(path, data):
    p=Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent))
    import os as _os
    try:
        _os.write(fd, json.dumps(data, ensure_ascii=False, indent=2).encode())
        _os.close(fd)
        Path(tmp).replace(p)
    except:
        try: _os.close(fd)
        except: pass
        raise
def atomic_write_text(path, text):
    from pathlib import Path
    import tempfile, os
    pth=Path(path); pth.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(pth.parent))
    try:
        os.write(fd, text.encode() if isinstance(text, str) else text)
        os.close(fd)
        Path(tmp).replace(pth)
    except:
        try: os.close(fd)
        except: pass
        raise
