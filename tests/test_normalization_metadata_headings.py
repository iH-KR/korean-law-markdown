from __future__ import annotations

import copy
import csv
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest
import yaml

import collect_law_markdown as collector


FIXED_TIME = "2026-08-30T03:34:56Z"
UTC_Z_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _frontmatter(markdown: str) -> dict[str, object]:
    match = re.match(
        r"\A---\n\n(.*?)\n\n---\n\n(?=# )",
        markdown,
        re.DOTALL,
    )
    assert match is not None
    loaded: dict[str, object] = {}
    for line in match.group(1).splitlines():
        assert line
        key, separator, value = line.partition(": ")
        assert separator
        assert key not in loaded
        loaded[key] = None if value == "null" else json.loads(value)
    assert yaml.safe_load(match.group(1)) == loaded
    return loaded


def _seed_cache(api_target: str, title: str, xml_text: str) -> Path:
    base_name = collector.base_name_for_title(title)
    if api_target == "thdCmp":
        base_name += collector.THDCMP_FILE_SUFFIX
    path = collector._xml_dir_for(api_target) / f"{base_name}.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml_text, encoding="utf-8")
    return path


def _read_manifest() -> dict[str, str]:
    with collector.MANIFEST_CSV.open(
        "r", encoding="utf-8-sig", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    return rows[0]


def test_search_profile_normalizes_units_and_token_boundaries_without_losing_markers() -> None:
    source = (
        "구경 65 ㎜, 체적 2 ㎥, 속도 3 ㎧, 온도 20 ℃, "
        "복합 80 ㎥/s, 가속 9.8 ㎧2, 변화 2 ℃/min, "
        "말줄임 …, 비율 ５０％, 괄호 （시험）, 범위 ～, 구분 ：, "
        "증축ㆍ개축 ① \uf100 https://example.test/ＦＩＬＥ "
        '<img src="/LSW/ＦＩＬＥ" alt="20 ℃"></img>'
    )
    normalized = collector.normalize_search_text(source)

    assert "65mm" in normalized
    assert "2m3" in normalized
    assert "3m/s" in normalized
    assert "20°C" in normalized
    assert "80m3/s" in normalized
    assert "9.8m/s2" in normalized
    assert "2°C/min" in normalized
    assert "말줄임 ..." in normalized
    assert "비율 50%" in normalized
    assert "괄호 (시험)" in normalized
    assert "범위 ~" in normalized
    assert "구분 :" in normalized
    assert "\\%" not in normalized
    assert "증축·개축" in normalized
    assert "①" in normalized
    assert "\uf100" in normalized
    assert "https://example.test/ＦＩＬＥ" in normalized
    assert '<img src="/LSW/ＦＩＬＥ" alt="20°C"></img>' in normalized
    assert not any(character in normalized for character in "㎜㎥㎧℃ㆍ")
    rendered_image = collector._normalize_legal_text(
        collector.normalize_search_text('<img id="77"></img>')
    )
    assert "flDownload.do?flSeq=77" in rendered_image
    assert "</img>" not in rendered_image
    assert "&lt;/img>" not in rendered_image


def test_normalized_copy_preserves_source_identifiers_dates_and_links() -> None:
    parsed = {
        "법령ID": "０００１２３",
        "시행일자": "２０２６０１０１",
        "첨부파일링크": "https://example.test/ＦＩＬＥ",
        "articles": [{"조문내용": "구경 65 ㎜ㆍ시험", "항번호": "①"}],
    }

    normalized, changed_fields = collector.normalize_parsed_for_search(parsed)

    assert parsed["articles"][0]["조문내용"] == "구경 65 ㎜ㆍ시험"
    assert normalized["법령ID"] == "０００１２３"
    assert normalized["시행일자"] == "２０２６０１０１"
    assert normalized["첨부파일링크"] == "https://example.test/ＦＩＬＥ"
    assert normalized["articles"][0]["조문내용"] == "구경 65mm·시험"
    assert normalized["articles"][0]["항번호"] == "①"
    assert changed_fields == 1


def test_nfkc_search_copy_does_not_activate_markdown_html_or_image_tags() -> None:
    hostile_text = (
        "＃ 새 제목\n"
        "＜script＞alert（1）＜/script＞\n"
        "［누르기］（javascript：alert（1））\n"
        "[혼합](javascript：alert(1))\n"
        "＜img id＝166595713＞\n"
        "　　1) 새 목록이 아닌 원문"
    )
    parsed = {
        "type": "admrul",
        "행정규칙명": "안전성 시험",
        "행정규칙ID": "90001",
        "조문형식여부": "Y",
        "articles": [{"조문내용": hostile_text}],
        "addenda": [],
        "appendices": [],
        "attachments": [],
        "amendments": [],
        "reasons": [],
    }

    markdown, _, _ = collector.render_document(
        parsed, "admrul", FIXED_TIME, "안전성 시험", None
    )

    assert "<script>" not in markdown
    assert not re.search(r"(?<!\\)\[누르기\]\(javascript:", markdown)
    assert not re.search(r"(?<!\\)\[혼합\]\(javascript:", markdown)
    assert "<img" not in markdown.lower()
    assert "flDownload.do?flSeq=166595713" not in markdown
    assert not re.search(r"^#{1,6} 새 제목$", markdown, re.MULTILINE)
    assert "　　1) 새 목록이 아닌 원문" in markdown


@pytest.mark.parametrize(
    (
        "fixture_name",
        "api_target",
        "expected_type",
        "expected_status",
        "expected_authority",
        "expected_effective_date",
    ),
    [
        ("law.xml", "law", "법령", None, "테스트부", "2026-02-01"),
        (
            "admrul.xml",
            "admrul",
            "행정규칙",
            "Y",
            "테스트청",
            "2026-01-03",
        ),
        (
            "thdcmp.xml",
            "thdCmp",
            "3단비교(인용조문, knd=1)",
            None,
            None,
            None,
        ),
    ],
)
def test_frontmatter_is_safe_typed_and_preserves_leading_zero_ids(
    fixture_text,
    fixture_name: str,
    api_target: str,
    expected_type: str,
    expected_status: str | None,
    expected_authority: str | None,
    expected_effective_date: str | None,
) -> None:
    parsed = collector.parse_and_validate_xml(api_target, fixture_text(fixture_name))
    if api_target == "law":
        parsed["부서명"] = "법제과"
        parsed["부서연락처"] = "02-0000-0001"
    elif api_target == "admrul":
        parsed["전화번호"] = "02-0000-0002"
    title = "A: # 제목\n---\nfake: yes\u0085\u2028\u2029"
    provenance = collector.SourceProvenance(
        "CACHE",
        "2026-08-30T03:30:00+00:00",
        "CACHE_METADATA",
        FIXED_TIME,
        "a" * 64,
        123,
    )
    markdown, _, _ = collector.render_document(
        parsed, api_target, FIXED_TIME, title, provenance
    )

    metadata = _frontmatter(markdown)
    assert metadata["YAML스키마버전"] == "2"
    assert metadata["문서유형"] == expected_type
    assert isinstance(metadata["문서ID"], str)
    assert metadata["문서ID"].startswith("0") or api_target == "admrul"
    assert metadata["현행여부"] == expected_status
    assert metadata["소관부처"] == expected_authority
    assert metadata["시행일자"] == expected_effective_date
    assert metadata["원문API수신일시"] == "2026-08-30T03:30:00Z"
    assert metadata["수집일시"] == FIXED_TIME
    assert metadata["마크다운처리일시"] == FIXED_TIME
    assert metadata["텍스트정규화"] == collector.NORMALIZATION_PROFILE
    assert metadata["원문SHA-256"] == "a" * 64
    assert metadata["조회명칭"] == collector.normalize_search_text(title)
    assert metadata["변환기버전"] == "4.0"
    assert {
        "frontmatter_schema_version",
        "title",
        "document_type",
        "api_target",
        "document_id",
        "requested_title",
        "text_normalization",
    }.isdisjoint(metadata)
    assert "fake" not in metadata
    assert "## 문서정보" not in markdown
    assert "- 문서유형:" not in markdown
    assert "- 수집일시:" not in markdown
    assert "- Markdown 처리일시:" not in markdown
    if api_target == "law":
        assert metadata["법령명약칭"] == "테스트법"
        assert metadata["법종구분"] == "법률"
        assert metadata["공포·발령번호"] == "1"
        assert metadata["담당부서"] == "법제과"
        assert metadata["담당부서연락처"] == "02-0000-0001"
    elif api_target == "admrul":
        assert metadata["행정규칙일련번호"] == "2100000000001"
        assert metadata["행정규칙종류"] == "고시"
        assert metadata["제개정구분명"] == "제정"
        assert metadata["조문형식여부"] == "Y"
        assert metadata["공포·발령번호"] == "2026-1"
        assert metadata["담당부서"] == "테스트과"
        assert metadata["담당부서연락처"] == "02-0000-0002"
    else:
        assert metadata["응답명칭"] == "테스트 법률"
        assert metadata["시행령명"] == "테스트 법률 시행령"
        assert metadata["시행령요약정보"].startswith("[대통령령 제2호")
        assert metadata["시행규칙명"] == "테스트 법률 시행규칙"
        assert metadata["시행규칙요약정보"].startswith("[부령 제3호")
        assert metadata["법령요약정보"].startswith("[법률 제1호")
        assert metadata["삼단비교기준"] == "L"
        assert metadata["삼단비교존재여부"] == "Y"
    assert '<a id="' not in markdown
    assert "](#" not in markdown


@pytest.mark.parametrize(
    ("fixture_name", "api_target"),
    [
        ("law.xml", "law"),
        ("admrul.xml", "admrul"),
        ("thdcmp.xml", "thdCmp"),
    ],
)
def test_default_render_is_normalized_and_non_mutating_for_all_document_types(
    fixture_text, fixture_name: str, api_target: str
) -> None:
    parsed = collector.parse_and_validate_xml(api_target, fixture_text(fixture_name))
    requested_title = "조회ㆍ구경 65 ㎜"
    if api_target == "thdCmp":
        parsed["articles"][0]["조내용"] += " 구경 65 ㎜ㆍ시험"
        parsed["법령명"] += "ㆍ구경 65 ㎜"
        parsed["시행령명"] += "ㆍ구경 65 ㎜"
    else:
        parsed["articles"][0]["조문내용"] += " 구경 65 ㎜ㆍ시험"
        if api_target == "law":
            parsed["법령명_한글"] += "ㆍ구경 65 ㎜"
            parsed["법령명약칭"] += "ㆍ구경 65 ㎜"
        else:
            parsed["행정규칙명"] += "ㆍ구경 65 ㎜"
            parsed["행정규칙종류"] += "ㆍ구경 65 ㎜"
    before = copy.deepcopy(parsed)

    markdown, _, changed_fields = collector.render_document(
        parsed, api_target, FIXED_TIME, requested_title, None
    )

    assert parsed == before
    assert "65 ㎜ㆍ시험" not in markdown
    assert "65mm·시험" in markdown
    assert changed_fields > 0
    metadata = _frontmatter(markdown)
    assert metadata["문서ID"] == collector._law_id(before)
    assert metadata["텍스트정규화"] == collector.NORMALIZATION_PROFILE
    assert metadata["조회명칭"] == "조회·구경 65mm"
    assert "·구경 65mm" in metadata["응답명칭"]
    if api_target == "law":
        assert "·구경 65mm" in metadata["법령명약칭"]
    elif api_target == "admrul":
        assert "·구경 65mm" in metadata["행정규칙종류"]
    else:
        assert "·구경 65mm" in metadata["시행령명"]
    assert "## 문서정보" not in markdown
    assert '<a id="' not in markdown
    assert "](#" not in markdown


def test_heading_levels_are_continuous_and_cross_references_remain_plain_text() -> None:
    articles = [
        {"조문여부": "전문", "조문내용": "제1장 총칙"},
        {"조문여부": "전문", "조문내용": "제1절 목적"},
        {
            "조문번호": "1",
            "조문제목": "목적",
            "조문내용": "제1조(목적) 이 법의 목적이다.",
        },
    ]
    body = collector.render_law_body(articles)

    assert "## 제1장 총칙" in body
    assert "### 제1절 목적" in body
    assert "#### 제1조 (목적)" in body
    assert "#####" not in body

    nftc = collector._render_nftc_lines(
        "1. 일반사항\n\n1.1 적용범위\n\n"
        "1.1.1 배관은 2.2.1.10 및 2.2.1.11에 적합해야 한다."
    )
    nftc_markdown = "\n".join(nftc)
    assert "## 1. 일반사항" in nftc_markdown
    assert "### 1.1 적용범위" in nftc_markdown
    assert "1.1.1 배관은 2.2.1.10 및 2.2.1.11에" in nftc_markdown
    assert '<a id="' not in nftc_markdown
    assert "](#" not in nftc_markdown


def test_thdcmp_heading_levels_reflect_law_and_child_relationships() -> None:
    parsed = {
        "type": "thdCmp",
        "법령ID": "000001",
        "법령명": "계층 시험법",
        "articles": [
            {"조제목": "", "조내용": "제1장 총칙"},
            {"조제목": "", "조내용": "제1절 목적"},
            {
                "조번호": "1",
                "조가지번호": "0",
                "조제목": "목적",
                "조내용": "제1조(목적) 시험한다.",
                "시행령조문": [
                    {
                        "조번호": "2",
                        "조가지번호": "0",
                        "조제목": "위임",
                        "조내용": "제2조(위임) 정한다.",
                    }
                ],
                "시행규칙조문": [],
            },
        ],
    }

    markdown, _ = collector.render_thdcmp_markdown(
        parsed, FIXED_TIME, "계층 시험법"
    )

    assert "## 제1장 총칙" in markdown
    assert "### 제1절 목적" in markdown
    assert "#### 법률 제1조 (목적)" in markdown
    assert "##### 시행령 제2조 (위임)" in markdown
    assert "**인용조문:** 시행령 제2조(위임)" in markdown
    assert '<a id="' not in markdown
    assert "](#" not in markdown


def test_cli_writes_single_normalized_artifact_and_manifest_hash(
    isolated_project: Path,
    fixture_text,
    write_targets_csv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    title = "테스트 행정규칙"
    xml_text = fixture_text("admrul.xml").replace(
        "행정규칙 변환을 시험한다.",
        "구경 65 ㎜ㆍ온도 20 ℃인 경우를 시험한다.",
    )
    source = write_targets_csv(
        isolated_project / "targets.csv",
        [{"문서유형": "행정규칙", "명칭": title}],
    )
    _seed_cache("admrul", title, xml_text)
    monkeypatch.setattr(
        collector.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("--from-cache attempted network access"),
    )

    assert collector.main(["--from-cache", "--input", str(source)]) == 0
    row = _read_manifest()
    markdown_path = isolated_project / Path(row["markdown_path"])
    markdown = markdown_path.read_text(encoding="utf-8")

    assert "65 ㎜ㆍ온도 20 ℃" not in markdown
    assert "65mm·온도 20°C" in markdown
    assert row["normalization_profile"] == collector.NORMALIZATION_PROFILE
    assert int(row["normalization_changed_field_count"]) > 0
    assert row["markdown_sha256"] == sha256(markdown_path.read_bytes()).hexdigest()
    assert int(row["markdown_size_bytes"]) == markdown_path.stat().st_size
    assert _frontmatter(markdown)["텍스트정규화"] == collector.NORMALIZATION_PROFILE
    assert not (isolated_project / "output" / "markdown_normalized").exists()
    assert not any(key.startswith("normalized_markdown_") for key in row)
    for field in ("collected_at", "processed_at"):
        assert UTC_Z_PATTERN.fullmatch(row[field])
        parsed_time = datetime.fromisoformat(row[field].replace("Z", "+00:00"))
        assert parsed_time.utcoffset() is not None


def test_legacy_normalize_option_and_parallel_output_fields_are_removed() -> None:
    with pytest.raises(SystemExit):
        collector.parse_args(["--normalize"])
    assert not hasattr(collector, "NORMALIZED_MD_LAW_DIR")
    assert not hasattr(collector, "NORMALIZED_MD_ADMRUL_DIR")
    assert not hasattr(collector, "NORMALIZED_MD_THDCMP_DIR")
    assert not any(
        field.startswith("normalized_markdown_")
        or field == "has_normalized_markdown"
        for field in collector.MANIFEST_COLUMNS
    )
