from __future__ import annotations

import csv
from pathlib import Path

import pytest

import collect_law_markdown as collector


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _seed_cache(api_target: str, title: str, xml_text: str) -> Path:
    base_name = collector.base_name_for_title(title)
    if api_target == "thdCmp":
        base_name += collector.THDCMP_FILE_SUFFIX
    target_dir = collector._xml_dir_for(api_target)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{base_name}.xml"
    path.write_text(xml_text, encoding="utf-8")
    return path


def test_cache_only_missing_item_returns_nonzero_without_network(
    isolated_project,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "캐시에 없는 법률"}],
    )
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("--from-cache attempted network access"),
    )

    assert collector.main(["--from-cache", "--input", str(source)]) == 2
    rows = _read_csv(collector.MANIFEST_CSV)
    assert len(rows) == 1
    assert rows[0]["status"] == "SKIP_NO_CACHE"
    assert _read_csv(collector.FAILED_CSV) == []


def test_cache_only_cli_succeeds_in_isolated_output_and_manifest_is_consistent(
    isolated_project,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        ("법령", "law", "테스트 법률", "law.xml"),
        ("행정규칙", "admrul", "테스트 행정규칙", "admrul.xml"),
        ("3단비교", "thdCmp", "테스트 법률", "thdcmp.xml"),
    ]
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": dtype, "명칭": title} for dtype, _, title, _ in targets],
    )
    seeded = {
        _seed_cache(api_target, title, fixture_text(fixture_name))
        for _, api_target, title, fixture_name in targets
    }
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("--from-cache attempted network access"),
    )

    assert collector.main(["--from-cache", "--input", str(source)]) == 0

    manifest_rows = _read_csv(collector.MANIFEST_CSV)
    assert len(manifest_rows) == 3
    assert {row["status"] for row in manifest_rows} == {"OK"}
    assert {row["api_target"] for row in manifest_rows} == {"law", "admrul", "thdCmp"}
    assert _read_csv(collector.FAILED_CSV) == []

    raw_paths: list[Path] = []
    markdown_paths: list[Path] = []
    for row in manifest_rows:
        raw = isolated_project / Path(row["raw_xml_path"])
        markdown = isolated_project / Path(row["markdown_path"])
        assert raw.exists() and raw.stat().st_size > 0
        assert markdown.exists() and markdown.stat().st_size > 0
        assert raw in seeded
        assert raw.is_relative_to(isolated_project / "output" / "raw_xml")
        assert markdown.is_relative_to(isolated_project / "output" / "markdown")
        raw_paths.append(raw)
        markdown_paths.append(markdown)

    # No successful row may alias another row's source or deliverable.
    assert len(raw_paths) == len(set(raw_paths))
    assert len(markdown_paths) == len(set(markdown_paths))

    law_markdown = (collector.MD_LAW_DIR / "테스트_법률.md").read_text(encoding="utf-8")
    admrul_markdown = (
        collector.MD_ADMRUL_DIR / "테스트_행정규칙.md"
    ).read_text(encoding="utf-8")
    thdcmp_markdown = (
        collector.MD_THDCMP_DIR
        / f"테스트_법률{collector.THDCMP_FILE_SUFFIX}.md"
    ).read_text(encoding="utf-8")
    assert law_markdown.startswith("# 테스트 법률\n")
    assert admrul_markdown.startswith("# 테스트 행정규칙\n")
    assert thdcmp_markdown.startswith("# 테스트 법률 인용조문 3단비교\n")


def test_empty_markdown_is_never_reported_as_ok(
    isolated_project,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "테스트 법률"}],
    )
    _seed_cache("law", "테스트 법률", fixture_text("law.xml"))

    def write_empty(api_target: str, base_name: str, _md_text: str) -> Path:
        path = collector._md_dir_for(api_target) / f"{base_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        return path

    monkeypatch.setattr(collector, "write_markdown", write_empty)

    assert collector.main(["--from-cache", "--input", str(source)]) == 2
    rows = _read_csv(collector.MANIFEST_CSV)
    assert len(rows) == 1
    assert rows[0]["status"] == "FAIL"
    assert rows[0]["markdown_path"] == ""


def test_corrupt_existing_cache_isolated_as_item_failure_without_traceback(
    isolated_project,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "깨진 캐시 법률"}],
    )
    cache = _seed_cache("law", "깨진 캐시 법률", "<법령><기본정보>")
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("--from-cache attempted network access"),
    )

    assert collector.main(["--from-cache", "--input", str(source)]) == 2
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err
    assert cache.read_text(encoding="utf-8") == "<법령><기본정보>"

    manifest_rows = _read_csv(collector.MANIFEST_CSV)
    failed_rows = _read_csv(collector.FAILED_CSV)
    assert len(manifest_rows) == 1
    assert manifest_rows[0]["status"] == "FAIL"
    assert manifest_rows[0]["markdown_path"] == ""
    assert len(failed_rows) == 1
    assert failed_rows[0]["error_type"] in {"CACHE_READ_FAIL", "PARSE_FAIL"}


def test_fetched_xml_save_failure_never_leaves_ok_or_markdown(
    isolated_project,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": "테스트 법률"}],
    )
    monkeypatch.setenv("LAW_OC", "test-only-secret")
    monkeypatch.setattr(
        collector,
        "fetch_xml",
        lambda _url, _logger: collector.FetchResult(
            True,
            xml_text=fixture_text("law.xml"),
            http_status=200,
        ),
    )
    monkeypatch.setattr(
        collector,
        "save_raw_xml",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("simulated atomic XML save failure")
        ),
    )

    assert collector.main(["--input", str(source)]) == 2
    manifest_rows = _read_csv(collector.MANIFEST_CSV)
    failed_rows = _read_csv(collector.FAILED_CSV)
    assert len(manifest_rows) == 1
    assert manifest_rows[0]["status"] == "FAIL"
    assert manifest_rows[0]["raw_xml_path"] == ""
    assert manifest_rows[0]["markdown_path"] == ""
    assert len(failed_rows) == 1
    assert failed_rows[0]["error_type"] in {"RAW_XML_WRITE_FAIL", "CACHE_WRITE_FAIL"}
    assert not list(collector.MD_LAW_DIR.glob("*.md"))
