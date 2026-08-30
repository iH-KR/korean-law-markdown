from __future__ import annotations

import csv
import logging
import os
import unicodedata
from pathlib import Path
from urllib.parse import quote_plus

import pandas as pd
import pytest
import requests

import collect_law_markdown as collector


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_sensitive_oc_is_redacted_from_console_log_manifest_and_failed_csv(
    isolated_project,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canary = "CANARY_OC_8f3f2d7c_NEVER_PERSIST"
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "민감값 누출 시험법"}],
    )
    monkeypatch.setenv("LAW_OC", canary)

    def fail_request(url: str, **_kwargs):
        # Requests often includes the complete request URL in its exception text.
        raise requests.exceptions.RequestException(f"transport failed for {url}")

    monkeypatch.setattr(collector.requests, "get", fail_request)

    assert collector.main(["--input", str(source), "--verbose"]) == 2

    captured = capsys.readouterr()
    artifacts = [captured.out, captured.err]
    artifacts.extend(
        path.read_text(encoding="utf-8-sig", errors="replace")
        for path in (isolated_project / "output").rglob("*")
        if path.is_file()
    )
    combined = "\n".join(artifacts)
    assert canary not in combined
    assert quote_plus(canary) not in combined
    assert "{LAW_OC}" in combined

    manifest_rows = _read_csv(collector.MANIFEST_CSV)
    failed_rows = _read_csv(collector.FAILED_CSV)
    assert len(manifest_rows) == 1
    assert len(failed_rows) == 1
    assert manifest_rows[0]["status"] == "FAIL"


def test_redact_sensitive_handles_url_plain_text_and_registered_secret() -> None:
    canary = "CANARY_OC_4cd1"
    collector._register_sensitive_value(canary)
    value = (
        f"plain={canary} "
        f"url=https://www.law.go.kr/DRF/lawService.do?target=law&OC={canary}&type=XML"
    )
    redacted = collector.redact_sensitive(value)
    assert canary not in redacted
    assert "OC={LAW_OC}" in redacted


def test_invalid_fetched_xml_never_replaces_last_known_good_cache(
    isolated_project,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "테스트 법률"}],
    )
    cache_path = collector.XML_LAW_DIR / "테스트_법률.xml"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    known_good = fixture_text("law.xml")
    cache_path.write_text(known_good, encoding="utf-8")

    monkeypatch.setenv("LAW_OC", "test-only-secret")
    monkeypatch.setattr(
        collector,
        "fetch_xml",
        lambda _url, _logger: collector.FetchResult(
            True,
            xml_text="<법령><기본정보><법령ID>broken</법령ID>",
            http_status=200,
        ),
    )

    assert collector.main(["--input", str(source)]) == 2
    assert cache_path.read_text(encoding="utf-8") == known_good
    row = _read_csv(collector.MANIFEST_CSV)[0]
    assert row["status"] == "FAIL"
    assert not list(collector.MD_LAW_DIR.glob("*.md"))


