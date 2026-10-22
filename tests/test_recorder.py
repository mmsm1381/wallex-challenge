"""Tests for the recorder."""

from lagcatcher import recorder


def test_recorder_with_raw_disabled_writes_no_raw_file(tmp_path) -> None:
    """After the disk guard turns raw recording off, raw writes do nothing."""
    # given
    rec = recorder.Recorder(str(tmp_path), levels=10)
    rec.raw_enabled = False

    # when
    rec.book(1.0, "XRPTMN", "ask", [(101.0, 5.0)])
    rec.close()

    # then
    assert list((tmp_path / "raw").iterdir()) == []


def test_recorder_with_raw_enabled_writes_book_file(tmp_path) -> None:
    """By default the recorder writes the raw book stream."""
    # given
    rec = recorder.Recorder(str(tmp_path), levels=10)

    # when
    rec.book(1.0, "XRPTMN", "ask", [(101.0, 5.0)])
    rec.close()

    # then
    assert [p.name for p in (tmp_path / "raw").iterdir()] == ["wallex_book-1970010100.jsonl.gz"]
