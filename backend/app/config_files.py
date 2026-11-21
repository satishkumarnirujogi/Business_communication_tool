"""Edit .env, clinic.yaml and rules.yaml from the admin UI, keeping comments and ordering.

Every write is validated first (a broken file would stop the server), written to a temp file and
swapped in atomically, then the cached settings/rules are reloaded.
"""

from __future__ import annotations

import io
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from .config import ENV_FILE, ConfigError, get_settings, load_clinic, reload_settings

_lock = threading.Lock()


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 120
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError:  # folder not writable (e.g. /app in Docker): write the file itself
        path.write_text(text, encoding="utf-8", newline="\n")
        return
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        try:
            os.replace(tmp, path)
        except OSError:
            # A single bind-mounted file (Docker: ./.env:/app/.env) can't be replaced by a rename;
            # the text was written and validated above, so write it in place instead.
            path.write_text(text, encoding="utf-8", newline="\n")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def reload_all() -> None:
    from .rules import get_rules

    reload_settings()
    get_rules.cache_clear()


# ---- .env -----------------------------------------------------------------------------
_SAFE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]*$")


def _quote(value: str) -> str:
    if _SAFE.match(value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def update_env(values: Mapping[str, str | None], path: Path = ENV_FILE) -> None:
    """Set (or with None: remove) KEY=value lines, keeping every other line and comment as it is."""
    with _lock:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        pending = {k.upper(): v for k, v in values.items()}
        out = []
        for line in lines:
            m = re.match(r"^\s*([A-Z0-9_]+)\s*=", line)
            if m and m.group(1) in pending:
                v = pending.pop(m.group(1))
                if v is not None:
                    out.append(f"{m.group(1)}={_quote(str(v))}")
                continue
            out.append(line)
        new = [f"{k}={_quote(str(v))}" for k, v in pending.items() if v is not None]
        if new:
            if out and out[-1].strip():
                out.append("")
            out.extend(new)
        _atomic_write(path, "\n".join(out) + "\n")
        # Real environment variables beat .env; keep the running process consistent with the file.
        for k, v in values.items():
            if v is None:
                os.environ.pop(k.upper(), None)
            elif k.upper() in os.environ:
                os.environ[k.upper()] = str(v)


# ---- clinic.yaml -----------------------------------------------------------------------
def update_clinic(changes: dict[str, Any]) -> None:
    path = get_settings().clinic_file
    y = _yaml()
    with _lock:
        data = y.load(path.read_text(encoding="utf-8")) if path.exists() else {}
        for key, value in changes.items():
            if isinstance(value, dict):
                section = data.get(key) or {}
                section.update(value)
                data[key] = section
            else:
                data[key] = value
        buf = io.StringIO()
        y.dump(data, buf)
        # Validate with the real loader before swapping the file in.
        tmp = path.with_name(".clinic.check.yaml")
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        try:
            load_clinic(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        _atomic_write(path, buf.getvalue())
    reload_all()


# ---- rules.yaml -----------------------------------------------------------------------------
def read_rules_raw() -> Any:
    return _yaml().load(get_settings().rules_file.read_text(encoding="utf-8"))


def write_rules(data: Any) -> None:
    """Validate a full rules document (as the Rules class would load it) and save it."""
    import yaml as pyyaml

    from .rules import Rules

    buf = io.StringIO()
    _yaml().dump(data, buf)
    text = buf.getvalue()
    try:
        Rules(pyyaml.safe_load(text), get_settings().clinic)
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"These rules are not valid: {e}") from None
    with _lock:
        _atomic_write(get_settings().rules_file, text)
    reload_all()
