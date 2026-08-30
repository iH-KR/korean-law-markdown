from __future__ import annotations

import csv
from datetime import datetime
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

import collect_law_markdown as collector


LEGACY_MANIFEST_PREFIX = (
    "source_index",
    "status",
    "requested_title",
    "document_type",
    "api_target",
    "api_url_without_oc",
    "law_id_or_rule_id",
    "api_title",
    "issuer_or_rule_type",
    "issue_number",
    "promulgation_or_issue_date",
    "effective_date",
    "raw_xml_path",
    "markdown_path",
    "article_count",
    "appendix_count",
    "appendix_text_count",
    "appendix_pdf_link_count",
    "appendix_file_link_count",
    "appendix_image_link_count",
    "attachment_link_count",
    "has_appendix_text",
    "has_pdf_links",
    "has_hwp_or_file_links",
    "has_image_links",
    "normalized_links_count",
    "error_message",
    "collected_at",
)

PROVENANCE_AND_LINK_COLUMNS = (
    "manifest_schema_version",
    "renderer_version",
    "run_id",
    "source_mode",
    "source_retrieved_at",
    "source_timestamp_basis",
    "processed_at",
    "raw_xml_sha256",
    "raw_xml_size_bytes",
    "markdown_sha256",
    "markdown_size_bytes",
    "embedded_image_reference_count",
    "embedded_image_link_count",
    "unique_normalized_link_count",
    "duplicate_link_count",
    "invalid_link_count",
    "has_attachment_links",
    "has_any_links",
    "has_embedded_image_references",
    "thdcmp_referenced_article_count",
    "thdcmp_rendered_article_count",
    "thdcmp_duplicate_reference_count",
    "normalization_profile",
    "normalization_changed_field_count",
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _manifest_row() -> dict[str, str]:
    rows = _read_rows(collector.MANIFEST_CSV)
    assert len(rows) == 1
    return rows[0]


def _seed_cache(
    api_target: str,
    document_type: str,
    title: str,
    xml_text: str,
) -> Path:
    expected_target, base_name = collector._expected_base_name(document_type, title)
    assert expected_target == api_target
    target_dir = collector._xml_dir_for(api_target)
    target_dir.mkdir(parents=True, exist_ok=True)
    raw_path = target_dir / f"{base_name}.xml"
    raw_path.write_text(xml_text, encoding="utf-8", newline="")
    return raw_path


def _sidecar_path(raw_path: Path) -> Path:
    return raw_path.with_suffix(raw_path.suffix + ".meta.json")


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _assert_utc_iso8601(value: str) -> None:
    assert value
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset().total_seconds() == 0


def _assert_common_artifact_metadata(row: dict[str, str]) -> tuple[Path, Path]:
    raw_path = collector.PROJECT_DIR / Path(row["raw_xml_path"])
    markdown_path = collector.PROJECT_DIR / Path(row["markdown_path"])
    assert raw_path.is_file()
    assert markdown_path.is_file()
    assert row["raw_xml_sha256"] == _sha256(raw_path)
    assert int(row["raw_xml_size_bytes"]) == raw_path.stat().st_size
    assert row["markdown_sha256"] == _sha256(markdown_path)
    assert int(row["markdown_size_bytes"]) == markdown_path.stat().st_size
    _assert_utc_iso8601(row["processed_at"])
    return raw_path, markdown_path


def _fail_on_network(*_args, **_kwargs):
    pytest.fail("cache-only provenance test attempted network access")


def test_manifest_schema_appends_new_columns_without_reordering_legacy_prefix() -> None:
    assert collector.MANIFEST_SCHEMA_VERSION == "4"
    assert collector.RENDERER_VERSION == "4.0"
    assert tuple(collector.MANIFEST_COLUMNS[: len(LEGACY_MANIFEST_PREFIX)]) == (
        LEGACY_MANIFEST_PREFIX
    )
    assert tuple(collector.MANIFEST_COLUMNS[len(LEGACY_MANIFEST_PREFIX) :]) == (
        PROVENANCE_AND_LINK_COLUMNS
    )


def test_fresh_fetch_writes_verified_provenance_sidecar_and_cache_replay_preserves_it(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 법률"
    xml_text = fixture_text("law.xml")
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": title}],
    )
    canary_secret = "provenance-test-secret"
    monkeypatch.setenv("LAW_OC", canary_secret)
    monkeypatch.setattr(
        collector,
        "fetch_xml",
        lambda _url, _logger: collector.FetchResult(
            True,
            xml_text=xml_text,
            http_status=200,
        ),
    )

    assert collector.main(["--input", str(source)]) == 0
    fetched_row = _manifest_row()
    assert fetched_row["manifest_schema_version"] == "4"
    assert fetched_row["renderer_version"] == "4.0"
    assert fetched_row["run_id"]
    assert fetched_row["source_mode"] == "API_FETCH"
    assert fetched_row["source_timestamp_basis"] == "HTTP_FETCH"
    _assert_utc_iso8601(fetched_row["source_retrieved_at"])
    retrieved_at = datetime.fromisoformat(
        fetched_row["source_retrieved_at"].replace("Z", "+00:00")
    )
    processed_at = datetime.fromisoformat(
        fetched_row["processed_at"].replace("Z", "+00:00")
    )
    assert processed_at >= retrieved_at
    raw_path, _ = _assert_common_artifact_metadata(fetched_row)

    assert fetched_row["embedded_image_reference_count"] == "1"
    assert fetched_row["embedded_image_link_count"] == "1"
    assert fetched_row["has_embedded_image_references"] == "Y"
    assert fetched_row["normalized_links_count"] == "4"
    assert fetched_row["unique_normalized_link_count"] == "4"
    assert fetched_row["duplicate_link_count"] == "0"
    assert fetched_row["invalid_link_count"] == "0"
    assert fetched_row["has_any_links"] == "Y"

    sidecar = _sidecar_path(raw_path)
    assert sidecar.is_file()
    sidecar_before = sidecar.read_bytes()
    metadata = json.loads(sidecar_before.decode("utf-8"))
    assert metadata["source_retrieved_at"] == fetched_row["source_retrieved_at"]
    assert metadata["raw_xml_sha256"] == fetched_row["raw_xml_sha256"]
    assert int(metadata["raw_xml_size_bytes"]) == raw_path.stat().st_size
    assert canary_secret not in sidecar_before.decode("utf-8")

    original_retrieved_at = fetched_row["source_retrieved_at"]
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)
    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    cached_row = _manifest_row()
    assert cached_row["source_mode"] == "CACHE"
    assert cached_row["source_timestamp_basis"] == "CACHE_METADATA"
    assert cached_row["source_retrieved_at"] == original_retrieved_at
    assert cached_row["raw_xml_sha256"] == fetched_row["raw_xml_sha256"]
    assert sidecar.read_bytes() == sidecar_before
    _assert_common_artifact_metadata(cached_row)


