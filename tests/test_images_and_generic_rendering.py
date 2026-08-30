from __future__ import annotations

import re

import pytest

import collect_law_markdown as collector


FIXED_TIME = "2026-08-30T03:34:56Z"
RAW_IMAGE_TAG_RE = re.compile(r"</?\s*img\b", re.IGNORECASE)
UNESCAPED_BACKTICK_RE = re.compile(r"(?<!\\)`")
IMAGE_ROOT = "https://www.law.go.kr/flDownload.do?flSeq="


def assert_no_raw_image_tags(text: str) -> None:
    assert RAW_IMAGE_TAG_RE.search(text) is None


def expected_image(
    image_id: str, *, url: str | None = None, description: str | None = None
) -> str:
    source_url = url or IMAGE_ROOT + image_id
    alt = description or f"법령정보센터 원문 이미지 {image_id}"
    title = "국가법령정보센터 원문 이미지"
    if image_id:
        title += f" (ID: {image_id})"
    return (
        f'![{alt}](<{source_url}> "{title}") '
        f'([원문 이미지 열기 (ID: {image_id})](<{source_url}>))'
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            '<img id="123">\n</img>',
            expected_image("123"),
        ),
        (
            '<IMG ID=456 />',
            expected_image("456"),
        ),
        (
            '<img src="http://www.law.go.kr/flDownload.do?flSeq=7&amp;x=1" '
            'alt="img7"></img>',
            expected_image("7"),
        ),
        (
            '<img src="/LSW/flDownload.do?flSeq=8" alt="img8" />',
            expected_image("8"),
        ),
        (
            '<img alt="img789"></img>',
            expected_image("789"),
        ),
        (
            "<img></img>",
            "이미지 참조",
        ),
    ],
)
def test_normalize_embedded_images_handles_observed_tag_variants(
    source: str, expected: str
) -> None:
    normalized = collector.normalize_embedded_images(source)

    assert normalized.strip() == expected
    assert_no_raw_image_tags(normalized)


def test_normalize_embedded_images_preserves_meaningful_alt_and_url() -> None:
    latex = (
        r"@@LATEX@@\frac{\mathrm{도급금액}- 계약상선급금}"
        r"{\mathrm{공사기간}\left(월\right)} \times 4@@/LATEX@@"
    )
    source = (
        f'<img alt="{latex}" '
        'src="/LSW/flDownload.do?flSeq=115092115" />'
    )

    normalized = collector.normalize_embedded_images(source)

    assert collector._markdown_image_alt(latex) in normalized
    assert "https://www.law.go.kr/flDownload.do?flSeq=115092115" in normalized
    assert_no_raw_image_tags(normalized)


def test_alt_is_used_as_id_only_when_the_entire_value_matches_img_number() -> None:
    image_id = collector.normalize_embedded_images('<img alt="img456"></img>')
    descriptive = collector.normalize_embedded_images(
        '<img alt="계산식 456"></img>'
    )

    assert image_id == expected_image("456")
    assert "계산식 456" in descriptive
    assert "ID: 456" not in descriptive
    assert "flDownload.do" not in descriptive
    assert_no_raw_image_tags(image_id)
    assert_no_raw_image_tags(descriptive)


@pytest.mark.parametrize(
    "image_id", ["abc", "123&x=1", "1/../../etc", "１２３"]
)
def test_non_numeric_image_id_is_not_interpolated_into_download_url(
    image_id: str,
) -> None:
    normalized = collector.normalize_embedded_images(
        f'<img id="{image_id}"></img>'
    )

    assert normalized.startswith("이미지 참조(ID: ")
    assert "flDownload.do" not in normalized


def test_attribute_names_must_match_exactly() -> None:
    normalized = collector.normalize_embedded_images(
        '<img data-src="https://evil.example/track.gif" id="123"></img>'
    )

    assert normalized == expected_image("123")
    assert "evil.example" not in normalized


def test_foreign_host_is_link_only_and_not_automatically_embedded() -> None:
    normalized = collector.normalize_embedded_images(
        '<img src="https://example.test/track.gif" alt="외부 도표"></img>'
    )

    assert normalized == (
        "[원문 이미지 열기](<https://example.test/track.gif>) (설명: 외부 도표)"
    )
    assert "![" not in normalized


def test_src_flseq_wins_when_id_and_machine_alt_conflict() -> None:
    normalized = collector.normalize_embedded_images(
        '<img src="https://www.law.go.kr/flDownload.do?flSeq=1" '
        'id="2" alt="img2"></img>'
    )

    assert normalized == expected_image("1")
    assert "ID: 2" not in normalized


