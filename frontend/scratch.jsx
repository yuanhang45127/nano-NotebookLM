/* global React, API, NanoMarkdown */
/* Scratch notes wizard (从零生成知识笔记) — rendered inside CoursePickerModal.
 *
 * Flow: topic → streamed outline (editable) → streamed full note →
 * key-term extraction (terms get dotted underlines) → click a term for an
 * AI explanation → save: the finished note becomes a .md File handed back
 * to the ordinary upload pipeline, so the new course ingests it exactly
 * like an uploaded document.
 */
const { useState: useStateS, useEffect: useEffectS, useRef: useRefS } = React;

const SCRATCH_DEPTHS = ["intro", "standard", "deep"];

function scratchEscapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* Minimal markdown → HTML for scratch notes (headings / bold / code /
   lists / paragraphs). Math is left as $...$ for KaTeX auto-render. */
function scratchMdToHtml(md) {
  let html = scratchEscapeHtml(String(md || ""));
  html = html
    .replace(/^### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^## (.+)$/gm, "<h3>$1</h3>")
    .replace(/^# (.+)$/gm, "<h3>$1</h3>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^- (.+)$/gm, "<li>$1</li>");
  html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, "<ul>$1</ul>");
  html = html.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br/>");
  return "<p>" + html + "</p>".replace(/<p>\s*<\/p>/g, "");
}

/* Wrap key terms (longest first) in dotted-underline spans. Splits the
   html on tags so attributes / markup never get rewritten. */
function scratchWrapTerms(html, terms, onTermClick) {
  const clean = (terms || []).filter(t => t && String(t).trim().length >= 2);
  if (!clean.length) return html;
  const ordered = clean.slice().sort((a, b) => String(b).length - String(a).length);
  const esc = ordered.map(t => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const rx = new RegExp("(" + esc.join("|") + ")", "g");
  return html.split(/(<[^>]+>)/).map(part => {
    if (part.startsWith("<")) return part;
    return part.split(rx).map(piece => {
      if (ordered.includes(piece)) {
        return '<span class="scratch-term" data-term="' + scratchEscapeHtml(piece) + '">' + piece + "</span>";
      }
      return piece;
    }).join("");
  }).join("");
}

function ScratchPanel({ userLang = null, backend = null, onPicked, onCancel }) {
  const t = (k, vars) => window.I18N.t(k, userLang || "zh", vars);
  const [step, setStep] = useStateS("topic");       // topic | outline | note | review
  const [topic, setTopic] = useStateS("");
  const [depth, setDepth] = useStateS("standard");
  const [audience, setAudience] = useStateS("");
  const [outline, setOutline] = useStateS("");
  const [outlineFinal, setOutlineFinal] = useStateS("");
  const [noteText, setNoteText] = useStateS("");
  const [busy, setBusy] = useStateS(null);          // "outline" | "note" | "terms" | null
  const [error, setError] = useStateS("");
  const [terms, setTerms] = useStateS([]);
  const [explain, setExplain] = useStateS(null);    // {term, text, loading}
  const previewRef = useRefS(null);
  const bodyReqRef = useRefS(null);

  const baseBody = () => ({
    topic: topic.trim(),
    depth,
    audience: audience.trim(),
    user_lang: userLang || "zh",
    backend,
  });

  function generateOutline() {
    if (!topic.trim() || busy) return;
    setError("");
    setBusy("outline");
    setOutline("");
    setStep("outline");
    API.scratchOutlineStream(baseBody(), ev => {
      if (ev.type === "chunk") setOutline(ev.partial || "");
      else if (ev.type === "done") { setOutline(ev.content || ""); setBusy(null); }
      else if (ev.type === "error") { setError(ev.error || "stream_failed"); setBusy(null); }
    }).catch(e => { setError(e.message || "network"); setBusy(null); });
  }

  function confirmOutline() {
    if (!outline.trim()) return;
    setOutlineFinal(outline.trim());
    setStep("note");
    generateNote(outline.trim());
  }

  function generateNote(finalOutline) {
    setError("");
    setBusy("note");
    setNoteText("");
    setTerms([]);
    API.scratchNoteStream({ ...baseBody(), outline: finalOutline }, ev => {
      if (ev.type === "chunk") setNoteText(ev.partial || "");
      else if (ev.type === "done") { setNoteText(ev.content || ""); setBusy(null); extractTerms(ev.content || ""); }
      else if (ev.type === "error") { setError(ev.error || "stream_failed"); setBusy(null); }
    }).catch(e => { setError(e.message || "network"); setBusy(null); });
  }

  function extractTerms(text) {
    if (!text || text.length < 80) return;
    setBusy(prev => prev || "terms");
    API.scratchTerms({ note_text: text, user_lang: userLang || "zh", backend })
      .then(data => setTerms((data && data.terms) || []))
      .catch(() => setTerms([]))
      .finally(() => setBusy(prev => (prev === "terms" ? null : prev)));
  }

  function explainTerm(term, deeper) {
    const excerpt = (() => {
      const i = noteText.indexOf(term);
      if (i < 0) return "";
      return noteText.slice(Math.max(0, i - 200), i + 300);
    })();
    setExplain({ term, text: "", loading: true });
    const q = deeper
      ? t("scratch.explain_deeper_prompt", { term })
      : t("scratch.explain_prompt", { term });
    const ctx = excerpt
      ? "\n\n" + t("scratch.explain_context") + "\n" + excerpt
      : "";
    API.chat(q + ctx, null, 5, null, {}, { userLang, backend })
      .then(data => setExplain({ term, text: data.answer || t("scratch.explain_failed"), loading: false }))
      .catch(e => setExplain({ term, text: e.message || t("scratch.explain_failed"), loading: false }));
  }

  // KaTeX sweep after preview HTML lands (math in scratch notes).
  useEffectS(() => {
    if (step !== "review" && step !== "note") return;
    const root = previewRef.current;
    if (!root) return;
    if (window.NanoMarkdown && NanoMarkdown.renderMath) NanoMarkdown.renderMath(root);
  }, [noteText, step, terms]);

  function handlePreviewClick(e) {
    const el = e.target.closest && e.target.closest(".scratch-term");
    if (el) explainTerm(el.dataset.term, false);
  }

  function saveCourse() {
    if (!noteText.trim()) return;
    const name = (courseName() || topic).trim();
    const safe = name.replace(/[\\/:*?"<>|]/g, "-").slice(0, 80);
    const file = new File([noteText], safe + ".md", { type: "text/markdown" });
    onPicked(name, [file]);
  }
  function courseName() { return topic.trim(); }

  return (
    <div className="scratch-panel">
      {/* ── step 1: topic ── */}
      {step === "topic" && (
        <div className="scratch-step">
          <div className="course-picker-label">{t("scratch.topic_label")}</div>
          <input
            className="scratch-topic-input"
            value={topic}
            onChange={e => setTopic(e.target.value)}
            placeholder={t("scratch.topic_placeholder")}
            autoFocus
            onKeyDown={e => { if (e.key === "Enter" && topic.trim()) generateOutline(); }}
          />
          <div className="course-picker-label">{t("scratch.depth_label")}</div>
          <div className="scratch-depth-row">
            {SCRATCH_DEPTHS.map(d => (
              <button
                key={d}
                type="button"
                className={"scratch-depth-chip" + (depth === d ? " on" : "")}
                onClick={() => setDepth(d)}
              >{t("scratch.depth_" + d)}</button>
            ))}
          </div>
          <div className="course-picker-label">{t("scratch.audience_label")}</div>
          <input
            className="scratch-topic-input"
            value={audience}
            onChange={e => setAudience(e.target.value)}
            placeholder={t("scratch.audience_placeholder")}
          />
          <div className="scratch-actions">
            <button className="btn ghost" onClick={onCancel}>{t("common.cancel")}</button>
            <button
              className="btn primary"
              onClick={generateOutline}
              disabled={!topic.trim() || busy === "outline"}
            >{busy === "outline" ? t("scratch.outlining") : "✨ " + t("scratch.gen_outline")}</button>
          </div>
        </div>
      )}

      {/* ── step 2: outline (streamed, editable) ── */}
      {step === "outline" && (
        <div className="scratch-step">
          <div className="course-picker-label">
            {t("scratch.outline_label")}
            <span className="scratch-hint">{t("scratch.outline_hint")}</span>
          </div>
          <textarea
            className="scratch-outline-editor mono"
            value={outline}
            onChange={e => setOutline(e.target.value)}
            rows={14}
            spellCheck={false}
          />
          {error && <div className="error-banner">{t("scratch.error_stream")}</div>}
          <div className="scratch-actions">
            <button className="btn ghost" onClick={() => { setStep("topic"); }}>{t("scratch.back")}</button>
            <button className="btn ghost" onClick={generateOutline} disabled={busy === "outline"}>↻ {t("scratch.regenerate")}</button>
            <button className="btn primary" onClick={confirmOutline} disabled={!outline.trim() || busy === "outline"}>
              {t("scratch.gen_note")}
            </button>
          </div>
        </div>
      )}

      {/* ── step 3: note streaming / review with terms ── */}
      {step === "note" && (
        <div className="scratch-step">
          <div className="course-picker-label">
            {t("scratch.note_label")}
            {busy === "note" && <span className="scratch-hint"> {t("scratch.note_streaming")}</span>}
            {busy === "terms" && <span className="scratch-hint"> · {t("scratch.extracting_terms")}</span>}
          </div>
          <div
            ref={previewRef}
            className="scratch-preview"
            onClick={handlePreviewClick}
            dangerouslySetInnerHTML={{ __html: scratchWrapTerms(scratchMdToHtml(noteText), terms) }}
          />
          {terms.length > 0 && (
            <div className="scratch-terms-row">
              <span className="scratch-hint">{t("scratch.terms_label")}</span>
              {terms.map(term => (
                <span key={term} className="scratch-term-chip" onClick={() => explainTerm(term, false)}>{term}</span>
              ))}
            </div>
          )}
          {error && <div className="error-banner">{t("scratch.error_stream")}</div>}

          <ScratchExplain
            explain={explain}
            t={t}
            onClose={() => setExplain(null)}
            onDeeper={() => explain && explainTerm(explain.term, true)}
          />

          <div className="course-picker-label">{t("scratch.course_name_label")}</div>
          <input
            className="scratch-topic-input"
            value={topic}
            onChange={e => setTopic(e.target.value)}
            placeholder={t("scratch.topic_placeholder")}
          />
          <div className="scratch-actions">
            <button className="btn ghost" onClick={onCancel}>{t("common.cancel")}</button>
            <button
              className="btn primary"
              onClick={saveCourse}
              disabled={!noteText.trim() || busy === "note"}
            >{t("scratch.save_course")}</button>
          </div>
          <div className="scratch-hint" style={{ marginTop: 6 }}>{t("scratch.save_hint")}</div>
        </div>
      )}
    </div>
  );
}

function ScratchExplain({ explain, t, onClose, onDeeper }) {
  const ref = useRefS(null);
  useEffectS(() => {
    if (ref.current && window.NanoMarkdown && NanoMarkdown.renderMath) NanoMarkdown.renderMath(ref.current);
  }, [explain && explain.text]);
  if (!explain) return null;
  return (
    <div className="scratch-explain">
      <div className="scratch-explain-head">
        <b>{explain.term}</b>
        <button className="side-close" onClick={onClose} aria-label="close">×</button>
      </div>
      <div className="scratch-explain-body" ref={ref}>
        {explain.loading
          ? <span className="scratch-hint">{t("scratch.explaining")}<span className="stream-cursor"></span></span>
          : <div dangerouslySetInnerHTML={{ __html: NanoMDRenderLite(explain.text) }} />}
      </div>
      {!explain.loading && (
        <div className="scratch-explain-actions">
          <button className="btn ghost" onClick={onDeeper}>{t("scratch.explain_deeper")}</button>
        </div>
      )}
    </div>
  );
}

/* Tiny markdown renderer for explanation bubbles (same vocabulary the
   assistant chat uses: headings, bold, code, lists). */
function NanoMDRenderLite(text) {
  let html = scratchEscapeHtml(String(text || ""));
  html = html
    .replace(/^### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^## (.+)$/gm, "<h3>$1</h3>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^- (.+)$/gm, "<li>$1</li>")
    .replace(/\n{2,}/g, "</p><p>")
    .replace(/\n/g, "<br/>");
  html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, "<ul>$1</ul>");
  return "<p>" + html + "</p>".replace(/<p>\s*<\/p>/g, "");
}

Object.assign(window, { ScratchPanel });
