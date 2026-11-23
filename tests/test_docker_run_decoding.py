import sys

from repo2rlenv.bootstrap.docker import _run


def test_run_replaces_invalid_utf8_instead_of_raising():
    script = "import sys; sys.stdout.buffer.write(b'ok\\xfc\\n'); sys.stderr.buffer.write(b'\\xff')"
    result = _run([sys.executable, "-c", script], timeout=30)
    assert result.exit_code == 0
    assert result.stdout == "ok�\n"
    assert result.stderr == "�"