def test_untrusted_id_cannot_create_markdown_link_or_break_title() -> None:
    fallback = collector.normalize_embedded_images(
        '<img id="[bad](https://evil.example)"></img>'
    )
    official = collector.normalize_embedded_images(
        '<img src="https://www.law.go.kr/flDownload.do?flSeq=1" '
        'id="x&quot; ) [bad](https://evil.example)"></img>'
    )

    assert r"\[bad\](https://evil.example)" in fallback
    assert "[bad](https://evil.example)" not in fallback
    assert official == expected_image("1")
    assert "evil.example" not in official


def test_placeholder_like_source_text_is_preserved() -> None:
    literal = "\ue000KLMIMAGE0TOKEN\ue001"
    normalized = collector.normalize_embedded_images(
        literal + '<img id="777"></img>'
    )

    assert normalized.startswith(literal)
    assert expected_image("777") in normalized


def test_redundant_img_number_alt_is_omitted_when_src_exists() -> None:
    normalized = collector.normalize_embedded_images(
        '<img src="http://www.law.go.kr/flDownload.do?flSeq=123" '
        'alt="img123"></img>'
    )

    assert normalized == expected_image("123")
    assert "img123" not in normalized


def test_adjacent_images_remain_separate_readable_references() -> None:
    normalized = collector.normalize_embedded_images(
        '<img id="11"></img><img id="12"></img>'
    )

    assert normalized.count("![법령정보센터 원문 이미지") == 2
    assert IMAGE_ROOT + "11" in normalized
    assert IMAGE_ROOT + "12" in normalized
    assert re.search(r"flSeq=11>\)\)\s+/\s+!\[", normalized)
    assert_no_raw_image_tags(normalized)


def test_generated_image_markdown_survives_appendix_neutralization() -> None:
    normalized = collector.neutralize_appendix_markdown(
        '앞 <img id="321"></img> 뒤'
    )

    assert expected_image("321") in normalized
    assert r"\![" not in normalized
    assert r"\[원문 이미지 열기" not in normalized


def test_meaningful_alt_is_escaped_without_breaking_image_markdown() -> None:
    source = r'<img id="654" alt="계산식 [A] \times * B `C` &lt;D&gt;" />'

    normalized = collector.normalize_embedded_images(source)

    assert r"계산식 \[A\] \\times \* B \`C\` &lt;D&gt;" in normalized
    assert IMAGE_ROOT + "654" in normalized
    assert_no_raw_image_tags(normalized)


