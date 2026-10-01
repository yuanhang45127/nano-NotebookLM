"""Per-page PyMuPDF / MinerU routing (ingest/page_router.py)."""

from pathlib import Path

import pytest

fitz = pytest.importorskip("fitz")

from nano_notebooklm.ingest import page_router as R
from nano_notebooklm.types import PageInfo

_BODY = (
    "Lecture 3: Gradient descent\n"
    "We update parameters iteratively using the gradient of the loss.\n"
    "The learning rate controls the step size."
)
_MATH_FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def _plain(doc):
    doc.new_page().insert_text((50, 80), _BODY)


def _scan(doc):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200), 0)
    pix.clear_with(200)
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=pix)


def _table(doc):
    page = doc.new_page()
    page.insert_text((50, 60), "Comparison of optimizers across benchmarks")
    for r in range(5):
        for c in range(4):
            rect = fitz.Rect(50 + c * 120, 100 + r * 30, 170 + c * 120, 130 + r * 30)
            page.draw_rect(rect, color=(0, 0, 0))
            page.insert_text((rect.x0 + 5, rect.y0 + 20), f"v{r}{c}")


def _symbol_bullet(doc):
    page = doc.new_page()
    page.insert_text((50, 80), "•", fontname="symb")
    page.insert_text((70, 80), _BODY)


def _make_pdf(path: Path, builders) -> Path:
    doc = fitz.open()
    for build in builders:
        build(doc)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("MINERU_PAGE_ROUTING", raising=False)
    monkeypatch.delenv("MINERU_ROUTE_TABLES", raising=False)
    R._PLAN_CACHE.clear()
    yield
    R._PLAN_CACHE.clear()


def _stats(**kw):
    base = dict(
        text_chars=200, image_coverage=0.0, math_font_chars=0,
        math_symbols=0, garbled_chars=0, has_table=False,
    )
    base.update(kw)
    return R.PageStats(**base)


def test_classify_rules():
    assert R.classify(_stats()) is None
    assert R.classify(_stats(text_chars=5, image_coverage=0.9)) == "scan"
    # A near-empty page with no raster image has nothing for OCR to recover.
    assert R.classify(_stats(text_chars=5)) is None
    assert R.classify(_stats(math_font_chars=4)) == "formula"
    assert R.classify(_stats(math_symbols=3)) == "formula"
    assert R.classify(_stats(math_symbols=2)) is None
    assert R.classify(_stats(has_table=True)) == "table"
    assert R.classify(_stats(garbled_chars=20)) == "garbled"
    # A few stray PUA glyphs in a long page are noise, not a broken encoding.
    assert R.classify(_stats(text_chars=2000, garbled_chars=10)) is None


def test_plan_routes_only_hard_pages(tmp_path):
    builders = [_plain, _scan, _table, _symbol_bullet, _plain]
    if _MATH_FONT.exists():
        def _formula(doc):
            page = doc.new_page()
            page.insert_text((50, 80), _BODY)
            page.insert_text((50, 200), "L = ∑ (y − ŷ)², ∫ f dx ≤ ∞", fontname="dj", fontfile=str(_MATH_FONT))
        builders.append(_formula)
    pdf = _make_pdf(tmp_path / "lec.pdf", builders)

    plan = R.plan_pdf(pdf)

    expected = [1, 2] + ([5] if _MATH_FONT.exists() else [])
    assert plan.mineru_pages == expected
    assert plan.total_pages == len(builders)
    # Symbol-font bullets alone must not route a slide.
    assert 3 not in plan.mineru_pages
    assert sorted(plan.fast_pages) == [i for i in range(len(builders)) if i != 1]


def test_tables_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_ROUTE_TABLES", "0")
    pdf = _make_pdf(tmp_path / "t.pdf", [_plain, _table])
    assert R.plan_pdf(pdf).mineru_pages == []


def test_routing_sends_subset_and_maps_pages_back(tmp_path):
    pdf = _make_pdf(tmp_path / "lec.pdf", [_plain, _scan, _plain, _table])
    routing = R.MineruRouting([pdf])
    try:
        (sent,) = routing.send
        assert sent != pdf.resolve()
        assert sent.name == "lec.pdf"  # mineru-api keys results by filename
        with fitz.open(str(sent)) as sub:
            assert len(sub) == 2

        raw = {str(sent): [
            PageInfo(text="OCR of scan", page=1, total_pages=2),
            PageInfo(text="| a | b |", page=2, total_pages=2),
        ]}
        pages = routing.merge(raw)[str(pdf.resolve())]
    finally:
        routing.cleanup()

    assert [p.page for p in pages] == [1, 2, 3, 4]
    assert pages[1].text == "OCR of scan"
    assert pages[3].text == "| a | b |"
    assert pages[0].text.startswith("Lecture 3")
    assert all(p.total_pages == 4 for p in pages)
    assert not sent.exists()


