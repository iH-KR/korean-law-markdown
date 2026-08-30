# -*- coding: utf-8 -*-
"""
collect_law_markdown.py

법제처 Open API(XML)로 대한민국 법령/행정규칙 본문을 수집해
NotebookLM 업로드용 Markdown(.md) 파일로 변환한다.

입력:
  - law_targets.csv  (기본값, 필수 컬럼: 문서유형, 명칭)
      문서유형은 "법령", "행정규칙", "3단비교" 세 값을 허용.
      명칭은 법제처 API의 LM 파라미터로 그대로 사용.
      ※ 이 두 컬럼만 사용한다. 변환되는 Markdown의 문서 메타데이터(법령ID,
        공포번호/발령번호, 공포일자/발령일자, 시행일자, 소관부처, 법종구분 등)
        는 모두 API 응답 XML에서 추출한다.
  - 환경변수 LAW_OC (또는 .env)  ※ --from-cache 모드에서는 필요 없음
출력:
  - output/markdown/laws/*.md
  - output/markdown/administrative_rules/*.md
  - output/markdown/thdcmp/*_인용조문_3단비교.md
  - output/raw_xml/laws/*.xml
  - output/raw_xml/administrative_rules/*.xml
  - output/raw_xml/thdcmp/*.xml
  - output/manifest.csv
  - output/failed_items.csv
  - output/logs/run_*.log

실행 모드:
  python collect_law_markdown.py                  # 기본: 모든 항목을 API로 새로 받기
  python collect_law_markdown.py --from-cache     # 네트워크 호출 0, output/raw_xml 캐시만 사용
  python collect_law_markdown.py --missing-only   # 캐시가 없는 항목만 API, 있는 건 캐시 사용
  python collect_law_markdown.py --input targets.csv  # 다른 수집 목록 사용
  python collect_law_markdown.py --verbose        # 항목별 상세 로그도 터미널에 표시

API URL 규칙 (OC 값은 환경변수 LAW_OC에서 읽음):
  - 법령:
    https://www.law.go.kr/DRF/lawService.do?target=law&OC={LAW_OC}&type=XML&LM={명칭}
  - 행정규칙:
    https://www.law.go.kr/DRF/lawService.do?target=admrul&OC={LAW_OC}&type=XML&LM={명칭}
  - 3단비교(인용조문 전용, knd=1 고정):
    https://www.law.go.kr/DRF/lawService.do?target=thdCmp&OC={LAW_OC}&type=XML&knd=1&LM={명칭}
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import os
import re
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, quote_plus, urlencode

import pandas as pd
import requests
from dotenv import load_dotenv
from tqdm import tqdm

try:
    from lxml import etree as LET  # type: ignore
    _HAS_LXML = True
except Exception:
    LET = None  # type: ignore
    _HAS_LXML = False

import xml.etree.ElementTree as ET


# ---------------------------------------------------------------------------
# 상수 / 경로
# ---------------------------------------------------------------------------
BASE_URL = "https://www.law.go.kr/DRF/lawService.do"
LAW_SITE_ROOT = "https://www.law.go.kr"

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_TARGETS_CSV = PROJECT_DIR / "law_targets.csv"
OUTPUT_DIR = PROJECT_DIR / "output"
MD_LAW_DIR = OUTPUT_DIR / "markdown" / "laws"
MD_ADMRUL_DIR = OUTPUT_DIR / "markdown" / "administrative_rules"
MD_THDCMP_DIR = OUTPUT_DIR / "markdown" / "thdcmp"
XML_LAW_DIR = OUTPUT_DIR / "raw_xml" / "laws"
XML_ADMRUL_DIR = OUTPUT_DIR / "raw_xml" / "administrative_rules"
XML_THDCMP_DIR = OUTPUT_DIR / "raw_xml" / "thdcmp"

THDCMP_FILE_SUFFIX = "_인용조문_3단비교"
LOG_DIR = OUTPUT_DIR / "logs"
MANIFEST_CSV = OUTPUT_DIR / "manifest.csv"
FAILED_CSV = OUTPUT_DIR / "failed_items.csv"

REQUEST_SLEEP = 0.3          # 요청 간 sleep(>=0.3초 권장)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 60
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = [2, 5, 10]
MAX_FILENAME_LEN = 150

MANIFEST_COLUMNS = [
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
]

FAILED_COLUMNS = [
    "source_index",
    "document_type",
    "title",
    "api_target",
    "api_url_without_oc",
    "error_type",
    "error_message",
    "http_status",
    "collected_at",
]


# ---------------------------------------------------------------------------
# 로깅 / 터미널 출력
# ---------------------------------------------------------------------------
_SENSITIVE_VALUES: set[str] = set()
_OC_QUERY_RE = re.compile(
    r"(?i)(\bOC(?:=|%3D))([^&\s\"'<>]+)"
)


def _register_sensitive_value(value: str) -> None:
    """로그/결과표에서 삭제해야 할 인증값을 등록한다."""
    secret = (value or "").strip()
    if not secret:
        return
    _SENSITIVE_VALUES.update({
        secret,
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
    })


def redact_sensitive(value: Any) -> str:
    """OC 쿼리 값과 현재 실행의 LAW_OC 값을 중앙 마스킹한다."""
    if value is None:
        return ""
    text = str(value)
    for secret in sorted(_SENSITIVE_VALUES, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "{LAW_OC}")
    return _OC_QUERY_RE.sub(r"\1{LAW_OC}", text)


def safe_error_message(error: Any, *, max_length: int = 2000) -> str:
    """외부 예외 문구를 인증값 없이 제한된 길이로 반환한다."""
    message = redact_sensitive(error).replace("\x00", "\\0")
    if len(message) > max_length:
        return message[:max_length] + "…(생략)"
    return message


class RedactingFormatter(logging.Formatter):
    """파일·콘솔·traceback을 포함한 최종 로그 문자열을 마스킹한다."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive(super().format(record))


class CompactConsoleFormatter(RedactingFormatter):
    """파일 로그의 예외 상세를 유지하면서 터미널에는 한 줄만 표시한다."""

    def format(self, record: logging.LogRecord) -> str:
        exc_info = record.exc_info
        exc_text = record.exc_text
        stack_info = record.stack_info
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            return super().format(record)
        finally:
            record.exc_info = exc_info
            record.exc_text = exc_text
            record.stack_info = stack_info