def test_law_renderer_normalizes_images_in_every_legal_text_field() -> None:
    parsed = {
        "type": "law",
        "법령ID": "1",
        "법령명_한글": "이미지 경로 테스트 법률",
        "articles": [
            {
                "조문번호": "1",
                "조문가지번호": "",
                "조문여부": "조문",
                "조문제목": "목적",
                "조문내용": '제1조(목적) 본문 <img id="101"></img>',
                "조문참고자료": '참고 <img id="102"></img>',
                "항": [
                    {
                        "항번호": "1",
                        "항가지번호": "",
                        "항내용": '① 항 <img id="103"></img>',
                        "호": [
                            {
                                "호번호": "1",
                                "호가지번호": "",
                                "호내용": '1. 호 <img id="104"></img>',
                                "목": [
                                    {
                                        "목번호": "가",
                                        "목가지번호": "",
                                        "목내용": '가. 목 <img id="105"></img>',
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "addenda": [
            {"부칙공포번호": "1", "부칙내용": '<img id="106"></img>'}
        ],
        "appendices": [
            {
                "별표번호": "1",
                "별표가지번호": "",
                "별표구분": "별표",
                "별표제목": "이미지 별표",
                "별표내용": '<img id="107"></img>',
            }
        ],
        "attachments": [],
        "amendments": ['<img id="108"></img>'],
        "reasons": ['<img id="109"></img>'],
    }

    markdown, _ = collector.render_markdown(
        parsed, "law", FIXED_TIME, "이미지 경로 테스트 법률"
    )

    assert_no_raw_image_tags(markdown)
    for image_id in range(101, 110):
        assert expected_image(str(image_id)) in markdown


def test_admrul_renderer_normalizes_images_in_flat_article_body() -> None:
    parsed = {
        "type": "admrul",
        "행정규칙ID": "2",
        "행정규칙명": "임의 명칭 행정규칙",
        "조문형식여부": "Y",
        "articles": [
            {
                "조문번호": "",
                "조문가지번호": "",
                "조문제목": "",
                "조문내용": '제1조(목적) <img id="201"></img>',
                "조문참고자료": "",
                "항": [],
            }
        ],
        "addenda": [],
        "appendices": [],
        "attachments": [],
        "amendments": [],
        "reasons": [],
    }

    markdown, _ = collector.render_markdown(
        parsed, "admrul", FIXED_TIME, "임의 명칭 행정규칙"
    )

    assert_no_raw_image_tags(markdown)
    assert expected_image("201") in markdown


def test_thdcmp_renderer_normalizes_law_enforcement_and_rule_bodies() -> None:
    parsed = {
        "type": "thdCmp",
        "법령ID": "3",
        "법령명": "3단비교 이미지 테스트 법률",
        "articles": [
            {
                "조번호": "1",
                "조가지번호": "",
                "조제목": "목적",
                "조내용": '제1조(목적) <img id="301"></img>',
                "시행령조문": [
                    {
                        "조번호": "2",
                        "조가지번호": "",
                        "조제목": "위임",
                        "조내용": '제2조(위임) <img id="302"></img>',
                    }
                ],
                "시행규칙조문": [
                    {
                        "조번호": "3",
                        "조가지번호": "",
                        "조제목": "서식",
                        "조내용": '제3조(서식) <img id="303"></img>',
                    }
                ],
            }
        ],
    }

    markdown, _ = collector.render_thdcmp_markdown(
        parsed, FIXED_TIME, "3단비교 이미지 테스트 법률"
    )

    assert_no_raw_image_tags(markdown)
    for image_id in range(301, 304):
        assert expected_image(str(image_id)) in markdown


def make_admrul(
    content: str, *, format_flag: str | None, title: str = "임의 명칭 행정규칙"
) -> dict:
    parsed = {
        "type": "admrul",
        "행정규칙ID": "10",
        "행정규칙명": title,
        "articles": [
            {
                "조문번호": "",
                "조문가지번호": "",
                "조문제목": "",
                "조문내용": content,
                "조문참고자료": "",
                "항": [],
            }
        ],
        "addenda": [],
        "appendices": [],
        "attachments": [],
        "amendments": [],
        "reasons": [],
    }
    if format_flag is not None:
        parsed["조문형식여부"] = format_flag
    return parsed


@pytest.mark.parametrize("format_flag", ["Y", " y "])
def test_xml_format_flag_enables_flat_legal_rendering_for_arbitrary_title(
    format_flag: str,
) -> None:
    parsed = make_admrul(
        "제1조(목적) 본문\n① 첫째 항\n  1. 첫째 호\n    가. 첫째 목\n      1) 하위 항목",
        format_flag=format_flag,
    )

    markdown, _ = collector.render_markdown(
        parsed, "admrul", FIXED_TIME, "임의 명칭 행정규칙"
    )

    assert "## 제1조 (목적)" in markdown
    assert "**①** 첫째 항" in markdown
    assert "  1\\. 첫째 호" in markdown
    assert "    가\\. 첫째 목" in markdown
    assert "      1\\) 하위 항목" in markdown


def test_missing_format_flag_uses_strict_article_structure_fallback() -> None:
    parsed = make_admrul(
        "제2조의3(적용범위) 본문\n① 첫째 항\n  1. 첫째 호",
        format_flag=None,
    )

    markdown, _ = collector.render_markdown(
        parsed, "admrul", FIXED_TIME, "임의 명칭 행정규칙"
    )

    assert "## 제2조의3 (적용범위)" in markdown
    assert "**①** 첫째 항" in markdown
    assert "  1\\. 첫째 호" in markdown


@pytest.mark.parametrize(
    "content",
    [
        "제7조제1호에 따른 내용이다.",
        "제7조에 따라 처리한다.",
    ],
)
def test_cross_reference_prefix_is_not_inferred_as_article_heading(
    content: str,
) -> None:
    parsed = make_admrul(content, format_flag=None)

    markdown, _ = collector.render_markdown(
        parsed, "admrul", FIXED_TIME, "임의 명칭 행정규칙"
    )

    assert "## 제7조" not in markdown
    assert content in markdown


@pytest.mark.parametrize("api_target", ["law", "admrul"])
def test_literal_backticks_in_article_body_are_escaped(api_target: str) -> None:
    if api_target == "admrul":
        parsed = make_admrul(
            "제1조(목적) `원문 표시`를 보존한다.",
            format_flag="Y",
        )
    else:
        parsed = {
            "type": "law",
            "법령ID": "11",
            "법령명_한글": "백틱 테스트 법률",
            "articles": [
                {
                    "조문번호": "1",
                    "조문가지번호": "",
                    "조문여부": "조문",
                    "조문제목": "목적",
                    "조문내용": "제1조(목적) `원문 표시`를 보존한다.",
                    "조문참고자료": "",
                    "항": [],
                }
            ],
            "addenda": [],
            "appendices": [],
            "attachments": [],
            "amendments": [],
            "reasons": [],
        }

    markdown, _ = collector.render_markdown(
        parsed, api_target, FIXED_TIME, "백틱 테스트"
    )

    assert r"\`원문 표시\`" in markdown
    source_section = markdown.split("\n---\n", maxsplit=1)[1]
    assert UNESCAPED_BACKTICK_RE.search(source_section) is None
