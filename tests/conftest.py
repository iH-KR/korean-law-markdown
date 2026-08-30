from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Iterable, Mapping

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import collect_law_markdown as collector  # noqa: E402


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixture_text():
    def load(name: str) -> str:
        return (FIXTURES_DIR / name).read_text(encoding="utf-8")

    return load


@pytest.fixture
def isolated_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect every collector artifact path into pytest's temporary directory."""
    output = tmp_path / "output"
    paths = {
        "PROJECT_DIR": tmp_path,
        "DEFAULT_TARGETS_CSV": tmp_path / "law_targets.csv",
        "OUTPUT_DIR": output,
        "MD_LAW_DIR": output / "markdown" / "laws",
        "MD_ADMRUL_DIR": output / "markdown" / "administrative_rules",
        "MD_THDCMP_DIR": output / "markdown" / "thdcmp",
        "XML_LAW_DIR": output / "raw_xml" / "laws",
        "XML_ADMRUL_DIR": output / "raw_xml" / "administrative_rules",
        "XML_THDCMP_DIR": output / "raw_xml" / "thdcmp",
        "LOG_DIR": output / "logs",
        "MANIFEST_CSV": output / "manifest.csv",
        "FAILED_CSV": output / "failed_items.csv",
    }
    for name, value in paths.items():
        monkeypatch.setattr(collector, name, value)

    # Tests never wait between mocked requests.
    monkeypatch.setattr(collector, "REQUEST_SLEEP", 0)
    monkeypatch.setattr(collector, "RETRY_DELAYS", [])
    return tmp_path


@pytest.fixture(autouse=True)
def close_collector_handlers():
    """Release per-run log files so Windows can clean tmp_path reliably."""
    yield
    logger = logging.getLogger("collect_law")
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


@pytest.fixture
def write_targets_csv():
    def write(path: Path, rows: Iterable[Mapping[str, str]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["문서유형", "명칭"])
            writer.writeheader()
            writer.writerows(rows)
        return path

    return write