def test_plain_pdf_never_reaches_mineru(tmp_path):
    pdf = _make_pdf(tmp_path / "plain.pdf", [_plain, _plain])
    routing = R.MineruRouting([pdf])
    assert routing.send == []
    assert routing.resolved_without_mineru == 1
    pages = routing.merge({})[str(pdf.resolve())]
    assert [p.page for p in pages] == [1, 2]


def test_routed_page_mineru_dropped_keeps_pymupdf_text(tmp_path):
    pdf = _make_pdf(tmp_path / "lec.pdf", [_plain, _table])
    routing = R.MineruRouting([pdf])
    (sent,) = routing.send
    pages = routing.merge({str(sent): []})[str(pdf.resolve())]
    routing.cleanup()
    assert [p.page for p in pages] == [1, 2]
    assert "Comparison of optimizers" in pages[1].text


def test_failed_mineru_leg_is_omitted(tmp_path):
    pdf = _make_pdf(tmp_path / "lec.pdf", [_plain, _scan])
    routing = R.MineruRouting([pdf])
    assert routing.merge({}) == {}
    routing.cleanup()


def test_unreadable_pdf_is_sent_whole(tmp_path):
    bogus = tmp_path / "deck.pptx.pdf"
    bogus.write_bytes(b"%PDF-not-really")
    routing = R.MineruRouting([bogus])
    assert routing.send == [bogus.resolve()]
    fake = [PageInfo(text="x", page=1)]
    assert routing.merge({str(bogus.resolve()): fake}) == {str(bogus.resolve()): fake}


def test_kill_switch_sends_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_PAGE_ROUTING", "0")
    pdf = _make_pdf(tmp_path / "plain.pdf", [_plain])
    routing = R.MineruRouting([pdf])
    assert routing.send == [pdf.resolve()]
    assert R.count_mineru_pages(pdf) is None


def test_eta_counts_only_routed_pages(tmp_path, monkeypatch):
    from api import server as srv
    from nano_notebooklm.ingest import extractors_mineru as m

    monkeypatch.setattr(m, "_MINERU_SERVER_DISABLED_REASON", None)
    pdf = _make_pdf(tmp_path / "plain.pdf", [_plain] * 20)
    monkeypatch.setattr(srv, "_scan_file_pages", lambda _d: (20, {pdf: 20}))

    routed_eta = srv._estimate_upload_duration_seconds(tmp_path, engine="mineru", mineru_warm=True)
    monkeypatch.setenv("MINERU_PAGE_ROUTING", "0")
    full_eta = srv._estimate_upload_duration_seconds(tmp_path, engine="mineru", mineru_warm=True)

    # 20 pages x 13s/page of MinerU disappears from the estimate.
    assert full_eta - routed_eta > 200


def test_ingest_course_sends_only_hard_pages_to_mineru(tmp_path, monkeypatch):
    import json

    from nano_notebooklm.kb.store import KBStore

    course = tmp_path / "Course"
    course.mkdir()
    long_plain = lambda doc: doc.new_page().insert_text((50, 80), (_BODY + "\n") * 6)  # noqa: E731
    _make_pdf(course / "lec.pdf", [long_plain, _scan, long_plain])

    sent_pages: list[int] = []

    def fake_batch(filepaths, *, lang="ch", timeout_seconds=3600, device="cpu", on_file_done=None):
        out = {}
        for fp in filepaths:
            with fitz.open(fp) as d:
                sent_pages.append(len(d))
            out[str(Path(fp).resolve())] = [PageInfo(page=1, text="scanned " * 120, total_pages=1)]
        return out

    monkeypatch.setattr("nano_notebooklm.ingest.extractors_mineru.extract_pdfs_mineru_batch", fake_batch)
    store = KBStore(artifacts_dir=tmp_path / "artifacts", embed_fn=lambda texts: [[0.0] * 8 for _ in texts])
    store.ingest_course(str(course), course_id="Course", engine="mineru")

    assert sent_pages == [1]
    chunks = json.loads((tmp_path / "artifacts" / "courses" / "Course" / "chunks.json").read_text("utf-8"))
    pages = {c["page"] for c in chunks}
    assert pages == {1, 2, 3}
    assert any("scanned" in c["text"] and c["page"] == 2 for c in chunks)
