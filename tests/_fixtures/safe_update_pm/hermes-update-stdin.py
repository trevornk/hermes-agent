#!/usr/bin/python3
"""Run stdin under PM's installation-bound runtime, not an obsolete venv.

The first boundary asks the published launcher for its recorded store Python.
The isolated child calls the supported runtime_command(code=...) factory; its
bootstrap owns generation activation/leases. stdin is never consumed by either
resolver and no dependency-manager fallback is provided.
"""
import json
import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(sys.argv[1]).resolve()
    launcher = root / '.hermes/bin/hermes'
    resolved = subprocess.run([str(launcher), '--print-runtime-command'],
                              check=False, stdout=subprocess.PIPE, text=True)
    if resolved.returncode:
        return resolved.returncode
    command = json.loads(resolved.stdout)
    if not isinstance(command, list) or not command or not isinstance(command[0], str):
        raise RuntimeError('invalid installation runtime command')
    interpreter = command[0]
    if not Path(interpreter).is_absolute() or not os.access(interpreter, os.X_OK):
        raise RuntimeError('published runtime interpreter is not executable')
    stdin_code = "sys.argv[0] = '-'; exec(compile(sys.stdin.read(), '<stdin>', 'exec'))"
    resolver = (
        "import os,sys; "
        "os.environ.pop('PYTHONHOME',None); os.environ.pop('PYTHONPATH',None); "
        "os.environ.pop('VIRTUAL_ENV',None); "
        f"sys.path.insert(0,{str(root)!r}); "
        "from pathlib import Path; from hermes_cli._launchers import runtime_command; "
        f"command=runtime_command(Path({str(root)!r}),sys.argv[1:],code={stdin_code!r}); "
        "os.execvpe(command[0],command,os.environ)"
    )
    os.execvpe(interpreter, [interpreter, '-I', '-c', resolver, *sys.argv[2:]], os.environ)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
