"""Offload transfer logic.

The interesting behaviour is what happens when a transfer is interrupted, so
these drive the client with a stubbed network layer rather than a real server:
partial files, repeated failures, and wrong sizes are all easy to arrange and
awkward to provoke against a live service.
"""

from __future__ import annotations

import pytest

from gcs.offload import CompanionClient, CompanionError, OffloadResult, PhotoTransfer


class StubClient(CompanionClient):
    """A client whose downloads are scripted instead of networked."""

    def __init__(self, contents: dict[str, bytes], fail_times: int = 0, truncate_to: int | None = None):
        super().__init__("http://stub")
        self.contents = contents
        self.fail_times = fail_times
        self.truncate_to = truncate_to
        self.requests: list[tuple[str, int]] = []

    def photos(self, session):
        return [{"name": n, "bytes": len(b)} for n, b in self.contents.items()]

    def _download_file(self, path, target, resume_from: int = 0) -> int:
        self.requests.append((path, resume_from))

        if self.fail_times > 0:
            self.fail_times -= 1
            raise CompanionError("simulated network failure")

        name = path.rsplit("/", 1)[-1]
        data = self.contents.get(name)
        if data is None:
            raise CompanionError("not found")

        if self.truncate_to is not None:
            data = data[: self.truncate_to]

        chunk = data[resume_from:]
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("ab" if resume_from else "wb") as handle:
            handle.write(chunk)
        return len(chunk)


PHOTOS = {"IMG_0001.jpg": b"a" * 1000, "IMG_0002.jpg": b"b" * 2000}


class TestFreshDownload:
    def test_downloads_everything(self, tmp_path):
        client = StubClient(dict(PHOTOS))
        result = client.download_session("flight", tmp_path)

        assert result.complete
        assert result.downloaded == 2
        assert result.skipped == 0
        assert result.bytes_transferred == 3000

    def test_writes_files_with_correct_contents(self, tmp_path):
        StubClient(dict(PHOTOS)).download_session("flight", tmp_path)
        images = tmp_path / "flight" / "images"
        assert (images / "IMG_0001.jpg").read_bytes() == b"a" * 1000
        assert (images / "IMG_0002.jpg").read_bytes() == b"b" * 2000


class TestSkipAndResume:
    def test_complete_files_are_skipped(self, tmp_path):
        client = StubClient(dict(PHOTOS))
        client.download_session("flight", tmp_path)

        again = StubClient(dict(PHOTOS))
        result = again.download_session("flight", tmp_path)

        assert result.skipped == 2
        assert result.downloaded == 0
        assert result.bytes_transferred == 0
        # No photo requests at all, only the manifest attempt.
        assert not [r for r in again.requests if "photos" in r[0]]

    def test_partial_file_resumes_from_where_it_stopped(self, tmp_path):
        images = tmp_path / "flight" / "images"
        images.mkdir(parents=True)
        (images / "IMG_0001.jpg").write_bytes(b"a" * 400)

        client = StubClient(dict(PHOTOS))
        result = client.download_session("flight", tmp_path)

        transfer = next(t for t in result.transfers if t.name == "IMG_0001.jpg")
        assert transfer.resumed
        assert transfer.transferred_bytes == 600  # only the missing tail
        assert (images / "IMG_0001.jpg").read_bytes() == b"a" * 1000

    def test_oversized_local_file_is_restarted(self, tmp_path):
        """A local file bigger than the source is corrupt, not resumable."""
        images = tmp_path / "flight" / "images"
        images.mkdir(parents=True)
        (images / "IMG_0001.jpg").write_bytes(b"x" * 5000)

        client = StubClient(dict(PHOTOS))
        result = client.download_session("flight", tmp_path)

        assert result.complete
        assert (images / "IMG_0001.jpg").read_bytes() == b"a" * 1000

    def test_a_second_run_repairs_a_partial_first_run(self, tmp_path):
        broken = StubClient(dict(PHOTOS), truncate_to=300)
        first = broken.download_session("flight", tmp_path)
        assert not first.complete

        good = StubClient(dict(PHOTOS))
        second = good.download_session("flight", tmp_path)
        assert second.complete


class TestFailureHandling:
    def test_transient_failures_are_retried(self, tmp_path):
        client = StubClient(dict(PHOTOS), fail_times=2)
        result = client.download_session("flight", tmp_path, attempts=4)
        assert result.complete

    def test_persistent_failure_is_reported_not_hidden(self, tmp_path):
        client = StubClient(dict(PHOTOS), fail_times=99)
        result = client.download_session("flight", tmp_path, attempts=2)

        assert not result.complete
        assert len(result.failed) == 2
        assert "simulated network failure" in result.failed[0].error

    def test_size_mismatch_is_caught(self, tmp_path):
        """A truncated transfer must not be reported as success."""
        client = StubClient(dict(PHOTOS), truncate_to=100)
        result = client.download_session("flight", tmp_path, attempts=1)

        assert not result.complete
        assert "size mismatch" in result.failed[0].error

    def test_exit_status_reflects_failure(self, tmp_path):
        client = StubClient(dict(PHOTOS), fail_times=99)
        result = client.download_session("flight", tmp_path, attempts=1)
        assert result.failed and not result.complete


class TestSummary:
    def test_summary_counts_each_category(self):
        result = OffloadResult(session="f", destination=".")
        result.transfers = [
            PhotoTransfer("a", 10, transferred_bytes=10),
            PhotoTransfer("b", 10, skipped=True),
            PhotoTransfer("c", 10, error="boom"),
        ]
        summary = result.summary()
        assert "1 downloaded" in summary
        assert "1 already present" in summary
        assert "1 FAILED" in summary

    def test_clean_run_does_not_mention_failures(self):
        result = OffloadResult(session="f", destination=".")
        result.transfers = [PhotoTransfer("a", 10, transferred_bytes=10)]
        assert "FAILED" not in result.summary()


class TestDiscovery:
    def test_returns_none_when_nothing_answers(self):
        assert CompanionClient.discover(candidates=("http://127.0.0.1:1",), timeout=0.2) is None
