"""Explicit namcap integration for trusted recipes and package archives."""

import os
from pathlib import Path
import re
import shutil
import subprocess


def run_namcap(path, timeout=60):
    path = Path(path)
    result = {'path': str(path), 'status': 'PASS', 'findings': []}

    def finding(severity, code, message):
        result['findings'].append({'severity': severity, 'code': code, 'message': path.name + ': ' + message})

    executable = shutil.which('namcap')
    if not executable:
        finding('error', 'namcap.unavailable', 'requested namcap check requires the namcap executable')
        result['status'] = 'MISSING'
        return result
    try:
        native = subprocess.run([executable, '-i', str(path.resolve())], capture_output=True, text=True,
                                timeout=timeout, env={**os.environ, 'LC_ALL': 'C'})
    except subprocess.TimeoutExpired:
        finding('error', 'namcap.timeout', f'namcap exceeded {timeout} seconds')
    except (OSError, ValueError) as error:
        finding('error', 'namcap.unavailable', str(error))
    else:
        result['returncode'] = native.returncode
        for line in (native.stdout + '\n' + native.stderr).splitlines():
            if not line.strip():
                continue
            match = re.match(r'^.+? ([EWI]): (.*)$', line)
            if match:
                severity = {'E': 'error', 'W': 'warning', 'I': 'note'}[match[1]]
                finding(severity, 'namcap.' + severity, match[2])
            else:
                finding('warning', 'namcap.output', line)
        if native.returncode:
            finding('error', 'namcap.execution', f'namcap exited with status {native.returncode}')
    levels = {item['severity'] for item in result['findings']}
    result['status'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'
    return result
