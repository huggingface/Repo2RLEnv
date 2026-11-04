"""Controller protocol tests; untrusted code is exercised only in remote sandboxes."""

from __future__ import annotations

import pytest

from repo2rlenv.pipelines.recipes.repository.isolated_call import WorkerFailure, decode_value


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b"NaN", b"Infinity", b"1e999", b"{", b"{}{}"])
def test_invalid_worker_values_fail_closed(raw):
    with pytest.raises(WorkerFailure, match="finite JSON"):
        decode_value(raw)


def test_value_is_data_not_a_verdict():
    value = decode_value(b'{"passed":true,"value":0}')
    assert value == {"passed": True, "value": 0}
    assert value["value"] != 42  # The trusted caller still performs the actual assertion.


def test_output_bound_is_enforced_before_parsing():
    with pytest.raises(WorkerFailure, match="byte limit"):
        decode_value(b'"oversized"', limit=3)
