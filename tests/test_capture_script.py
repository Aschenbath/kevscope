"""A failed image build must stop before QEMU and restore the user's snippets."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != 'posix', reason='capture.sh runs on a Linux/WSL host')


@pytest.mark.parametrize('build_body,limit', [('exit 42', '30'), ('sleep 5', '1')])
def test_build_failure_or_timeout_restores_snippets_and_does_not_boot(tmp_path, build_body, limit):
    tools = tmp_path / 'bin'
    tools.mkdir()
    vm = tmp_path / 'vm'
    snippets = vm / 'local/snippets'
    snippets.mkdir(parents=True)
    originals = {'data_files.custom': 'original data\n', 'post_start.custom': 'original startup\n'}
    for name, body in originals.items():
        (snippets / name).write_text(body)
    marker = tmp_path / 'qemu-started'
    for name, body in {'qcc': 'exit 0', 'mkqnximage': build_body,
                       'qemu-system-x86_64': 'touch "$KEVSCOPE_TEST_QEMU_MARKER"\nexit 1'}.items():
        path = tools / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    sdk = tmp_path / 'sdk.sh'
    sdk.write_text('# test environment\n')
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'QNX_SDP_ENV': str(sdk), 'KEVSCOPE_VM': str(vm), 'KEVSCOPE_BUILD_TIMEOUT': limit,
           'KEVSCOPE_TEST_QEMU_MARKER': str(marker)}
    result = subprocess.run(['bash', str(ROOT / 'demo/run-capture.sh'), str(tmp_path / 'output')],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 1
    assert 'QEMU was not started' in result.stderr
    assert not marker.exists() and not (vm / '.kevscope-capture.lock').exists()
    for name, body in originals.items():
        assert (snippets / name).read_text() == body
    assert not list((tmp_path / 'output').glob('.kevscope-run.*'))
