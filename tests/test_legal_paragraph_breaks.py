from __future__ import annotations

import re

import pytest
from markdown_it import MarkdownIt

import collect_law_markdown as collector


def paragraph_texts(markdown: str) -> list[str]:
    tokens = MarkdownIt("commonmark").parse(markdown)
    assert not any(
        token.type in {"code_block", "fence", "ordered_list_open", "bullet_list_open"}
        for token in tokens
    )
    return [
        "".join(child.content for child in token.children or []).strip()
        for i, token in enumerate(tokens)
        if token.type == "inline" and tokens[i - 1].type == "paragraph_open"
    ]


@pytest.mark.parametrize("api_target", ["law", "admrul"])
def test_insurance_article_renders_each_numbered_item_as_a_paragraph(
    api_target, fixture_text
) -> None:
    # Cached source excerpt for the exact article reported by the user.
    parsed = collector.parse_law_xml(fixture_text("law_numbered_paragraphs.xml"))
    markdown, _, _ = collector.render_document(
        parsed, api_target, "2026-10-24T00:00:00Z", parsed["법령명_한글"]
    )
    body = markdown[markdown.index("## 제13조의3"):].split("\n## ", 1)[0]
    actual = [text for text in paragraph_texts(body) if re.match(r"^\d+\.", text)]
    expected = [
        collector.normalize_search_text(ho["호내용"]).strip()
        for hang in parsed["articles"][0]["항"]
        for ho in hang["호"]
    ]
    assert len(expected) == 8
    assert actual == expected
    assert "변경한 경우\n\n  2\\. 제9조" in body


def test_structured_nested_items_remain_paragraphs_with_original_numbers() -> None:
    art = {
        "조문번호": "1",
        "항": [{
            "항번호": "①", "항내용": "① 다음 각 호에 따른다.",
            "호": [
                {"호번호": "2", "호내용": "2. 둘째 호", "목": [
                    {"목번호": "가", "목내용": "가. 첫째 목\n1) 하위 하나\n2) 하위 둘"},
                    {"목번호": "나", "목내용": "나. 둘째 목"},
                ]},
                {"호번호": "2", "호가지번호": "2", "호내용": "2의2. 가지 호"},
                {"호번호": "5", "호내용": "5. 다섯째 호"},
            ],
        }],
    }
    body = "\n".join(collector._render_law_article_lines(art))
    assert paragraph_texts(body) == [
        "① 다음 각 호에 따른다.", "2. 둘째 호", "가. 첫째 목",
        "1) 하위 하나", "2) 하위 둘", "나. 둘째 목", "2의2. 가지 호", "5. 다섯째 호",
    ]


@pytest.mark.parametrize("separator", ["\n", "\n\n", "\r\n"])
def test_flat_items_in_admrul_and_comparison_body_do_not_merge(separator) -> None:
    source_lines = [
        "① 다음 각 호에 따른다.", "1. 첫째 호", "가. 첫째 목",
        "1) 하위 하나", "2) 하위 둘", "나. 둘째 목", "2. 둘째 호", "② 다음 항이다.",
    ]
    body = collector._render_flat_legal_body(separator.join(source_lines))
    assert paragraph_texts(body) == source_lines
