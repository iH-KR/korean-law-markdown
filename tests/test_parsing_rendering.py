from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256

import pytest

import collect_law_markdown as collector


FIXED_TIME = "2026-08-30 12:34:56"


@pytest.mark.parametrize(
    (
        "fixture_name",
        "api_target",
        "parser",
        "title",
        "expected_id",
        "golden_sha256",
        "snippets",
        "counts",
    ),
    [
        (
            "law.xml",
            "law",
            collector.parse_law_xml,
            "테스트 법률",
            "000001",
            "7ebd96f4f518ab705f52e107c7f93ae46592c86197f8f4f7799bf6331f1ec88c",
            (
                "# 테스트 법률",
                "##### 제1조 (목적)",
                "**1** 첫 번째 항이다.",
                "### 부칙 1",
                "### [별표 1] 시험표",
                "https://www.law.go.kr/LSW/file.pdf",
            ),
            {
                "article_count": 1,
                "appendix_count": 1,
                "appendix_text_count": 1,
                "appendix_pdf_link_count": 1,
                "appendix_file_link_count": 1,
                "appendix_image_link_count": 1,
                "attachment_link_count": 0,
            },
        ),
        (
            "admrul.xml",
            "admrul",
            collector.parse_admrul_xml,
            "테스트 행정규칙",
            "90001",
            "b91ba1879382cbef3b5ef0e62ce91d85b1ec1a44c67acad0d9989c13bdc8fa5b",
            (
                "# 테스트 행정규칙",
                "##### 제1조 (목적)",
                "### [서식 1] 행정 서식",
                "## 첨부파일 링크",
                "https://www.law.go.kr/LSW/attach.pdf",
            ),
            {
                "article_count": 1,
                "appendix_count": 1,
                "appendix_text_count": 1,
                "appendix_pdf_link_count": 0,
                "appendix_file_link_count": 0,
                "appendix_image_link_count": 0,
                "attachment_link_count": 1,
            },
        ),
    ],
)
def test_law_and_admrul_parse_and_render_golden_sections(
    fixture_text: Callable[[str], str],
    fixture_name: str,
    api_target: str,
    parser: Callable[[str], dict],
    title: str,
    expected_id: str,
    golden_sha256: str,
    snippets: tuple[str, ...],
    counts: dict[str, int],
) -> None:
    xml_text = fixture_text(fixture_name)

    # The high-level validator and the historical parser entrypoint must agree.
    validated = collector.parse_and_validate_xml(api_target, xml_text)
    parsed = parser(xml_text)
    assert validated == parsed
    assert collector._law_id(parsed) == expected_id

    markdown, actual_counts = collector.render_markdown(
        parsed, api_target, FIXED_TIME, title
    )
    assert actual_counts == counts
    assert sha256(markdown.encode("utf-8")).hexdigest() == golden_sha256
    for snippet in snippets:
        assert snippet in markdown
    assert markdown.endswith("\n")


def test_thdcmp_parse_and_render_golden_sections(fixture_text) -> None:
    xml_text = fixture_text("thdcmp.xml")
    parsed = collector.parse_and_validate_xml("thdCmp", xml_text)

    assert parsed == collector.parse_thdcmp_xml(xml_text)
    assert parsed["법령ID"] == "000001"
    assert len(parsed["articles"]) == 1
    assert len(parsed["articles"][0]["시행령조문"]) == 1
    assert len(parsed["articles"][0]["시행규칙조문"]) == 1

    markdown, counts = collector.render_thdcmp_markdown(
        parsed, FIXED_TIME, "테스트 법률"
    )
    assert counts == {
        "article_count": 1,
        "appendix_count": 0,
        "appendix_text_count": 0,
        "appendix_pdf_link_count": 0,
        "appendix_file_link_count": 0,
        "appendix_image_link_count": 0,
        "attachment_link_count": 2,
    }
    assert sha256(markdown.encode("utf-8")).hexdigest() == (
        "1e0024c20b97d1d68ae6aeacf8e82c3f42a6bc90d17aeea3d0e56cb3bd14590e"
    )
    assert "# 테스트 법률 인용조문 3단비교" in markdown
    assert "### 법률 제1조 (목적)" in markdown
    assert "#### 인용조문: 시행령 제2조(위임) / 시행규칙 제3조(서식)" in markdown
    assert "### 시행령 제2조 (위임)" in markdown
    assert "### 시행규칙 제3조 (서식)" in markdown


