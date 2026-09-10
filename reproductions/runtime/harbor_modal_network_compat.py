"""Strict offline-only Harbor sidecar compatibility for Modal VM kernels.

Harbor 0.20.0 uses nft_fib_inet, absent on this Modal VM kernel. For the
no-network policy only, use a filter chain that drops every non-loopback packet.
Public/allowlist behavior is unchanged; allowlists still require kernel support.
Run in each remote virtual environment that needs offline Harbor execution.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import harbor

path = (
    Path(harbor.__file__).parent
    / "environments/docker/harbor-docker-egress-control-sidecar/bin/network-policy"
)
original = path.read_text()
before = """  deny-all)
    : > "$ALLOWLIST"
    wait_reload
    setup_nftables
    echo "ok: deny all controlled TCP egress"
    ;;"""
after = """  deny-all)
    : > "$ALLOWLIST"
    # Repo2RLenv compatibility: strict offline policy without nft_fib_inet.
    remove_nftables
    nft --file - <<EOF
table inet $NFTABLES_RULESET_NAME {
  chain output {
    type filter hook output priority filter; policy drop;
    ip daddr 127.0.0.11 drop
    oifname "lo" accept
  }
}
EOF
    echo "ok: deny all non-loopback egress"
    ;;"""
if before in original:
    path.write_text(original.replace(before, after))
elif "Repo2RLenv compatibility" in original and "    ip daddr 127.0.0.11 drop" not in original:
    # Prevent Docker's loopback DNS proxy from forwarding queries outside the task.
    path.write_text(
        original.replace(
            '    oifname "lo" accept', '    ip daddr 127.0.0.11 drop\n    oifname "lo" accept'
        )
    )
elif after not in original:
    raise RuntimeError("Harbor sidecar source changed; inspect before adapting")
print(
    json.dumps(
        {
            "file": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "policy": "no-network only: drop all non-loopback packets",
            "reason": "Modal VM lacks nft_fib_inet; original sidecar startup failed",
        }
    )
)
