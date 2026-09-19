"""Public API contract; production-derived narrative omitted."""
import os

from exitmgr import flex_ingest


def test_the_archive_directory_is_redirectable():
    """Public API contract; production-derived narrative omitted."""
    import inspect
    src = inspect.getsource(flex_ingest.archive_statement_xml)
    assert "EXITMGR_FLEX_ARCHIVE" in src, (
        "archive_statement_xml must honour EXITMGR_FLEX_ARCHIVE, or the suite writes into "
        "~/flex-archive again")


def test_the_suite_is_pointed_away_from_the_real_archive():
    """Public API contract; production-derived narrative omitted."""
    val = os.environ.get("EXITMGR_FLEX_ARCHIVE")
    assert val, "EXITMGR_FLEX_ARCHIVE is unset -- conftest isolation is not in effect"
    assert os.path.expanduser("~/flex-archive") not in val, (
        "the suite is pointed AT the production archive: %r" % val)


def test_archiving_actually_lands_in_the_redirected_directory(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    dest = tmp_path / "arch"
    monkeypatch.setenv("EXITMGR_FLEX_ARCHIVE", str(dest))
    path = flex_ingest.archive_statement_xml("<FlexQueryResponse/>")
    assert path, "archiving returned nothing"
    assert str(dest) in path, "wrote to %r, not the redirected dir" % path
    assert os.path.exists(path)
    assert os.path.expanduser("~/flex-archive") not in path


def test_archiving_still_never_raises(tmp_path, monkeypatch):
    """Public API contract; production-derived narrative omitted."""
    blocked = tmp_path / "afile"
    blocked.write_text("x")
    monkeypatch.setenv("EXITMGR_FLEX_ARCHIVE", str(blocked / "cannot" / "exist"))
    assert flex_ingest.archive_statement_xml("<x/>") is None
