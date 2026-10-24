from __future__ import annotations

import re

import pytest
from markdown_it import MarkdownIt

import collect_law_markdown as collector


def parsed(markdown):
    return MarkdownIt("commonmark").parse(markdown)


def visible_lines(markdown):
    """Recover source lines from paragraphs/fences without Markdown syntax."""
    lines = []
    for token in parsed(markdown):
        if token.type == "fence":
            lines.extend(token.content.splitlines())
        elif token.type == "inline":
            text = "".join(
                "\n" if child.type in {"hardbreak", "softbreak"} else child.content
                for child in token.children or []
            )
            lines.extend(text.splitlines())
    return [line.strip() for line in lines if line.strip()]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_appendix_prose_keeps_items_and_printed_continuation_lines(newline):
    lines = [
        "■ 별표 1", "", "  1. 교육인력", "    가. 강사의 자격요건",
        "      (1) 강사", "        (가) 소방 관련 기관에서 강의경",
        "  력이 있는 자", "        (나) 다음 자격을 소지한 자",
        "    나. 자격 확인", "  2. 교육시설", "      1) 첫째 기준",
        "      2)둘째 기준", "  비고: 원문을 확인할 것",
    ]
    markdown = collector.render_appendix_content(newline.join(lines))
    tokens = parsed(markdown)
    assert visible_lines(markdown) == [line.strip() for line in lines if line.strip()]
    assert not any(t.type in {"code_block", "fence", "ordered_list_open"} for t in tokens)
    paragraphs = [t for t in tokens if t.type == "inline"]
    assert len(paragraphs) == 11
    assert sum(c.type == "hardbreak" for t in paragraphs for c in t.children) == 1
    assert not any(c.type == "softbreak" for t in paragraphs for c in t.children)


def test_existing_blank_line_between_unnumbered_paragraphs_is_preserved():
    source = "일반 설명\n\n다음 문단\n\n\n\n마지막 문단"
    assert collector.normalize_appendix_blank_lines(source) == (
        "일반 설명\n\n다음 문단\n\n마지막 문단"
    )
    assert visible_lines(collector.render_appendix_content(source)) == [
        "일반 설명", "다음 문단", "마지막 문단",
    ]
    assert sum(t.type == "paragraph_open" for t in parsed(collector.render_appendix_content(source))) == 3


def test_mixed_prose_and_merged_table_preserve_rows_and_empty_cells():
    table = [
        "┌──────┬──────┐",
        "│종류  │기준  │",
        "├──┬───┤      │",
        "│가│1  │      │",
        "│  │2  │      │",
        "└──┴───┴──────┘",
    ]
    source = "1. 적용 기준\n가. 다음 표에 따른다.\n\n" + "\n\n".join(table) + "\n비고\n1) 추가 기준"
    markdown = collector.render_appendix_content(source)
    fences = [t for t in parsed(markdown) if t.type == "fence"]
    assert len(fences) == 1
    assert fences[0].info == "text"
    assert fences[0].content.splitlines() == table
    assert visible_lines(markdown) == ["1. 적용 기준", "가. 다음 표에 따른다.", *table, "비고", "1) 추가 기준"]


def test_table_image_keeps_position_and_clickable_image_outside_fence():
    source = '┌────┐\n│<img id="123"></img>│\n└────┘'
    markdown = collector.render_appendix_content(source)
    tokens = parsed(markdown)
    fence = next(t for t in tokens if t.type == "fence")
    assert "[원문 이미지 1]" in fence.content
    assert "![" not in fence.content
    images = [c for t in tokens for c in t.children or [] if c.type == "image"]
    assert len(images) == 1
    assert images[0].attrGet("src") == "https://www.law.go.kr/flDownload.do?flSeq=123"
    assert "원문 이미지 열기" in markdown
    assert not re.search(r"</?img\b|\ue000KLMIMAGE", markdown)


def test_literal_markdown_cannot_activate_or_close_layout_fence():
    source = "# 원문 제목\n1. 원문 번호\n[문자](https://example.test)\n┌────┐\n│````</script>│\n└────┘"
    markdown = collector.render_appendix_content(source)
    tokens = parsed(markdown)
    assert not any(t.type in {"heading_open", "ordered_list_open", "html_block"} for t in tokens)
    assert not any(c.type == "link_open" for t in tokens for c in t.children or [])
    assert len([t for t in tokens if t.type == "fence"]) == 1
    assert visible_lines(markdown) == source.splitlines()


@pytest.mark.parametrize("kind", ["별표", "서식", "별지", "별도", "별첨"])
def test_appendix_entrypoint_applies_layout_to_each_kind(kind):
    markdown, counts = collector._render_appendix({
        "별표구분": kind, "별표번호": "1", "별표제목": "예시",
        "별표내용": "1. 첫째\n가. 다음 항목\n2. 둘째",
    }, 1)
    assert "1\\. 첫째\n\n가. 다음 항목\n\n2\\. 둘째" in markdown
    assert counts == {"text": 1, "pdf": 0, "file": 0, "image": 0}


@pytest.mark.parametrize("source", ["", " \n\t\n"])
def test_empty_appendix_does_not_create_empty_code_or_paragraph_blocks(source):
    assert collector.render_appendix_content(source) == ""


@pytest.mark.parametrize("fixture", ["appendix_mixed_law.txt", "appendix_prose_admrul.txt"])
def test_cached_real_appendices_keep_text_order_and_all_source_lines(fixture, fixture_text):
    source = fixture_text(fixture)
    markdown = collector.render_appendix_content(source)
    assert visible_lines(markdown) == [line.strip() for line in source.splitlines() if line.strip()]
    tokens = parsed(markdown)
    assert not any(t.type in {"code_block", "ordered_list_open", "bullet_list_open"} for t in tokens)
    for token in tokens:
        if token.type == "inline":
            assert not any(c.type == "softbreak" for c in token.children or [])
            # One legal item may have several printed continuation lines.
            assert sum(bool(collector._APPENDIX_PARAGRAPH_START_RE.match(line))
                       for line in visible_lines(token.content)) <= 1