def test_atomic_markdown_write_uses_replace_and_leaves_no_temp_file(
    isolated_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        return real_replace(source, destination)

    monkeypatch.setattr(collector.os, "replace", spy_replace)
    target = collector.write_markdown("law", "atomic", "new contents\n")

    assert target.read_text(encoding="utf-8") == "new contents\n"
    assert len(calls) == 1
    assert calls[0][1] == target
    assert calls[0][0] != target
    assert [p for p in target.parent.iterdir() if p != target] == []


@pytest.mark.parametrize("writer_name", ["save_raw_xml", "write_markdown"])
def test_atomic_text_write_failure_preserves_existing_file(
    isolated_project,
    monkeypatch: pytest.MonkeyPatch,
    writer_name: str,
) -> None:
    if writer_name == "save_raw_xml":
        target = collector.XML_LAW_DIR / "preserve.xml"
        call = lambda: collector.save_raw_xml("law", "preserve", "new xml")
    else:
        target = collector.MD_LAW_DIR / "preserve.md"
        call = lambda: collector.write_markdown("law", "preserve", "new markdown")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("known good", encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(collector.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        call()

    assert target.read_text(encoding="utf-8") == "known good"
    assert [p for p in target.parent.iterdir() if p != target] == []


@pytest.mark.parametrize("writer_name", ["manifest", "failed"])
def test_atomic_csv_write_failure_preserves_existing_file(
    isolated_project,
    monkeypatch: pytest.MonkeyPatch,
    writer_name: str,
) -> None:
    target = collector.MANIFEST_CSV if writer_name == "manifest" else collector.FAILED_CSV
    writer = collector.write_manifest if writer_name == "manifest" else collector.write_failed_items
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("known good", encoding="utf-8")

    monkeypatch.setattr(
        collector.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure")),
    )
    with pytest.raises(OSError, match="simulated replace failure"):
        writer([])

    assert target.read_text(encoding="utf-8") == "known good"
    assert [p for p in target.parent.iterdir() if p != target] == []


def test_single_run_lock_rejects_overlap_and_is_reusable_after_release(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "collector.lock"

    with collector.SingleRunLock(lock_path):
        with pytest.raises(collector.AlreadyRunningError):
            with collector.SingleRunLock(lock_path):
                pytest.fail("overlapping collector lock was acquired")

    with collector.SingleRunLock(lock_path):
        assert lock_path.exists()


def test_main_releases_owned_log_file_handlers(
    isolated_project,
    write_targets_csv,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "지원하지않음", "명칭": "테스트"}],
    )

    assert collector.main(["--from-cache", "--input", str(source)]) != 0
    assert logging.getLogger("collect_law").handlers == []


@pytest.mark.parametrize(
    ("rows", "error_fragment"),
    [
        ([], "비어"),
        ([{"문서유형": "법령", "명칭": "   "}], "명칭"),
        ([{"문서유형": "", "명칭": "테스트"}], "문서유형"),
        ([{"문서유형": "기타", "명칭": "테스트"}], "문서유형"),
        (
            [
                {"문서유형": "법령", "명칭": "A/B"},
                {"문서유형": "법령", "명칭": "AB"},
            ],
            "충돌",
        ),
        ([{"문서유형": "법령", "명칭": "CON"}], "예약"),
    ],
)
def test_source_preflight_rejects_invalid_rows(
    rows: list[dict[str, str]], error_fragment: str
) -> None:
    frame = pd.DataFrame(rows, columns=["문서유형", "명칭"])
    with pytest.raises(ValueError, match=error_fragment):
        collector.validate_source_rows(frame)


def test_source_preflight_rejects_nfc_nfd_filename_collision() -> None:
    nfc_title = "가나다 법"
    nfd_title = unicodedata.normalize("NFD", nfc_title)
    assert nfc_title != nfd_title
    frame = pd.DataFrame(
        [
            {"문서유형": "법령", "명칭": nfc_title},
            {"문서유형": "법령", "명칭": nfd_title},
        ]
    )

    with pytest.raises(ValueError, match="충돌"):
        collector.validate_source_rows(frame)


def test_preflight_failure_never_attempts_network(
    isolated_project,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "지원하지않음", "명칭": "테스트"}],
    )
    request_count = 0

    def unexpected_request(*_args, **_kwargs):
        nonlocal request_count
        request_count += 1
        pytest.fail("CSV 사전검증 실패 후 네트워크 호출이 발생했습니다")

    monkeypatch.setenv("LAW_OC", "test-only-secret")
    monkeypatch.setattr(collector.requests, "get", unexpected_request)

    assert collector.main(["--input", str(source)]) != 0
    assert request_count == 0


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"문서유형": "법령", "명칭": ""}],
        [{"문서유형": "", "명칭": "테스트"}],
        [{"문서유형": "unknown", "명칭": "테스트"}],
        [
            {"문서유형": "행정규칙", "명칭": "A:B"},
            {"문서유형": "행정규칙", "명칭": "AB"},
        ],
        [{"문서유형": "3단비교", "명칭": "NUL.txt"}],
    ],
)
def test_invalid_source_is_a_clean_cli_failure(
    isolated_project, write_targets_csv, rows
) -> None:
    source = write_targets_csv(isolated_project / "targets.csv", rows)
    assert collector.main(["--from-cache", "--input", str(source)]) != 0
