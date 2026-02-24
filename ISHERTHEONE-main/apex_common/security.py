from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Tuple

def is_placeholder(value: str) -> bool:
    v = (value or "").strip().lower()
    if not v:
        return True
    placeholders = ("changeme", "example", "your_", "xxx", "fill", "replace")
    return any(p in v for p in placeholders)

def validate_api_credentials(api_key: str, api_secret: str) -> Tuple[bool, str]:
    if not api_key or not api_secret:
        return False, "API key/secret ausentes."
    if is_placeholder(api_key) or is_placeholder(api_secret):
        return False, "API key/secret parecem ser placeholders. Preencha com suas próprias chaves."
    if len(api_key) < 8 or len(api_secret) < 8:
        return False, "API key/secret muito curtas para serem válidas."
    return True, "OK"

def check_env_file_permissions(env_path: str | Path = ".env") -> Tuple[bool, str]:
    p = Path(env_path)
    if not p.exists():
        return True, ".env não encontrado (ok)."
    try:
        mode = stat.S_IMODE(p.stat().st_mode)
        bad = bool(mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH))
        if bad:
            return False, f"Permissões inseguras em {p} (mode {oct(mode)}). Recomendo: chmod 600 {p}"
        return True, f"Permissões ok em {p} (mode {oct(mode)})"
    except Exception as e:
        return False, f"Falha ao verificar permissões do .env: {e}"