class TqdmLoggingHandler(logging.StreamHandler):
    """진행률 표시를 깨뜨리지 않고 경고와 오류를 출력한다."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record), file=self.stream)
        except Exception:
            self.handleError(record)


PROGRESS_BAR_FORMAT = (
    "{desc}: {n_fmt}/{total_fmt} |{bar:24}| "
    "{percentage:3.0f}% [{elapsed}<{remaining}]"
)


def setup_logging(
    verbose: bool = False,
    run_id: Optional[str] = None,
) -> Tuple[logging.Logger, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id or uuid.uuid4().hex[:12])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = LOG_DIR / f"run_{stamp}_{safe_run_id}.log"

    logger = logging.getLogger("collect_law")
    logger.setLevel(logging.INFO)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()

    fmt = RedactingFormatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = TqdmLoggingHandler()
    sh.setLevel(logging.INFO if verbose else logging.WARNING)
    sh.setFormatter(CompactConsoleFormatter("%(levelname)s | %(message)s"))
    logger.addHandler(sh)

    logger.propagate = False
    return logger, log_path


# ---------------------------------------------------------------------------
# 설정 로드
# ---------------------------------------------------------------------------
def load_config() -> str:
    """환경변수 또는 .env에서 LAW_OC를 읽어 반환한다."""
    load_dotenv(dotenv_path=PROJECT_DIR / ".env", override=False)
    oc = os.environ.get("LAW_OC", "").strip()
    if not oc:
        raise RuntimeError(
            "LAW_OC 환경변수가 설정되지 않았습니다. "
            "프로젝트 폴더에 .env 파일을 만들고 LAW_OC=발급받은값 형식으로 입력하세요."
        )
    _register_sensitive_value(oc)
    return oc


# ---------------------------------------------------------------------------
# CSV 입력
# ---------------------------------------------------------------------------
def read_source_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"입력 CSV를 찾을 수 없습니다: {path}")

    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    for col in df.columns:
        df[col] = df[col].astype(str).fillna("").map(lambda v: v.strip())

    required = ["문서유형", "명칭"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"필수 컬럼이 없습니다: {col}")
    return df


def deduplicate_rows(df: pd.DataFrame, logger: logging.Logger) -> pd.DataFrame:
    """중복 행 제거.

    키: 문서유형 + 명칭
    NFPC/NFTC 등 동명이형은 명칭에 식별자가 포함돼 있어 자연히 분리된다.
    """
    before = len(df)
    df["_dedup_key"] = df["문서유형"].astype(str) + "||" + df["명칭"].astype(str)
    deduped = df.drop_duplicates(subset=["_dedup_key"], keep="first").drop(columns=["_dedup_key"])
    logger.info(f"중복 처리: {before} -> {len(deduped)} 건")
    return deduped.reset_index(drop=True)


# ---------------------------------------------------------------------------
# API target / URL
# ---------------------------------------------------------------------------
def detect_api_target(row: pd.Series) -> str:
    """문서유형 값에 따라 'law' / 'admrul' / 'thdCmp'를 반환."""
    dtype = (row.get("문서유형") or "").strip()
    if dtype == "법령":
        return "law"
    if dtype == "행정규칙":
        return "admrul"
    if dtype == "3단비교":
        return "thdCmp"
    # 보수적 fallback: 명칭에 '법' / '령' / '규칙' 등이 있더라도
    # 명세에 없는 유형은 admrul로 시도하지 않고 raise한다.
    raise ValueError(f"알 수 없는 문서유형: {dtype!r}")


def build_api_url(row: pd.Series, oc: str) -> Tuple[str, str, str]:
    """API URL을 생성한다.

    반환: (api_target, full_url, masked_url)
    3단비교는 인용조문(knd=1) 전용으로 호출한다.
    """
    target = detect_api_target(row)
    lm = row.get("명칭", "").strip()
    params: Dict[str, str] = {
        "target": target,
        "OC": oc,
        "type": "XML",
        "LM": lm,
    }
    if target == "thdCmp":
        # 인용조문 전용
        params["knd"] = "1"
    qs = urlencode(params, encoding="utf-8")
    full_url = f"{BASE_URL}?{qs}"
    masked_url = mask_oc_in_url(full_url)
    return target, full_url, masked_url


def mask_oc_in_url(url: str) -> str:
    """URL에 포함된 OC 값을 {LAW_OC}로 마스킹한다."""
    return redact_sensitive(url)


# ---------------------------------------------------------------------------
# HTTP fetch
# ---------------------------------------------------------------------------
@dataclass
class FetchResult:
    ok: bool
    xml_text: str = ""
    http_status: Optional[int] = None
    error_type: str = ""
    error_message: str = ""


def fetch_xml(url: str, logger: logging.Logger) -> FetchResult:
    """XML 응답을 받아온다. 429/5xx는 재시도(2s/5s/10s)."""
    last_err = ""
    last_status: Optional[int] = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        try:
            resp = requests.get(
                url,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                headers={
                    "User-Agent": "korean-law-markdown/1.0 (+notebooklm)",
                    "Accept": "application/xml,text/xml,*/*",
                },
            )
            last_status = resp.status_code

            if resp.status_code == 200:
                # 인코딩 우선순위: apparent_encoding > UTF-8
                if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding or "utf-8"
                text = resp.text or ""
                if not text.strip():
                    return FetchResult(False, http_status=200,
                                       error_type="EMPTY_BODY",
                                       error_message="응답 본문이 비어 있음")
                return FetchResult(True, xml_text=text, http_status=200)

            if resp.status_code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    f"HTTP {resp.status_code} -> {delay}s 후 재시도 ({attempt + 1}/{len(RETRY_DELAYS)}) :: "
                    f"{mask_oc_in_url(url)}"
                )
                time.sleep(delay)
                continue

            return FetchResult(False, http_status=resp.status_code,
                               error_type="HTTP_ERROR",
                               error_message=f"HTTP {resp.status_code}")
        except requests.exceptions.Timeout as e:
            last_err = safe_error_message(f"Timeout: {e}")
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(f"Timeout -> {delay}s 후 재시도 ({attempt + 1}/{len(RETRY_DELAYS)})")
                time.sleep(delay)
                continue
            return FetchResult(False, http_status=last_status,
                               error_type="TIMEOUT", error_message=last_err)
        except requests.exceptions.RequestException as e:
            last_err = safe_error_message(f"RequestException: {e}")
            if attempt < len(RETRY_DELAYS):
                delay = RETRY_DELAYS[attempt]
                logger.warning(
                    f"RequestException -> {delay}s 후 재시도: "
                    f"{safe_error_message(e)}"
                )
                time.sleep(delay)
                continue
            return FetchResult(False, http_status=last_status,
                               error_type="REQUEST_EXCEPTION", error_message=last_err)
    return FetchResult(False, http_status=last_status,
                       error_type="UNKNOWN", error_message=last_err or "알 수 없는 오류")


def _xml_dir_for(api_target: str) -> Path:
    if api_target == "law":
        return XML_LAW_DIR
    if api_target == "admrul":
        return XML_ADMRUL_DIR
    if api_target == "thdCmp":
        return XML_THDCMP_DIR
    raise ValueError(f"알 수 없는 api_target: {api_target!r}")


def _md_dir_for(api_target: str) -> Path:
    if api_target == "law":
        return MD_LAW_DIR
    if api_target == "admrul":
        return MD_ADMRUL_DIR
    if api_target == "thdCmp":
        return MD_THDCMP_DIR
    raise ValueError(f"알 수 없는 api_target: {api_target!r}")


def _atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str,
    newline: Optional[str] = None,
) -> None:
    """같은 디렉터리의 임시 파일을 완성한 뒤 원자적으로 승격한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open(
            "x",
            encoding=encoding,
            newline=newline,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def save_raw_xml(api_target: str, base_name: str, xml_text: str) -> Path:
    """검증이 끝난 XML을 원자적으로 저장하고 경로를 반환한다."""
    if not xml_text or not xml_text.strip():
        raise ValueError("빈 XML은 캐시로 저장할 수 없습니다.")
    target_dir = _xml_dir_for(api_target)
    path = target_dir / f"{base_name}.xml"
    _atomic_write_text(path, xml_text, encoding="utf-8", newline="")
    return path


# ---------------------------------------------------------------------------
# 파일명 안전화
# ---------------------------------------------------------------------------
_INVALID_FN_CHARS = r'[\\/:*?"<>|\r\n\t]'


def safe_filename(name: str) -> str:
    """Windows에서 안전한 파일명으로 정규화."""
    if not name:
        return "untitled"
    s = name.strip()
    # 금지 문자 제거
    s = re.sub(_INVALID_FN_CHARS, "", s)
    # 공백 -> _
    s = re.sub(r"\s+", "_", s)
    # 연속 _ 정리
    s = re.sub(r"_{2,}", "_", s)
    # 점/공백으로 끝나면 제거 (Windows 제약)
    s = s.rstrip(". _")

    if len(s) > MAX_FILENAME_LEN:
        # 너무 길면 괄호 안 내용을 제거하여 단축 시도
        shortened = re.sub(r"\([^)]*\)", "", s)
        shortened = re.sub(r"_{2,}", "_", shortened).strip("_")
        s = shortened if shortened else s
        if len(s) > MAX_FILENAME_LEN:
            s = s[:MAX_FILENAME_LEN]
    return s or "untitled"


def base_name_for_title(title: str) -> str:
    """명칭으로부터 안전한 단일 base_name을 만든다.

    CSV에는 이제 명칭 외 식별자가 없으므로 충돌 회피 접미사를 만들지 않는다.
    dedup이 문서유형+명칭 기준으로 이뤄져 동명이형은 사실상 발생하지 않는다.
    """
    return safe_filename(title)


_ALLOWED_DOCUMENT_TYPES = {"법령", "행정규칙", "3단비교"}
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _expected_base_name(document_type: str, title: str) -> Tuple[str, str]:
    target_by_type = {
        "법령": "law",
        "행정규칙": "admrul",
        "3단비교": "thdCmp",
    }
    target = target_by_type[document_type]
    base_name = base_name_for_title(title)
    if target == "thdCmp":
        base_name = f"{base_name}{THDCMP_FILE_SUFFIX}"
    return target, base_name


def _is_windows_reserved_filename(base_name: str) -> bool:
    first_component = base_name.rstrip(" .").split(".", 1)[0].rstrip(" ").upper()
    return first_component in _WINDOWS_RESERVED_NAMES


def validate_source_rows(df: pd.DataFrame) -> None:
    """수집 시작 전에 CSV 전체의 값과 예상 출력 경로를 검증한다."""
    required = ["문서유형", "명칭"]
    missing_columns = [column for column in required if column not in df.columns]
    if missing_columns:
        raise ValueError(f"필수 컬럼이 없습니다: {', '.join(missing_columns)}")
    if df.empty:
        raise ValueError("수집 대상 CSV가 비어 있습니다.")

    errors: List[str] = []
    seen_paths: Dict[Tuple[str, str], Tuple[str, str, int]] = {}
    for position, (_, row) in enumerate(df.iterrows(), start=2):
        document_type = str(row.get("문서유형", "") or "").strip()
        title = str(row.get("명칭", "") or "").strip()

        if not document_type:
            errors.append(f"{position}행: 문서유형이 비어 있음")
        elif document_type not in _ALLOWED_DOCUMENT_TYPES:
            errors.append(
                f"{position}행: 허용되지 않은 문서유형 {document_type!r}"
            )
        if not title:
            errors.append(f"{position}행: 명칭이 비어 있음")
        if document_type not in _ALLOWED_DOCUMENT_TYPES or not title:
            continue

        title_base_name = base_name_for_title(title)
        target, base_name = _expected_base_name(document_type, title)
        if title_base_name == "untitled" and title.casefold() != "untitled":
            errors.append(f"{position}행: 명칭을 안전한 파일명으로 변환할 수 없음")
            continue
        if _is_windows_reserved_filename(title_base_name):
            errors.append(
                f"{position}행: Windows 예약 파일명을 사용할 수 없음 "
                f"({title_base_name!r})"
            )
            continue

        normalized_name = unicodedata.normalize("NFC", base_name).casefold()
        path_key = (target, normalized_name)
        current = (document_type, title, position)
        previous = seen_paths.get(path_key)
        if previous is None:
            seen_paths[path_key] = current
        elif previous[:2] != current[:2]:
            errors.append(
                f"{position}행: 예상 파일명 {base_name!r}이 "
                f"{previous[2]}행 {previous[1]!r}과 충돌"
            )

    if errors:
        preview = "\n - ".join(errors[:20])
        remainder = len(errors) - min(len(errors), 20)
        if remainder:
            preview += f"\n - 외 {remainder}건"
        raise ValueError(f"CSV 사전 검증 실패:\n - {preview}")


def find_cached_xml(api_target: str, base_name: str) -> Optional[Path]:
    """캐시된 raw_xml 파일이 있으면 경로를 반환."""
    target_dir = _xml_dir_for(api_target)
    p = target_dir / f"{base_name}.xml"
    if p.exists():
        return p
    return None


# ---------------------------------------------------------------------------
# XML 유틸
# ---------------------------------------------------------------------------
def _localname(tag: str) -> str:
    """namespace를 떼고 로컬 태그명을 반환."""
    if not isinstance(tag, str):
        return ""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _text(elem) -> str:
    """element의 모든 텍스트를 합쳐서 반환."""
    if elem is None:
        return ""
    if _HAS_LXML and isinstance(elem, LET._Element):  # type: ignore[attr-defined]
        parts = list(elem.itertext())
        return "".join(parts).strip()
    # ElementTree
    parts: List[str] = []
    if elem.text:
        parts.append(elem.text)
    for sub in list(elem):
        parts.append(_text(sub))
        if sub.tail:
            parts.append(sub.tail)
    return "".join(parts).strip()


def _find_first(elem, name: str):
    """하위에서 로컬 태그명이 일치하는 첫 element 반환."""
    if elem is None:
        return None
    for sub in elem.iter():
        if _localname(sub.tag) == name:
            return sub
    return None


def _find_all(elem, name: str) -> list:
    """하위에서 로컬 태그명이 일치하는 모든 element 반환."""
    if elem is None:
        return []
    return [sub for sub in elem.iter() if _localname(sub.tag) == name]


def _find_direct(elem, name: str):
    """직속 자식 중 로컬 태그명이 일치하는 첫 element."""
    if elem is None:
        return None
    for sub in list(elem):
        if _localname(sub.tag) == name:
            return sub
    return None


def _find_direct_all(elem, name: str) -> list:
    if elem is None:
        return []
    return [sub for sub in list(elem) if _localname(sub.tag) == name]


def _get_text(elem, name: str) -> str:
    """하위 첫 매칭 요소의 텍스트를 반환."""
    e = _find_first(elem, name)
    return _text(e) if e is not None else ""


_APPENDIX_CONTENT_XML_RE = re.compile(
    r"(<별표내용(?:\s[^>]*)?>)(.*?)(</별표내용>)",
    re.DOTALL,
)
_CDATA_XML_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_UNSAFE_XML_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_EXPECTED_XML_ROOTS = {
    "law": {"법령"},
    "admrul": {"AdmRulService"},
    "thdCmp": {"ThdCmpLawXService"},
}


def _normalize_appendix_cdata_boundaries(xml_text: str) -> str:
    """별표내용의 CDATA 조각 사이에 생긴 XML 구조 줄바꿈을 정규화한다.

    법제처 API는 별표내용의 각 행을 여러 CDATA 조각으로 직렬화한다.
    조각 자체에 줄바꿈이 있으면 조각 경계의 XML 줄바꿈과 중복되므로 경계
    줄바꿈을 추가하지 않는다. 양쪽 조각 모두 줄바꿈을 갖지 않으면 내용 행을
    붙이지 않도록 경계에 줄바꿈 1개를 둔다. CDATA 안의 원문은 변경하지 않는다.
    """
    def normalize_content(match: re.Match[str]) -> str:
        opening, body, closing = match.groups()
        chunks = list(_CDATA_XML_RE.finditer(body))
        if len(chunks) < 2:
            return match.group(0)

        rebuilt: List[str] = [body[:chunks[0].start()], chunks[0].group(0)]
        previous = chunks[0]

        for current in chunks[1:]:
            boundary = body[previous.end():current.start()]
            if boundary.strip():
                rebuilt.append(boundary)
            else:
                previous_payload = previous.group(1)
                current_payload = current.group(1)
                if not (
                    previous_payload.endswith(("\r", "\n"))
                    or current_payload.startswith(("\r", "\n"))
                ):
                    rebuilt.append("\n")
            rebuilt.append(current.group(0))
            previous = current

        rebuilt.append(body[chunks[-1].end():])
        return opening + "".join(rebuilt) + closing

    return _APPENDIX_CONTENT_XML_RE.sub(normalize_content, xml_text)


def parse_xml_root(xml_text: str):
    """외부 엔티티와 복구 파싱을 금지하고 XML root를 반환한다."""
    if not isinstance(xml_text, str) or not xml_text.strip():
        raise ValueError("XML 본문이 비어 있습니다.")
    if _UNSAFE_XML_DECL_RE.search(xml_text):
        raise ValueError("DOCTYPE/ENTITY 선언이 포함된 XML은 허용하지 않습니다.")
    if _HAS_LXML:
        parser = LET.XMLParser(  # type: ignore[attr-defined]
            recover=False,
            resolve_entities=False,
            load_dtd=False,
            no_network=True,
            huge_tree=False,
        )
        data = xml_text.encode("utf-8")
        root = LET.fromstring(data, parser=parser)  # type: ignore[attr-defined]
        if root is None:
            raise ValueError("XML 파싱 실패(lxml): root 없음")
        return root
    parser = ET.XMLParser()
    return ET.fromstring(xml_text, parser=parser)


def _validate_root_for_target(root, api_target: str) -> None:
    expected = _EXPECTED_XML_ROOTS.get(api_target)
    if expected is None:
        raise ValueError(f"알 수 없는 api_target: {api_target!r}")
    actual = _localname(root.tag)
    if actual not in expected:
        expected_label = ", ".join(sorted(expected))
        raise ValueError(
            f"XML root 불일치: target={api_target}, "
            f"예상={expected_label}, 실제={actual or '(empty)'}"
        )


_ARTICLE_BODY_KEYS_BY_TARGET = {
    "law": frozenset({"조문내용", "항내용", "호내용", "목내용"}),
    "admrul": frozenset({"조문내용", "항내용", "호내용", "목내용"}),
    "thdCmp": frozenset({"조내용"}),
}


def _contains_article_body(value: Any, body_keys: frozenset[str]) -> bool:
    """조문 번호ㆍ제목을 제외한 실제 본문 필드가 있는지 확인한다."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in body_keys and bool(str(item or "").strip()):
                return True
            if isinstance(item, (dict, list, tuple)) and _contains_article_body(
                item, body_keys
            ):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_article_body(item, body_keys) for item in value)
    return False


def _validate_parsed_document(parsed: Dict[str, Any], api_target: str) -> None:
    if api_target == "law":
        identifier = parsed.get("법령ID", "")
        title = parsed.get("법령명_한글", "")
    elif api_target == "admrul":
        identifier = (
            parsed.get("행정규칙ID", "")
            or parsed.get("행정규칙일련번호", "")
        )
        title = parsed.get("행정규칙명", "")
    elif api_target == "thdCmp":
        identifier = parsed.get("법령ID", "")
        title = parsed.get("법령명", "")
    else:
        raise ValueError(f"알 수 없는 api_target: {api_target!r}")

    if not str(identifier or "").strip():
        raise ValueError(f"{api_target} XML에 필수 ID가 없습니다.")
    if not str(title or "").strip():
        raise ValueError(f"{api_target} XML에 필수 명칭이 없습니다.")

    articles = parsed.get("articles") or []
    body_keys = _ARTICLE_BODY_KEYS_BY_TARGET[api_target]
    if not articles or not _contains_article_body(articles, body_keys):
        raise ValueError(f"{api_target} XML에 유효한 본문 조문이 없습니다.")


def detect_xml_error(root) -> str:
    """법제처 API 오류 응답 감지. 오류 메시지가 있으면 문자열, 없으면 ''"""
    success_messages = {"정상", "success", "ok"}
    success_codes = {"", "0", "00", "000", "success", "ok"}

    # 잘 알려진 구조화 형식: <RESULT><CODE>...</CODE><MSG>...</MSG></RESULT>
    msg_e = _find_first(root, "MSG")
    if msg_e is not None:
        msg = _text(msg_e).strip()
        code_e = _find_first(root, "CODE")
        code = _text(code_e).strip() if code_e is not None else ""
        normalized_msg = re.sub(r"\s+", " ", msg).casefold()
        normalized_code = code.casefold()
        if normalized_msg in success_messages and normalized_code in success_codes:
            return ""
        if msg or (code and normalized_code not in success_codes):
            return safe_error_message(f"{code} {msg}".strip())

    # 오류 메시지 후보 태그. 부분 문자열이 아닌 정확한 성공값만 제외한다.
    candidates = ["Error", "error", "ErrorMessage", "errorMessage", "ERROR"]
    for tag in candidates:
        e = _find_first(root, tag)
        if e is not None:
            msg = _text(e).strip()
            if msg:
                if re.sub(r"\s+", " ", msg).casefold() in success_messages:
                    continue
                return safe_error_message(msg)

    # RESULT에 자식 없이 문구만 담긴 구형 응답.
    for tag in ("RESULT", "Result", "result"):
        result = _find_first(root, tag)
        if result is None or list(result):
            continue
        message = _text(result).strip()
        if message and re.sub(r"\s+", " ", message).casefold() not in success_messages:
            return safe_error_message(message)
    return ""


# ---------------------------------------------------------------------------
# 링크 정규화
# ---------------------------------------------------------------------------
def normalize_law_link(value: str) -> str:
    """법제처 응답 파일링크를 절대 URL로 정규화."""
    if value is None:
        return ""
    v = str(value).strip()
    if not v:
        return ""
    # HTML entity 복원
    v = html.unescape(v)
    # 줄바꿈/내부 공백 정리 (URL 중간 공백이 있으면 +로 변환된 경우 등은 그대로 둠)
    v = v.strip().replace("\r", "").replace("\n", "")
    if not v:
        return ""

    if v.startswith("http://") or v.startswith("https://"):
        return v
    if v.startswith("//"):
        return "https:" + v
    if v.startswith("/"):
        return LAW_SITE_ROOT + v
    if v.startswith("LSW/") or v.startswith("DRF/"):
        return LAW_SITE_ROOT + "/" + v
    # 그 외 상대값 → 기본적으로 root 기준으로 붙임
    return LAW_SITE_ROOT + "/" + v


# ---------------------------------------------------------------------------
# 파서: 공통 보조
# ---------------------------------------------------------------------------
def _parse_appendix_block(byp_root) -> Dict[str, Any]:
    """별표 1건의 공통 필드를 dict로 반환."""
    별표번호 = _get_text(byp_root, "별표번호")
    별표가지번호 = _get_text(byp_root, "별표가지번호")
    별표구분 = _get_text(byp_root, "별표구분")
    별표제목 = _get_text(byp_root, "별표제목") or _get_text(byp_root, "별표제목문자열")
    별표시행일자 = _get_text(byp_root, "별표시행일자")
    별표내용 = _get_text(byp_root, "별표내용")

    파일링크 = normalize_law_link(_get_text(byp_root, "별표서식파일링크"))
    PDF링크 = normalize_law_link(_get_text(byp_root, "별표서식PDF파일링크"))
    이미지링크 = normalize_law_link(_get_text(byp_root, "별표서식이미지파일링크"))

    # HWP/파일 일반은 파일링크에 통합 (확장자가 다양할 수 있음)
    return {
        "별표번호": 별표번호,
        "별표가지번호": 별표가지번호,
        "별표구분": 별표구분,
        "별표제목": 별표제목,
        "별표시행일자": 별표시행일자,
        "별표내용": 별표내용,
        "별표서식파일링크": 파일링크,
        "별표서식PDF파일링크": PDF링크,
        "별표서식이미지파일링크": 이미지링크,
    }


def _parse_article_block_law(jo) -> Dict[str, Any]:
    """법령 조문 1건 파싱.

    조문여부('조문'/'전문')와 각 단위의 가지번호도 함께 추출한다.
    조문여부='전문'은 편/장/절/관 헤더가 담긴 배치용 블록을 뜻하며,
    항/호/목 가지번호는 'N의M' 표기 렌더링에 사용한다.
    """
    조문번호 = _get_text(jo, "조문번호")
    조문가지번호 = _get_text(jo, "조문가지번호")
    조문여부 = _get_text(jo, "조문여부")
    조문제목 = _get_text(jo, "조문제목")
    조문내용 = _get_text(jo, "조문내용")
    조문참고자료 = _get_text(jo, "조문참고자료")

    # 항 / 호 / 목 (중첩될 수 있음)
    hangs: List[Dict[str, Any]] = []
    for hang in _find_direct_all(jo, "항"):
        h_no = _get_text(hang, "항번호")
        h_branch = _get_text(hang, "항가지번호")
        h_text = _get_text(hang, "항내용")
        hos: List[Dict[str, Any]] = []
        for ho in _find_direct_all(hang, "호"):
            ho_no = _get_text(ho, "호번호")
            ho_branch = _get_text(ho, "호가지번호")
            ho_text = _get_text(ho, "호내용")
            moks: List[Dict[str, Any]] = []
            for mok in _find_direct_all(ho, "목"):
                m_no = _get_text(mok, "목번호")
                m_branch = _get_text(mok, "목가지번호")
                m_text = _get_text(mok, "목내용")
                moks.append({"목번호": m_no, "목가지번호": m_branch, "목내용": m_text})
            hos.append({"호번호": ho_no, "호가지번호": ho_branch, "호내용": ho_text, "목": moks})
        hangs.append({"항번호": h_no, "항가지번호": h_branch, "항내용": h_text, "호": hos})

    return {
        "조문번호": 조문번호,
        "조문가지번호": 조문가지번호,
        "조문여부": 조문여부,
        "조문제목": 조문제목,
        "조문내용": 조문내용,
        "조문참고자료": 조문참고자료,
        "항": hangs,
    }


# ---------------------------------------------------------------------------
# 파서: 법령
# ---------------------------------------------------------------------------
def parse_law_xml(xml_text: str) -> Dict[str, Any]:
    root = parse_xml_root(_normalize_appendix_cdata_boundaries(xml_text))
    err = detect_xml_error(root)
    if err:
        raise ValueError(f"API 오류 응답: {err}")
    _validate_root_for_target(root, "law")

    basic = _find_first(root, "기본정보")
    if basic is None:
        basic = root

    parsed: Dict[str, Any] = {
        "type": "law",
        "법령ID": _get_text(basic, "법령ID") or _get_text(root, "법령ID"),
        "법령명_한글": _get_text(basic, "법령명_한글") or _get_text(basic, "법령명한글"),
        "법령명약칭": _get_text(basic, "법령명약칭"),
        "법종구분": _get_text(basic, "법종구분"),
        "공포일자": _get_text(basic, "공포일자"),
        "공포번호": _get_text(basic, "공포번호"),
        "시행일자": _get_text(basic, "시행일자"),
        "소관부처": _get_text(basic, "소관부처"),
        "부서명": _get_text(basic, "부서명"),
        "부서연락처": _get_text(basic, "부서연락처"),
        "articles": [],
        "addenda": [],
        "appendices": [],
        "amendments": [],
        "reasons": [],
    }

    # 조문
    for jo in _find_all(root, "조문단위"):
        parsed["articles"].append(_parse_article_block_law(jo))
    if not parsed["articles"]:
        # 일부 응답은 조문이 <조> 또는 <조문>으로 올 수 있음
        for jo in _find_all(root, "조"):
            parsed["articles"].append(_parse_article_block_law(jo))

    # 부칙
    for bc in _find_all(root, "부칙단위"):
        bc_no = _get_text(bc, "부칙공포번호") or _get_text(bc, "부칙공포일자")
        bc_text = _get_text(bc, "부칙내용")
        if bc_text or bc_no:
            parsed["addenda"].append({"부칙공포번호": bc_no, "부칙내용": bc_text})
    if not parsed["addenda"]:
        for bc in _find_all(root, "부칙"):
            bc_text = _get_text(bc, "부칙내용") or _text(bc)
            if bc_text:
                parsed["addenda"].append({"부칙공포번호": "", "부칙내용": bc_text})

    # 별표
    appendix_roots = _find_all(root, "별표단위")
    if not appendix_roots:
        appendix_roots = _find_all(root, "별표")
    for byp in appendix_roots:
        parsed["appendices"].append(_parse_appendix_block(byp))

    # 개정문/제개정이유
    for e in _find_all(root, "개정문내용"):
        t = _text(e)
        if t:
            parsed["amendments"].append(t)
    for e in _find_all(root, "제개정이유내용"):
        t = _text(e)
        if t:
            parsed["reasons"].append(t)

    _validate_parsed_document(parsed, "law")
    return parsed


# ---------------------------------------------------------------------------
# 파서: 행정규칙
# ---------------------------------------------------------------------------
def parse_admrul_xml(xml_text: str) -> Dict[str, Any]:
    root = parse_xml_root(_normalize_appendix_cdata_boundaries(xml_text))
    err = detect_xml_error(root)
    if err:
        raise ValueError(f"API 오류 응답: {err}")
    _validate_root_for_target(root, "admrul")

    basic = _find_first(root, "행정규칙기본정보")
    if basic is None:
        basic = _find_first(root, "기본정보")
    if basic is None:
        basic = root

    parsed: Dict[str, Any] = {
        "type": "admrul",
        "행정규칙일련번호": _get_text(basic, "행정규칙일련번호"),
        "행정규칙명": _get_text(basic, "행정규칙명"),
        "행정규칙종류": _get_text(basic, "행정규칙종류"),
        "발령일자": _get_text(basic, "발령일자"),
        "발령번호": _get_text(basic, "발령번호"),
        "제개정구분명": _get_text(basic, "제개정구분명"),
        "조문형식여부": _get_text(basic, "조문형식여부"),
        "행정규칙ID": _get_text(basic, "행정규칙ID"),
        "소관부처명": _get_text(basic, "소관부처명"),
        "담당부서기관명": _get_text(basic, "담당부서기관명"),
        "전화번호": _get_text(basic, "전화번호"),
        "현행여부": _get_text(basic, "현행여부"),
        "시행일자": _get_text(basic, "시행일자"),
        "articles": [],
        "addenda": [],
        "appendices": [],
        "attachments": [],
        "amendments": [],
        "reasons": [],
    }

    # 조문 (행정규칙은 조문 구조가 단순 텍스트일 수 있음)
    article_units = _find_all(root, "조문단위")
    if article_units:
        for jo in article_units:
            parsed["articles"].append(_parse_article_block_law(jo))
    else:
        # 조문내용이 단일/다수로 올 수 있음
        for jo in _find_all(root, "조문내용"):
            t = _text(jo)
            if t:
                parsed["articles"].append(
                    {"조문번호": "", "조문가지번호": "", "조문제목": "",
                     "조문내용": t, "조문참고자료": "", "항": []}
                )

    # 부칙
    for bc in _find_all(root, "부칙단위"):
        bc_no = _get_text(bc, "부칙공포번호") or _get_text(bc, "부칙공포일자")
        bc_text = _get_text(bc, "부칙내용")
        if bc_text or bc_no:
            parsed["addenda"].append({"부칙공포번호": bc_no, "부칙내용": bc_text})
    if not parsed["addenda"]:
        for bc in _find_all(root, "부칙내용"):
            t = _text(bc)
            if t:
                parsed["addenda"].append({"부칙공포번호": "", "부칙내용": t})

    # 별표
    appendix_roots = _find_all(root, "별표단위")
    if not appendix_roots:
        appendix_roots = _find_all(root, "별표")
    for byp in appendix_roots:
        parsed["appendices"].append(_parse_appendix_block(byp))

    # 첨부파일 (행정규칙에서는 첨부파일명/첨부파일링크가 페어로 다수 등장 가능)
    att_units = _find_all(root, "첨부파일단위") or _find_all(root, "첨부파일")
    if att_units:
        for au in att_units:
            name = _get_text(au, "첨부파일명")
            link = normalize_law_link(_get_text(au, "첨부파일링크"))
            if name or link:
                parsed["attachments"].append({"첨부파일명": name, "첨부파일링크": link})
    else:
        names = [_text(e) for e in _find_all(root, "첨부파일명") if _text(e)]
        links = [normalize_law_link(_text(e)) for e in _find_all(root, "첨부파일링크") if _text(e)]
        for i in range(max(len(names), len(links))):
            parsed["attachments"].append({
                "첨부파일명": names[i] if i < len(names) else "",
                "첨부파일링크": links[i] if i < len(links) else "",
            })

    # 개정문/제개정이유
    for e in _find_all(root, "개정문내용"):
        t = _text(e)
        if t:
            parsed["amendments"].append(t)
    for e in _find_all(root, "제개정이유내용"):
        t = _text(e)
        if t:
            parsed["reasons"].append(t)

    _validate_parsed_document(parsed, "admrul")
    return parsed


# ---------------------------------------------------------------------------
# 파서: 3단비교(인용조문)
# ---------------------------------------------------------------------------
def _parse_thdcmp_article(jo) -> Dict[str, Any]:
    """3단비교 응답의 법률조문/시행령조문/시행규칙조문 1건을 파싱한다.

    하위에 시행령조문목록/시행규칙조문목록을 가질 수 있다(법률조문에 한함).
    위임행정규칙목록은 본 프로젝트에서 사용하지 않으므로 무시한다.
    """
    art: Dict[str, Any] = {
        "조번호": _get_text(jo, "조번호"),
        "조가지번호": _get_text(jo, "조가지번호"),
        "조제목": _get_text(jo, "조제목"),
        "조내용": _get_text(jo, "조내용"),
        "시행령조문": [],
        "시행규칙조문": [],
    }

    enf_list = _find_direct(jo, "시행령조문목록")
    if enf_list is not None:
        for sub in _find_direct_all(enf_list, "시행령조문"):
            art["시행령조문"].append({
                "조번호": _get_text(sub, "조번호"),
                "조가지번호": _get_text(sub, "조가지번호"),
                "조제목": _get_text(sub, "조제목"),
                "조내용": _get_text(sub, "조내용"),
            })

    rule_list = _find_direct(jo, "시행규칙조문목록")
    if rule_list is not None:
        for sub in _find_direct_all(rule_list, "시행규칙조문"):
            art["시행규칙조문"].append({
                "조번호": _get_text(sub, "조번호"),
                "조가지번호": _get_text(sub, "조가지번호"),
                "조제목": _get_text(sub, "조제목"),
                "조내용": _get_text(sub, "조내용"),
            })
    return art


def parse_thdcmp_xml(xml_text: str) -> Dict[str, Any]:
    """3단비교(인용조문, knd=1) XML을 파싱한다."""
    root = parse_xml_root(xml_text)
    err = detect_xml_error(root)
    if err:
        raise ValueError(f"API 오류 응답: {err}")
    _validate_root_for_target(root, "thdCmp")

    basic = _find_first(root, "기본정보")
    if basic is None:
        basic = root

    parsed: Dict[str, Any] = {
        "type": "thdCmp",
        "법령ID": _get_text(basic, "법령ID"),
        "시행령ID": _get_text(basic, "시행령ID"),
        "시행규칙ID": _get_text(basic, "시행규칙ID"),
        "법령명": _get_text(basic, "법령명"),
        "시행령명": _get_text(basic, "시행령명"),
        "시행규칙명": _get_text(basic, "시행규칙명"),
        "법령요약정보": _get_text(basic, "법령요약정보"),
        "시행령요약정보": _get_text(basic, "시행령요약정보"),
        "시행규칙요약정보": _get_text(basic, "시행규칙요약정보"),
        "삼단비교기준": _get_text(basic, "삼단비교기준"),
        "삼단비교존재여부": _get_text(basic, "삼단비교존재여부"),
        "articles": [],
    }

    body = _find_first(root, "인용조문삼단비교")
    if body is None:
        body = root
    for jo in _find_direct_all(body, "법률조문"):
        parsed["articles"].append(_parse_thdcmp_article(jo))

    _validate_parsed_document(parsed, "thdCmp")
    return parsed


def parse_and_validate_xml(api_target: str, xml_text: str) -> Dict[str, Any]:
    """target에 맞는 엄격 파서로 XML을 파싱하고 의미 구조를 검증한다."""
    if api_target == "law":
        return parse_law_xml(xml_text)
    if api_target == "admrul":
        return parse_admrul_xml(xml_text)
    if api_target == "thdCmp":
        return parse_thdcmp_xml(xml_text)
    raise ValueError(f"알 수 없는 api_target: {api_target!r}")


# ---------------------------------------------------------------------------
# Markdown 렌더링
# ---------------------------------------------------------------------------
# --- 정규화 / 구조 감지 (converter.py 규칙 이식) ---
_DOT_NORMALIZE = str.maketrans({
    "·": "ㆍ",  # · Middle Dot -> ㆍ Hangul Letter Araea
    "・": "ㆍ",  # ・ Katakana Middle Dot -> ㆍ
    "･": "ㆍ",  # ･ Halfwidth Katakana Middle Dot -> ㆍ
})

# 편/장/절/관(및 드문 항/속/목) 구조 헤더 감지. '제N장 …' / '제N편 …' 형태.
_STRUCTURE_RE = re.compile(
    r"^제\s*[\d일이삼사오육칠팔구십]+(?:의\s*[\d일이삼사오육칠팔구십]+)?\s*([편장절관항속목])\s*"
)
# 조문내용 앞의 '제N조(제목)' 접두어 (헤더로 이미 출력되므로 본문에서 제거)
_JO_PREFIX_RE = re.compile(r"^제\d+조(?:의\d+)?\s*(?:\([^)]*\)\s*)?")
# 평면 행정규칙의 실제 조문 헤더만 인식한다. '제7조제1호…', '제6조에…'처럼
# 교차참조로 시작하는 본문을 조문 헤더로 오인하지 않도록 조 번호 뒤 경계를 확인한다.
_FLAT_ADMRUL_JO_HEADING_RE = re.compile(
    r"^제\s*(\d+)\s*조(?:의\s*(\d+))?"
    r"(?:(?:\s*\(([^)]*)\))|(?=\s|$))\s*"
)
_HO_PREFIX_RE = re.compile(r"^\d+(?:의\d+)?\.\s*")
_MOK_PREFIX_RE = re.compile(r"^[가-힣](?:의\d+)?\.\s*")
# 목내용 안에서 별도 XML 요소 없이 텍스트로 들어오는 1), 2) 형식의 하위 열거.
# 줄 시작 표기만 대상으로 삼아 문장 중간의 "1) 또는 2)"는 보존한다.
_MOK_CHILD_ORDERED_LIST_RE = re.compile(r"(?m)^([ \t]*\d{1,9})\)(?=[ \t])")
_CIRCLED_RE = re.compile(r"^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]\s*")

# NFTC/NFPC 화재안전기술기준: 조문번호·항이 없고 본문 전체가
# '1. / 1.1 / 1.1.1 …' 숫자 체계 한 덩어리로 오는 형태를 감지한다.
# (전수 조사 결과 이 형태는 NFTC/NFPC 문서에만 존재하여 오적용 위험이 없다.)
_NFTC_LEAD_RE = re.compile(r"^\d+(?:\.\d+)*\.?(?:\s|$)")

# 구조 헤더 레벨(converter.py와 동일): 편=#, 장=##, 절=###, 관=####
_STRUCTURE_LEVEL = {"편": "#", "장": "##", "절": "###", "관": "####"}
_CAPTION_STRUCTURE_TYPES = {"항", "속", "목"}

# 별표·서식 원문은 Markdown이 아니므로, 원문 문자가 우연히 Markdown 문법으로
# 해석되는 경우만 선택적으로 중화한다. 백슬래시 이스케이프는 렌더링하면 원래
# 문자로 보이며, 들여쓰기 코드블록은 첫 공백을 숫자 엔터티로 바꿔 차단한다.
_APPENDIX_SETEXT_OR_HR_RE = re.compile(
    r"^ {0,3}(?:=+|-+|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})[ \t]*$"
)
_APPENDIX_GFM_TABLE_DELIMITER_RE = re.compile(
    r"^ {0,3}\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_APPENDIX_ORDERED_LIST_RE = re.compile(r"^([ ]{0,3}\d{1,9})([.)])([ \t]+)")
_APPENDIX_UNORDERED_LIST_RE = re.compile(r"^([ ]{0,3})([-+*])([ \t]+)")
_APPENDIX_ATX_HEADING_RE = re.compile(r"^([ ]{0,3})(#{1,6})(?=[ \t]|$)")
_APPENDIX_BLOCKQUOTE_RE = re.compile(r"^([ ]{0,3})(>)")
_APPENDIX_FENCE_RE = re.compile(r"^([ ]{0,3})(`{3,}|~{3,})")
_APPENDIX_LINK_DEFINITION_RE = re.compile(r"^([ ]{0,3})(\[[^\]]+\])(:[ \t]*\S)")
_APPENDIX_INLINE_LINK_RE = re.compile(r"(?<!\\)(!?)\[([^\]\n]+)\]\(([^)\n]*)\)")
_APPENDIX_AUTOLINK_RE = re.compile(r"(?<!\\)<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^ <>]*)>")
_APPENDIX_ASCII_HTML_RE = re.compile(
    r"(?<!\\)</?[A-Za-z][A-Za-z0-9-]*(?:[ \t]+[^<>]*)?/?>"
)
_APPENDIX_HTML_SPECIAL_RE = re.compile(r"(?<!\\)<(?=!--|\?|!\[CDATA\[|![A-Z])")
_APPENDIX_LITERAL_INLINE_RE = re.compile(r"(?<!\\)([*_`])")

# 개정문·제개정이유는 법령식 번호와 의도된 하이픈 목록을 포함하므로 별표보다
# 좁은 범위만 중화한다. 숫자 목록, 들여쓰기 코드블록, 백틱 및 API의 img 태그가
# 실제 의미와 다르게 해석되는 경우만 대상으로 삼는다.
_REVISION_ORDERED_LIST_RE = re.compile(r"^([ ]{0,3}\d{1,9})([.)])([ \t]+)")
_REVISION_IMAGE_TAG_RE = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_REVISION_IMAGE_CLOSE_RE = re.compile(r"</img\s*>", re.IGNORECASE)
_REVISION_LITERAL_BACKTICK_RE = re.compile(r"(?<!\\)`")


def normalize_dots(s: str) -> str:
    """가운뎃점 변형(·・･)을 표준 ㆍ(U+318D)로 통일."""
    return (s or "").translate(_DOT_NORMALIZE)


def normalize_appendix_blank_lines(text: str) -> str:
    """별표·서식 본문의 불필요한 빈 행을 보수적으로 축소한다.

    내용 행 사이의 빈 행이 1개면 제거하고, 2개 이상 연속되면 1개만
    남긴다. 내용이 있는 행의 문자, 순서, 행 내부 공백은 변경하지 않는다.
    법령과 행정규칙의 모든 별표 구분에 공통 적용한다.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized: List[str] = []
    index = 0

    while index < len(lines):
        if lines[index].strip():
            normalized.append(lines[index])
            index += 1
            continue

        run_end = index + 1
        while run_end < len(lines) and not lines[run_end].strip():
            run_end += 1

        if run_end - index >= 2:
            normalized.append("")
        index = run_end

    return "\n".join(normalized)


def neutralize_appendix_markdown(text: str) -> str:
    """별표·서식 원문이 Markdown 문법으로 오인되지 않도록 중화한다.

    목록, 제목, 인용문, 구분선, 링크, 강조, 코드 등 실제 문법이 성립하는
    기호에만 백슬래시를 추가한다. 빈 행 다음에 공백 4칸 이상 또는 탭으로
    시작하는 행은 들여쓰기 코드블록이 되지 않도록 첫 공백을 숫자 엔터티로
    바꾼다. Markdown으로 렌더링했을 때 보이는 원문과 행 정렬은 유지된다.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    neutralized: List[str] = []

    def escape_first_nonspace(line: str) -> str:
        position = len(line) - len(line.lstrip(" "))
        return line[:position] + "\\" + line[position:]

    for index, original_line in enumerate(lines):
        line = original_line
        at_block_boundary = index == 0 or not lines[index - 1].strip()

        # Markdown은 빈 행 다음의 공백 4칸/탭을 코드블록으로 해석한다.
        if at_block_boundary and re.match(r"^(?: {4,}|\t)", line):
            if line.startswith("\t"):
                line = "&#9;" + line[1:]
            else:
                line = "&#32;" + line[1:]
        elif _APPENDIX_SETEXT_OR_HR_RE.match(line):
            line = escape_first_nonspace(line)
        elif _APPENDIX_GFM_TABLE_DELIMITER_RE.match(line):
            hyphen = line.find("-")
            line = line[:hyphen] + "\\" + line[hyphen:]
        elif _APPENDIX_ATX_HEADING_RE.match(line):
            line = _APPENDIX_ATX_HEADING_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2), line, count=1
            )
        elif _APPENDIX_BLOCKQUOTE_RE.match(line):
            line = _APPENDIX_BLOCKQUOTE_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2), line, count=1
            )
        elif _APPENDIX_FENCE_RE.match(line):
            line = _APPENDIX_FENCE_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2), line, count=1
            )
        elif _APPENDIX_ORDERED_LIST_RE.match(line):
            line = _APPENDIX_ORDERED_LIST_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2) + match.group(3),
                line,
                count=1,
            )
        elif _APPENDIX_UNORDERED_LIST_RE.match(line):
            line = _APPENDIX_UNORDERED_LIST_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2) + match.group(3),
                line,
                count=1,
            )
        elif _APPENDIX_LINK_DEFINITION_RE.match(line):
            line = _APPENDIX_LINK_DEFINITION_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2) + match.group(3),
                line,
                count=1,
            )

        line = _APPENDIX_INLINE_LINK_RE.sub(
            lambda match: (
                f"{match.group(1)}\\[{match.group(2)}]({match.group(3)})"
            ),
            line,
        )
        line = _APPENDIX_AUTOLINK_RE.sub(
            lambda match: "\\<" + match.group(1) + ">", line
        )
        line = _APPENDIX_ASCII_HTML_RE.sub(
            lambda match: "\\" + match.group(0), line
        )
        line = _APPENDIX_HTML_SPECIAL_RE.sub(r"\\<", line)
        line = _APPENDIX_LITERAL_INLINE_RE.sub(r"\\\1", line)
        neutralized.append(line)

    return "\n".join(neutralized)


def _revision_image_text(attributes: str) -> str:
    """API의 img 태그 속성을 NotebookLM이 읽을 수 있는 텍스트로 바꾼다."""
    values: Dict[str, str] = {}
    for match in re.finditer(
        r"\b(src|id|alt)\s*=\s*(?:([\"'])(.*?)\2|([^\s\"'=<>`]+))",
        attributes,
        re.IGNORECASE,
    ):
        values[match.group(1).lower()] = html.unescape(match.group(3) or match.group(4) or "")

    src = normalize_law_link(values.get("src", ""))
    if src:
        return f"이미지 원문(URL: {src})"

    image_id = values.get("id", "").strip()
    if not image_id:
        alt = values.get("alt", "").strip()
        image_id_match = re.search(r"\d+", alt)
        image_id = image_id_match.group(0) if image_id_match else ""
    if image_id:
        return f"이미지 참조(ID: {image_id})"
    return "이미지 참조"


def neutralize_revision_markdown(text: str) -> str:
    """개정문·제개정이유의 원문을 보존하며 Markdown 오인만 중화한다.

    원래의 줄바꿈과 행 내부 공백은 유지한다. 문서 시작 또는 빈 행 다음의
    탭/공백 4칸 이상 들여쓰기는 코드블록이 되지 않게 첫 칸만 숫자 엔터티로
    바꾸고, CommonMark 숫자 목록과 백틱은 백슬래시로 이스케이프한다.
    API가 텍스트 안에 넣은 img 태그는 URL 또는 이미지 ID를 남기는 평문으로
    변환한다. 의도된 하이픈 목록과 법령식 '가.', '(1)' 표기는 그대로 둔다.
    """
    if not text:
        return ""

    converted = _REVISION_IMAGE_CLOSE_RE.sub("", text)
    converted = _REVISION_IMAGE_TAG_RE.sub(
        lambda match: _revision_image_text(match.group(1)), converted
    )
    # 한 행에 img 태그가 연속된 API 응답은 각 이미지 참조를 분리해 읽게 한다.
    converted = re.sub(
        r"\)(?=이미지 (?:원문|참조)\()",
        ") / ",
        converted,
    )

    lines = converted.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    neutralized: List[str] = []
    for index, original_line in enumerate(lines):
        line = original_line
        at_block_boundary = index == 0 or not lines[index - 1].strip()

        if at_block_boundary and re.match(r"^(?: {4,}|\t)", line):
            if line.startswith("\t"):
                line = "&#9;" + line[1:]
            else:
                line = "&#32;" + line[1:]
        elif _REVISION_ORDERED_LIST_RE.match(line):
            line = _REVISION_ORDERED_LIST_RE.sub(
                lambda match: match.group(1) + "\\" + match.group(2) + match.group(3),
                line,
                count=1,
            )

        line = _REVISION_LITERAL_BACKTICK_RE.sub(r"\`", line)
        neutralized.append(line)

    return "\n".join(neutralized)


def normalize_addendum_blank_lines(text: str) -> str:
    """부칙의 각 원문 내용 행을 독립된 Markdown 문단으로 정규화한다.

    법령 API는 내용 행 사이에 빈 행을 2개씩 넣고, 행정규칙 API는 같은
    법적 단위를 빈 행 없이 연속해서 제공한다. 빈 행 수에는 구조 정보가 없고
    실제 구조는 내용 행 자체에 있으므로, 내용이 있는 행의 문자·순서·행 내부
    공백은 유지하면서 각 행 사이의 빈 행을 정확히 1개로 통일한다.
    """
    if not text:
        return ""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    content_lines = [line for line in lines if line.strip()]
    return "\n\n".join(content_lines)


def format_date(date_str: str) -> str:
    """YYYYMMDD -> YYYY-MM-DD. 8자리 숫자가 아니면 원본 유지."""
    s = (date_str or "").strip()
    if len(s) != 8 or not s.isdigit():
        return s
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def _normalize_ws_inline(text: str) -> str:
    """수평 공백(스페이스/탭) 연속을 단일 공백으로. 줄바꿈은 보존."""
    return re.sub(r"[ \t]+", " ", text or "").strip()


def _neutralize_mok_child_ordered_lists(text: str) -> str:
    """목내용의 줄 시작 ``숫자)``를 Markdown 순서목록이 아니게 만든다.

    백슬래시는 Markdown 렌더링 결과에 표시되지 않으므로 원문의 가시적인
    ``1)`` 표기는 유지된다. 문장 중간 표기와 ``(1)`` 형식은 건드리지 않는다.
    """
    return _MOK_CHILD_ORDERED_LIST_RE.sub(
        lambda match: f"{match.group(1)}\\)",
        text or "",
    )


def _render_article_reference_lines(reference: str) -> List[str]:
    """여러 줄의 조문참고자료를 하나의 Markdown 인용문으로 렌더한다.

    API가 후속 참고자료에 붙이는 탭 들여쓰기를 제거하고 모든 내용 줄에
    인용문 표식을 붙인다. 원문의 빈 줄은 빈 인용문 줄로 유지한다.
    """
    ref = normalize_dots((reference or "").strip())
    if not ref:
        return []

    lines: List[str] = []
    first_content = True
    for raw_line in ref.splitlines():
        content = raw_line.strip()
        if content:
            prefix = "> 참고자료: " if first_content else "> "
            lines.append(f"{prefix}{content}")
            first_content = False
        else:
            lines.append(">")
    return lines


def _render_nftc_lines(content: str, ref: str = "") -> List[str]:
    """NFTC/NFPC 숫자 체계 본문(한 덩어리)을 헤더 계층으로 승격해 렌더한다.

    - 깊이 1('1. 일반사항')  -> #####  (조 상당 대분류)
    - 깊이 2('1.1 적용범위')  -> ###### (절 상당 중분류)
    - 깊이 3 이상('1.1.1 …')  -> 번호를 유지한 본문(실제 규정 문장)
    빈 줄로 구분된 문단 단위로 처리하며, 번호가 없는 문단은 그대로 본문으로 둔다.
    """
    lines: List[str] = []
    for block in re.split(r"\n\s*\n", content):
        block = block.strip()
        if not block:
            continue
        m = re.match(r"^(\d+(?:\.\d+)*)", block)
        depth = (m.group(1).count(".") + 1) if (m and _NFTC_LEAD_RE.match(block)) else 0
        if depth in (1, 2):
            first, _, rest = block.partition("\n")
            hdr = "#####" if depth == 1 else "######"
            lines.append(f"{hdr} {first.strip()}")
            lines.append("")
            if rest.strip():
                lines.append(rest.strip())
                lines.append("")
        else:
            lines.append(block)
            lines.append("")
    ref_lines = _render_article_reference_lines(ref)
    if ref_lines:
        lines.extend(ref_lines)
        lines.append("")
    return lines


def _render_law_article_lines(
    art: Dict[str, Any], *, flat_legal_rules: bool = False
) -> List[str]:
    """법령/행정규칙 조문 1건을 Markdown 라인 목록으로 렌더한다(converter.py 규칙).

    - 조문여부='전문'이면서 조문내용이 '제N장 …' 형태면 편/장/절/관 헤더로 승격한다.
      (조문번호는 배치 키일 뿐이므로 '제N조' 헤더로 만들지 않는다.)
    - 조 헤더는 '##### 제N조 (제목)'. 조문내용의 중복 '제N조(제목)' 접두어는 제거한다.
    - 항은 '**번호** 내용', 호는 2칸 들여쓰기 'N\\.', 목은 4칸 들여쓰기 '가\\.'로
      조판하고, 목내용의 줄 시작 'N)'도 'N\\)'로 바꾸어 Markdown 자동
      순서목록화를 방지한다.
    - 가운뎃점은 ㆍ로 통일하고, 호/목 라인은 수평 공백을 단일화한다.
    - 조문참고자료는 인용문으로 보존한다(데이터 유실 방지).
    - flat_legal_rules=True이면 XML 구조 없이 조문내용에 포함된 항ㆍ호ㆍ목
      표지도 같은 규칙으로 조판한다.
    """
    lines: List[str] = []
    number = (art.get("조문번호") or "").strip()
    branch = (art.get("조문가지번호") or "").strip()
    kind = (art.get("조문여부") or "").strip()
    title = (art.get("조문제목") or "").strip()
    content = normalize_dots((art.get("조문내용") or "").strip())
    inferred_prefix_end: Optional[int] = None

    # 편/장/절/관 등 구조 헤더 감지
    match = _STRUCTURE_RE.match(content) if (not title and content) else None
    if match and (kind == "전문" or not kind):
        st = match.group(1)
        level = _STRUCTURE_LEVEL.get(st)
        if level:
            lines.append(f"{level} {content}")
            lines.append("")
            return lines
        if st in _CAPTION_STRUCTURE_TYPES:
            lines.append(f"**{content}**")
            lines.append("")
            return lines

    # 그 외 '전문' 블록(구조 헤더가 아님)은 본문 텍스트로만 출력
    if kind == "전문":
        if content:
            lines.append(content)
            lines.append("")
        return lines

    # NFTC/NFPC 화재안전기술기준: 조문번호·항이 없고 본문이 '1. / 1.1 …' 숫자 체계로
    # 시작하면 번호 체계를 헤더 계층으로 승격한다(다른 문서에는 없는 형태).
    if not number and not (art.get("항") or []) and _NFTC_LEAD_RE.match(content):
        ref = normalize_dots((art.get("조문참고자료") or "").strip())
        return _render_nftc_lines(content, ref)

    # 조문번호 필드가 비어 있는 일부 행정규칙: 조문내용 앞의 '제N조(제목)'에서
    # 번호·가지·제목을 보강한다(접두어는 아래 공통 로직에서 제거되므로 유실되지 않는다).
    if not number:
        if flat_legal_rules:
            m = _FLAT_ADMRUL_JO_HEADING_RE.match(content)
        else:
            m = re.match(r"^제(\d+)조(?:의(\d+))?\s*(?:\(([^)]*)\))?\s*", content)
        if m:
            number = m.group(1)
            if m.group(2):
                branch = m.group(2)
            if not title and m.group(3):
                title = m.group(3)
            inferred_prefix_end = m.end()

    # 조 헤더 (번호가 있을 때만 '제N조'. 번호가 없고 제목만 있으면 제목을 헤더로,
    # 번호·제목이 모두 없으면 — 폐지 고시 등 단일 문장 — 헤더 없이 본문만 출력)
    strip_prefix = True
    if number:
        heading = f"##### 제{number}조"
        if branch and branch != "0":
            heading += f"의{branch}"
        if title:
            heading += f" ({title})"
        lines.append(heading)
        lines.append("")
    elif title:
        lines.append(f"##### {title}")
        lines.append("")
    else:
        strip_prefix = False  # 헤더가 없으니 본문 접두어를 제거하지 않는다

    if content:
        if strip_prefix and inferred_prefix_end is not None:
            cleaned = content[inferred_prefix_end:]
        else:
            cleaned = _JO_PREFIX_RE.sub("", content) if strip_prefix else content
        if cleaned:
            if flat_legal_rules:
                cleaned = _render_flat_legal_body(cleaned)
            lines.append(cleaned)
            lines.append("")

    for para in art.get("항") or []:
        para_num = (para.get("항번호") or "").strip()
        para_branch = (para.get("항가지번호") or "").strip()
        if para_num and para_branch:
            para_num = f"{para_num}의{para_branch}"
        para_content = normalize_dots(para.get("항내용") or "")
        if para_content:
            stripped = _CIRCLED_RE.sub("", para_content.strip())
            if para_num:
                lines.append(f"**{para_num}** {stripped}")
            else:
                lines.append(stripped)
            lines.append("")

        for subpara in para.get("호") or []:
            sub_num = (subpara.get("호번호") or "").strip().rstrip(".")
            sub_branch = (subpara.get("호가지번호") or "").strip()
            if sub_num and sub_branch:
                sub_num = f"{sub_num}의{sub_branch}"
            sub_content = normalize_dots(subpara.get("호내용") or "")
            if sub_content:
                s = _normalize_ws_inline(_HO_PREFIX_RE.sub("", sub_content.strip()))
                if sub_num:
                    lines.append(f"  {sub_num}\\. {s}")
                else:
                    lines.append(f"  {s}")

            for item in subpara.get("목") or []:
                item_num = (item.get("목번호") or "").strip().rstrip(".")
                item_branch = (item.get("목가지번호") or "").strip()
                if item_num and item_branch:
                    item_num = f"{item_num}의{item_branch}"
                item_content = normalize_dots(item.get("목내용") or "")
                if item_content:
                    s = _normalize_ws_inline(_MOK_PREFIX_RE.sub("", item_content.strip()))
                    s = _neutralize_mok_child_ordered_lists(s)
                    if item_num:
                        lines.append(f"    {item_num}\\. {s}")
                    else:
                        lines.append(f"    {s}")

        if para.get("호"):
            lines.append("")

    ref_lines = _render_article_reference_lines(art.get("조문참고자료") or "")
    if ref_lines:
        lines.extend(ref_lines)
        lines.append("")

    return lines


def render_law_body(
    articles: List[Dict[str, Any]], *, flat_legal_rules: bool = False
) -> str:
    """조문 리스트를 편/장/절/관 계층이 반영된 본문 Markdown 문자열로 렌더."""
    lines: List[str] = []
    for art in articles:
        lines.extend(
            _render_law_article_lines(art, flat_legal_rules=flat_legal_rules)
        )
    return "\n".join(lines).strip()


def _render_appendix(ap: Dict[str, Any], idx_fallback: int) -> Tuple[str, Dict[str, int]]:
    """별표 1건 Markdown 렌더링. counts에는 본 별표가 가진 링크 카운트를 반환."""
    counts = {"text": 0, "pdf": 0, "file": 0, "image": 0}

    번호 = ap.get("별표번호", "")
    가지 = ap.get("별표가지번호", "")
    제목 = normalize_dots((ap.get("별표제목") or "").strip())
    구분 = ap.get("별표구분", "")
    시행일 = format_date(ap.get("별표시행일자", ""))
    내용 = neutralize_appendix_markdown(
        normalize_appendix_blank_lines(
            normalize_dots((ap.get("별표내용") or "").strip())
        )
    )

    label_no = f"{번호}{('-' + 가지) if 가지 and 가지 != '0' else ''}" or str(idx_fallback)
    구분명 = 구분 or "별표"
    header = f"### [{구분명} {label_no}] {제목}".rstrip()

    md: List[str] = [header]
    info_bits = []
    if 구분:
        info_bits.append(f"- 별표구분: {구분}")
    if 번호:
        info_bits.append(f"- 별표번호: {번호}")
    if 가지 and 가지 != "0":
        info_bits.append(f"- 별표가지번호: {가지}")
    if 시행일:
        info_bits.append(f"- 별표 시행일자: {시행일}")
    if info_bits:
        md.append("\n".join(info_bits))

    if 내용:
        md.append(내용)
        counts["text"] = 1
    else:
        md.append("> 별표내용 없음, 원문 링크 참고")

    파일링크 = ap.get("별표서식파일링크", "")
    PDF링크 = ap.get("별표서식PDF파일링크", "")
    이미지링크 = ap.get("별표서식이미지파일링크", "")

    link_lines: List[str] = []
    if 파일링크:
        link_lines.append(f"- 별표서식파일링크: {파일링크}")
        counts["file"] = 1
    if PDF링크:
        link_lines.append(f"- 별표서식PDF파일링크: {PDF링크}")
        counts["pdf"] = 1
    if 이미지링크:
        link_lines.append(f"- 별표서식이미지파일링크: {이미지링크}")
        counts["image"] = 1

    if link_lines:
        md.append("#### 원문 파일 링크")
        md.append("\n".join(link_lines))

    return "\n\n".join(md), counts


def _uses_admrul_flat_legal_rules(title: str) -> bool:
    """법령식 평면 조문 표기를 적용할 핵심 행정규칙인지 판별한다.

    NFTC 및 그 밖의 행정규칙은 각기 다른 번호 체계를 가지므로 제외한다.
    """
    normalized = (title or "").strip()
    return "NFPC" in normalized or "형식승인 및 제품검사의 기술기준" in normalized


def render_markdown(parsed: Dict[str, Any], api_target: str,
                    collected_at: str, requested_title: str) -> Tuple[str, Dict[str, int]]:
    """Markdown 문자열과 집계용 카운트를 반환.

    문서 메타데이터는 모두 API 응답(parsed)에서 가져온다.
    CSV에서 받는 정보는 API 호출용 (문서유형, 명칭=LM) 뿐이며,
    여기서는 응답 명칭과의 비교용 메모로만 requested_title을 사용한다.
    """
    api_title = normalize_dots((_api_title(parsed) or "").strip())
    title = api_title or requested_title or "(제목 없음)"
    doc_type_label = "법령" if api_target == "law" else "행정규칙"

    parts: List[str] = []
    parts.append(f"# {title}")
    parts.append("")

    # 문서정보 (모든 값은 API 응답 XML에서 추출)
    parts.append("## 문서정보")
    info_lines = [
        f"- 문서유형: {doc_type_label}",
        f"- API target: {api_target}",
        f"- 조회명칭 LM: {requested_title}",
    ]
    if api_title and api_title != requested_title:
        info_lines.append(f"- 응답 명칭(요청과 다름): {api_title}")
    info_lines.append(f"- 수집일시: {collected_at}")

    if parsed.get("type") == "law":
        if parsed.get("법령ID"):
            info_lines.append(f"- 법령ID: {parsed.get('법령ID','')}")
        if api_title:
            info_lines.append(f"- 법령명_한글: {api_title}")
        if parsed.get("법령명약칭"):
            info_lines.append(f"- 법령명약칭: {normalize_dots(parsed.get('법령명약칭',''))}")
        if parsed.get("법종구분"):
            info_lines.append(f"- 법종구분: {parsed.get('법종구분','')}")
        if parsed.get("공포일자"):
            info_lines.append(f"- 공포일자: {format_date(parsed.get('공포일자',''))}")
        if parsed.get("공포번호"):
            info_lines.append(f"- 공포번호: {parsed.get('공포번호','')}")
        if parsed.get("시행일자"):
            info_lines.append(f"- 시행일자: {format_date(parsed.get('시행일자',''))}")
        if parsed.get("소관부처"):
            info_lines.append(f"- 소관부처: {parsed.get('소관부처','')}")
        if parsed.get("부서명"):
            info_lines.append(f"- 부서명: {parsed.get('부서명','')}")
        if parsed.get("부서연락처"):
            info_lines.append(f"- 부서연락처: {parsed.get('부서연락처','')}")
    else:
        if parsed.get("행정규칙ID"):
            info_lines.append(f"- 행정규칙ID: {parsed.get('행정규칙ID','')}")
        if parsed.get("행정규칙일련번호"):
            info_lines.append(f"- 행정규칙일련번호: {parsed.get('행정규칙일련번호','')}")
        if api_title:
            info_lines.append(f"- 행정규칙명: {api_title}")
        if parsed.get("행정규칙종류"):
            info_lines.append(f"- 행정규칙종류: {parsed.get('행정규칙종류','')}")
        if parsed.get("제개정구분명"):
            info_lines.append(f"- 제개정구분명: {parsed.get('제개정구분명','')}")
        if parsed.get("발령일자"):
            info_lines.append(f"- 발령일자: {format_date(parsed.get('발령일자',''))}")
        if parsed.get("발령번호"):
            info_lines.append(f"- 발령번호: {parsed.get('발령번호','')}")
        if parsed.get("시행일자"):
            info_lines.append(f"- 시행일자: {format_date(parsed.get('시행일자',''))}")
        if parsed.get("소관부처명"):
            info_lines.append(f"- 소관부처명: {parsed.get('소관부처명','')}")
        if parsed.get("담당부서기관명"):
            info_lines.append(f"- 담당부서기관명: {parsed.get('담당부서기관명','')}")
        if parsed.get("전화번호"):
            info_lines.append(f"- 전화번호: {parsed.get('전화번호','')}")
        if parsed.get("현행여부"):
            info_lines.append(f"- 현행여부: {parsed.get('현행여부','')}")

    parts.append("\n".join(info_lines))
    parts.append("\n---\n")

    # 본문 (편/장/절/관 계층 감지 + 조 헤더 + 항/호/목 조판)
    # '## 본문' 래퍼 헤더는 두지 않는다 — 장(##)과 헤더 레벨이 겹치기 때문.
    articles = parsed.get("articles") or []
    flat_legal_rules = (
        api_target == "admrul" and _uses_admrul_flat_legal_rules(title)
    )
    body_md = (
        render_law_body(articles, flat_legal_rules=flat_legal_rules)
        if articles else ""
    )
    if body_md:
        parts.append(body_md)
    else:
        parts.append("> 본문 조문 데이터가 응답에 포함되지 않았습니다.")
    parts.append("\n---\n")

    # 부칙
    parts.append("## 부칙")
    addenda = parsed.get("addenda") or []
    if addenda:
        for bc in addenda:
            num = bc.get("부칙공포번호", "")
            txt = neutralize_revision_markdown(
                normalize_addendum_blank_lines(
                    normalize_dots((bc.get("부칙내용") or "").strip())
                )
            )
            if num:
                parts.append(f"### 부칙 {num}")
            if txt:
                parts.append(txt)
            parts.append("")
    else:
        parts.append("> 부칙 정보 없음")
    parts.append("\n---\n")

    # 별표 및 서식
    appendix_count = 0
    appendix_text_count = 0
    pdf_link_count = 0
    file_link_count = 0
    image_link_count = 0

    parts.append("## 별표 및 서식")
    appendices = parsed.get("appendices") or []
    if appendices:
        for i, ap in enumerate(appendices, 1):
            md, c = _render_appendix(ap, i)
            parts.append(md)
            parts.append("")
            appendix_count += 1
            appendix_text_count += c["text"]
            pdf_link_count += c["pdf"]
            file_link_count += c["file"]
            image_link_count += c["image"]
    else:
        parts.append("> 별표 정보 없음")
    parts.append("\n---\n")

    # 첨부파일 (행정규칙)
    attachment_count = 0
    attachments = parsed.get("attachments") or []
    if attachments:
        parts.append("## 첨부파일 링크")
        for att in attachments:
            name = normalize_dots((att.get("첨부파일명") or "").strip())
            link = (att.get("첨부파일링크") or "").strip()
            if not (name or link):
                continue
            parts.append(f"- 첨부파일명: {name}")
            if link:
                parts.append(f"  - 첨부파일링크: {link}")
            attachment_count += 1
        parts.append("\n---\n")

    # 개정문 / 제개정이유
    if parsed.get("amendments") or parsed.get("reasons"):
        parts.append("## 개정문 및 제·개정이유")
        if parsed.get("amendments"):
            parts.append("### 개정문")
            for t in parsed["amendments"]:
                parts.append(neutralize_revision_markdown(normalize_dots(t)))
        if parsed.get("reasons"):
            parts.append("### 제·개정이유")
            for t in parsed["reasons"]:
                parts.append(neutralize_revision_markdown(normalize_dots(t)))
        parts.append("\n---\n")

    # 수집 참고
    parts.append("## 수집 참고")
    parts.append(
        "- 이 문서는 법제처 Open API XML 응답을 NotebookLM 업로드용 Markdown으로 변환한 것이다.\n"
        "- 별표·서식 PDF/HWP/이미지 원본은 다운로드하지 않았으며, API의 별표내용을 본문에 통합하였다.\n"
        "- 원문 파일 링크는 법제처 API 응답의 파일링크를 정규화하여 기록하였다."
    )

    md_text = "\n".join(parts).rstrip() + "\n"

    counts = {
        "article_count": len(articles),
        "appendix_count": appendix_count,
        "appendix_text_count": appendix_text_count,
        "appendix_pdf_link_count": pdf_link_count,
        "appendix_file_link_count": file_link_count,
        "appendix_image_link_count": image_link_count,
        "attachment_link_count": attachment_count,
    }
    return md_text, counts


# ---------------------------------------------------------------------------
# Markdown 렌더링: 3단비교(인용조문)
# ---------------------------------------------------------------------------
def _thdcmp_jo_label(jo_no: str, jo_branch: str) -> str:
    """'제3조' / '제3조의2' 식의 라벨을 만든다."""
    try:
        n = int(jo_no) if jo_no else 0
    except ValueError:
        n = 0
    try:
        b = int(jo_branch) if jo_branch else 0
    except ValueError:
        b = 0
    if n <= 0:
        return ""
    if b > 0:
        return f"제{n}조의{b}"
    return f"제{n}조"


def _thdcmp_jo_key(jo: Dict[str, Any]) -> Tuple[str, str]:
    """동일 시행령/시행규칙 조문 중복 제거용 키."""
    return (jo.get("조번호", "") or "", jo.get("조가지번호", "") or "")


def _strip_jo_prefix_in_title(title: str) -> str:
    """조제목에 이미 '제N조(...)' 형태가 들어있으면 괄호 안 텍스트만 반환.

    예: '제3조(소방시설)' → '소방시설', '제3조' → '', '소방시설' → '소방시설'
    """
    title = (title or "").strip()
    if not title:
        return ""
    m = re.match(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*\((.+)\)\s*$", title)
    if m:
        return m.group(1).strip()
    if re.match(r"^제\s*\d+\s*조(?:의\s*\d+)?\s*$", title):
        return ""
    return title


def _thdcmp_title_with_label(prefix: str, jo: Dict[str, Any]) -> str:
    """'시행령 제3조(소방시설)' 또는 '제3조(소방시설)' 색인용 라벨을 만든다.

    prefix가 빈 문자열이면 종류 표기 없이 조 라벨만 반환한다.
    """
    label = _thdcmp_jo_label(jo.get("조번호", ""), jo.get("조가지번호", ""))
    inner = normalize_dots(_strip_jo_prefix_in_title(jo.get("조제목", "")))
    base = f"{prefix} {label}".strip() if prefix else label
    if inner:
        return f"{base}({inner})"
    return base


def _render_thdcmp_jo_header(jo: Dict[str, Any], kind: str) -> str:
    """3단비교 조문 1건의 헤더 라인(### …)을 만든다."""
    label = _thdcmp_jo_label(jo.get("조번호", ""), jo.get("조가지번호", ""))
    title_inner = normalize_dots(_strip_jo_prefix_in_title(jo.get("조제목", "")))
    header_bits: List[str] = [f"### {kind}"]
    if label:
        header_bits.append(label)
    header = " ".join(header_bits)
    if title_inner:
        return f"{header} ({title_inner})"
    return f"{header} ()"


_FLAT_LEGAL_HANG_LINE_RE = re.compile(
    r"^[ \t]*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])[ \t]+(.*)$"
)
_FLAT_LEGAL_HO_LINE_RE = re.compile(
    r"^[ \t]*(\d+(?:의\d+)?)\.[ \t]+(.*)$"
)
_FLAT_LEGAL_MOK_LINE_RE = re.compile(
    r"^[ \t]*([가-힣](?:의\d+)?)\.[ \t]+(.*)$"
)
_FLAT_LEGAL_MOK_CHILD_LINE_RE = re.compile(
    r"^[ \t]*(\d{1,9})\)[ \t]+(.*)$"
)
_FLAT_LEGAL_TRAILING_REFERENCE_RE = re.compile(r"^[ \t]*\[[^\]\n]+\][ \t]*$")


def _render_flat_legal_body(text: str, *, strip_jo_prefix: bool = False) -> str:
    """평면 조문내용을 일반 법령 본문과 같은 표기 규칙으로 렌더한다.

    3단비교 및 일부 행정규칙 API는 항ㆍ호ㆍ목을 별도 XML 요소로 제공하지
    않지만 각 표지를 줄 시작에 보존한다. 따라서 줄 단위로 표지만 조판하고
    원문 내용은 유지한다. 조문 말미의 대괄호 단독 줄은 참고자료 인용문으로
    분리한다.
    """
    content = normalize_dots((text or "").strip())
    if strip_jo_prefix:
        content = _JO_PREFIX_RE.sub("", content)
    if not content:
        return ""

    source_lines = content.splitlines()
    reference_start: Optional[int] = None
    for i, line in enumerate(source_lines):
        if not _FLAT_LEGAL_TRAILING_REFERENCE_RE.match(line):
            continue
        if all(
            not rest.strip() or _FLAT_LEGAL_TRAILING_REFERENCE_RE.match(rest)
            for rest in source_lines[i:]
        ):
            reference_start = i
            break

    if reference_start is None:
        body_lines = source_lines
        reference_lines: List[str] = []
    else:
        body_lines = source_lines[:reference_start]
        reference_lines = source_lines[reference_start:]
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()

    rendered: List[str] = []
    for line in body_lines:
        match = _FLAT_LEGAL_HANG_LINE_RE.match(line)
        if match:
            rendered.append(f"**{match.group(1)}** {match.group(2).strip()}")
            continue

        match = _FLAT_LEGAL_HO_LINE_RE.match(line)
        if match:
            rendered.append(f"  {match.group(1)}\\. {match.group(2).strip()}")
            continue

        match = _FLAT_LEGAL_MOK_LINE_RE.match(line)
        if match:
            rendered.append(f"    {match.group(1)}\\. {match.group(2).strip()}")
            continue

        match = _FLAT_LEGAL_MOK_CHILD_LINE_RE.match(line)
        if match:
            rendered.append(f"      {match.group(1)}\\) {match.group(2).strip()}")
            continue

        rendered.append(line)

    if reference_lines:
        if rendered:
            rendered.append("")
        rendered.extend(_render_article_reference_lines("\n".join(reference_lines)))

    return "\n".join(rendered)


def render_thdcmp_markdown(parsed: Dict[str, Any], collected_at: str,
                           requested_title: str) -> Tuple[str, Dict[str, int]]:
    """3단비교(인용조문) Markdown 렌더링.

    구조:
      # {법령명} 인용조문 3단비교
      ## 문서정보
      ## 본문
        ### 법률 제N조 (제목)
        #### 인용조문: 시행령 제M조(...), 제M'조(...) / 시행규칙 제K조(...)
        법률 본문
        ### 시행령 제M조 (제목)
        시행령 본문
        ### 시행규칙 제K조 (제목)
        시행규칙 본문

    동일한 시행령/시행규칙 조문 본문은 한 번만 출력하고, 색인에는 모두 표시한다.
    """
    api_title = parsed.get("법령명") or ""
    title = api_title or requested_title or "(제목 없음)"

    parts: List[str] = [f"# {title} 인용조문 3단비교", ""]

    parts.append("## 문서정보")
    info: List[str] = [
        "- 문서유형: 3단비교(인용조문, knd=1)",
        "- API target: thdCmp",
        f"- 조회명칭 LM: {requested_title}",
    ]
    if api_title and api_title != requested_title:
        info.append(f"- 응답 명칭(요청과 다름): {api_title}")
    info.append(f"- 수집일시: {collected_at}")
    if parsed.get("법령ID"):
        info.append(f"- 법령ID: {parsed['법령ID']}")
    if parsed.get("법령요약정보"):
        info.append(f"- 법령요약정보: {parsed['법령요약정보']}")
    if parsed.get("시행령ID") and parsed.get("시행령ID") != "0":
        info.append(f"- 시행령ID: {parsed['시행령ID']}")
        if parsed.get("시행령명"):
            info.append(f"- 시행령명: {parsed['시행령명']}")
        if parsed.get("시행령요약정보"):
            info.append(f"- 시행령요약정보: {parsed['시행령요약정보']}")
    if parsed.get("시행규칙ID") and parsed.get("시행규칙ID") != "0":
        info.append(f"- 시행규칙ID: {parsed['시행규칙ID']}")
        if parsed.get("시행규칙명"):
            info.append(f"- 시행규칙명: {parsed['시행규칙명']}")
        if parsed.get("시행규칙요약정보"):
            info.append(f"- 시행규칙요약정보: {parsed['시행규칙요약정보']}")
    if parsed.get("삼단비교기준"):
        info.append(f"- 삼단비교기준: {parsed['삼단비교기준']}")
    if parsed.get("삼단비교존재여부"):
        info.append(f"- 삼단비교존재여부: {parsed['삼단비교존재여부']}")
    parts.append("\n".join(info))
    parts.append("\n---\n")

    # '## 본문' 래퍼 헤더는 두지 않는다 — 장(##)과 헤더 레벨이 겹치기 때문.
    articles: List[Dict[str, Any]] = parsed.get("articles") or []
    seen_enf: set = set()
    seen_rule: set = set()
    article_count = 0
    enf_body_count = 0
    rule_body_count = 0

    for art in articles:
        조제목 = (art.get("조제목") or "").strip()
        # 장(章) 헤더 같이 조제목이 없고 조내용이 짧은 경우는 그대로 출력
        if not 조제목:
            chapter = normalize_dots((art.get("조내용") or "").strip())
            if chapter:
                parts.append(f"## {chapter}")
                parts.append("")
            continue

        article_count += 1
        enf_list = art.get("시행령조문") or []
        rule_list = art.get("시행규칙조문") or []

        # 법률 조문 헤더
        parts.append(_render_thdcmp_jo_header(art, "법률"))

        # 색인 라인 (헤더 바로 아래, 있을 때만): 첫 항목은 종류 prefix 포함
        index_groups: List[str] = []
        for prefix, jos in (("시행령", enf_list), ("시행규칙", rule_list)):
            if not jos:
                continue
            labels = [
                _thdcmp_title_with_label(prefix if i == 0 else "", j)
                for i, j in enumerate(jos)
            ]
            index_groups.append(", ".join(labels))
        if index_groups:
            parts.append(f"#### 인용조문: {' / '.join(index_groups)}")

        # 법률 본문
        law_body = _render_flat_legal_body(
            art.get("조내용") or "", strip_jo_prefix=True
        )
        if law_body:
            parts.append("")
            parts.append(law_body)

        parts.append("")  # 법률 본문과 하위(시행령/시행규칙) 본문 사이 공백

        # 시행령조문 본문 (중복 제거)
        for j in enf_list:
            key = _thdcmp_jo_key(j)
            if key in seen_enf:
                continue
            seen_enf.add(key)
            parts.append(_render_thdcmp_jo_header(j, "시행령"))
            j_body = _render_flat_legal_body(
                j.get("조내용") or "", strip_jo_prefix=True
            )
            if j_body:
                parts.append("")
                parts.append(j_body)
            parts.append("")
            enf_body_count += 1

        # 시행규칙조문 본문 (중복 제거)
        for j in rule_list:
            key = _thdcmp_jo_key(j)
            if key in seen_rule:
                continue
            seen_rule.add(key)
            parts.append(_render_thdcmp_jo_header(j, "시행규칙"))
            j_body = _render_flat_legal_body(
                j.get("조내용") or "", strip_jo_prefix=True
            )
            if j_body:
                parts.append("")
                parts.append(j_body)
            parts.append("")
            rule_body_count += 1

    if not articles:
        parts.append("> 본문 조문 데이터가 응답에 포함되지 않았습니다.")

    parts.append("\n---\n")
    parts.append("## 수집 참고")
    parts.append(
        "- 이 문서는 법제처 Open API의 3단비교(인용조문, knd=1) XML 응답을 Markdown으로 변환한 것이다.\n"
        "- 각 법률 조문 헤더 아래의 `#### 인용조문:` 라인은 해당 법률 조문이 인용하는 시행령/시행규칙 조문 색인이다.\n"
        "- 동일한 시행령/시행규칙 조문은 처음 등장한 위치에서 한 번만 본문을 펼친다.\n"
        "- 위임행정규칙(NFPC/NFTC) 목록은 본 문서의 범위에서 제외했다."
    )

    md_text = "\n".join(parts).rstrip() + "\n"

    counts = {
        "article_count": article_count,
        "appendix_count": 0,
        "appendix_text_count": 0,
        "appendix_pdf_link_count": 0,
        "appendix_file_link_count": 0,
        "appendix_image_link_count": 0,
        "attachment_link_count": enf_body_count + rule_body_count,
    }
    return md_text, counts


# ---------------------------------------------------------------------------
# 파일 저장 (Markdown / Manifest / Failed)
# ---------------------------------------------------------------------------
def write_markdown(api_target: str, base_name: str, md_text: str) -> Path:
    if not md_text or not md_text.strip():
        raise ValueError("빈 Markdown은 저장할 수 없습니다.")
    target_dir = _md_dir_for(api_target)
    path = target_dir / f"{base_name}.md"
    _atomic_write_text(path, md_text, encoding="utf-8", newline="")
    return path


def _atomic_write_csv(
    path: Path,
    fieldnames: List[str],
    rows: List[Dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temp_path.open("x", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                safe_row = {
                    key: redact_sensitive(row.get(key, ""))
                    for key in fieldnames
                }
                writer.writerow(safe_row)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def write_manifest(rows: List[Dict[str, Any]]) -> None:
    _atomic_write_csv(MANIFEST_CSV, MANIFEST_COLUMNS, rows)


def write_failed_items(rows: List[Dict[str, Any]]) -> None:
    _atomic_write_csv(FAILED_CSV, FAILED_COLUMNS, rows)


# ---------------------------------------------------------------------------
# 메인
# ---------------------------------------------------------------------------
class AlreadyRunningError(RuntimeError):
    pass


class SingleRunLock:
    """프로세스 종료 시 OS가 자동 해제하는 단일 실행 파일 잠금."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "SingleRunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as error:
            handle.close()
            raise AlreadyRunningError(
                "다른 수집 실행이 진행 중이거나 잠금을 확보할 수 없습니다."
            ) from error
        self._handle = handle
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _ensure_dirs() -> None:
    for d in (
        MD_LAW_DIR, MD_ADMRUL_DIR, MD_THDCMP_DIR,
        XML_LAW_DIR, XML_ADMRUL_DIR, XML_THDCMP_DIR,
        LOG_DIR,
    ):
        d.mkdir(parents=True, exist_ok=True)


def _law_id(parsed: Dict[str, Any]) -> str:
    t = parsed.get("type")
    if t == "law":
        return parsed.get("법령ID", "")
    if t == "thdCmp":
        return parsed.get("법령ID", "")
    return parsed.get("행정규칙ID", "") or parsed.get("행정규칙일련번호", "")


def _api_title(parsed: Dict[str, Any]) -> str:
    t = parsed.get("type")
    if t == "law":
        return parsed.get("법령명_한글", "")
    if t == "thdCmp":
        return parsed.get("법령명", "")
    return parsed.get("행정규칙명", "")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """커맨드라인 인자를 파싱한다.

    --from-cache   : 네트워크 호출 없이 output/raw_xml 캐시만 사용해 Markdown 재생성.
                     캐시가 없는 항목은 SKIP_NO_CACHE로 manifest에 기록하고 건너뛴다.
                     이 모드에서는 LAW_OC가 없어도 동작한다.
    --missing-only : 캐시(XML)가 있는 항목은 그대로 사용하고, 없는 항목만 API로 받는다.
    (둘 다 미지정) : 기본 동작 - 모든 항목을 API로 새로 받는다.
    """
    p = argparse.ArgumentParser(
        prog="collect_law_markdown.py",
        description="법제처 Open API(XML) → NotebookLM 업로드용 Markdown 변환기",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--from-cache",
        dest="from_cache",
        action="store_true",
        help="네트워크 호출 없이 output/raw_xml 캐시만 사용해 Markdown을 재생성",
    )
    mode.add_argument(
        "--missing-only",
        dest="missing_only",
        action="store_true",
        help="캐시가 없는 항목만 API로 새로 받고, 캐시가 있는 항목은 캐시에서 재생성",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="항목별 처리 내역을 터미널에도 자세히 표시",
    )
    p.add_argument(
        "--input",
        type=Path,
        default=DEFAULT_TARGETS_CSV,
        metavar="CSV",
        help="수집 대상 CSV 경로(기본값: law_targets.csv)",
    )
    return p.parse_args(argv)


def _main_locked(args: argparse.Namespace, run_id: str) -> int:
    logger, log_path = setup_logging(verbose=args.verbose, run_id=run_id)
    _ensure_dirs()
    logger.info(f"run_id: {run_id}")

    source_csv = args.input
    if not source_csv.is_absolute():
        source_csv = PROJECT_DIR / source_csv
    source_csv = source_csv.resolve()
    logger.info(f"입력 목록: {source_csv}")

    try:
        df = read_source_csv(source_csv)
        total_input = len(df)
        validate_source_rows(df)
        df = deduplicate_rows(df, logger)
        total_targets = len(df)
    except Exception as error:
        logger.error(f"입력 CSV 검증 실패: {safe_error_message(error)}")
        return 1

    if args.from_cache:
        mode_label = "CACHE_ONLY (네트워크 호출 없음)"
        oc = ""  # API를 호출하지 않으므로 OC가 필요 없음
        logger.info(f"실행 모드: {mode_label}")
    else:
        try:
            oc = load_config()
        except Exception as error:
            logger.error(safe_error_message(error))
            return 1
        mode_label = "MISSING_ONLY (캐시 우선, 빠진 것만 API)" if args.missing_only else "FETCH_ALL (전부 API)"
        logger.info(f"실행 모드: {mode_label}")
        logger.info("LAW_OC 로드 완료 (값은 마스킹)")

    manifest_rows: List[Dict[str, Any]] = []
    failed_rows: List[Dict[str, Any]] = []

    success = 0
    fail = 0
    skipped = 0
    cache_used = 0
    fetched = 0
    law_success = 0
    admrul_success = 0
    thdcmp_success = 0
    has_appendix_text_docs = 0
    has_pdf_docs = 0
    has_file_docs = 0
    has_image_docs = 0

    console_mode = (
        "캐시 전용" if args.from_cache
        else ("캐시 우선" if args.missing_only else "API 전체 수집")
    )
    tqdm.write(
        f"법령 Markdown 수집 | {console_mode} | 대상 {total_targets}건",
        file=sys.stderr,
    )
    iterator = tqdm(
        enumerate(df.iterrows(), start=1),
        total=total_targets,
        desc="진행",
        unit="건",
        bar_format=PROGRESS_BAR_FORMAT,
        dynamic_ncols=True,
        mininterval=0.2,
        disable=None,
        file=sys.stderr,
    )

    for idx, (_, row) in iterator:
        source_index = idx
        title = (row.get("명칭", "") or "").strip()
        dtype = (row.get("문서유형", "") or "").strip()
        collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        manifest = {
            "source_index": source_index,
            "status": "",
            "requested_title": title,
            "document_type": dtype,
            "api_target": "",
            "api_url_without_oc": "",
            "law_id_or_rule_id": "",
            "api_title": "",
            "issuer_or_rule_type": "",
            "issue_number": "",
            "promulgation_or_issue_date": "",
            "effective_date": "",
            "raw_xml_path": "",
            "markdown_path": "",
            "article_count": 0,
            "appendix_count": 0,
            "appendix_text_count": 0,
            "appendix_pdf_link_count": 0,
            "appendix_file_link_count": 0,
            "appendix_image_link_count": 0,
            "attachment_link_count": 0,
            "has_appendix_text": "N",
            "has_pdf_links": "N",
            "has_hwp_or_file_links": "N",
            "has_image_links": "N",
            "normalized_links_count": 0,
            "error_message": "",
            "collected_at": collected_at,
        }

        # API URL 구성 (manifest 기록용 - 캐시 모드에서도 URL은 기록한다)
        try:
            api_target, url, masked_url = build_api_url(row, oc)
            manifest["api_target"] = api_target
            manifest["api_url_without_oc"] = masked_url
        except Exception as error:
            error_message = safe_error_message(error)
            fail += 1
            manifest["status"] = "FAIL"
            manifest["error_message"] = f"URL 구성 실패: {error_message}"
            manifest_rows.append(manifest)
            failed_rows.append({
                "source_index": source_index,
                "document_type": dtype,
                "title": title,
                "api_target": "",
                "api_url_without_oc": "",
                "error_type": "BUILD_URL_FAIL",
                "error_message": error_message,
                "http_status": "",
                "collected_at": collected_at,
            })
            logger.error(
                f"[FAIL] {source_index} {title}: URL 구성 실패 - {error_message}"
            )
            continue

        # 파일 base_name (명칭만으로 결정).
        # 3단비교는 동일 명칭의 일반 법령과 구분하기 위해 접미사를 붙인다.
        _, base_name = _expected_base_name(dtype, title)
        try:
            cached_path = find_cached_xml(api_target, base_name)
        except Exception as error:
            error_message = safe_error_message(error)
            fail += 1
            manifest["status"] = "FAIL"
            manifest["error_message"] = f"캐시 확인 실패: {error_message}"
            manifest_rows.append(manifest)
            failed_rows.append({
                "source_index": source_index,
                "document_type": dtype,
                "title": title,
                "api_target": api_target,
                "api_url_without_oc": masked_url,
                "error_type": "CACHE_LOOKUP_FAIL",
                "error_message": error_message,
                "http_status": "",
                "collected_at": collected_at,
            })
            logger.error(
                f"[FAIL] {source_index} {title}: 캐시 확인 실패 - "
                f"{error_message}"
            )
            continue

        # 소스 결정: 캐시 vs HTTP fetch
        xml_text = ""
        xml_path: Optional[Path] = None
        parsed: Optional[Dict[str, Any]] = None
        md_text: Optional[str] = None
        counts: Optional[Dict[str, int]] = None
        http_status_for_failed: Any = ""

        if args.from_cache and cached_path is None:
            skipped += 1
            manifest["status"] = "SKIP_NO_CACHE"
            manifest["error_message"] = "캐시된 XML 없음 (--from-cache 모드)"
            manifest_rows.append(manifest)
            logger.warning(f"[SKIP] {source_index} {title}: 캐시 없음")
            continue

        use_cache = cached_path is not None and (
            args.from_cache or args.missing_only
        )
        if use_cache:
            try:
                xml_text = cached_path.read_text(encoding="utf-8")
                xml_path = cached_path
            except Exception as error:
                error_message = safe_error_message(error)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = f"캐시 XML 읽기 실패: {error_message}"
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": "CACHE_READ_FAIL",
                    "error_message": error_message,
                    "http_status": "",
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: 캐시 XML 읽기 실패 - "
                    f"{error_message}"
                )
                continue
            cache_used += 1
            logger.info(
                f"캐시: {source_index} [{api_target}] {title} <- {cached_path.name}"
            )

        else:
            # API로 새로 받기 (기본 모드, 또는 missing-only인데 캐시가 없는 경우)
            logger.info(f"요청: {source_index} [{api_target}] {title}")
            time.sleep(REQUEST_SLEEP)
            result = fetch_xml(url, logger)
            http_status_for_failed = result.http_status or ""

            if not result.ok:
                error_message = safe_error_message(result.error_message)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = error_message
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": result.error_type,
                    "error_message": error_message,
                    "http_status": http_status_for_failed,
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: HTTP {result.http_status} "
                    f"{error_message}"
                )
                continue

            xml_text = result.xml_text
            try:
                # 새 응답은 파싱·의미 검증을 통과해야만 기존 캐시를 교체한다.
                parsed = parse_and_validate_xml(api_target, xml_text)
            except Exception as error:
                error_message = safe_error_message(error)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = f"XML 파싱 실패: {error_message}"
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": "PARSE_FAIL",
                    "error_message": error_message,
                    "http_status": http_status_for_failed,
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: 파싱 실패 - {error_message}"
                )
                continue

            try:
                # 렌더링까지 메모리에서 성공한 응답만 캐시로 승격한다.
                if api_target == "thdCmp":
                    md_text, counts = render_thdcmp_markdown(
                        parsed, collected_at, title
                    )
                else:
                    md_text, counts = render_markdown(
                        parsed, api_target, collected_at, title
                    )
                if not md_text or not md_text.strip():
                    raise ValueError("Markdown 렌더링 결과가 비어 있습니다.")
            except Exception as error:
                error_message = safe_error_message(error)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = f"Markdown 렌더링 실패: {error_message}"
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": "RENDER_FAIL",
                    "error_message": error_message,
                    "http_status": http_status_for_failed,
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: 렌더링 실패 - "
                    f"{error_message}"
                )
                continue

            try:
                xml_path = save_raw_xml(api_target, base_name, xml_text)
            except Exception as error:
                error_message = safe_error_message(error)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = f"원시 XML 저장 실패: {error_message}"
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": "RAW_XML_WRITE_FAIL",
                    "error_message": error_message,
                    "http_status": http_status_for_failed,
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: 원시 XML 저장 실패 - "
                    f"{error_message}"
                )
                continue
            fetched += 1

        if xml_path is not None:
            manifest["raw_xml_path"] = str(xml_path.relative_to(PROJECT_DIR)).replace("\\", "/")

        # Markdown 파일명 - 저장된 XML과 같은 base_name 사용
        md_base_name = xml_path.stem if xml_path is not None else base_name

        # 캐시 XML도 동일한 엄격 검증을 통과해야 Markdown으로 승격한다.
        if parsed is None:
            try:
                parsed = parse_and_validate_xml(api_target, xml_text)
            except Exception as error:
                error_message = safe_error_message(error)
                fail += 1
                manifest["status"] = "FAIL"
                manifest["error_message"] = f"XML 파싱 실패: {error_message}"
                manifest_rows.append(manifest)
                failed_rows.append({
                    "source_index": source_index,
                    "document_type": dtype,
                    "title": title,
                    "api_target": api_target,
                    "api_url_without_oc": masked_url,
                    "error_type": "PARSE_FAIL",
                    "error_message": error_message,
                    "http_status": http_status_for_failed,
                    "collected_at": collected_at,
                })
                logger.error(
                    f"[FAIL] {source_index} {title}: 파싱 실패 - "
                    f"{error_message}"
                )
                continue

        # Markdown 렌더링
        try:
            if md_text is None or counts is None:
                if api_target == "thdCmp":
                    md_text, counts = render_thdcmp_markdown(
                        parsed, collected_at, title
                    )
                else:
                    md_text, counts = render_markdown(
                        parsed, api_target, collected_at, title
                    )
            if not md_text or not md_text.strip():
                raise ValueError("Markdown 렌더링 결과가 비어 있습니다.")
            md_path = write_markdown(api_target, md_base_name, md_text)
            if md_path.stat().st_size == 0:
                raise ValueError("Markdown 파일이 0바이트")
        except Exception as error:
            error_message = safe_error_message(error)
            error_type = (
                "EMPTY_MD" if error_message == "Markdown 파일이 0바이트"
                else "RENDER_FAIL"
            )
            fail += 1
            manifest["status"] = "FAIL"
            manifest["error_message"] = f"Markdown 작성 실패: {error_message}"
            manifest_rows.append(manifest)
            failed_rows.append({
                "source_index": source_index,
                "document_type": dtype,
                "title": title,
                "api_target": api_target,
                "api_url_without_oc": masked_url,
                "error_type": error_type,
                "error_message": error_message,
                "http_status": http_status_for_failed,
                "collected_at": collected_at,
            })
            logger.error(
                f"[FAIL] {source_index} {title}: 렌더 실패 - {error_message}"
            )
            continue

        # 성공 처리
        success += 1
        if api_target == "law":
            law_success += 1
        elif api_target == "thdCmp":
            thdcmp_success += 1
        else:
            admrul_success += 1

        # 명칭 불일치 메모
        api_title_val = (_api_title(parsed) or "").strip()
        title_note = ""
        if api_title_val and api_title_val != title:
            title_note = f"명칭 차이 - 요청: {title} / 응답: {api_title_val}"
            logger.info(f"[NOTE] {title_note}")

        # 별표 내용 없음 표시
        appendix_no_text_flag = ""
        if counts["appendix_count"] > 0 and counts["appendix_text_count"] == 0:
            appendix_no_text_flag = "별표내용없음"
            logger.info(f"[NOTE] {source_index} {title}: 별표내용없음 (링크만 존재)")

        msgs = [m for m in [title_note, appendix_no_text_flag] if m]
        manifest["error_message"] = " | ".join(msgs)

        # 카운트/플래그 채우기 (XML 응답 기반)
        manifest["status"] = "OK"
        manifest["markdown_path"] = str(md_path.relative_to(PROJECT_DIR)).replace("\\", "/")
        manifest["law_id_or_rule_id"] = _law_id(parsed)
        manifest["api_title"] = api_title_val
        if parsed.get("type") == "law":
            manifest["issuer_or_rule_type"] = (
                parsed.get("법종구분", "") or parsed.get("소관부처", "")
            )
            manifest["issue_number"] = parsed.get("공포번호", "")
            manifest["promulgation_or_issue_date"] = parsed.get("공포일자", "")
            manifest["effective_date"] = parsed.get("시행일자", "")
        elif parsed.get("type") == "thdCmp":
            # 3단비교: 별도 발령번호/일자가 없으므로 요약정보로 대체
            manifest["issuer_or_rule_type"] = "3단비교(인용조문)"
            manifest["issue_number"] = ""
            manifest["promulgation_or_issue_date"] = ""
            manifest["effective_date"] = ""
        else:
            manifest["issuer_or_rule_type"] = parsed.get("행정규칙종류", "")
            manifest["issue_number"] = parsed.get("발령번호", "")
            manifest["promulgation_or_issue_date"] = parsed.get("발령일자", "")
            manifest["effective_date"] = parsed.get("시행일자", "")
        manifest["article_count"] = counts["article_count"]
        manifest["appendix_count"] = counts["appendix_count"]
        manifest["appendix_text_count"] = counts["appendix_text_count"]
        manifest["appendix_pdf_link_count"] = counts["appendix_pdf_link_count"]
        manifest["appendix_file_link_count"] = counts["appendix_file_link_count"]
        manifest["appendix_image_link_count"] = counts["appendix_image_link_count"]
        manifest["attachment_link_count"] = counts["attachment_link_count"]
        manifest["has_appendix_text"] = "Y" if counts["appendix_text_count"] > 0 else "N"
        manifest["has_pdf_links"] = "Y" if counts["appendix_pdf_link_count"] > 0 else "N"
        manifest["has_hwp_or_file_links"] = "Y" if counts["appendix_file_link_count"] > 0 else "N"
        manifest["has_image_links"] = "Y" if counts["appendix_image_link_count"] > 0 else "N"
        manifest["normalized_links_count"] = (
            counts["appendix_pdf_link_count"]
            + counts["appendix_file_link_count"]
            + counts["appendix_image_link_count"]
            + counts["attachment_link_count"]
        )

        if counts["appendix_text_count"] > 0:
            has_appendix_text_docs += 1
        if counts["appendix_pdf_link_count"] > 0:
            has_pdf_docs += 1
        if counts["appendix_file_link_count"] > 0:
            has_file_docs += 1
        if counts["appendix_image_link_count"] > 0:
            has_image_docs += 1

        manifest_rows.append(manifest)
        logger.info(f"[OK]   {source_index} {title} -> {manifest['markdown_path']}")

    # 결과 파일 작성
    output_write_failed = False
    try:
        write_manifest(manifest_rows)
    except Exception as error:
        output_write_failed = True
        logger.error(
            f"manifest 저장 실패(기존 파일 보존): "
            f"{safe_error_message(error)}"
        )
    try:
        write_failed_items(failed_rows)
    except Exception as error:
        output_write_failed = True
        logger.error(
            f"failed_items 저장 실패(기존 파일 보존): "
            f"{safe_error_message(error)}"
        )

    # 요약
    summary = (
        "\n===== 수집 요약 =====\n"
        f"실행 모드: {mode_label}\n"
        f"전체 입력 건수: {total_input}\n"
        f"중복 제거 후 수집 대상 건수: {total_targets}\n"
        f"성공 건수: {success}\n"
        f"실패 건수: {fail}\n"
        f"스킵 건수(캐시 없음): {skipped}\n"
        f"  - 캐시 사용: {cache_used}\n"
        f"  - API 신규 수집: {fetched}\n"
        f"  - 법령 성공: {law_success}\n"
        f"  - 행정규칙 성공: {admrul_success}\n"
        f"  - 3단비교 성공: {thdcmp_success}\n"
        f"별표내용 포함 문서 수: {has_appendix_text_docs}\n"
        f"PDF 링크 보유 문서 수: {has_pdf_docs}\n"
        f"HWP/파일 링크 보유 문서 수: {has_file_docs}\n"
        f"이미지 링크 보유 문서 수: {has_image_docs}\n"
        f"manifest: {MANIFEST_CSV}\n"
        f"failed_items: {FAILED_CSV}\n"
        f"markdown: {MD_LAW_DIR} / {MD_ADMRUL_DIR} / {MD_THDCMP_DIR}\n"
        f"raw_xml: {XML_LAW_DIR} / {XML_ADMRUL_DIR} / {XML_THDCMP_DIR}\n"
        "====================="
    )
    logger.info(summary)
    console_summary = (
        "\n수집 완료\n"
        f"  결과 | 성공 {success} | 실패 {fail} | 스킵 {skipped}\n"
        f"  소스 | 캐시 {cache_used} | API {fetched}\n"
        f"  유형 | 법령 {law_success} | 행정규칙 {admrul_success} | 3단비교 {thdcmp_success}\n"
        f"  결과표 | {MANIFEST_CSV.relative_to(PROJECT_DIR)}\n"
        f"  상세 로그 | {log_path.relative_to(PROJECT_DIR)}"
    )
    tqdm.write(console_summary, file=sys.stderr)
    return 0 if fail == 0 and skipped == 0 and not output_write_failed else 2


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    run_id = uuid.uuid4().hex[:12]
    lock_path = OUTPUT_DIR / ".collect_law_markdown.lock"
    owns_run_logger = False
    try:
        with SingleRunLock(lock_path):
            owns_run_logger = True
            return _main_locked(args, run_id)
    except AlreadyRunningError as error:
        tqdm.write(f"ERROR | {safe_error_message(error)}", file=sys.stderr)
        return 3
    except Exception as error:
        error_message = safe_error_message(error)
        logger = logging.getLogger("collect_law")
        if logger.handlers:
            logger.exception(f"실행 중 예기치 못한 오류: {error_message}")
        tqdm.write(
            f"ERROR | 실행 중 예기치 못한 오류: {error_message}",
            file=sys.stderr,
        )
        return 1
    finally:
        # CLI 프로세스가 끝나기 전에도 main()을 라이브러리처럼 호출한 쪽이
        # 임시 출력 디렉터리를 바로 정리할 수 있도록 파일 핸들을 해제한다.
        # 잠금을 얻지 못한 호출은 진행 중인 다른 실행의 logger를 닫지 않는다.
        if owns_run_logger:
            logger = logging.getLogger("collect_law")
            for handler in list(logger.handlers):
                handler.close()
                logger.removeHandler(handler)


if __name__ == "__main__":
    sys.exit(main())
