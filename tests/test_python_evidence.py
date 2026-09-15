from repo2rlenv.quality.python_evidence import test_excerpts as excerpts


def test_parametrized_alias_keeps_its_binding_in_author_evidence(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_access.py").write_text(
        "import pytest\nfrom package import get_in, get_lax\n\n"
        '@pytest.mark.parametrize("get", [get_in, get_lax])\n'
        'def test_access(get):\n    assert get({}, ["absent"]) is None\n\n'
        "def test_unrelated():\n    assert False\n"
    )
    evidence = excerpts(tmp_path, ["tests.test_access::test_access[get_in]"])
    text = evidence["tests/test_access.py"]
    assert '@pytest.mark.parametrize("get", [get_in, get_lax])' in text
    assert "def test_access(get)" in text
    assert "def test_unrelated" not in text
