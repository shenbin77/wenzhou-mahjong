"""Read Python 3.11 bytecode metadata without importing or executing target code."""

from __future__ import annotations

import dis
import hashlib
import json
import marshal
import sys
import types
from pathlib import Path


SENSITIVE = ("key", "token", "password", "secret", "cookie", "authorization")


def safe_string(value: str) -> str:
    if any(word in value.lower() for word in SENSITIVE):
        return "<redacted sensitive string>"
    return value


def describe(code: types.CodeType) -> dict:
    constants = []
    children = []
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            children.append(describe(constant))
        elif isinstance(constant, (str, int, float, bool)) or constant is None:
            constants.append(safe_string(constant) if isinstance(constant, str) else constant)
        elif isinstance(constant, (tuple, frozenset)):
            constants.append(repr(constant)[:500])
    instructions = []
    for op in dis.get_instructions(code):
        argument = op.argval
        if isinstance(argument, types.CodeType):
            argument = f"<code:{argument.co_name}>"
        elif isinstance(argument, str):
            argument = safe_string(argument)
        elif not isinstance(argument, (int, float, bool, type(None))):
            argument = repr(argument)[:160]
        instructions.append({"offset": op.offset, "op": op.opname, "arg": argument})
    return {
        "name": code.co_name,
        "first_line": code.co_firstlineno,
        "args": list(code.co_varnames[: code.co_argcount + code.co_kwonlyargcount]),
        "names": [safe_string(name) for name in code.co_names],
        "constants": constants,
        "instructions": instructions,
        "children": children,
    }


def main() -> None:
    source = Path(sys.argv[1])
    target = Path(sys.argv[2])
    data = source.read_bytes()
    if data[:4].hex() != "a70d0d0a":
        raise ValueError("Expected CPython 3.11 pyc magic")
    code = marshal.loads(data[16:])
    if not isinstance(code, types.CodeType):
        raise ValueError("pyc has no code object")
    report = {
        "source": str(source),
        "sha256": hashlib.sha256(data).hexdigest(),
        "python_magic": data[:4].hex(),
        "analysis": describe(code),
        "limit": "Static bytecode inventory; no target code was executed.",
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
