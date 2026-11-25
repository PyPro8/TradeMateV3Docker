"""Test package. Every test process uses a THROWAWAY data folder, never the project's real `data/`.

Reason: the application now keeps persistent safety state (Emergency Stop lock, consecutive-loss record, notifications, daily risk log). A test that presses Emergency
Stop or closes a losing simulated trade through the real routes must not leave a lock or a loss streak in the real folder, where it would block every later test (and,
worse, a real session). The temporary folder is removed when the process exits. Set TRADEMATE_TEST_USE_REAL_DATA=1 to opt out (not recommended).
"""

import atexit
import os
import re
import shutil
import tempfile
from pathlib import Path

if not os.environ.get("TRADEMATE_TEST_USE_REAL_DATA") and not os.environ.get("TRADEMATE_TEST_DATA_ISOLATED"):
    _root = Path(__file__).resolve().parent.parent
    _tmp = tempfile.mkdtemp(prefix="trademate-tests-")
    atexit.register(shutil.rmtree, _tmp, ignore_errors=True)
    _src = Path(os.environ.get("TRADEMATE_CONFIG", _root / "config" / "settings.toml"))
    if _src.is_file():
        _text = _src.read_text(encoding="utf-8")
        _data = (Path(_tmp) / "data").as_posix()
        _new, _n = re.subn(r'(?m)^(\s*data_dir\s*=\s*)"data"\s*$', lambda m: f'{m.group(1)}"{_data}"', _text, count=1)
        if _n:
            _cfg = Path(_tmp) / "settings.toml"
            _cfg.write_text(_new, encoding="utf-8")
            os.environ["TRADEMATE_CONFIG"] = str(_cfg)
            os.environ["TRADEMATE_TEST_DATA_ISOLATED"] = "1"
