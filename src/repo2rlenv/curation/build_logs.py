from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from pathlib import Path

BUILD_LOG_TIMEOUT_SEC = 30
MAX_BUILD_LOG_READ_BYTES = 2_000_000
MAX_BUILD_LOG_BYTES = 256_000
IMAGE_ID = re.compile(r"(?<![A-Za-z0-9_-])im-[A-Za-z0-9]{22}(?![A-Za-z0-9_-])")


def redact_build_log(text: str) -> str:
    """Keep host credential values and credentialed URLs out of saved feedback."""
    secrets = {
        value
        for key, value in os.environ.items()
        if len(value) >= 6
        and any(
            word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY")
        )
    }
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)


async def collect_modal_build_log(error: str, folder: Path) -> bool:
    """Fetch existing failed-image logs only; never build an image or run a shell."""
    if not re.search(r"ImageBuildError|image build.{0,80}fail", error, re.IGNORECASE):
        return False
    matches = list(dict.fromkeys(IMAGE_ID.findall(error)))
    image_id = matches[0] if len(matches) == 1 else None
    process = None
    tail = bytearray()
    consumed = 0
    status = "No unambiguous valid Modal image ID was found in the build error."
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "MODAL_PROFILE",
            "MODAL_ENVIRONMENT",
            "MODAL_TOKEN_ID",
            "MODAL_TOKEN_SECRET",
        }
    }
    env.update(NO_COLOR="1", TERM="dumb", PYTHONUNBUFFERED="1")
    try:
        if image_id is not None:
            async with asyncio.timeout(BUILD_LOG_TIMEOUT_SEC):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "modal",
                    "image",
                    "logs",
                    image_id,
                    "--layers",
                    "1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                )
                while consumed < MAX_BUILD_LOG_READ_BYTES:
                    chunk = await process.stdout.read(
                        min(65536, MAX_BUILD_LOG_READ_BYTES - consumed)
                    )
                    if not chunk:
                        break
                    consumed += len(chunk)
                    tail.extend(chunk)
                    del tail[: max(0, len(tail) - MAX_BUILD_LOG_BYTES)]
                if consumed >= MAX_BUILD_LOG_READ_BYTES:
                    status = f"Read limit reached after {consumed} bytes; output may omit later build errors."
                else:
                    code = await process.wait()
                    status = (
                        "Log retrieval completed."
                        if code == 0
                        else f"Log retrieval exited with code {code}."
                    )
    except TimeoutError:
        status = (
            f"Log retrieval timed out after {BUILD_LOG_TIMEOUT_SEC} seconds; output is incomplete."
        )
    except Exception as exc:
        status = f"Log retrieval failed ({type(exc).__name__}): {redact_build_log(str(exc))[:1500]}"
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            # Drain only after termination so a full stdout pipe cannot keep
            # Process.wait() blocked. Bound cleanup independently of retrieval.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.communicate(), timeout=5)
    content = redact_build_log(tail.decode("utf-8", errors="replace"))
    footer = f"\n\n[Modal build log: image={image_id or 'unavailable'}; {status}]\n"
    available = max(0, MAX_BUILD_LOG_BYTES - len(footer.encode()) - 100)
    data = content.encode()
    omitted = consumed > len(data) or len(data) > available
    content = data[-available:].decode("utf-8", errors="replace") if available else ""
    if omitted:
        footer = "\n[Earlier build-log output omitted; retained bounded tail.]" + footer
    folder.mkdir(parents=True, exist_ok=True)
    saved = (content + footer).encode()[-MAX_BUILD_LOG_BYTES:].decode("utf-8", errors="replace")
    (folder / "build.log").write_text(saved)
    return True
