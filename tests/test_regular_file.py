"""Agent-controlled paths: a FIFO, directory or device is rejected at once instead of blocking."""
import hashlib
import os
from pathlib import Path
import threading

import pytest

from gcl.core import verify
from gcl.core.regular_file import open_regular, read_regular, sha256_regular
from gcl.hooks import transcript_occupancy
from gcl.observe import _telemetry


def finishes(call, timeout=5):
    """Run call in a daemon thread, so a regression that blocks fails instead of hanging the suite."""
    outcome = {}

    def run():
        try:
            outcome["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "blocked on a non-regular file"
    return outcome


@pytest.fixture
def fifo(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    yield path
    try:  # a writer releases any reader that a regression left blocked
        os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


def test_regular_file_is_read_and_hashed(tmp_path):
    path = tmp_path / "evidence.log"
    path.write_bytes(b"line one\nline two\n")
    assert read_regular(path) == b"line one\nline two\n"
    assert sha256_regular(path) == hashlib.sha256(b"line one\nline two\n").hexdigest()
    with open_regular(path, "r", encoding="utf-8") as stream:
        assert stream.readline() == "line one\n"


def test_fifo_directory_and_device_raise_os_error(fifo, tmp_path):
    for path in (fifo, tmp_path, Path(os.devnull)):
        outcome = finishes(lambda: sha256_regular(path))
        assert isinstance(outcome.get("error"), OSError), path


def test_fifo_evidence_is_untrusted_without_blocking_verification(fifo, tmp_path):
    checkpoint = {"verified_results": [{"evidence_path": str(fifo), "sha256": "0" * 64,
                                        "observed_at": 0}]}
    outcome = finishes(lambda: verify.verify_checkpoint(checkpoint, tmp_path, {}, 1, 3600))
    items = {item["check"]: item["ok"] for item in outcome["value"]["items"]}
    assert items["verified_results[0]"] is False


def test_fifo_transcript_is_rejected_without_blocking(fifo):
    assert isinstance(finishes(lambda: transcript_occupancy(str(fifo))).get("error"), OSError)
    telemetry = finishes(lambda: _telemetry("claude", {"transcript_path": str(fifo)}))["value"]
    assert telemetry["quality"] == "UNKNOWN"