def test_legacy_cache_does_not_claim_file_mtime_as_source_retrieval_time(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 행정규칙"
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "행정규칙", "명칭": title}],
    )
    raw_path = _seed_cache(
        "admrul", "행정규칙", title, fixture_text("admrul.xml")
    )
    os.utime(raw_path, (946684800, 946684800))  # 2000-01-01 UTC; not provenance.
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["source_mode"] == "CACHE"
    assert row["source_retrieved_at"] == ""
    assert row["source_timestamp_basis"] == "UNKNOWN_LEGACY_CACHE"
    _assert_common_artifact_metadata(row)


def test_cache_metadata_hash_mismatch_is_reported_without_trusting_timestamp(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 법률"
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": title}],
    )
    monkeypatch.setenv("LAW_OC", "hash-mismatch-test-secret")
    monkeypatch.setattr(
        collector,
        "fetch_xml",
        lambda _url, _logger: collector.FetchResult(
            True,
            xml_text=fixture_text("law.xml"),
            http_status=200,
        ),
    )
    assert collector.main(["--input", str(source)]) == 0
    first_row = _manifest_row()
    raw_path = collector.PROJECT_DIR / Path(first_row["raw_xml_path"])
    sidecar = _sidecar_path(raw_path)
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))
    untrusted_timestamp = "2001-02-03T04:05:06Z"
    metadata["source_retrieved_at"] = untrusted_timestamp
    metadata["raw_xml_sha256"] = "0" * 64
    sidecar.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["source_mode"] == "CACHE"
    assert row["source_retrieved_at"] == ""
    assert row["source_timestamp_basis"] == "METADATA_HASH_MISMATCH"
    assert row["raw_xml_sha256"] == _sha256(raw_path)
    markdown_path = collector.PROJECT_DIR / Path(row["markdown_path"])
    assert untrusted_timestamp not in markdown_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "invalid_case",
    ("wrong_identity", "wrong_basis", "future_timestamp", "non_object"),
)
def test_cache_metadata_requires_valid_identity_basis_and_timestamp(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
    invalid_case: str,
) -> None:
    title = "테스트 법률"
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": title}],
    )
    monkeypatch.setenv("LAW_OC", "metadata-boundary-test-secret")
    monkeypatch.setattr(
        collector,
        "fetch_xml",
        lambda _url, _logger: collector.FetchResult(
            True,
            xml_text=fixture_text("law.xml"),
            http_status=200,
        ),
    )
    assert collector.main(["--input", str(source)]) == 0
    first_row = _manifest_row()
    raw_path = collector.PROJECT_DIR / Path(first_row["raw_xml_path"])
    sidecar = _sidecar_path(raw_path)
    metadata = json.loads(sidecar.read_text(encoding="utf-8"))

    if invalid_case == "wrong_identity":
        metadata["api_target"] = "admrul"
        serialized: object = metadata
    elif invalid_case == "wrong_basis":
        metadata["source_timestamp_basis"] = "FORGED"
        serialized = metadata
    elif invalid_case == "future_timestamp":
        metadata["source_retrieved_at"] = "2999-01-01T00:00:00Z"
        serialized = metadata
    else:
        serialized = []

    sidecar.write_text(
        json.dumps(serialized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["source_retrieved_at"] == ""
    assert row["source_timestamp_basis"] == "INVALID_CACHE_METADATA"


def test_multiple_appendix_links_are_preserved_canonicalized_and_counted(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 법률"
    marker = (
        "      <별표서식이미지파일링크>/LSW/image.png"
        "</별표서식이미지파일링크>"
    )
    replacement = "\n".join(
        (
            "      <별표서식이미지파일링크>/LSW/shared.png</별표서식이미지파일링크>",
            "      <별표서식이미지파일링크>//www.law.go.kr/LSW/shared.png</별표서식이미지파일링크>",
            "      <별표서식이미지파일링크>http://www.law.go.kr/LSW/other.png</별표서식이미지파일링크>",
            "      <별표서식이미지파일링크>javascript:alert(1)</별표서식이미지파일링크>",
        )
    )
    xml_text = fixture_text("law.xml").replace(marker, replacement)
    assert replacement in xml_text
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "법령", "명칭": title}],
    )
    _seed_cache("law", "법령", title, xml_text)
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["appendix_pdf_link_count"] == "1"
    assert row["appendix_file_link_count"] == "1"
    assert row["appendix_image_link_count"] == "3"
    assert row["normalized_links_count"] == "6"
    assert row["unique_normalized_link_count"] == "5"
    assert row["duplicate_link_count"] == "1"
    assert row["invalid_link_count"] == "1"
    assert row["has_image_links"] == "Y"
    assert row["has_any_links"] == "Y"

    markdown_path = collector.PROJECT_DIR / Path(row["markdown_path"])
    markdown = markdown_path.read_text(encoding="utf-8")
    shared = "https://www.law.go.kr/LSW/shared.png"
    assert markdown.count(shared) == 1
    assert "https://www.law.go.kr/LSW/other.png" in markdown
    assert "http://www.law.go.kr" not in markdown
    assert "javascript:" not in markdown