def test_appendix_cdata_addendum_and_revision_markdown_are_protected(fixture_text) -> None:
    parsed = collector.parse_and_validate_xml("law", fixture_text("law.xml"))
    appendix = parsed["appendices"][0]["별표내용"]

    # Adjacent CDATA chunks remain separate source lines without a duplicate blank line.
    assert "- 원문 항목\n---" in appendix

    markdown, _ = collector.render_markdown(
        parsed, "law", FIXED_TIME, "테스트 법률"
    )
    assert "\\# 원문 제목" in markdown
    assert "1\\. 원문 번호" in markdown
    assert "\\- 원문 항목" in markdown
    assert "\\---" in markdown
    assert "\\[링크](https://example.test/item)" in markdown
    assert "\\`원문 코드\\`" in markdown
    assert "1\\. 이 법은 공포한 날부터 시행한다." in markdown
    assert "- 이 하이픈은 원문이다." in markdown
    assert "이미지 참조(ID: 77)" in markdown


@pytest.mark.parametrize(
    ("api_target", "xml_text", "message"),
    [
        (
            "law",
            "<RESULT><CODE>999</CODE><MSG>인증 오류</MSG></RESULT>",
            "API 오류",
        ),
        (
            "law",
            "<html><head><title>gateway</title></head><body>bad</body></html>",
            "root",
        ),
        ("law", "<법령><기본정보><법령ID>1</법령ID>", "XML|Premature|파싱"),
        (
            "law",
            '<!DOCTYPE 법령 [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<법령><기본정보><법령ID>1</법령ID>"
            "<법령명_한글>&xxe;</법령명_한글></기본정보></법령>",
            "DOCTYPE",
        ),
        (
            "law",
            "<법령><기본정보><법령ID>1</법령ID>"
            "<법령명_한글> </법령명_한글></기본정보>"
            "<조문단위><조문번호>1</조문번호>"
            "<조문제목>목적</조문제목></조문단위></법령>",
            "명칭|법령명",
        ),
    ],
)
def test_strict_xml_validation_rejects_bad_responses(
    api_target: str, xml_text: str, message: str
) -> None:
    # lxml raises XMLSyntaxError for malformed input while semantic failures use
    # ValueError. Both are strict rejection; no recovery result may escape.
    with pytest.raises(Exception, match=message):
        collector.parse_and_validate_xml(api_target, xml_text)


@pytest.mark.parametrize(
    ("api_target", "xml_text"),
    [
        (
            "law",
            "<법령><기본정보><법령ID>1</법령ID>"
            "<법령명_한글>본문 없는 법률</법령명_한글></기본정보>"
            "<조문단위><조문번호>1</조문번호>"
            "<조문제목>목적</조문제목></조문단위></법령>",
        ),
        (
            "admrul",
            "<AdmRulService><행정규칙기본정보>"
            "<행정규칙ID>1</행정규칙ID>"
            "<행정규칙명>본문 없는 행정규칙</행정규칙명>"
            "</행정규칙기본정보><조문단위>"
            "<조문번호>1</조문번호><조문제목>목적</조문제목>"
            "</조문단위></AdmRulService>",
        ),
        (
            "thdCmp",
            "<ThdCmpLawXService><기본정보><법령ID>1</법령ID>"
            "<법령명>본문 없는 법률</법령명></기본정보>"
            "<인용조문삼단비교><법률조문><조번호>1</조번호>"
            "<조제목>목적</조제목></법률조문>"
            "</인용조문삼단비교></ThdCmpLawXService>",
        ),
    ],
)
def test_strict_xml_validation_rejects_articles_without_body(
    api_target: str, xml_text: str
) -> None:
    with pytest.raises(ValueError, match="본문 조문"):
        collector.parse_and_validate_xml(api_target, xml_text)


@pytest.mark.parametrize(
    "api_target",
    ["law", "admrul", "thdCmp"],
)
def test_strict_xml_validation_rejects_target_mismatch(
    api_target: str, fixture_text
) -> None:
    fixture_for_other_target = {
        "law": "admrul.xml",
        "admrul": "thdcmp.xml",
        "thdCmp": "law.xml",
    }[api_target]
    with pytest.raises(ValueError, match="root|루트|형식|target"):
        collector.parse_and_validate_xml(api_target, fixture_text(fixture_for_other_target))
