from pathlib import Path
import re

path = Path('.github/scripts/one_shot_tenets_hardening.py')
source = path.read_text(encoding='utf-8')
source, count = re.subn(
    r'fake\.write_bytes\(b"MZ" if os\.name == "nt" else b".*?"\)',
    'fake.write_bytes(b"MZ" if os.name == "nt" else b"x")',
    source,
    count=1,
)
if count != 1:
    raise AssertionError(f'expected one ambient fake executable anchor, found {count}')
exec(compile(source, str(path), 'exec'), {'__name__': '__main__', '__file__': str(path)})