def test_multiple_administrative_rule_attachments_count_only_valid_links(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 행정규칙"
    old_block = """  <첨부파일단위>
    <첨부파일명><![CDATA[붙임.pdf]]></첨부파일명>
    <첨부파일링크>/LSW/attach.pdf</첨부파일링크>
  </첨부파일단위>"""
    new_block = """  <첨부파일>
    <첨부파일명><![CDATA[A.pdf]]></첨부파일명>
    <첨부파일링크>http://www.law.go.kr/LSW/a.pdf</첨부파일링크>
    <첨부파일명><![CDATA[B.pdf]]></첨부파일명>
    <첨부파일링크>/LSW/b.pdf</첨부파일링크>
    <첨부파일명><![CDATA[위험한 링크]]></첨부파일명>
    <첨부파일링크>data:text/plain,blocked</첨부파일링크>
    <첨부파일명><![CDATA[이름만 있는 첨부]]></첨부파일명>
  </첨부파일>"""
    xml_text = fixture_text("admrul.xml").replace(old_block, new_block)
    assert new_block in xml_text
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "행정규칙", "명칭": title}],
    )
    _seed_cache("admrul", "행정규칙", title, xml_text)
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["attachment_link_count"] == "2"
    assert row["normalized_links_count"] == "2"
    assert row["unique_normalized_link_count"] == "2"
    assert row["duplicate_link_count"] == "0"
    assert row["invalid_link_count"] == "1"
    assert row["has_attachment_links"] == "Y"
    assert row["has_any_links"] == "Y"

    markdown_path = collector.PROJECT_DIR / Path(row["markdown_path"])
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "https://www.law.go.kr/LSW/a.pdf" in markdown
    assert "https://www.law.go.kr/LSW/b.pdf" in markdown
    assert "http://www.law.go.kr" not in markdown
    assert "data:text/plain" not in markdown


