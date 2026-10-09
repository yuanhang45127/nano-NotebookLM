/* global React, SAMPLE_COLLECTIONS */
const { useState, useRef, useEffect } = React;

function FileIcon({ type }) {
  const cls = "ficon " + type;
  return <div className={cls}>{type.toUpperCase()}</div>;
}

function SourceItem({ s, active, onPick, onCheckboxClick }) {
  const t = useT();
  return (
    <div className={"source-item" + (active ? " active" : "")} onClick={() => onPick(s.id)}>
      <FileIcon type={s.type} />
      <div className="title">{s.title}</div>
      <div
        className={"check" + (s.checked ? " on" : "")}
        title={t("library.row_toggle_tip")}
        onClick={(e) => { e.stopPropagation(); onCheckboxClick(e, s.id); }}
      ></div>
      <div className="meta mono">{s.meta}</div>
    </div>
  );
}

function Library({
  sources, collections, activeId, onPick, onToggle, onToggleMany,
  onStartUpload, uploading,
  courses, activeCourse, onCourseChange, totalChunks, onManageCourses,
  hiddenCount, onOpenSettings, theme, onToggleTheme,
}) {
  const t = useT();
  // Collections list — prefer the explicit prop (lifted to React state
  // in App by review-swarm v2 fix-soon #8). Fall back to the legacy
  // window global for any host that hasn't migrated yet (e.g. demo
  // data path).
  const collectionsList = Array.isArray(collections)
    ? collections
    : (typeof SAMPLE_COLLECTIONS !== "undefined" ? SAMPLE_COLLECTIONS : []);
  // Anchor for shift-click range select — id of the last checkbox the user
  // clicked. Cleared when the source list changes underneath us (e.g.
  // course switch) since the previous id would no longer make sense.
  const lastToggledRef = useRef(null);
  // review-swarm v2 fix-now #3: the original implementation only said "is
  // cleared on source-list change" in a comment but never actually did it.
  // After a course switch, ids like `s0` get reused, so a Shift+Click on
  // the first checkbox in the new course range-toggled against a stale
  // anchor from the previous course. Reset whenever `sources` identity
  // changes (it's a fresh array reference on every getSources resolve).
  useEffect(() => { lastToggledRef.current = null; }, [sources]);

  // Storage usage gauge (sidebar footer). Recomputed when the source
  // list changes — cheap enough, and covers the common moments where
  // cache size moves (generation writes, course switch cleanup).
  const [bytes, setBytes] = useState(0);
  useEffect(() => {
    try {
      let n = 0;
      for (let i = 0; i < localStorage.length; i++) {
        const k = localStorage.key(i) || "";
        n += (k.length + (localStorage.getItem(k) || "").length) * 2;
      }
      setBytes(n);
    } catch (e) { /* private mode */ }
  }, [sources, uploading]);
  const cap = 5 * 1024 * 1024;
  const storagePct = Math.min(100, (bytes / cap) * 100);

  const checkedCount = sources.filter(s => s.checked).length;
  const total = sources.length;
  const allChecked = total > 0 && checkedCount === total;
  const noneChecked = checkedCount === 0;

  function selectAll() {
    if (typeof onToggleMany === "function") {
      onToggleMany(sources.map(s => s.id), true);
    } else {
      // Legacy fallback: emit per-id toggles for unchecked ones only.
      sources.filter(s => !s.checked).forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }
  function selectNone() {
    if (typeof onToggleMany === "function") {
      onToggleMany(sources.map(s => s.id), false);
    } else {
      sources.filter(s => s.checked).forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }
  function invertSelection() {
    if (typeof onToggleMany === "function") {
      const on = sources.filter(s => !s.checked).map(s => s.id);
      const off = sources.filter(s => s.checked).map(s => s.id);
      if (on.length) onToggleMany(on, true);
      if (off.length) onToggleMany(off, false);
    } else {
      sources.forEach(s => onToggle(s.id));
    }
    lastToggledRef.current = null;
  }

  function handleCheckboxClick(e, id) {
    // Shift+Click: toggle every source between the anchor and the clicked
    // id (inclusive) to MATCH the clicked id's NEW state. This mirrors
    // GitHub / Gmail / VS Code range-select semantics: you set one end
    // explicitly, then Shift+Click the other end and everything in
    // between snaps to the same state.
    const idx = sources.findIndex(s => s.id === id);
    if (idx < 0) return;
    const target = sources[idx];
    const desiredState = !target.checked;  // what `id` itself becomes after this click

    if (e.shiftKey && lastToggledRef.current && typeof onToggleMany === "function") {
      const anchorIdx = sources.findIndex(s => s.id === lastToggledRef.current);
      if (anchorIdx >= 0 && anchorIdx !== idx) {
        const [lo, hi] = anchorIdx < idx ? [anchorIdx, idx] : [idx, anchorIdx];
        const ids = sources.slice(lo, hi + 1).map(s => s.id);
        onToggleMany(ids, desiredState);
        lastToggledRef.current = id;
        return;
      }
    }
    onToggle(id);
    lastToggledRef.current = id;
  }

  const [dragOver, setDragOver] = useState(false);
  return (
    <aside
      className={"library" + (dragOver ? " drag-over" : "")}
      data-screen-label="Library"
      onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDragOver(false); }}
      onDrop={(e) => { e.preventDefault(); setDragOver(false); onStartUpload(); }}
    >
      <div className="lib-brand">
        <span className="logo-tile">N</span>
        <span className="mark">nano-NotebookLM</span>
        <span className="ed mono">v0.2</span>
      </div>

      <div className="lib-course">
        <div className="lib-course-select">
          <span className="globe">🌐</span>
          <select
            value={activeCourse || ""}
            onChange={e => onCourseChange && onCourseChange(e.target.value)}
            aria-label={t("library.all_courses")}
          >
            <option value="">
              {t("library.all_courses")}{typeof totalChunks === "number" && totalChunks > 0 ? ` · ${totalChunks} chunks` : ""}
            </option>
            {(courses || []).map(c => (
              <option key={c.id} value={c.id}>
                {c.name}{typeof c.chunks === "number" ? ` · ${c.chunks} chunks` : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="lib-scroll">
        <div className="lib-section">
          <h3>{t("library.sources")}</h3>
          <span className="count mono">{t("library.in_context", { n: checkedCount, total })}</span>
        </div>

        {total > 0 && (
          <div className="lib-bulk-bar mono">
            <button
              className="lib-bulk-btn"
              onClick={selectAll}
              disabled={allChecked}
              title={t("library.select_all_tip")}
            >{t("library.select_all")}</button>
            <button
              className="lib-bulk-btn"
              onClick={selectNone}
              disabled={noneChecked}
              title={t("library.select_none_tip")}
            >{t("library.select_none")}</button>
            <button
              className="lib-bulk-btn"
              onClick={invertSelection}
              title={t("library.invert_tip")}
            >{t("library.invert")}</button>
          </div>
        )}

        <button
          className={"upload-btn" + (dragOver ? " drag-hot" : "")}
          onClick={onStartUpload}
          title={t("library.drop")}
        >
          <span className="plus">＋</span>{t("scratch.mode_upload")}
        </button>

        {uploading && (
          <div className="uploading">
            <div className="lbl mono">{uploading.name}</div>
            <div className="bar"><div style={{ width: uploading.pct + "%" }}></div></div>
            <div className="lbl mono">{uploading.pct}%</div>
          </div>
        )}

        <div className="lib-list">
          {sources.map(s => (
            <SourceItem
              key={s.id}
              s={s}
              active={s.id === activeId}
              onPick={onPick}
              onCheckboxClick={handleCheckboxClick}
            />
          ))}
        </div>

        <div className="collections">
          <div className="lib-section">
            <h3>{t("library.collections")}</h3>
            {typeof onManageCourses === "function" && (
              <button
                className="course-manage-btn mono"
                style={{ marginLeft: "auto" }}
                title={hiddenCount ? `${hiddenCount} hidden` : t("library.collections")}
                onClick={onManageCourses}
              >{hiddenCount ? `▾ ${hiddenCount}` : "▾"}</button>
            )}
          </div>
          {collectionsList.map(c => (
            <div
              key={c.id}
              className={"collection-row" + (c.id === activeCourse ? " active" : "")}
              onClick={() => onCourseChange && onCourseChange(c.id)}
            >
              <div className="dot" style={{ color: c.color }}></div>
              <span>{c.name}</span>
              <span className="n">{c.count} chunks</span>
            </div>
          ))}
          <button className="lib-newcourse" onClick={onStartUpload}>＋ {t("library.new_course_action")}</button>
        </div>
      </div>

      <div className="lib-footer">
        <div className="lib-storage-label">
          <span>{t("library.storage")}</span>
          <span className="mono">{fmtBytesLib(bytes)} / 5 MB</span>
        </div>
        <div className="lib-storage-bar"><div style={{ width: storagePct + "%" }} /></div>
        <div className="lib-storage-hint">{t("library.storage_ok")}</div>
        <div className="lib-footer-actions">
          <button className="lib-set-btn" onClick={onOpenSettings}>⚙ {t("library.settings")}</button>
          <div className="spacer"></div>
          <button
            className="icon-btn"
            title={`${theme} → ${({ modern: "dark", dark: "classic", classic: "modern" })[theme] || "modern"}`}
            onClick={onToggleTheme}
          >{theme === "dark" ? "📜" : theme === "classic" ? "☀" : "🌙"}</button>
        </div>
      </div>
    </aside>
  );
}

function fmtBytesLib(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

Object.assign(window, { Library });
