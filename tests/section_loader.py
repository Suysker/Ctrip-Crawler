"""Load readable source sections for focused tests before the artifact exists."""
import importlib.util
import pathlib
import sys
import tempfile
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_sections(*names):
    name = 'crawler_test_' + uuid.uuid4().hex
    directory = pathlib.Path(tempfile.mkdtemp(prefix='ctrip-test-'))
    path = directory / (name + '.py')
    path.write_text('\n\n'.join(
        (ROOT / 'source_sections' / (part + '.py.inc')).read_text(encoding='utf-8')
        for part in names
    ), encoding='utf-8')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