def test_thdcmp_referenced_articles_are_not_reported_as_attachment_links(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 법률"
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "3단비교", "명칭": title}],
    )
    _seed_cache("thdCmp", "3단비교", title, fixture_text("thdcmp.xml"))
    monkeypatch.setattr(collector.requests, "get", _fail_on_network)

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _manifest_row()
    assert row["article_count"] == "1"
    assert row["thdcmp_referenced_article_count"] == "2"
    assert row["attachment_link_count"] == "0"
    assert row["normalized_links_count"] == "0"
    assert row["unique_normalized_link_count"] == "0"
    assert row["duplicate_link_count"] == "0"
    assert row["invalid_link_count"] == "0"
    assert row["has_attachment_links"] == "N"
    assert row["has_any_links"] == "N"
    assert row["embedded_image_reference_count"] == "0"
    assert row["embedded_image_link_count"] == "0"
    assert row["has_embedded_image_references"] == "N"
    assert row["thdcmp_rendered_article_count"] == "2"
    assert row["thdcmp_duplicate_reference_count"] == "0"


def test_thdcmp_reference_occurrences_are_separate_from_deduplicated_rendering(
    fixture_text,
) -> None:
    parsed = collector.parse_thdcmp_xml(fixture_text("thdcmp.xml"))
    duplicate = dict(parsed["articles"][0]["시행령조문"][0])
    parsed["articles"][0]["시행령조문"].append(duplicate)

    markdown, counts = collector.render_thdcmp_markdown(
        parsed, "2026-08-30 12:34:56", "테스트 법률"
    )

    assert counts["thdcmp_referenced_article_count"] == 3
    assert counts["thdcmp_rendered_article_count"] == 2
    assert counts["thdcmp_duplicate_reference_count"] == 1
    assert markdown.count("### 시행령 제") == 1


def test_link_normalization_rejects_malformed_urls_and_drops_upgraded_default_port(
) -> None:
    assert collector.normalize_law_link("http://[broken") == ""
    for invalid in (
        "http://exa mple.com/x",
        "https://www.law.go.kr/a b.pdf",
        r"https://www.law.go.kr\LSW\a.pdf",
        "https://www.law.go.kr/LSW/%zz.pdf",
    ):
        assert collector.normalize_law_link(invalid) == ""
    assert collector.normalize_law_link("http://www.law.go.kr:80/LSW/a") == (
        "https://www.law.go.kr/LSW/a"
    )
    assert collector.normalize_law_link("www.law.go.kr/LSW/a") == (
        "https://www.law.go.kr/LSW/a"
    )
