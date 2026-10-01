"""Per-page routing between PyMuPDF and MinerU for engine=mineru uploads.

MinerU's pipeline backend runs layout + formula + table + OCR models on
every page (~13s/page on an M4 CPU). Lecture slides exported from
PowerPoint / Keynote / Beamer carry a clean text layer on most pages, so
only the pages PyMuPDF genuinely mangles need MinerU:

  - scanned / image-only pages (little or no text layer, big raster image)
  - formula pages (math fonts or a cluster of math symbols)
  - table pages (ruled tables PyMuPDF flattens into a word soup)
  - garbled text layers (private-use glyphs from broken font encodings)

Everything else keeps PyMuPDF's text. The routed pages are copied into a
subset PDF (same filename, temp dir) so mineru-api keys the result the
same way; page numbers are mapped back to the original afterwards.

Kill switch: ``MINERU_PAGE_ROUTING=0`` sends every page to MinerU again.
``MINERU_ROUTE_TABLES=0`` stops table detection from routing pages (it is
the most expensive classifier step).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from nano_notebooklm.types import PageInfo

logger = logging.getLogger(__name__)

# Matches PyMuPDF's plain extractor: pages at or below this are dropped.
_FAST_PAGE_MIN_CHARS = 30
# Below this many text-layer chars a page is "nearly empty"; it goes to
# MinerU only if a raster image covers enough of it to be a scan.
_LOW_TEXT_CHARS = 40
_SCAN_IMAGE_COVERAGE = 0.3
_MATH_FONT_CHARS_MIN = 3
_MATH_SYMBOLS_MIN = 3
_GARBLED_CHARS_MIN = 8
_GARBLED_RATIO = 0.05

_MATH_FONT_RE = re.compile(
    r"cmmi|cmsy|cmex|cmbsy|msbm|msam|eufm|rsfs|lmmath|latinmodern-?math|"
    r"math|stix|xits|mtextra|mt ?extra|euclid|mtpro|mt2|esint|wasy|"
    r"\bsymbol\b|symbolmt",
    re.IGNORECASE,
)
_MATH_SYMBOLS = frozenset(
    "∑∫∮∬∭∂∇√∞≤≥≠≈≡≅∼∝∈∉∋⊂⊃⊆⊇⊄∀∃∄⇒⇐⇔→←↔↦∏∐⊗⊕⊙×÷±∓∩∪∧∨¬∠⊥∥ℝℕℤℚℂ𝔼ℙ"
)
# Bullet glyphs that PowerPoint / Word emit in Symbol / Wingdings fonts.
# They must not count as math, or every bulleted slide would be routed.
_BULLET_CHARS = frozenset("•◦▪▫■□●○◆◇►▶▸➢➤✓✔·")


def routing_enabled() -> bool:
    return os.environ.get("MINERU_PAGE_ROUTING", "1").strip().lower() not in {"0", "false", "no", "off"}


def _route_tables() -> bool:
    return os.environ.get("MINERU_ROUTE_TABLES", "1").strip().lower() not in {"0", "false", "no", "off"}


def _is_pua(ch: str) -> bool:
    return "" <= ch <= ""


@dataclass
class PageStats:
    text_chars: int
    image_coverage: float
    math_font_chars: int
    math_symbols: int
    garbled_chars: int
    has_table: bool


def classify(stats: PageStats) -> str | None:
    """Return why a page needs MinerU, or None if PyMuPDF text is enough."""
    if stats.text_chars < _LOW_TEXT_CHARS:
        return "scan" if stats.image_coverage >= _SCAN_IMAGE_COVERAGE else None
    if (
        stats.garbled_chars >= _GARBLED_CHARS_MIN
        and stats.garbled_chars >= _GARBLED_RATIO * stats.text_chars
    ):
        return "garbled"
    if stats.math_font_chars >= _MATH_FONT_CHARS_MIN or stats.math_symbols >= _MATH_SYMBOLS_MIN:
        return "formula"
    if stats.has_table:
        return "table"
    return None


def _page_stats(page, text: str, check_tables: bool) -> PageStats:
    stripped = "".join(text.split())

    area = abs(page.rect) or 1.0
    covered = 0.0
    try:
        for info in page.get_image_info():
            x0, y0, x1, y1 = info.get("bbox", (0, 0, 0, 0))
            covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    except Exception:  # noqa: BLE001 — stats are best-effort
        pass

    math_font_chars = 0
    try:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if not _MATH_FONT_RE.search(span.get("font", "")):
                        continue
                    math_font_chars += sum(
                        1 for ch in span.get("text", "")
                        if not ch.isspace() and ch not in _BULLET_CHARS and not _is_pua(ch)
                    )
    except Exception:  # noqa: BLE001
        pass

    # A PUA glyph that opens a line is a bullet, not a broken encoding.
    garbled = 0
    for line in text.splitlines():
        body = line.strip()
        for i, ch in enumerate(body):
            if ch == "�" or (_is_pua(ch) and i > 0):
                garbled += 1

    has_table = False
    if check_tables:
        try:
            has_table = bool(page.find_tables().tables)
        except Exception:  # noqa: BLE001
            has_table = False

    return PageStats(
        text_chars=len(stripped),
        image_coverage=min(1.0, covered / area),
        math_font_chars=math_font_chars,
        math_symbols=sum(1 for ch in stripped if ch in _MATH_SYMBOLS),
        garbled_chars=garbled,
        has_table=has_table,
    )


@dataclass
class PdfPlan:
    total_pages: int
    mineru_pages: list[int]  # 0-based indices routed to MinerU
    fast_pages: dict[int, PageInfo]  # 0-based index -> PyMuPDF text, every page with text
    reasons: dict[str, int] = field(default_factory=dict)


_PLAN_CACHE: dict[tuple[str, int, int], PdfPlan] = {}
_PLAN_CACHE_MAX = 64
_PLAN_CACHE_LOCK = threading.Lock()


def plan_pdf(path: str | Path) -> PdfPlan:
    """Classify every page of a PDF. Cached by (path, size, mtime) so the
    upload ETA scan and the extraction pass share one classification."""
    import fitz

    p = Path(path).resolve()
    st = p.stat()
    key = (str(p), st.st_size, st.st_mtime_ns)
    with _PLAN_CACHE_LOCK:
        cached = _PLAN_CACHE.get(key)
    if cached is not None:
        return cached

    check_tables = _route_tables()
    mineru_pages: list[int] = []
    fast_pages: dict[int, PageInfo] = {}
    reasons: dict[str, int] = {}
    with fitz.open(str(p)) as doc:
        total = len(doc)
        for i in range(total):
            page = doc[i]
            text = page.get_text().strip()
            if len(text) > _FAST_PAGE_MIN_CHARS:
                fast_pages[i] = PageInfo(text=text, page=i + 1, total_pages=total)
            reason = classify(_page_stats(page, text, check_tables))
            if reason:
                mineru_pages.append(i)
                reasons[reason] = reasons.get(reason, 0) + 1

    plan = PdfPlan(total_pages=total, mineru_pages=mineru_pages, fast_pages=fast_pages, reasons=reasons)
    with _PLAN_CACHE_LOCK:
        if len(_PLAN_CACHE) >= _PLAN_CACHE_MAX:
            _PLAN_CACHE.pop(next(iter(_PLAN_CACHE)))
        _PLAN_CACHE[key] = plan
    return plan


def count_mineru_pages(path: str | Path) -> int | None:
    """Pages of ``path`` that MinerU will actually see, or None if unknown."""
    if not routing_enabled():
        return None
    try:
        return len(plan_pdf(path).mineru_pages)
    except Exception:  # noqa: BLE001 — ETA must never raise
        return None


@dataclass
class _Routed:
    original: Path
    plan: PdfPlan
    send: Path | None  # file handed to MinerU (subset or original), None = fast only


class MineruRouting:
    """Plan for one MinerU batch: which files (or page subsets) to send,
    and how to fold MinerU's output back onto the original files.

    Usage::

        routing = MineruRouting(batch_inputs)
        try:
            raw = extract_pdfs_mineru_batch([str(p) for p in routing.send], ...)
        except ...:
            raw = {}
        results = routing.merge(raw)
        routing.cleanup()
    """

    def __init__(self, inputs: list[Path]):
        self._tmp: Path | None = None
        self._entries: list[_Routed] = []
        self._passthrough: list[Path] = []
        enabled = routing_enabled()
        for inp in inputs:
            original = Path(inp).resolve()
            if not enabled:
                self._passthrough.append(original)
                continue
            try:
                plan = plan_pdf(original)
            except Exception as exc:  # noqa: BLE001 — unreadable by fitz: let MinerU try
                logger.info("page routing: cannot classify %s (%s); sending whole file", original.name, exc)
                self._passthrough.append(original)
                continue
            send = self._subset_for(original, plan)
            self._entries.append(_Routed(original=original, plan=plan, send=send))
            logger.info(
                "page routing: %s — %d/%d pages to MinerU %s",
                original.name, len(plan.mineru_pages), plan.total_pages, plan.reasons or "",
            )

    def _subset_for(self, original: Path, plan: PdfPlan) -> Path | None:
        if not plan.mineru_pages:
            return None
        if len(plan.mineru_pages) == plan.total_pages:
            return original
        import fitz

        if self._tmp is None:
            self._tmp = Path(tempfile.mkdtemp(prefix="mineru_subset_"))
        # One sub-directory per original keeps the filename identical,
        # which is what mineru-api keys its results by.
        target_dir = self._tmp / str(len(self._entries))
        target_dir.mkdir()
        target = target_dir / original.name
        with fitz.open(str(original)) as doc:
            doc.select(plan.mineru_pages)
            doc.save(str(target), garbage=3, deflate=True)
        return target.resolve()

    @property
    def send(self) -> list[Path]:
        return [e.send for e in self._entries if e.send is not None] + self._passthrough

    @property
    def resolved_without_mineru(self) -> int:
        """Files fully handled by PyMuPDF (for progress ticks)."""
        return sum(1 for e in self._entries if e.send is None)

    def merge(self, raw: dict[str, list[PageInfo]]) -> dict[str, list[PageInfo]]:
        """Map MinerU output back to ``{original_path: pages}``.

        A file whose MinerU leg failed is omitted, matching the batch
        contract (caller falls back per-file).
        """
        out: dict[str, list[PageInfo]] = {}
        for orig in self._passthrough:
            if str(orig) in raw:
                out[str(orig)] = raw[str(orig)]

        for e in self._entries:
            plan = e.plan
            if e.send is None:
                out[str(e.original)] = [plan.fast_pages[i] for i in sorted(plan.fast_pages)]
                continue
            mineru = raw.get(str(e.send))
            if mineru is None:
                continue
            by_index: dict[int, PageInfo] = {}
            for pg in mineru:
                j = (pg.page or 0) - 1
                if 0 <= j < len(plan.mineru_pages):
                    orig_idx = plan.mineru_pages[j]
                    by_index[orig_idx] = PageInfo(
                        text=pg.text, page=orig_idx + 1, total_pages=plan.total_pages,
                    )
            routed = set(plan.mineru_pages)
            for i, fp in plan.fast_pages.items():
                # MinerU returning nothing for a routed page (e.g. it judged
                # the page blank) should not lose PyMuPDF's text for it.
                if i not in routed or i not in by_index:
                    by_index[i] = fp
            out[str(e.original)] = [by_index[i] for i in sorted(by_index)]
        return out

    def cleanup(self) -> None:
        if self._tmp is not None:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None
