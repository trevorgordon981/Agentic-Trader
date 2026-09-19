import io
from pathlib import Path
import sys
import threading

from exitmgr.log_streams import BoundedRotatingTextStream, install_standard_stream_rotation


def test_service_entrypoint_requires_explicit_wrapper_opt_in(monkeypatch, tmp_path):
    import run_trader

    monkeypatch.delenv("EXITMGR_ROTATE_SERVICE_LOGS", raising=False)
    assert run_trader._install_service_log_rotation("protective") is None

    monkeypatch.setenv("EXITMGR_ROTATE_SERVICE_LOGS", "1")
    monkeypatch.setenv("EXITMGR_APP_LOG_DIR", str(tmp_path))
    installed = run_trader._install_service_log_rotation("protective")
    try:
        assert installed is not None
        assert installed.stdout.path == tmp_path / "exitmgr-protective.log"
        assert installed.stderr.path == tmp_path / "exitmgr-protective.error.log"
        print("rotating stdout")
        sys.stderr.write("rotating stderr\n")
    finally:
        installed.restore()
    assert "rotating stdout" in (tmp_path / "exitmgr-protective.log").read_text()
    assert "rotating stderr" in (tmp_path / "exitmgr-protective.error.log").read_text()


def test_both_service_wrappers_enable_app_owned_rotation():
    root = Path(__file__).resolve().parents[1]
    for wrapper in ("run_trader_service.sh", "run_protective_service.sh"):
        assert "export EXITMGR_ROTATE_SERVICE_LOGS=1" in (root / wrapper).read_text()


def _chronological_bytes(path: Path, backups: int):
    parts = []
    for index in range(backups, 0, -1):
        backup = Path(f"{path}.{index}")
        if backup.exists():
            parts.append(backup.read_bytes())
    if path.exists():
        parts.append(path.read_bytes())
    return b"".join(parts)


def test_rotation_is_bounded_and_byte_exact(tmp_path):
    path = tmp_path / "service.log"
    payload = "".join(f"line-{index:02d}\n" for index in range(20))
    stream = BoundedRotatingTextStream(path, max_bytes=32, backup_count=10)
    stream.write(payload)
    stream.close()

    files = [item for item in tmp_path.iterdir() if item.name.startswith("service.log")]
    assert files
    assert all(item.stat().st_size <= 32 for item in files)
    assert _chronological_bytes(path, 10).decode("utf-8") == payload


def test_rotation_never_splits_a_utf8_codepoint(tmp_path):
    path = tmp_path / "unicode.log"
    payload = "gravy-£-🚀\n" * 12
    stream = BoundedRotatingTextStream(path, max_bytes=17, backup_count=20)
    stream.write(payload)
    stream.close()

    files = [item for item in tmp_path.iterdir() if item.name.startswith("unicode.log")]
    assert all(item.read_bytes().decode("utf-8") is not None for item in files)
    assert _chronological_bytes(path, 20).decode("utf-8") == payload


def test_backup_count_discards_only_oldest_data(tmp_path):
    path = tmp_path / "bounded.log"
    stream = BoundedRotatingTextStream(path, max_bytes=8, backup_count=2)
    stream.write("01234567abcdefghABCDEFGHlast")
    stream.close()

    assert sorted(item.name for item in tmp_path.iterdir()
                  if item.name.startswith("bounded.log")) == [
        "bounded.log", "bounded.log.1", "bounded.log.2"]
    assert _chronological_bytes(path, 2) == b"abcdefghABCDEFGHlast"


def test_threaded_writes_are_not_torn(tmp_path):
    path = tmp_path / "threaded.log"
    stream = BoundedRotatingTextStream(path, max_bytes=4096, backup_count=2)
    expected = {f"thread-{worker}-{index}\n" for worker in range(4) for index in range(50)}

    def _write(worker):
        for index in range(50):
            stream.write(f"thread-{worker}-{index}\n")

    threads = [threading.Thread(target=_write, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stream.close()

    actual = set(_chronological_bytes(path, 2).decode("utf-8").splitlines(keepends=True))
    assert actual == expected


def test_install_keeps_stdout_and_stderr_separate_and_restores(tmp_path, monkeypatch):
    original_out, original_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", original_out)
    monkeypatch.setattr(sys, "stderr", original_err)
    installed = install_standard_stream_rotation(
        tmp_path / "stdout.log", tmp_path / "stderr.log", max_bytes=64, backup_count=2)
    assert installed is not None

    print("ordinary")
    print("problem", file=sys.stderr)
    installed.restore()

    assert (tmp_path / "stdout.log").read_text() == "ordinary\n"
    assert (tmp_path / "stderr.log").read_text() == "problem\n"
    assert sys.stdout is original_out and sys.stderr is original_err


def test_install_failure_leaves_process_streams_unchanged(tmp_path, monkeypatch):
    original_out, original_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", original_out)
    monkeypatch.setattr(sys, "stderr", original_err)
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("x")

    installed = install_standard_stream_rotation(
        not_a_directory / "stdout.log", tmp_path / "stderr.log")

    assert installed is None
    assert sys.stdout is original_out and sys.stderr is original_err
    assert "[log-rotation] disabled:" in original_err.getvalue()


def test_same_path_refuses_without_replacing_either_stream(tmp_path, monkeypatch):
    original_out, original_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", original_out)
    monkeypatch.setattr(sys, "stderr", original_err)
    path = tmp_path / "combined.log"

    installed = install_standard_stream_rotation(path, path)

    assert installed is None
    assert sys.stdout is original_out and sys.stderr is original_err
    assert "paths must differ" in original_err.getvalue()
