(() => {
  // frontend/app.jsx
  var { useState, useEffect, useRef, useCallback, useMemo } = React;
  var api = {
    get: (url) => fetch(url).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || data.message || "API Error");
      return data;
    }),
    post: (url, body) => fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || data.message || "API Error");
      return data;
    }),
    upload: (url, file) => {
      const fd = new FormData();
      fd.append("file", file);
      return fetch(url, { method: "POST", body: fd }).then(async (r) => {
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || data.message || "API Error");
        return data;
      });
    },
    delete: (url) => fetch(url, { method: "DELETE" }).then(async (r) => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || data.message || "API Error");
      return data;
    })
  };
  function applyDevTweaks(tweaks = {}) {
    const root = document.documentElement;
    if (tweaks.accent) {
      root.style.setProperty("--accent", tweaks.accent);
      root.style.setProperty("--accent-h", tweaks.accent);
      root.style.setProperty("--accent-d", `${tweaks.accent}22`);
      root.style.setProperty("--accent-b", `${tweaks.accent}55`);
    }
    root.dataset.density = tweaks.density || "comfortable";
    root.dataset.experiment = tweaks.experiment || "standard";
  }
  function runPhaseSSE(n, { onStart, onLog, onDone, onError, rerun = false, params = {} }) {
    const qs = new URLSearchParams(params);
    const url = `/api/phase/${n}/${rerun ? "rerun" : "run"}${qs.toString() ? `?${qs}` : ""}`;
    const es = new EventSource(url);
    es.onmessage = (e) => {
      let m;
      try {
        m = JSON.parse(e.data);
      } catch {
        return;
      }
      if (m.type === "start") onStart && onStart(m);
      if (m.type === "log") onLog && onLog(m);
      if (m.type === "done") {
        onDone && onDone(m);
        es.close();
      }
      if (m.type === "error") {
        onError && onError(m);
        es.close();
      }
    };
    es.onerror = () => {
      onError && onError({ message: "Connection lost" });
      es.close();
    };
    return es;
  }
  function runPhasePromise(n, { rerun = false, onStart, params = {} } = {}) {
    return new Promise((resolve, reject) => {
      runPhaseSSE(n, {
        rerun,
        params,
        onStart,
        onDone: resolve,
        onError: reject
      });
    });
  }
  function Icon({ name, size = 16, color = "currentColor", style = {} }) {
    const ref = useRef(null);
    useEffect(() => {
      if (!ref.current || !window.lucide) return;
      ref.current.innerHTML = "";
      const el = document.createElement("i");
      el.setAttribute("data-lucide", name);
      el.style.width = size + "px";
      el.style.height = size + "px";
      el.style.color = color;
      ref.current.appendChild(el);
      window.lucide.createIcons({ nodes: [el] });
    }, [name, size, color]);
    return /* @__PURE__ */ React.createElement("span", { ref, className: "ic", style: { width: size, height: size, ...style } });
  }
  function BrandMark({ onClick }) {
    return /* @__PURE__ */ React.createElement("div", { className: "brand-mark", onClick }, /* @__PURE__ */ React.createElement("div", { className: "brand-glyph" }, /* @__PURE__ */ React.createElement("svg", { width: "12", height: "12", viewBox: "0 0 24 24", fill: "none" }, /* @__PURE__ */ React.createElement("path", { d: "M4 6h16M4 12h11M4 18h7", stroke: "#fff", strokeWidth: "2.5", strokeLinecap: "round" }), /* @__PURE__ */ React.createElement("circle", { cx: "20", cy: "18", r: "2.5", stroke: "#fff", strokeWidth: "2", fill: "none" }))), /* @__PURE__ */ React.createElement("div", { className: "brand-name" }, "jobs", /* @__PURE__ */ React.createElement("em", null, "ai")));
  }
  function PromoStrip({ onClose, text }) {
    const [t, setT] = useState({ m: 49, s: 7 });
    useEffect(() => {
      const id = setInterval(() => setT((p) => {
        let s = p.s - 1, m = p.m;
        if (s < 0) {
          s = 59;
          m = Math.max(0, m - 1);
        }
        return { m, s };
      }), 1e3);
      return () => clearInterval(id);
    }, []);
    return /* @__PURE__ */ React.createElement("div", { className: "promo-cell" }, /* @__PURE__ */ React.createElement("span", null, text || /* @__PURE__ */ React.createElement(React.Fragment, null, "Unlock unlimited applies \u2014 offer ends in ", /* @__PURE__ */ React.createElement("strong", null, String(t.m).padStart(2, "0"), "m ", String(t.s).padStart(2, "0"), "s"))), /* @__PURE__ */ React.createElement("button", { className: "promo-close", onClick: onClose }, /* @__PURE__ */ React.createElement(Icon, { name: "x", size: 13 })));
  }
  var NAV = [
    { id: "home", label: "Home", icon: "home" },
    { id: "jobs", label: "Jobs", icon: "briefcase" },
    { id: "resume", label: "Resume", icon: "file-text" },
    { id: "profile", label: "Profile", icon: "user-round" },
    { id: "agent", label: "Agent", icon: "sparkles" },
    { id: "dev", label: "Dev Ops", icon: "square-terminal" }
  ];
  var NAV_UTIL = [
    { id: "feedback", label: "Feedback", icon: "circle-help" },
    { id: "settings", label: "Settings", icon: "settings" },
    { id: "logout", label: "Sign out", icon: "log-out" }
  ];
  function Rail({ page, setPage, counts, isDev, onLogout }) {
    return /* @__PURE__ */ React.createElement("aside", { className: "rail" }, /* @__PURE__ */ React.createElement("nav", { className: "rail-nav" }, NAV.map((it) => /* @__PURE__ */ React.createElement(
      "div",
      {
        key: it.id,
        className: "rail-item" + (page === it.id ? " active" : ""),
        onClick: () => setPage(it.id)
      },
      /* @__PURE__ */ React.createElement("span", { className: "rail-icon" }, /* @__PURE__ */ React.createElement(Icon, { name: it.icon, size: 15 })),
      /* @__PURE__ */ React.createElement("span", { className: "lbl" }, it.label),
      it.badge && /* @__PURE__ */ React.createElement("span", { className: "rail-badge" }, it.badge),
      !it.badge && counts?.[it.id] != null && /* @__PURE__ */ React.createElement("span", { className: "rail-count" }, counts[it.id])
    ))), /* @__PURE__ */ React.createElement("div", { className: "rail-bottom" }, NAV_UTIL.map((it) => /* @__PURE__ */ React.createElement(
      "div",
      {
        key: it.id,
        className: "rail-item" + (page === it.id ? " active" : ""),
        onClick: () => it.id === "logout" ? onLogout() : setPage(it.id)
      },
      /* @__PURE__ */ React.createElement("span", { className: "rail-icon" }, /* @__PURE__ */ React.createElement(Icon, { name: it.icon, size: 15 })),
      /* @__PURE__ */ React.createElement("span", { className: "lbl" }, it.label)
    ))));
  }
  function HomePage({ state, setPage }) {
    return /* @__PURE__ */ React.createElement("div", { className: "lp" }, /* @__PURE__ */ React.createElement("div", { className: "lp-footer" }, "Jobs AI \xB7 Autonomous Job Application Agent \xB7 Built with React + Python"));
  }
  function Onboarding({ onLoaded }) {
    const [tab, setTab] = useState("paste");
    const [text, setText] = useState("");
    const [loading, setLoading] = useState(false);
    const [drag, setDrag] = useState(false);
    const fileRef = useRef(null);
    const handleFile = async (file) => {
      if (!file) return;
      setLoading(true);
      try {
        await api.upload("/api/resume/upload", file);
        onLoaded?.();
      } catch (e) {
        alert(e.message);
      } finally {
        setLoading(false);
      }
    };
    const handlePaste = async () => {
      if (!text.trim()) return;
      setLoading(true);
      try {
        const blob = new Blob([text], { type: "text/plain" });
        const file = new File([blob], "pasted_resume.txt", { type: "text/plain" });
        await api.upload("/api/resume/upload", file);
        onLoaded?.();
      } catch (e) {
        alert(e.message);
      } finally {
        setLoading(false);
      }
    };
    const handleDemo = async () => {
      setLoading(true);
      try {
        await api.post("/api/resume/demo", {});
        onLoaded?.();
      } finally {
        setLoading(false);
      }
    };
    return /* @__PURE__ */ React.createElement("div", { className: "onboard-wrap" }, /* @__PURE__ */ React.createElement("div", { style: { marginBottom: 28, marginTop: 16 } }, /* @__PURE__ */ React.createElement(BrandMark, null)), /* @__PURE__ */ React.createElement("div", { className: "ob-card fade-in" }, /* @__PURE__ */ React.createElement("div", { className: "ob-eyebrow" }, "Welcome to JobsAI"), /* @__PURE__ */ React.createElement("h1", { className: "ob-h1" }, "Your resume is your", /* @__PURE__ */ React.createElement("br", null), /* @__PURE__ */ React.createElement("em", null, "starting line.")), /* @__PURE__ */ React.createElement("p", { className: "ob-sub" }, "Drop it in and we'll find matching roles, score every opening against your profile, and handle applications automatically."), /* @__PURE__ */ React.createElement("div", { className: "ob-tab-row" }, /* @__PURE__ */ React.createElement("button", { className: "ob-tab" + (tab === "paste" ? " active" : ""), onClick: () => setTab("paste") }, "Paste text"), /* @__PURE__ */ React.createElement("button", { className: "ob-tab" + (tab === "upload" ? " active" : ""), onClick: () => setTab("upload") }, "Upload file")), tab === "paste" && /* @__PURE__ */ React.createElement(
      "textarea",
      {
        className: "ob-area",
        placeholder: "Paste your resume here\u2026",
        value: text,
        onChange: (e) => setText(e.target.value)
      }
    ), tab === "upload" && /* @__PURE__ */ React.createElement(
      "div",
      {
        className: "ob-drop" + (drag ? " drag" : ""),
        onDragOver: (e) => {
          e.preventDefault();
          setDrag(true);
        },
        onDragLeave: () => setDrag(false),
        onDrop: (e) => {
          e.preventDefault();
          setDrag(false);
          handleFile(e.dataTransfer.files?.[0]);
        },
        onClick: () => fileRef.current?.click()
      },
      /* @__PURE__ */ React.createElement(Icon, { name: "upload-cloud", size: 28, color: "var(--t3)" }),
      /* @__PURE__ */ React.createElement("div", { style: { marginTop: 8, fontSize: 13.5, color: "var(--t1)", fontWeight: 500 } }, "Drop your file or click to browse"),
      /* @__PURE__ */ React.createElement("div", { style: { marginTop: 4, fontSize: 12, color: "var(--t3)" } }, "PDF \xB7 DOCX \xB7 TXT"),
      /* @__PURE__ */ React.createElement(
        "input",
        {
          ref: fileRef,
          type: "file",
          accept: ".pdf,.docx,.txt",
          style: { display: "none" },
          onChange: (e) => handleFile(e.target.files?.[0])
        }
      )
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "ob-cta",
        disabled: loading || tab === "paste" && !text.trim(),
        onClick: tab === "paste" ? handlePaste : () => fileRef.current?.click()
      },
      loading ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : /* @__PURE__ */ React.createElement(Icon, { name: "arrow-right", size: 15, color: "#fff" }),
      loading ? "Processing\u2026" : "Continue"
    ), /* @__PURE__ */ React.createElement("button", { className: "ob-demo", onClick: handleDemo }, "Try with a sample resume \u2192")));
  }
  var LOGO_VARIANTS = ["v1", "v2", "v3", "v4", "v5"];
  var POSTED_LABELS = ["2 days ago", "1 week ago", "3 days ago", "Just posted", "5 days ago", "Reposted today"];
  var WORK_MODELS = ["Onsite", "Hybrid", "Remote"];
  var EXP_LEVELS = ["Internship", "Entry-level", "Mid-level", "Senior"];
  function ScoreRing({ score }) {
    const pct = Math.max(0, Math.min(100, Math.round(score)));
    const C = 26, circ = 2 * Math.PI * C;
    const off = circ - circ * pct / 100;
    const tone = pct >= 85 ? "score-high" : pct >= 65 ? "score-mid" : "score-low";
    const color = pct >= 85 ? "var(--good)" : pct >= 65 ? "var(--accent-h)" : "var(--t3)";
    const label = pct >= 85 ? "Strong" : pct >= 65 ? "Good" : pct >= 50 ? "Fair" : "Reach";
    return /* @__PURE__ */ React.createElement("div", { className: "job-score-col " + tone }, /* @__PURE__ */ React.createElement("div", { className: "score-ring" }, /* @__PURE__ */ React.createElement("svg", { width: "56", height: "56", viewBox: "0 0 56 56" }, /* @__PURE__ */ React.createElement("circle", { cx: "28", cy: "28", r: C, fill: "none", strokeWidth: "4", stroke: "rgba(255,255,255,.07)" }), /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: "28",
        cy: "28",
        r: C,
        fill: "none",
        strokeWidth: "4",
        stroke: color,
        strokeLinecap: "round",
        strokeDasharray: circ,
        strokeDashoffset: off,
        style: { transition: "stroke-dashoffset .8s cubic-bezier(.16,1,.3,1)" }
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "score-pct" }, pct)), /* @__PURE__ */ React.createElement("div", { className: "score-label" }, label));
  }
  function JobCard({ job, idx, isLiked, onLike, onHide }) {
    const logo = LOGO_VARIANTS[idx % LOGO_VARIANTS.length];
    const posted = POSTED_LABELS[idx % POSTED_LABELS.length];
    const model = WORK_MODELS[idx % WORK_MODELS.length];
    const exp = EXP_LEVELS[idx % EXP_LEVELS.length];
    const pct = Math.round(job.score || 0);
    const stripe = pct >= 85 ? "score-high" : pct >= 65 ? "score-mid" : "score-low";
    const initial = (job.co || "?").trim().charAt(0).toUpperCase();
    const tags = (job.skills || "").split(",").map((s) => s.trim()).filter(Boolean).slice(0, 3);
    return /* @__PURE__ */ React.createElement("div", { className: "job-card " + stripe }, /* @__PURE__ */ React.createElement("div", { className: "job-card-inner" }, /* @__PURE__ */ React.createElement("div", { className: "job-body" }, /* @__PURE__ */ React.createElement("div", { className: "job-header" }, /* @__PURE__ */ React.createElement("div", { className: "co-logo " + logo }, initial), /* @__PURE__ */ React.createElement("div", { className: "job-header-text" }, /* @__PURE__ */ React.createElement("div", { className: "job-posted" }, posted), /* @__PURE__ */ React.createElement("div", { className: "job-title", onClick: () => job.url && window.open(job.url, "_blank"), style: { cursor: "pointer" } }, job.role || "Untitled Role"), /* @__PURE__ */ React.createElement("div", { className: "job-company" }, /* @__PURE__ */ React.createElement("span", { className: "job-co-name" }, job.co || "\u2014"), tags[0] && /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "job-sep" }, "/"), /* @__PURE__ */ React.createElement("span", { className: "job-industry" }, tags[0]))))), /* @__PURE__ */ React.createElement("div", { className: "job-chips" }, job.loc && /* @__PURE__ */ React.createElement("span", { className: "job-chip" }, /* @__PURE__ */ React.createElement(Icon, { name: "map-pin", size: 11 }), job.loc), /* @__PURE__ */ React.createElement("span", { className: "job-chip" }, /* @__PURE__ */ React.createElement(Icon, { name: "building-2", size: 11 }), model), /* @__PURE__ */ React.createElement("span", { className: "job-chip" }, /* @__PURE__ */ React.createElement(Icon, { name: "graduation-cap", size: 11 }), exp), tags.slice(1).map((t, i) => /* @__PURE__ */ React.createElement("span", { key: i, className: "job-chip" }, t))), /* @__PURE__ */ React.createElement("div", { className: "job-footer" }, /* @__PURE__ */ React.createElement("span", { className: "job-app-count" }, idx * 31 + 47, " applicants"), /* @__PURE__ */ React.createElement("div", { className: "job-footer-actions" }, /* @__PURE__ */ React.createElement("button", { className: "icon-btn", title: "Hide", onClick: () => onHide?.(job) }, /* @__PURE__ */ React.createElement(Icon, { name: "eye-off", size: 13 })), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "icon-btn" + (isLiked ? " active" : ""),
        title: isLiked ? "Unlike" : "Save",
        onClick: () => onLike?.(job),
        style: isLiked ? { color: "var(--accent-h)", background: "var(--accent-d)", borderColor: "var(--accent-b)" } : {}
      },
      /* @__PURE__ */ React.createElement(Icon, { name: "bookmark", size: 13, fill: isLiked ? "currentColor" : "none" })
    ), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost" }, /* @__PURE__ */ React.createElement(Icon, { name: "sparkles", size: 12 }), " Ask Atlas"), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", onClick: () => job.url && window.open(job.url, "_blank") }, /* @__PURE__ */ React.createElement(Icon, { name: "zap", size: 12 }), " Quick Apply")))), /* @__PURE__ */ React.createElement(ScoreRing, { score: job.score || 0 })));
  }
  function JobsPage({ state, refresh, setPage }) {
    const [tab, setTab] = useState("recommended");
    const [searchQuery, setQuery] = useState("");
    const [running, setRun] = useState(false);
    const [searchingMore, setSearchingMore] = useState(false);
    const [runLabel, setRunLabel] = useState("");
    const autoStarted = useRef(false);
    const runningRef = useRef(false);
    const rawJobs = state?.scored_summary?.jobs || [];
    const apps = state?.applications || [];
    const liked = new Set(state?.liked_ids || []);
    const hidden = new Set(state?.hidden_ids || []);
    const filtered = useMemo(() => {
      let list = rawJobs;
      if (tab === "liked") {
        list = list.filter((j) => liked.has(j.id));
      } else if (tab === "applied") {
        const appTitles = new Set(apps.map((a) => `${a.co}|${a.role}`));
        list = list.filter((j) => appTitles.has(j.id));
      } else if (tab === "recommended") {
        list = list.filter((j) => !hidden.has(j.id));
      }
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase();
        list = list.filter(
          (j) => (j.co || "").toLowerCase().includes(q) || (j.role || "").toLowerCase().includes(q) || (j.skills || "").toLowerCase().includes(q)
        );
      }
      return list;
    }, [rawJobs, tab, searchQuery, liked, hidden, apps]);
    const tabCounts = {
      recommended: rawJobs.filter((j) => !hidden.has(j.id)).length,
      liked: liked.size,
      applied: apps.length,
      external: 0
    };
    const handleAction = async (action, job) => {
      const job_id = job.id || `${job.co}|${job.role}`;
      await api.post("/api/jobs/action", { action, job_id });
      refresh();
    };
    const removeSearch = async (title) => {
      const nextTitles = (state?.profile?.target_titles || []).filter((t) => t !== title);
      await api.post("/api/profile", { ...state.profile, target_titles: nextTitles });
      refresh();
    };
    const runDiscovery = useCallback(async ({ force = false, automatic = false, deep = false, more = false } = {}) => {
      if (runningRef.current || !state?.has_resume) return;
      runningRef.current = true;
      if (more) setSearchingMore(true);
      else setRun(true);
      const done = new Set(state?.done || []);
      try {
        if (!state?.profile) {
          setRunLabel("Reading resume");
          await runPhasePromise(1, { rerun: done.has(1) });
          await refresh();
        }
        setRunLabel(more ? "Searching more" : deep ? "Deep searching" : "Finding jobs");
        await runPhasePromise(2, {
          rerun: (force || deep || more) && done.has(2),
          params: more ? { append: 1 } : deep ? { deep: 1 } : {}
        });
        await refresh();
        setRunLabel("Ranking matches");
        await runPhasePromise(3, {
          rerun: true,
          params: { fast: 1 }
        });
        await refresh();
      } catch (e) {
        await refresh();
        if (!automatic) console.warn("Job discovery failed", e);
      } finally {
        runningRef.current = false;
        setRun(false);
        setSearchingMore(false);
        setRunLabel("");
      }
    }, [state, refresh]);
    useEffect(() => {
      if (autoStarted.current || !state?.has_resume) return;
      if (state?.scored_summary && state?.scored_summary?.total > 0) return;
      autoStarted.current = true;
      runDiscovery({ automatic: true });
    }, [state, runDiscovery]);
    const onScroll = (e) => {
      if (searchingMore || running || tab !== "recommended") return;
      const { scrollTop, scrollHeight, clientHeight } = e.target;
      if (scrollHeight - scrollTop - clientHeight < 150) {
        runDiscovery({ more: true });
      }
    };
    const handleRefresh = () => runDiscovery({ force: true });
    const handleDeepSearch = () => runDiscovery({ force: true, deep: true });
    const filters = [
      { label: state?.location || "United States", dropdown: true, id: "location" },
      { label: state?.profile?.target_titles?.[0] || "Any title", dropdown: true, active: !!state?.profile?.target_titles?.length, id: "title" },
      { label: "Experience level", dropdown: true, id: "exp" },
      { label: "Work model", dropdown: true, id: "model" },
      { label: "Date posted", dropdown: true, id: "date" },
      { label: "Salary", dropdown: true, id: "salary" }
    ];
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title" }, "JOBS"), /* @__PURE__ */ React.createElement("span", { className: "page-tab-sep" }, "\u203A"), /* @__PURE__ */ React.createElement("div", { className: "page-tabs" }, [["recommended", "Recommended"], ["liked", "Liked"], ["applied", "Applied"], ["external", "External"]].map(([id, label]) => /* @__PURE__ */ React.createElement("button", { key: id, className: "page-tab" + (tab === id ? " active" : ""), onClick: () => setTab(id) }, label, tabCounts[id] != null && /* @__PURE__ */ React.createElement("span", { className: "tab-count" }, tabCounts[id])))), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), /* @__PURE__ */ React.createElement("div", { className: "head-search" }, /* @__PURE__ */ React.createElement(Icon, { name: "search", size: 13, color: "var(--t3)" }), /* @__PURE__ */ React.createElement("input", { placeholder: "Search roles or companies", value: searchQuery, onChange: (e) => setQuery(e.target.value) })), /* @__PURE__ */ React.createElement("button", { className: "head-cta", onClick: handleRefresh, disabled: running }, running ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "spin" }), " ", runLabel || "Finding jobs", "...") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Icon, { name: "refresh-cw", size: 13, color: "#fff" }), " Refresh")), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: handleDeepSearch, disabled: running, style: { marginLeft: 8 } }, /* @__PURE__ */ React.createElement(Icon, { name: "radar", size: 12 }), " Deep search")), /* @__PURE__ */ React.createElement("div", { className: "page-body", onScroll, style: { overflowY: "auto" } }, /* @__PURE__ */ React.createElement("div", { className: "col-main" }, /* @__PURE__ */ React.createElement("div", { className: "filters" }, filters.map((f, i) => /* @__PURE__ */ React.createElement("button", { key: i, className: "f-chip" + (f.active ? " on" : ""), onClick: () => setPage("settings") }, f.label, f.dropdown && /* @__PURE__ */ React.createElement(Icon, { name: "chevron-down", size: 11 }))), /* @__PURE__ */ React.createElement("div", { className: "f-divider" }), /* @__PURE__ */ React.createElement("button", { className: "f-action secondary", onClick: () => setPage("settings") }, /* @__PURE__ */ React.createElement(Icon, { name: "sliders-horizontal", size: 11 }), " All filters"), hidden.size > 0 && /* @__PURE__ */ React.createElement("button", { className: "f-action primary", onClick: () => {
      hidden.forEach((id) => api.post("/api/jobs/action", { action: "unhide", job_id: id }));
      setTimeout(refresh, 500);
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "eye", size: 11 }), " Show ", hidden.size, " hidden")), filtered.length === 0 ? /* @__PURE__ */ React.createElement("div", { style: { background: "var(--surface)", border: "1px solid var(--bdr)", borderRadius: 14, padding: "52px 32px", textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { width: 52, height: 52, margin: "0 auto 16px", borderRadius: 14, background: "var(--accent-d)", border: "1px solid var(--accent-b)", display: "flex", alignItems: "center", justifyContent: "center" } }, /* @__PURE__ */ React.createElement(Icon, { name: tab === "liked" ? "bookmark" : "briefcase", size: 22, color: "var(--accent-h)" })), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 18, fontWeight: 600, marginBottom: 6 } }, tab === "liked" ? "No saved jobs" : tab === "applied" ? "No applications yet" : "No matched jobs yet"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--t2)", maxWidth: 400, margin: "0 auto 18px", lineHeight: 1.55 } }, running || searchingMore ? `${runLabel || "Finding jobs"} from your resume and profile.` : tab === "liked" ? "Jobs you save with the bookmark icon will appear here." : "Matched roles will appear here after the scraper checks relevant job boards."), tab === "recommended" && /* @__PURE__ */ React.createElement("button", { className: "btn-primary", onClick: handleRefresh, disabled: running, style: { margin: "0 auto" } }, running ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : /* @__PURE__ */ React.createElement(Icon, { name: "sparkles", size: 13, color: "#fff" }), running ? `${runLabel || "Working"}...` : "Find jobs now")) : /* @__PURE__ */ React.createElement("div", { className: "job-list" }, filtered.map((j, i) => /* @__PURE__ */ React.createElement(
      JobCard,
      {
        key: j.id || i,
        idx: i,
        job: j,
        isLiked: liked.has(j.id),
        onLike: () => handleAction(liked.has(j.id) ? "unlike" : "like", j),
        onHide: () => handleAction("hide", j)
      }
    )), searchingMore && /* @__PURE__ */ React.createElement("div", { style: { padding: 24, textAlign: "center", color: "var(--t3)" } }, /* @__PURE__ */ React.createElement("span", { className: "spin", style: { marginRight: 8 } }), " Finding more roles for you..."), !searchingMore && tab === "recommended" && rawJobs.length < 500 && /* @__PURE__ */ React.createElement("div", { style: { padding: 32, textAlign: "center" } }, /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: () => runDiscovery({ more: true }) }, /* @__PURE__ */ React.createElement(Icon, { name: "chevron-down", size: 14 }), " Load more jobs")))), /* @__PURE__ */ React.createElement("div", { className: "col-rail" }, /* @__PURE__ */ React.createElement("div", { className: "rcard" }, /* @__PURE__ */ React.createElement("div", { className: "user-row" }, /* @__PURE__ */ React.createElement("div", { className: "user-avatar" }, (state?.profile?.name || "U").charAt(0).toUpperCase()), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "user-name", onClick: () => setPage("profile"), style: { cursor: "pointer" } }, state?.profile?.name ? state.profile.name.split(" ")[0] : "My Account"))), /* @__PURE__ */ React.createElement("div", { className: "rcard-h" }, "Saved searches", /* @__PURE__ */ React.createElement("span", { className: "rcard-add", onClick: () => setPage("profile"), title: "Add search role" }, /* @__PURE__ */ React.createElement(Icon, { name: "plus", size: 13 }))), state?.profile?.target_titles?.slice(0, 4).map((t, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "saved-filter" }, /* @__PURE__ */ React.createElement(Icon, { name: "bookmark", size: 13, color: "var(--accent-h)" }), /* @__PURE__ */ React.createElement("span", { onClick: () => {
      setQuery(t);
      setTab("recommended");
    }, style: { cursor: "pointer" } }, t, " \xB7 ", state?.location || "US"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 4 } }, /* @__PURE__ */ React.createElement("span", { className: "saved-filter-edit", onClick: () => setPage("profile"), title: "Edit titles" }, /* @__PURE__ */ React.createElement(Icon, { name: "pencil", size: 11 })), /* @__PURE__ */ React.createElement("span", { className: "saved-filter-edit", onClick: () => removeSearch(t), title: "Remove" }, /* @__PURE__ */ React.createElement(Icon, { name: "trash-2", size: 11 }))))), !state?.profile?.target_titles?.length && /* @__PURE__ */ React.createElement("div", { className: "saved-filter", onClick: () => setPage("profile"), style: { cursor: "pointer" } }, /* @__PURE__ */ React.createElement(Icon, { name: "plus-circle", size: 13 }), /* @__PURE__ */ React.createElement("span", { style: { color: "var(--t3)" } }, "Add a search filter"))), /* @__PURE__ */ React.createElement("div", { className: "rcard" }, /* @__PURE__ */ React.createElement("div", { className: "rcard-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "activity", size: 14 }), " Pipeline status"), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 7 } }, ["Profile", "Find jobs", "Score", "Tailor", "Apply"].map((label, i) => {
      const n = i + 1;
      const isDone = (state?.done || []).includes(n);
      return /* @__PURE__ */ React.createElement("div", { key: n, style: { display: "flex", alignItems: "center", gap: 10, fontSize: 12.5 } }, /* @__PURE__ */ React.createElement("div", { style: { width: 22, height: 22, borderRadius: 6, flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "var(--mono)", fontSize: 10, fontWeight: 600, background: isDone ? "var(--accent-d)" : "var(--bg-3)", color: isDone ? "var(--accent-h)" : "var(--t4)", border: "1px solid " + (isDone ? "var(--accent-b)" : "var(--bdr)") } }, isDone ? /* @__PURE__ */ React.createElement(Icon, { name: "check", size: 11, color: "var(--accent-h)" }) : n), /* @__PURE__ */ React.createElement("span", { style: { color: isDone ? "var(--t1)" : "var(--t3)" } }, label));
    }))))));
  }
  function ActionMenu({ items = [] }) {
    const [open, setOpen] = useState(false);
    const ref = useRef(null);
    useEffect(() => {
      if (!open) return;
      const hide = (e) => {
        if (ref.current && !ref.current.contains(e.target)) setOpen(false);
      };
      document.addEventListener("mousedown", hide);
      return () => document.removeEventListener("mousedown", hide);
    }, [open]);
    return /* @__PURE__ */ React.createElement("div", { className: "action-menu-wrap", ref }, /* @__PURE__ */ React.createElement("button", { className: "icon-btn", onClick: () => setOpen(!open), style: { borderColor: "transparent" } }, /* @__PURE__ */ React.createElement(Icon, { name: "more-horizontal", size: 14 })), open && /* @__PURE__ */ React.createElement("div", { className: "action-menu fade-in" }, items.map((it, i) => /* @__PURE__ */ React.createElement("button", { key: i, className: "menu-item" + (it.danger ? " danger" : ""), onClick: () => {
      setOpen(false);
      it.onClick();
    } }, /* @__PURE__ */ React.createElement(Icon, { name: it.icon, size: 13 }), /* @__PURE__ */ React.createElement("span", null, it.label)))));
  }
  function ResumePage({ state, refresh, setPage }) {
    const [resumeText, setResumeText] = useState("");
    const [tab, setTab] = useState("analysis");
    const [loading, setLoading] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [isEditing, setIsEditing] = useState(false);
    const [editText, setEditText] = useState("");
    const fileRef = useRef(null);
    const resumes = state?.resumes || [];
    const primary = resumes.find((r) => r.primary) || resumes[0];
    const has = !!resumes.length;
    const fname = primary?.filename;
    const target = state?.profile?.target_titles?.[0];
    const phase1 = (state?.done || []).includes(1);
    const p = state?.profile || {};
    useEffect(() => {
      if (primary && !resumeText && !loading) {
        setLoading(true);
        api.get("/api/resume/content").then((res) => {
          setResumeText(res.text);
          setEditText(res.text);
        }).catch(() => {
        }).finally(() => setLoading(false));
      }
      if (!primary) {
        setResumeText("");
        setEditText("");
      }
    }, [primary?.id]);
    const stats = useMemo(() => {
      if (!phase1) return null;
      return {
        skills: (p.top_hard_skills || []).length,
        exp: (p.experience || []).length,
        gaps: (p.resume_gaps || []).length
      };
    }, [p, phase1]);
    const handleUpload = async (file) => {
      if (!file) return;
      setUploading(true);
      try {
        await api.upload("/api/resume/upload", file);
        refresh();
      } catch (e) {
        alert(e.message);
      } finally {
        setUploading(false);
      }
    };
    const handleDelete = async (id) => {
      if (!confirm("Are you sure you want to delete this resume?")) return;
      try {
        await api.delete(`/api/resume/${id}`);
        refresh();
      } catch (e) {
        alert(e.message);
      }
    };
    const handleSetPrimary = async (id) => {
      try {
        await api.post(`/api/resume/primary/${id}`, {});
        setResumeText("");
        refresh();
      } catch (e) {
        alert(e.message);
      }
    };
    const handleRename = async (id, oldName) => {
      const next = prompt("Enter new filename:", oldName);
      if (!next || next === oldName) return;
      try {
        await api.post(`/api/resume/rename/${id}`, { filename: next });
        refresh();
      } catch (e) {
        alert(e.message);
      }
    };
    const handleSaveText = async () => {
      setLoading(true);
      try {
        await api.post("/api/resume/text", { id: primary.id, text: editText });
        setResumeText(editText);
        setIsEditing(false);
        refresh();
      } catch (e) {
        alert(e.message);
      } finally {
        setLoading(false);
      }
    };
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Resume"), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), /* @__PURE__ */ React.createElement("button", { className: "head-cta", onClick: () => fileRef.current?.click(), disabled: uploading }, uploading ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : /* @__PURE__ */ React.createElement(Icon, { name: "plus", size: 13, color: "#fff" }), uploading ? "Uploading..." : "Add resume"), /* @__PURE__ */ React.createElement("input", { ref: fileRef, type: "file", style: { display: "none" }, accept: ".pdf,.docx,.txt,.md,.tex", onChange: (e) => handleUpload(e.target.files?.[0]) })), /* @__PURE__ */ React.createElement("div", { className: "page-body solo", style: { paddingTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "col-main" }, /* @__PURE__ */ React.createElement("div", { className: "notice-strip" }, /* @__PURE__ */ React.createElement(Icon, { name: "shield-check", size: 13 }), "You have ", resumes.length, " of 5 resume slots used. Files are encrypted at rest."), /* @__PURE__ */ React.createElement("div", { className: "data-card" }, /* @__PURE__ */ React.createElement("div", { className: "dt-head" }, /* @__PURE__ */ React.createElement("div", null, "Resume"), /* @__PURE__ */ React.createElement("div", null, "Target role"), /* @__PURE__ */ React.createElement("div", null, "Modified"), /* @__PURE__ */ React.createElement("div", null, "Created"), /* @__PURE__ */ React.createElement("div", null)), has ? resumes.map((r) => /* @__PURE__ */ React.createElement("div", { key: r.id, className: "dt-row" }, /* @__PURE__ */ React.createElement("div", { className: "dt-name" }, /* @__PURE__ */ React.createElement("div", { className: "dt-icon", style: !r.primary ? { background: "var(--bg-3)", color: "var(--t3)" } : {} }, r.filename.charAt(0).toUpperCase()), /* @__PURE__ */ React.createElement("span", { title: r.filename }, r.filename.replace(/\.[^.]+$/, "")), r.primary && /* @__PURE__ */ React.createElement("span", { className: "badge b-accent" }, "Primary"), r.primary && phase1 && /* @__PURE__ */ React.createElement("span", { className: "badge b-good" }, "Analyzed")), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t2)" } }, r.primary ? target || /* @__PURE__ */ React.createElement("span", { style: { color: "var(--t3)" } }, "\u2014") : /* @__PURE__ */ React.createElement("span", { style: { color: "var(--t3)" } }, "\u2014")), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t3)", fontFamily: "var(--mono)", fontSize: 11.5 } }, r.created_at ? new Date(r.created_at).toLocaleDateString() : "just now"), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t3)", fontFamily: "var(--mono)", fontSize: 11.5 } }, r.created_at ? new Date(r.created_at).toLocaleDateString() : "just now"), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement(ActionMenu, { items: [
      { icon: "star", label: "Set as primary", onClick: () => handleSetPrimary(r.id) },
      { icon: "pencil", label: "Rename", onClick: () => handleRename(r.id, r.filename) },
      { icon: "edit-3", label: "Edit text", onClick: () => {
        setTab("preview");
        setIsEditing(true);
      } },
      { icon: "trash-2", label: "Delete", danger: true, onClick: () => handleDelete(r.id) }
    ] })))) : /* @__PURE__ */ React.createElement("div", { className: "dt-empty" }, "No resumes yet \u2014 add one to start matching jobs.")), primary && /* @__PURE__ */ React.createElement("div", { style: { marginTop: 24 } }, /* @__PURE__ */ React.createElement("div", { className: "prof-tabs", style: { marginBottom: 14 } }, /* @__PURE__ */ React.createElement("button", { className: "prof-tab" + (tab === "analysis" ? " active" : ""), onClick: () => {
      setTab("analysis");
      setIsEditing(false);
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "bar-chart-3", size: 13, style: { marginRight: 6 } }), " Analysis"), /* @__PURE__ */ React.createElement("button", { className: "prof-tab" + (tab === "preview" ? " active" : ""), onClick: () => setTab("preview") }, /* @__PURE__ */ React.createElement(Icon, { name: "eye", size: 13, style: { marginRight: 6 } }), " Preview")), tab === "preview" && /* @__PURE__ */ React.createElement("div", { className: "data-card fade-in", style: { padding: 0, overflow: "hidden" } }, /* @__PURE__ */ React.createElement("div", { style: { padding: "10px 16px", background: "var(--bg-2)", borderBottom: "1px solid var(--bdr)", display: "flex", alignItems: "center", justifyContent: "space-between" } }, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 12, color: "var(--t3)", fontFamily: "var(--mono)" } }, primary.filename), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8 } }, isEditing ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: () => {
      setIsEditing(false);
      setEditText(resumeText);
    } }, "Cancel"), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", onClick: handleSaveText, disabled: loading }, loading ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : /* @__PURE__ */ React.createElement(Icon, { name: "save", size: 12, color: "#fff" }), " Save")) : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("button", { className: "icon-btn", title: "Edit text", onClick: () => setIsEditing(true) }, /* @__PURE__ */ React.createElement(Icon, { name: "edit-3", size: 12 })), /* @__PURE__ */ React.createElement("button", { className: "icon-btn", title: "Copy text", onClick: () => {
      navigator.clipboard.writeText(resumeText);
      alert("Copied!");
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "copy", size: 12 })), /* @__PURE__ */ React.createElement("button", { className: "icon-btn", title: "Download" }, /* @__PURE__ */ React.createElement(Icon, { name: "download", size: 12 }))))), /* @__PURE__ */ React.createElement("div", { style: { padding: isEditing ? 0 : 20, maxHeight: 600, overflowY: "auto", background: "#0f0f13" } }, loading && !isEditing ? /* @__PURE__ */ React.createElement("div", { style: { padding: 40, textAlign: "center", color: "var(--t4)" } }, /* @__PURE__ */ React.createElement("span", { className: "spin" }), " Loading content\u2026") : isEditing ? /* @__PURE__ */ React.createElement(
      "textarea",
      {
        className: "ob-area",
        style: { margin: 0, width: "100%", minHeight: 500, border: "none", borderRadius: 0, background: "transparent" },
        value: editText,
        onChange: (e) => setEditText(e.target.value),
        placeholder: "Resume text..."
      }
    ) : /* @__PURE__ */ React.createElement("pre", { style: { margin: 0, whiteSpace: "pre-wrap", fontSize: 13, lineHeight: 1.6, color: "#d1d1d6", fontFamily: '"JetBrains Mono", Menlo, monospace' } }, resumeText || "No text content available."))), tab === "analysis" && /* @__PURE__ */ React.createElement("div", { className: "fade-in" }, !phase1 ? /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 40, textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { width: 48, height: 48, borderRadius: 12, background: "var(--bg-3)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" } }, /* @__PURE__ */ React.createElement(Icon, { name: "sparkles", size: 24, color: "var(--t4)" })), /* @__PURE__ */ React.createElement("h3", { style: { marginBottom: 8 } }, "Not yet analyzed"), /* @__PURE__ */ React.createElement("p", { style: { color: "var(--t3)", fontSize: 13, maxWidth: 300, margin: "0 auto 20px" } }, "Run the extraction agent to see a detailed analysis of your skills and improvements."), /* @__PURE__ */ React.createElement("button", { className: "head-cta", onClick: () => setPage("agent") }, "Go to Agent")) : /* @__PURE__ */ React.createElement("div", { className: "settings-grid" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "info", size: 14 }), " Resume Quality"), /* @__PURE__ */ React.createElement("div", { style: { display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 20 } }, /* @__PURE__ */ React.createElement("div", { className: "rcard", style: { textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 20, fontWeight: 700, color: "var(--accent-h)" } }, stats.skills), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, color: "var(--t3)", marginTop: 2 } }, "Skills found")), /* @__PURE__ */ React.createElement("div", { className: "rcard", style: { textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 20, fontWeight: 700, color: "var(--accent-h)" } }, stats.exp), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, color: "var(--t3)", marginTop: 2 } }, "Experiences")), /* @__PURE__ */ React.createElement("div", { className: "rcard", style: { textAlign: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { fontSize: 20, fontWeight: 700, color: stats.gaps > 0 ? "var(--warn)" : "var(--good)" } }, stats.gaps), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 11, color: "var(--t3)", marginTop: 2 } }, "Issues"))), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Critical Analysis"), /* @__PURE__ */ React.createElement("div", { className: "analysis-text", style: { fontSize: 13, color: "var(--t2)", lineHeight: 1.7, marginTop: 8, whiteSpace: "pre-wrap" } }, p.critical_analysis || "No detailed analysis available yet. Run the extraction agent to generate a deep critique."))), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h", style: { color: "var(--warn)" } }, /* @__PURE__ */ React.createElement(Icon, { name: "alert-circle", size: 14 }), " Things to Improve"), stats.gaps > 0 ? /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 10 } }, p.resume_gaps.map((gap, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "notice-strip", style: { background: "rgba(251, 191, 36, 0.05)", borderColor: "rgba(251, 191, 36, 0.2)", color: "var(--warn)", margin: 0 } }, /* @__PURE__ */ React.createElement(Icon, { name: "chevron-right", size: 12 }), gap))) : /* @__PURE__ */ React.createElement("div", { className: "notice-strip", style: { color: "var(--good)", background: "rgba(34, 197, 94, 0.05)", borderColor: "rgba(34, 197, 94, 0.2)", margin: 0 } }, /* @__PURE__ */ React.createElement(Icon, { name: "check-circle-2", size: 13 }), "No major issues detected. Great job!"), /* @__PURE__ */ React.createElement("div", { className: "set-helper", style: { marginTop: 12 } }, "These improvements are identified based on typical ATS requirements for ", target || "technical", " roles."))))))));
  }
  function ProfilePage({ state, refresh, setPage }) {
    const p = state?.profile;
    const [saving, setSaving] = useState(false);
    const [extracting, setExtracting] = useState(false);
    const [form, setForm] = useState(() => profileToForm(p));
    const [dirty, setDirty] = useState(false);
    useEffect(() => {
      if (!dirty) {
        setForm(profileToForm(p));
      }
    }, [p, dirty]);
    if (!p) return /* @__PURE__ */ React.createElement("div", { className: "placeholder-page" }, /* @__PURE__ */ React.createElement("div", { className: "placeholder-icon" }, /* @__PURE__ */ React.createElement(Icon, { name: "user", size: 22 })), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 18, fontWeight: 600 } }, "No profile found"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--t2)" } }, "Extract your resume or create the profile manually."), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 10, marginTop: 16 } }, /* @__PURE__ */ React.createElement("button", { className: "btn-primary", onClick: async () => {
      await api.post("/api/profile/extract", {});
      refresh?.();
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "scan-text", size: 14 }), " Extract from resume"), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: async () => {
      await api.post("/api/profile", { name: "", target_titles: [], top_hard_skills: [], top_soft_skills: [], education: [], experience: [], research: [], projects: [] });
      refresh?.();
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "pencil", size: 14 }), " Create manually")));
    const updateField = (key, value) => {
      setForm((prev) => ({ ...prev, [key]: value }));
      setDirty(true);
    };
    const updateRow = (key, index, field, value) => {
      setForm((prev) => ({
        ...prev,
        [key]: prev[key].map((item, i) => i === index ? { ...item, [field]: value } : item)
      }));
      setDirty(true);
    };
    const addRow = (key, row) => {
      setForm((prev) => ({ ...prev, [key]: [...prev[key], row] }));
      setDirty(true);
    };
    const removeRow = (key, index) => {
      setForm((prev) => ({ ...prev, [key]: prev[key].filter((_, i) => i !== index) }));
      setDirty(true);
    };
    const saveProfile = async () => {
      setSaving(true);
      try {
        await api.post("/api/profile", formToProfile(form));
        setDirty(false);
        await refresh?.();
      } finally {
        setSaving(false);
      }
    };
    const rerunExtraction = async () => {
      setExtracting(true);
      try {
        await api.post("/api/profile/extract", { preferred_titles: splitList(form.target_titles) });
        refresh?.();
      } finally {
        setExtracting(false);
      }
    };
    const syncSearch = async () => {
      await saveProfile();
      await api.post("/api/config", { job_titles: form.target_titles });
      setPage?.("jobs");
    };
    const profileAction = async (action, payload = {}) => {
      setSaving(true);
      try {
        await api.post("/api/profile/action", { action, ...payload });
        refresh?.();
      } finally {
        setSaving(false);
      }
    };
    const completion = p.completion || { percent: 0, missing: [] };
    const settings = p.settings || {};
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Profile"), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: rerunExtraction, disabled: extracting }, /* @__PURE__ */ React.createElement(Icon, { name: "scan-text", size: 14 }), " ", extracting ? "Extracting..." : "Re-scrape resume"), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: saveProfile, disabled: saving }, /* @__PURE__ */ React.createElement(Icon, { name: "save", size: 14 }), " ", saving ? "Saving..." : "Save profile"), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", onClick: syncSearch, disabled: saving }, /* @__PURE__ */ React.createElement(Icon, { name: "search", size: 14 }), " Explore jobs")), /* @__PURE__ */ React.createElement("div", { className: "page-body" }, /* @__PURE__ */ React.createElement("div", { className: "col-main", style: { gap: 24 } }, /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 24 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { marginBottom: 16, fontSize: 16 } }, "Personal"), /* @__PURE__ */ React.createElement("div", { className: "profile-grid" }, /* @__PURE__ */ React.createElement(ProfileInput, { label: "Name", value: form.name, onChange: (v) => updateField("name", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Email", value: form.email, onChange: (v) => updateField("email", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Phone", value: form.phone, onChange: (v) => updateField("phone", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Location", value: form.location, onChange: (v) => updateField("location", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "LinkedIn URL", value: form.linkedin, onChange: (v) => updateField("linkedin", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "GitHub URL", value: form.github, onChange: (v) => updateField("github", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Work authorization", value: form.work_authorization, onChange: (v) => updateField("work_authorization", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Target salary", value: form.target_salary, onChange: (v) => updateField("target_salary", v) })), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Professional summary", textarea: true, value: form.summary, onChange: (v) => updateField("summary", v) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 24 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { marginBottom: 20, fontSize: 16 } }, "Work Experience"), /* @__PURE__ */ React.createElement(EditableRoles, { items: form.experience, onChange: (i, f, v) => updateRow("experience", i, f, v), onAdd: () => addRow("experience", emptyRole()), onRemove: (i) => removeRow("experience", i) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 24 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { marginBottom: 20, fontSize: 16 } }, "Research & Lab Roles"), /* @__PURE__ */ React.createElement(EditableRoles, { items: form.research, onChange: (i, f, v) => updateRow("research", i, f, v), onAdd: () => addRow("research", emptyRole()), onRemove: (i) => removeRow("research", i) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 24 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { marginBottom: 20, fontSize: 16 } }, "Technical Projects"), /* @__PURE__ */ React.createElement(EditableProjects, { items: form.projects, onChange: (i, f, v) => updateRow("projects", i, f, v), onAdd: () => addRow("projects", { name: "", description: "", skills_used: [] }), onRemove: (i) => removeRow("projects", i) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 24 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { marginBottom: 16, fontSize: 16 } }, "Resume Critique & Gaps"), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Critical analysis", textarea: true, value: form.critical_analysis, onChange: (v) => updateField("critical_analysis", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "ATS gaps, comma-separated", value: form.resume_gaps, onChange: (v) => updateField("resume_gaps", v) }))), /* @__PURE__ */ React.createElement("div", { className: "col-rail", style: { gap: 24 } }, /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 20 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { fontSize: 14, marginBottom: 8 } }, /* @__PURE__ */ React.createElement(Icon, { name: "circle-check", size: 14 }), " Profile ", completion.percent, "% complete"), /* @__PURE__ */ React.createElement("div", { style: { height: 6, borderRadius: 999, background: "var(--bg-1)", overflow: "hidden", margin: "12px 0" } }, /* @__PURE__ */ React.createElement("div", { style: { height: "100%", width: `${completion.percent}%`, background: "var(--accent)" } })), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 12, color: "var(--t2)", lineHeight: 1.5 } }, completion.missing?.length ? `Missing: ${completion.missing.slice(0, 4).join(", ")}` : "Profile is ready for matching and autofill."), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", style: { width: "100%", marginTop: 14 }, onClick: syncSearch }, "Explore jobs")), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 8 } }, /* @__PURE__ */ React.createElement(ProfileAction, { icon: "rocket", label: settings.visibility_boosted ? "Visibility boosted" : "Boost visibility", onClick: () => profileAction("boost_visibility") }), /* @__PURE__ */ React.createElement(ProfileAction, { icon: "file-text", label: "Manage resume", onClick: () => setPage?.("resume") }), /* @__PURE__ */ React.createElement(ProfileAction, { icon: "linkedin", label: "Update LinkedIn URL", onClick: () => profileAction("update_linkedin", { linkedin: form.linkedin }) }), /* @__PURE__ */ React.createElement(ProfileAction, { icon: "bell", label: settings.job_alerts ? "Job alerts on" : "Job alerts off", onClick: () => profileAction("toggle_job_alerts", { enabled: !settings.job_alerts }) }), /* @__PURE__ */ React.createElement(ProfileAction, { icon: "globe", label: "Work authorization", onClick: () => profileAction("update_work_authorization", { work_authorization: form.work_authorization }) }), /* @__PURE__ */ React.createElement(ProfileAction, { icon: "badge-dollar-sign", label: "Target salary", onClick: () => profileAction("update_target_salary", { target_salary: form.target_salary }) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 20 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { fontSize: 13, marginBottom: 12 } }, "Target Roles"), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Comma-separated titles", value: form.target_titles, onChange: (v) => updateField("target_titles", v) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 20 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { fontSize: 13, marginBottom: 12 } }, "Top Hard Skills"), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Comma-separated skills", textarea: true, value: form.top_hard_skills, onChange: (v) => updateField("top_hard_skills", v) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 20 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { fontSize: 13, marginBottom: 12 } }, "Soft Skills"), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Comma-separated skills", value: form.top_soft_skills, onChange: (v) => updateField("top_soft_skills", v) })), /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 20 } }, /* @__PURE__ */ React.createElement("h3", { className: "prof-h", style: { fontSize: 13, marginBottom: 12 } }, "Education"), /* @__PURE__ */ React.createElement(EditableEducation, { items: form.education, onChange: (i, f, v) => updateRow("education", i, f, v), onAdd: () => addRow("education", { degree: "", institution: "", year: "", gpa: "" }), onRemove: (i) => removeRow("education", i) })))));
  }
  function splitList(value) {
    return String(value || "").split(",").map((s) => s.trim());
  }
  function emptyRole() {
    return { title: "", company: "", dates: "", bullets: [] };
  }
  function profileToForm(p = {}) {
    return {
      name: p?.name || "",
      email: p?.email || "",
      phone: p?.phone || "",
      location: p?.location || "",
      linkedin: p?.linkedin || "",
      github: p?.github || "",
      summary: p?.summary || "",
      work_authorization: p?.work_authorization || "",
      target_salary: p?.target_salary || "",
      critical_analysis: p?.critical_analysis || "",
      target_titles: (p?.target_titles || []).join(", "),
      top_hard_skills: (p?.top_hard_skills || []).join(", "),
      top_soft_skills: (p?.top_soft_skills || []).join(", "),
      resume_gaps: (p?.resume_gaps || []).join(", "),
      education: p?.education || [],
      experience: p?.experience || [],
      research: p?.research || [],
      projects: p?.projects || []
    };
  }
  function formToProfile(form) {
    const roleList = (rows) => rows.map((r) => ({ ...r, bullets: (Array.isArray(r.bullets) ? r.bullets : splitBullets(r.bullets)).filter(Boolean) }));
    return {
      ...form,
      target_titles: splitList(form.target_titles).filter(Boolean),
      top_hard_skills: splitList(form.top_hard_skills).filter(Boolean),
      top_soft_skills: splitList(form.top_soft_skills).filter(Boolean),
      resume_gaps: splitList(form.resume_gaps).filter(Boolean),
      experience: roleList(form.experience),
      research: roleList(form.research),
      education: form.education,
      projects: form.projects.map((p) => ({ ...p, skills_used: (Array.isArray(p.skills_used) ? p.skills_used : splitList(p.skills_used)).filter(Boolean) }))
    };
  }
  function splitBullets(value) {
    return String(value || "").split("\n").map((s) => s.trim());
  }
  function ProfileInput({ label, value, onChange, textarea = false }) {
    const Tag = textarea ? "textarea" : "input";
    return /* @__PURE__ */ React.createElement("label", { className: "set-field" }, /* @__PURE__ */ React.createElement("span", { className: "set-label" }, label), /* @__PURE__ */ React.createElement(Tag, { className: "profile-input" + (textarea ? " profile-textarea" : ""), value: value || "", onChange: (e) => onChange(e.target.value) }));
  }
  function ProfileAction({ icon, label, onClick }) {
    return /* @__PURE__ */ React.createElement("button", { className: "profile-action", onClick }, /* @__PURE__ */ React.createElement(Icon, { name: icon, size: 15 }), /* @__PURE__ */ React.createElement("span", null, label), /* @__PURE__ */ React.createElement(Icon, { name: "chevron-right", size: 14 }));
  }
  function EditableRoles({ items, onChange, onAdd, onRemove }) {
    return /* @__PURE__ */ React.createElement("div", { className: "profile-stack" }, (items || []).map((item, i) => /* @__PURE__ */ React.createElement("div", { className: "profile-edit-row", key: i }, /* @__PURE__ */ React.createElement("button", { className: "edit-trigger always", onClick: () => onRemove(i) }, /* @__PURE__ */ React.createElement(Icon, { name: "trash-2", size: 13 })), /* @__PURE__ */ React.createElement("div", { className: "profile-grid" }, /* @__PURE__ */ React.createElement(ProfileInput, { label: "Title", value: item.title, onChange: (v) => onChange(i, "title", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Organization", value: item.company || item.institution, onChange: (v) => onChange(i, "company", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Dates", value: item.dates, onChange: (v) => onChange(i, "dates", v) })), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Bullets, one per line", textarea: true, value: (item.bullets || []).join("\n"), onChange: (v) => onChange(i, "bullets", splitBullets(v)) }))), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: onAdd }, /* @__PURE__ */ React.createElement(Icon, { name: "plus", size: 13 }), " Add role"));
  }
  function EditableProjects({ items, onChange, onAdd, onRemove }) {
    return /* @__PURE__ */ React.createElement("div", { className: "profile-stack" }, (items || []).map((item, i) => /* @__PURE__ */ React.createElement("div", { className: "profile-edit-row", key: i }, /* @__PURE__ */ React.createElement("button", { className: "edit-trigger always", onClick: () => onRemove(i) }, /* @__PURE__ */ React.createElement(Icon, { name: "trash-2", size: 13 })), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Project name", value: item.name, onChange: (v) => onChange(i, "name", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Description", textarea: true, value: item.description, onChange: (v) => onChange(i, "description", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Skills used", value: (item.skills_used || []).join(", "), onChange: (v) => onChange(i, "skills_used", splitList(v)) }))), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: onAdd }, /* @__PURE__ */ React.createElement(Icon, { name: "plus", size: 13 }), " Add project"));
  }
  function EditableEducation({ items, onChange, onAdd, onRemove }) {
    return /* @__PURE__ */ React.createElement("div", { className: "profile-stack" }, (items || []).map((item, i) => /* @__PURE__ */ React.createElement("div", { className: "profile-edit-row compact", key: i }, /* @__PURE__ */ React.createElement("button", { className: "edit-trigger always", onClick: () => onRemove(i) }, /* @__PURE__ */ React.createElement(Icon, { name: "trash-2", size: 13 })), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Institution", value: item.institution, onChange: (v) => onChange(i, "institution", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Degree", value: item.degree, onChange: (v) => onChange(i, "degree", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "Year", value: item.year, onChange: (v) => onChange(i, "year", v) }), /* @__PURE__ */ React.createElement(ProfileInput, { label: "GPA", value: item.gpa, onChange: (v) => onChange(i, "gpa", v) }))), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: onAdd }, /* @__PURE__ */ React.createElement(Icon, { name: "plus", size: 13 }), " Add education"));
  }
  var PHASE_INFO = {
    1: { n: "Profile extraction", s: "Parse resume \u2192 structured profile" },
    2: { n: "Find jobs", s: "Scrape job boards for live openings" },
    3: { n: "Score & filter", s: "Rank roles by skill alignment" },
    4: { n: "Tailor resumes", s: "Generate ATS-tuned variants per role" },
    5: { n: "Apply", s: "Submit to high-confidence roles" },
    6: { n: "Track", s: "Update application tracker" },
    7: { n: "Report", s: "Generate session summary" }
  };
  var CLI_LINES = {
    1: ["$ agent.py --phase 1", "  parsing resume -> extracting text...", "  extracting skills and experience...", "  auditing skill evidence...", "  ranking target titles...", "OK phase_1 complete"],
    2: ["$ agent.py --phase 2", "  sources: linkedin / indeed / glassdoor / ziprecruiter", "  simplify dataset enabled", "  deduplicating postings...", "  applying education + citizenship filters...", "OK phase_2 complete"],
    3: ["$ agent.py --phase 3", "  scoring jobs against profile...", "  weighting skills, industry, location...", "  filtering by experience level...", "OK phase_3 complete"],
    4: ["$ agent.py --phase 4", "  tailoring resumes for shortlisted jobs...", "  reordering skills to match job descriptions...", "  running ATS gap analysis...", "  saving resume variants...", "OK phase_4 complete"],
    5: ["$ agent.py --phase 5", "  submitting auto-eligible applications...", "  flagging manual-review applications...", "OK phase_5 complete"],
    6: ["$ agent.py --phase 6", "  writing Job_Applications_Tracker...", "  status colors and dashboard applied", "OK phase_6 complete"],
    7: ["$ agent.py --phase 7", "  generating run report...", "  saving final summary...", "OK phase_7 complete"]
  };
  function PhaseLog({ n, logs = [], running }) {
    const lines = logs.length ? logs : CLI_LINES[n] || [];
    if (!lines.length && !running) return null;
    return /* @__PURE__ */ React.createElement("div", { className: "agent-log" }, (running && !logs.length ? lines.slice(0, -1) : lines).map((line, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: line.trim().startsWith("OK") ? "ok" : "" }, line)), running && /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", { className: "spin", style: { width: 10, height: 10, marginRight: 6 } }), "streaming backend output..."));
  }
  function KVList({ items }) {
    return /* @__PURE__ */ React.createElement("div", { className: "detail-kv" }, items.filter(Boolean).map(([k, v], i) => /* @__PURE__ */ React.createElement("div", { key: i }, /* @__PURE__ */ React.createElement("span", null, k), /* @__PURE__ */ React.createElement("b", null, v || "-"))));
  }
  function DetailTable({ columns, rows, empty = "No rows yet." }) {
    if (!rows?.length) return /* @__PURE__ */ React.createElement("div", { className: "wait-state" }, empty);
    return /* @__PURE__ */ React.createElement("div", { className: "detail-table-wrap" }, /* @__PURE__ */ React.createElement("table", { className: "detail-table" }, /* @__PURE__ */ React.createElement("thead", null, /* @__PURE__ */ React.createElement("tr", null, columns.map((c) => /* @__PURE__ */ React.createElement("th", { key: c.key }, c.label)))), /* @__PURE__ */ React.createElement("tbody", null, rows.map((row, i) => /* @__PURE__ */ React.createElement("tr", { key: i }, columns.map((c) => /* @__PURE__ */ React.createElement("td", { key: c.key, className: c.strong ? "t1" : "" }, c.render ? c.render(row, i) : row[c.key])))))));
  }
  function PhaseDetails({ n, data = {}, state = {}, threshold }) {
    if (n === 1) {
      const p = data.name || data.email || data.top_hard_skills ? data : state.profile || {};
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, /* @__PURE__ */ React.createElement(KVList, { items: [["Name", p.name], ["Email", p.email], ["Location", p.location], ["LinkedIn", p.linkedin]] }), /* @__PURE__ */ React.createElement("div", { className: "detail-grid" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Target titles"), /* @__PURE__ */ React.createElement("div", { className: "csv-text" }, (p.target_titles || []).join(", "))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Hard skills"), /* @__PURE__ */ React.createElement("div", { className: "csv-text" }, (p.top_hard_skills || []).join(", "))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Soft skills"), /* @__PURE__ */ React.createElement("div", { className: "csv-text" }, (p.top_soft_skills || []).join(", "))), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("h4", null, "Resume gaps"), /* @__PURE__ */ React.createElement("div", { className: "csv-text", style: { color: "var(--warn)" } }, (p.resume_gaps || []).join(", ")))));
    }
    if (n === 2) {
      const jobs = data.jobs || [];
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, /* @__PURE__ */ React.createElement("div", { className: "metrics" }, /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.total ?? state.job_count ?? jobs.length), /* @__PURE__ */ React.createElement("span", null, "Jobs discovered"))), /* @__PURE__ */ React.createElement(DetailTable, { columns: [{ key: "co", label: "Company", strong: true }, { key: "role", label: "Role" }, { key: "loc", label: "Location" }, { key: "experience", label: "Level" }, { key: "education", label: "Education" }, { key: "posted", label: "Posted" }, { key: "url", label: "URL", render: (j) => j.url ? /* @__PURE__ */ React.createElement("a", { href: j.url, target: "_blank", rel: "noreferrer" }, "Open") : "-" }], rows: jobs, empty: "Run or re-run Phase 2 to see every discovered posting." }));
    }
    if (n === 3) {
      const summary = state.scored_summary || {};
      const jobs = data.jobs || summary.jobs || [];
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, /* @__PURE__ */ React.createElement("div", { className: "metrics" }, /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.total ?? summary.total ?? 0), /* @__PURE__ */ React.createElement("span", null, "Scored")), /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.auto ?? summary.auto ?? 0), /* @__PURE__ */ React.createElement("span", null, "Auto at ", threshold)), /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.manual ?? summary.manual ?? 0), /* @__PURE__ */ React.createElement("span", null, "Manual review")), /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.filtered ?? summary.filtered ?? 0), /* @__PURE__ */ React.createElement("span", null, "Filtered"))), /* @__PURE__ */ React.createElement(DetailTable, { columns: [{ key: "co", label: "Company", strong: true }, { key: "role", label: "Role" }, { key: "score", label: "Score", strong: true }, { key: "status", label: "Status" }, { key: "matching", label: "Matching", render: (j) => Array.isArray(j.matching) ? j.matching.join(", ") : j.skills || "" }, { key: "missing", label: "Missing", render: (j) => Array.isArray(j.missing) ? j.missing.join(", ") : "" }, { key: "reason", label: "Reason" }], rows: jobs }));
    }
    if (n === 4) {
      const items = data.items || [];
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, /* @__PURE__ */ React.createElement("div", { className: "metrics" }, /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.count ?? items.length), /* @__PURE__ */ React.createElement("span", null, "Resume variants"))), /* @__PURE__ */ React.createElement(DetailTable, { columns: [{ key: "co", label: "Company", strong: true }, { key: "role", label: "Role" }, { key: "score", label: "Match" }, { key: "ats_after", label: "ATS after", strong: true }, { key: "ats_gaps", label: "Remaining gaps", render: (x) => (x.ats_gaps || []).join(", ") }, { key: "resume_file", label: "Resume", render: (x) => x.resume_file ? /* @__PURE__ */ React.createElement("a", { href: `/output/${x.resume_file}`, download: true }, x.resume_file) : "-" }], rows: items, empty: "Run or re-run Phase 4 to see tailored resume details." }));
    }
    if (n === 5) {
      const apps = data.apps || state.applications || [];
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, /* @__PURE__ */ React.createElement("div", { className: "metrics" }, /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.applied ?? apps.filter((a) => a.app_status === "Applied" || a.status === "Applied").length), /* @__PURE__ */ React.createElement("span", null, "Applied")), /* @__PURE__ */ React.createElement("div", { className: "met" }, /* @__PURE__ */ React.createElement("b", null, data.manual ?? apps.filter((a) => a.app_status === "Manual Required" || a.status === "Manual Required").length), /* @__PURE__ */ React.createElement("span", null, "Manual"))), /* @__PURE__ */ React.createElement(DetailTable, { columns: [{ key: "co", label: "Company", strong: true }, { key: "role", label: "Role" }, { key: "score", label: "Score" }, { key: "status", label: "Status", render: (x) => x.status || x.app_status }, { key: "confirmation", label: "Confirmation" }, { key: "resume", label: "Resume", render: (x) => x.resume || x.resume_version || "-" }, { key: "url", label: "URL", render: (x) => x.url ? /* @__PURE__ */ React.createElement("a", { href: x.url, target: "_blank", rel: "noreferrer" }, "Open") : "-" }], rows: apps }));
    }
    if (n === 6) {
      const tracker = data.tracker || state.output_files?.find((f) => f.phase === 6)?.name;
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, tracker ? /* @__PURE__ */ React.createElement("a", { className: "detail-file", href: `/output/${tracker}`, download: true }, /* @__PURE__ */ React.createElement(Icon, { name: "download", size: 13 }), " ", tracker) : /* @__PURE__ */ React.createElement("div", { className: "wait-state" }, "Tracker not generated yet."));
    }
    if (n === 7) {
      return /* @__PURE__ */ React.createElement("div", { className: "phase-detail" }, data.report ? /* @__PURE__ */ React.createElement("pre", { className: "report-pre" }, data.report) : /* @__PURE__ */ React.createElement("div", { className: "wait-state" }, "Run report not generated yet."));
    }
    return null;
  }
  function AgentPage({ state, refresh }) {
    const [open, setOpen] = useState({});
    const [running, setRunning] = useState(null);
    const [errors, setErrors] = useState({});
    const [phaseResults, setPhaseResults] = useState({});
    const [phaseLogs, setPhaseLogs] = useState({});
    const done = new Set(state?.done || []);
    const pct = Math.round(done.size / 7 * 100);
    const C = 65, circ = 2 * Math.PI * C;
    const off = circ - circ * pct / 100;
    const startPhase = (n, rerun = false) => {
      if (running) return;
      setRunning(n);
      setErrors((p) => ({ ...p, [n]: null }));
      setOpen((o) => ({ ...o, [n]: true }));
      setPhaseLogs((p) => ({ ...p, [n]: [] }));
      runPhaseSSE(n, {
        rerun,
        onLog: (m) => setPhaseLogs((p) => ({ ...p, [n]: [...p[n] || [], m.text || m.line || ""] })),
        onDone: (m) => {
          setPhaseResults((p) => ({ ...p, [n]: m.data || {} }));
          setRunning(null);
          refresh();
        },
        onError: (e) => {
          setRunning(null);
          setErrors((p) => ({ ...p, [n]: e.message || "failed" }));
          refresh();
        }
      });
    };
    const runAll = async () => {
      if (running) return;
      for (let n = 1; n <= 7; n++) {
        if (done.has(n)) continue;
        await new Promise((res, rej) => {
          setRunning(n);
          setOpen((o) => ({ ...o, [n]: true }));
          setPhaseLogs((p) => ({ ...p, [n]: [] }));
          runPhaseSSE(n, {
            onLog: (m) => setPhaseLogs((p) => ({ ...p, [n]: [...p[n] || [], m.text || m.line || ""] })),
            onDone: (m) => {
              setPhaseResults((p) => ({ ...p, [n]: m.data || {} }));
              setRunning(null);
              refresh();
              res();
            },
            onError: (e) => {
              setRunning(null);
              setErrors((p) => ({ ...p, [n]: e.message }));
              refresh();
              rej(e);
            }
          });
        }).catch(() => {
        });
      }
    };
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Agent"), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: () => api.post("/api/reset", {}).then(refresh) }, /* @__PURE__ */ React.createElement(Icon, { name: "rotate-ccw", size: 12 }), " Reset"), /* @__PURE__ */ React.createElement("button", { className: "head-cta", onClick: runAll, disabled: !!running, style: { marginLeft: 8 } }, running ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("span", { className: "spin" }), " Running phase ", running, "\u2026") : /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(Icon, { name: "play", size: 13, color: "#fff" }), " Run all phases"))), /* @__PURE__ */ React.createElement("div", { className: "page-body solo", style: { paddingTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "col-main" }, /* @__PURE__ */ React.createElement("div", { className: "agent-hero" }, /* @__PURE__ */ React.createElement("div", { style: { flex: 1, minWidth: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "agent-eyebrow" }, "Autonomous mode"), /* @__PURE__ */ React.createElement("h1", { className: "agent-h" }, "Atlas runs your entire job\u2011search pipeline."), /* @__PURE__ */ React.createElement("p", { className: "agent-p" }, "From resume parsing to one-click applies, seven chained phases handle everything. Pause and inspect any step, or let it run end-to-end.")), /* @__PURE__ */ React.createElement("div", { className: "agent-ring" }, /* @__PURE__ */ React.createElement("svg", { width: "120", height: "120", viewBox: "0 0 150 150" }, /* @__PURE__ */ React.createElement("circle", { cx: "75", cy: "75", r: C, fill: "none", strokeWidth: "7", stroke: "rgba(255,255,255,.06)" }), /* @__PURE__ */ React.createElement(
      "circle",
      {
        cx: "75",
        cy: "75",
        r: C,
        fill: "none",
        strokeWidth: "7",
        stroke: "var(--accent-h)",
        strokeLinecap: "round",
        strokeDasharray: circ,
        strokeDashoffset: off,
        style: { transition: "stroke-dashoffset .8s" }
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "agent-ring-pct" }, /* @__PURE__ */ React.createElement("b", null, pct, "%"), /* @__PURE__ */ React.createElement("span", null, done.size, "/7 phases")))), /* @__PURE__ */ React.createElement("div", { className: "phases" }, [1, 2, 3, 4, 5, 6, 7].map((n) => {
      const isDone = done.has(n);
      const isRun = running === n;
      const err = errors[n] || state?.error?.[n];
      const elapsed = state?.elapsed?.[n];
      const cls = isRun ? "run" : err ? "err" : isDone ? "done" : "";
      return /* @__PURE__ */ React.createElement("div", { key: n, className: "ph " + cls }, /* @__PURE__ */ React.createElement("div", { className: "ph-hd", onClick: () => setOpen((o) => ({ ...o, [n]: !o[n] })) }, /* @__PURE__ */ React.createElement("div", { className: "ph-num" }, n), /* @__PURE__ */ React.createElement("div", { style: { flex: 1, minWidth: 0 } }, /* @__PURE__ */ React.createElement("div", { className: "ph-name" }, PHASE_INFO[n].n), /* @__PURE__ */ React.createElement("div", { className: "ph-sub" }, PHASE_INFO[n].s)), isRun && /* @__PURE__ */ React.createElement("span", { className: "ph-badge", style: { background: "var(--warn-d)", color: "var(--warn)" } }, "Running"), !isRun && isDone && /* @__PURE__ */ React.createElement("span", { className: "ph-badge", style: { background: "var(--good-d)", color: "var(--good)" } }, "Done"), !isRun && !isDone && !err && /* @__PURE__ */ React.createElement("span", { className: "ph-badge", style: { background: "rgba(255,255,255,.04)", color: "var(--t3)" } }, "Waiting"), !isRun && err && /* @__PURE__ */ React.createElement("span", { className: "ph-badge", style: { background: "var(--bad-d)", color: "var(--bad)" } }, "Error"), elapsed != null && /* @__PURE__ */ React.createElement("span", { className: "ph-elapsed" }, elapsed.toFixed(1), "s"), /* @__PURE__ */ React.createElement(
        "button",
        {
          className: "btn-ghost",
          style: { marginLeft: 6 },
          onClick: (e) => {
            e.stopPropagation();
            startPhase(n, isDone);
          },
          disabled: !!running
        },
        /* @__PURE__ */ React.createElement(Icon, { name: isDone ? "rotate-ccw" : "play", size: 11 }),
        isDone ? "Re-run" : "Run"
      ), /* @__PURE__ */ React.createElement("span", { className: "ph-chev" + (open[n] ? " open" : ""), style: { marginLeft: 6 } }, /* @__PURE__ */ React.createElement(Icon, { name: "chevron-down", size: 14 }))), isRun && /* @__PURE__ */ React.createElement("div", { className: "ph-loading-bar" }, /* @__PURE__ */ React.createElement("div", { className: "ph-loading-fill" })), open[n] && /* @__PURE__ */ React.createElement("div", { className: "ph-body fade-in" }, /* @__PURE__ */ React.createElement(PhaseLog, { n, logs: phaseLogs[n], running: isRun }), err && /* @__PURE__ */ React.createElement("div", { className: "err-block" }, "Warning: ", err), !err && isDone && /* @__PURE__ */ React.createElement(PhaseDetails, { n, data: phaseResults[n] || state?.phase_results?.[n] || state?.phase_results?.[String(n)] || {}, state, threshold: state?.threshold || 75 }), !err && !isDone && !isRun && /* @__PURE__ */ React.createElement("div", { className: "wait-state" }, "Waiting for phase ", n - 1, " to complete first.")));
    })))));
  }
  function SettingsPage({ state, refresh }) {
    const [cfg, setCfg] = useState(state || {});
    const [saving, setSaving] = useState(false);
    const [ollamaModels, setOllamaModels] = useState([]);
    const [ollamaOk, setOllamaOk] = useState(null);
    const update = async (newCfg) => {
      setCfg((p) => ({ ...p, ...newCfg }));
      setSaving(true);
      try {
        await api.post("/api/config", newCfg);
        refresh();
      } finally {
        setTimeout(() => setSaving(false), 600);
      }
    };
    useEffect(() => {
      if (cfg.mode !== "ollama") return;
      api.get("/api/ollama/status").then((s) => {
        setOllamaOk(s.running);
        setOllamaModels(s.models || []);
        if (s.models && s.models.length > 0 && !s.models.find((m) => m.name === cfg.ollama_model)) {
          update({ ollama_model: s.models[0].name });
        }
      }).catch(() => setOllamaOk(false));
    }, [cfg.mode]);
    const Toggle = ({ field, label, sub }) => /* @__PURE__ */ React.createElement("div", { className: "set-row" }, /* @__PURE__ */ React.createElement("div", { style: { flex: 1 } }, /* @__PURE__ */ React.createElement("div", { className: "set-label", style: { marginBottom: 2 } }, label), sub && /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, sub)), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "set-toggle" + (cfg[field] ? " on" : ""),
        onClick: () => update({ [field]: !cfg[field] })
      }
    ));
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Settings"), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), saving && /* @__PURE__ */ React.createElement("div", { style: { fontSize: 12, color: "var(--accent-h)", marginRight: 12, display: "flex", alignItems: "center", gap: 6 } }, /* @__PURE__ */ React.createElement("span", { className: "spin" }), " Saving\u2026")), /* @__PURE__ */ React.createElement("div", { className: "page-body solo", style: { paddingTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "settings-grid" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "cpu", size: 14 }), " LLM Provider"), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Model mode"), /* @__PURE__ */ React.createElement("select", { className: "set-select", value: cfg.mode, onChange: (e) => update({ mode: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "anthropic" }, "Anthropic Claude (High quality)"), /* @__PURE__ */ React.createElement("option", { value: "ollama" }, "Local Ollama (Free/Private)"), /* @__PURE__ */ React.createElement("option", { value: "demo" }, "Demo mode (Offline/Template)"))), cfg.mode === "anthropic" && /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Anthropic API Key"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        type: "password",
        placeholder: "sk-ant-\u2026",
        value: cfg.api_key || "",
        onChange: (e) => update({ api_key: e.target.value })
      }
    )), cfg.mode === "ollama" && /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Ollama Model"), ollamaOk === false && /* @__PURE__ */ React.createElement("div", { className: "set-helper", style: { color: "#f87171", marginBottom: 8 } }, "Ollama not reachable \u2014 run: ", /* @__PURE__ */ React.createElement("code", null, "ollama serve")), ollamaModels.length > 0 ? /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement(
      "select",
      {
        className: "set-select",
        value: cfg.ollama_model || "",
        onChange: (e) => update({ ollama_model: e.target.value })
      },
      ollamaModels.map((m) => /* @__PURE__ */ React.createElement("option", { key: m.name, value: m.name }, m.name))
    ), ollamaModels.find((m) => m.name === cfg.ollama_model) && (() => {
      const m = ollamaModels.find((x) => x.name === cfg.ollama_model);
      return /* @__PURE__ */ React.createElement("div", { className: "set-helper", style: { marginTop: 6 } }, m.params, " \xB7 ", m.family, " \xB7 ", m.size_gb, " GB");
    })()) : ollamaOk && /* @__PURE__ */ React.createElement("div", { className: "set-helper", style: { color: "#fbbf24" } }, "No models pulled \u2014 run: ", /* @__PURE__ */ React.createElement("code", null, "ollama pull llama3.2")), !ollamaModels.length && /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        value: cfg.ollama_model || "",
        onChange: (e) => update({ ollama_model: e.target.value }),
        placeholder: "e.g. llama3.2",
        style: { marginTop: ollamaOk ? 8 : 0 }
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-row" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "LLM score limit"), /* @__PURE__ */ React.createElement("span", { className: "set-range-val" }, cfg.llm_score_limit)), /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "range",
        className: "set-range",
        min: "1",
        max: "50",
        value: cfg.llm_score_limit || 10,
        onChange: (e) => update({ llm_score_limit: parseInt(e.target.value) })
      }
    ), /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, "Only top N jobs from fast-score will use LLM (saves time/cost)."))), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "search", size: 14 }), " Discovery & Search"), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Target job titles (comma-sep)"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        placeholder: "e.g. Software Engineer, Frontend",
        value: cfg.job_titles || "",
        onChange: (e) => update({ job_titles: e.target.value })
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Target location"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        placeholder: "e.g. San Francisco, Remote",
        value: cfg.location || "",
        onChange: (e) => update({ location: e.target.value })
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-row" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Max jobs to scrape"), /* @__PURE__ */ React.createElement("span", { className: "set-range-val" }, cfg.max_scrape_jobs)), /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "range",
        className: "set-range",
        min: "1",
        max: "100",
        value: cfg.max_scrape_jobs || 20,
        onChange: (e) => update({ max_scrape_jobs: parseInt(e.target.value) })
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Posting age (days)"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        type: "number",
        value: cfg.days_old || 30,
        onChange: (e) => update({ days_old: parseInt(e.target.value) })
      }
    ))), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "bar-chart", size: 14 }), " Scoring Thresholds"), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-row" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Auto-apply threshold"), /* @__PURE__ */ React.createElement("span", { className: "set-range-val" }, cfg.threshold)), /* @__PURE__ */ React.createElement(
      "input",
      {
        type: "range",
        className: "set-range",
        min: "50",
        max: "100",
        value: cfg.threshold || 75,
        onChange: (e) => update({ threshold: parseInt(e.target.value) })
      }
    ), /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, "Jobs scoring above this will be marked for auto-submission.")), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Max applications per run"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        type: "number",
        min: "1",
        max: "50",
        value: cfg.max_apps || 10,
        onChange: (e) => update({ max_apps: parseInt(e.target.value) })
      }
    )), /* @__PURE__ */ React.createElement(Toggle, { field: "cover_letter", label: "Generate cover letters", sub: "Create a tailored cover letter for each role." })), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "filter", size: 14 }), " Eligibility Filters"), /* @__PURE__ */ React.createElement(Toggle, { field: "use_simplify", label: "Use SimplifyJobs scraper", sub: "High quality results, but can be slower." }), /* @__PURE__ */ React.createElement(Toggle, { field: "include_unknown_education", label: "Include unknown education", sub: "Don't skip if JD doesn't specify degree." }), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Experience levels"), /* @__PURE__ */ React.createElement("div", { className: "skill-grid", style: { marginTop: 4 } }, ["internship", "entry-level", "mid-level", "senior"].map((lvl) => {
      const active = (cfg.experience_levels || []).includes(lvl);
      return /* @__PURE__ */ React.createElement(
        "span",
        {
          key: lvl,
          className: "skill-pill" + (active ? " hard" : ""),
          style: { cursor: "pointer" },
          onClick: () => {
            const next = active ? cfg.experience_levels.filter((x) => x !== lvl) : [...cfg.experience_levels || [], lvl];
            update({ experience_levels: next });
          }
        },
        lvl
      );
    }))), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Education requirements"), /* @__PURE__ */ React.createElement("div", { className: "skill-grid", style: { marginTop: 4 } }, ["bachelors", "masters", "phd"].map((edu) => {
      const active = (cfg.education_filter || []).includes(edu);
      return /* @__PURE__ */ React.createElement(
        "span",
        {
          key: edu,
          className: "skill-pill" + (active ? " hard" : ""),
          style: { cursor: "pointer" },
          onClick: () => {
            const next = active ? cfg.education_filter.filter((x) => x !== edu) : [...cfg.education_filter || [], edu];
            update({ education_filter: next });
          }
        },
        edu
      );
    }))), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Citizenship filter"), /* @__PURE__ */ React.createElement("select", { className: "set-select", value: cfg.citizenship_filter, onChange: (e) => update({ citizenship_filter: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "none" }, "No filter"), /* @__PURE__ */ React.createElement("option", { value: "exclude_required" }, "Exclude roles requiring sponsorship"), /* @__PURE__ */ React.createElement("option", { value: "clearance_only" }, "Security clearance roles only")))), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "list-x", size: 14 }), " Lists & Exclusions"), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Company Blacklist (comma-sep)"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        placeholder: "Google, Meta",
        value: cfg.blacklist || "",
        onChange: (e) => update({ blacklist: e.target.value })
      }
    )), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Company Whitelist (comma-sep)"), /* @__PURE__ */ React.createElement(
      "input",
      {
        className: "set-input",
        placeholder: "NVIDIA, Apple",
        value: cfg.whitelist || "",
        onChange: (e) => update({ whitelist: e.target.value })
      }
    ), /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, "Whitelisted companies are always surfaced regardless of score."))), /* @__PURE__ */ React.createElement("div", { className: "set-sec" }, /* @__PURE__ */ React.createElement("div", { className: "set-sec-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "cpu", size: 14 }), " Advanced"), /* @__PURE__ */ React.createElement(Toggle, { field: "quick_score_only", label: "Quick score only", sub: "Skip LLM rubric scoring (faster, less accurate)." }), /* @__PURE__ */ React.createElement(Toggle, { field: "force_dev_mode", label: "Force Developer Mode", sub: "Show Dev Ops tools regardless of connection origin." })))));
  }
  function FeedbackPage({ refresh }) {
    const [message, setMessage] = useState("");
    const [submitting, setSubmitting] = useState(false);
    const [success, setSuccess] = useState(false);
    const handleSubmit = async (e) => {
      e.preventDefault();
      if (!message.trim()) return;
      setSubmitting(true);
      try {
        await api.post("/api/feedback", { message });
        setSuccess(true);
        setMessage("");
        refresh?.();
      } catch (e2) {
        alert(e2.message || "Failed to submit feedback");
      } finally {
        setSubmitting(false);
      }
    };
    if (success) {
      return /* @__PURE__ */ React.createElement("div", { className: "placeholder-page" }, /* @__PURE__ */ React.createElement("div", { className: "placeholder-icon", style: { background: "var(--good-d)", border: "1px solid var(--good-b)" } }, /* @__PURE__ */ React.createElement(Icon, { name: "check", size: 22, color: "var(--good)" })), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 18, fontWeight: 600 } }, "Thank You"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--t2)", maxWidth: 400, textAlign: "center", lineHeight: 1.55, marginTop: 8 } }, "Your feedback has been sent directly to the development team. We read every message and use it to improve Atlas."), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", style: { marginTop: 24 }, onClick: () => setSuccess(false) }, "Send another message"));
    }
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Feedback")), /* @__PURE__ */ React.createElement("div", { className: "page-body solo", style: { paddingTop: 14 } }, /* @__PURE__ */ React.createElement("div", { className: "col-main" }, /* @__PURE__ */ React.createElement("div", { className: "data-card", style: { padding: 32, maxWidth: 600, margin: "0 auto", width: "100%" } }, /* @__PURE__ */ React.createElement("div", { style: { textAlign: "center", marginBottom: 32 } }, /* @__PURE__ */ React.createElement("div", { style: { width: 56, height: 56, borderRadius: 14, background: "var(--accent-d)", border: "1px solid var(--accent-b)", display: "flex", alignItems: "center", justifyContent: "center", margin: "0 auto 16px" } }, /* @__PURE__ */ React.createElement(Icon, { name: "message-square", size: 24, color: "var(--accent-h)" })), /* @__PURE__ */ React.createElement("h2", { style: { fontSize: 20, fontWeight: 700, color: "var(--t1)" } }, "Tell us what you think"), /* @__PURE__ */ React.createElement("p", { style: { fontSize: 14, color: "var(--t2)", marginTop: 8, lineHeight: 1.6 } }, "Have a feature request, found a bug, or just want to share your experience? We want to hear from you.")), /* @__PURE__ */ React.createElement("form", { onSubmit: handleSubmit }, /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement(
      "textarea",
      {
        className: "profile-input profile-textarea",
        style: { minHeight: 150, padding: 16 },
        placeholder: "Your message...",
        value: message,
        onChange: (e) => setMessage(e.target.value),
        required: true
      }
    )), /* @__PURE__ */ React.createElement(
      "button",
      {
        type: "submit",
        className: "lp-btn-p",
        style: { width: "100%", marginTop: 16, padding: "14px" },
        disabled: submitting || !message.trim()
      },
      submitting ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : /* @__PURE__ */ React.createElement(Icon, { name: "send", size: 15 }),
      submitting ? "Sending..." : "Send Feedback"
    ))))));
  }
  function DevPage({ state: globalState, refresh: globalRefresh }) {
    const [data, setData] = useState(null);
    const [error, setError] = useState(null);
    const [selected, setSelected] = useState(null);
    const [fullState, setFullState] = useState(null);
    const [cli, setCli] = useState({ command: "git_status", output: "", running: false });
    const [tweaks, setTweaks] = useState(null);
    const [loadingFull, setLoadingFull] = useState(false);
    const [refreshing, setRefreshing] = useState(false);
    const refresh = useCallback(async () => {
      setRefreshing(true);
      try {
        const [next] = await Promise.all([
          api.get("/api/dev/overview"),
          globalRefresh?.()
        ]);
        if (next.detail === "Developer access denied" || next.error === "Developer access denied") {
          setError(403);
        } else {
          setData(next);
          setTweaks(next.status?.tweaks || {});
          setError(null);
        }
      } catch (e) {
        console.error("Dev refresh failed:", e);
        setError(500);
      } finally {
        setTimeout(() => setRefreshing(false), 400);
      }
    }, [globalRefresh]);
    useEffect(() => {
      refresh();
      const id = setInterval(refresh, 1e4);
      return () => clearInterval(id);
    }, [refresh]);
    useEffect(() => {
      if (selected) {
        setLoadingFull(true);
        api.get(`/api/dev/session/${selected.id}`).then((res) => setFullState(res)).finally(() => setLoadingFull(false));
      } else {
        setFullState(null);
      }
    }, [selected]);
    const enableDev = async () => {
      await api.post("/api/config", { force_dev_mode: true });
      globalRefresh();
      refresh();
    };
    const impersonate = async (id) => {
      await api.post(`/api/dev/session/${id}/impersonate`, {});
      window.location.href = "/app";
    };
    const stopImpersonating = async () => {
      await api.post("/api/dev/session/stop-impersonating", {});
      window.location.href = "/app#dev";
      window.location.reload();
    };
    const runCli = async (command) => {
      setCli({ command, output: "Running...", running: true });
      try {
        const res = await api.post("/api/dev/cli", { command });
        setCli({ command, output: res.output, running: false });
        refresh();
      } catch (e) {
        setCli({ command, output: "Command failed.", running: false });
      }
    };
    const saveTweaks = async (patch) => {
      const next = { ...tweaks || {}, ...patch };
      setTweaks(next);
      const res = await api.post("/api/dev/tweaks", next);
      setTweaks(res.tweaks);
      applyDevTweaks(res.tweaks);
      refresh();
    };
    const summary = data?.summary || {};
    const status = data?.status || {};
    const sessions = data?.sessions || [];
    const active = selected || sessions[0];
    const commands = [
      ["git_status", "Git"],
      ["recent_outputs", "Outputs"],
      ["session_db", "DB"],
      ["pip_freeze", "Deps"]
    ];
    const accents = ["#5e6ad2", "#0ea5e9", "#14b8a6", "#f97316", "#e11d48"];
    if (error === 403) return /* @__PURE__ */ React.createElement("div", { className: "placeholder-page" }, /* @__PURE__ */ React.createElement("div", { className: "placeholder-icon", style: { background: "var(--warn-d)", border: "1px solid var(--warn-b)" } }, /* @__PURE__ */ React.createElement(Icon, { name: "lock", size: 22, color: "var(--warn)" })), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 18, fontWeight: 600 } }, "Developer Access Required"), /* @__PURE__ */ React.createElement("div", { style: { fontSize: 13, color: "var(--t2)", maxWidth: 400, textAlign: "center", lineHeight: 1.55, marginTop: 8 } }, "This page contains diagnostic tools and session data. Access is restricted to local connections or authorized developer sessions."), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", style: { marginTop: 24 }, onClick: enableDev }, "Authorize this session"));
    if (!data) return /* @__PURE__ */ React.createElement("div", { className: "placeholder-page" }, /* @__PURE__ */ React.createElement("span", { className: "spin" }), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t2)" } }, "Loading dev console..."));
    return /* @__PURE__ */ React.createElement(React.Fragment, null, /* @__PURE__ */ React.createElement("div", { className: "page-head" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "page-title" }, "Operations"), /* @__PURE__ */ React.createElement("div", { className: "page-title-big" }, "Dev Overview")), /* @__PURE__ */ React.createElement("div", { className: "head-spacer" }), document.cookie.includes("dev_impersonate_id") && /* @__PURE__ */ React.createElement("button", { className: "btn-primary", style: { marginRight: 12, background: "var(--warn)", color: "#000" }, onClick: stopImpersonating }, /* @__PURE__ */ React.createElement(Icon, { name: "user-minus", size: 14 }), " Stop Impersonating"), /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", onClick: refresh, disabled: refreshing }, refreshing ? /* @__PURE__ */ React.createElement("span", { className: "spin", style: { marginRight: 6, width: 13, height: 13 } }) : /* @__PURE__ */ React.createElement(Icon, { name: "refresh-cw", size: 13 }), refreshing ? "Refreshing..." : "Refresh")), /* @__PURE__ */ React.createElement("div", { className: "dev-wrap" }, /* @__PURE__ */ React.createElement("div", { className: "dev-grid" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card dev-span" }, /* @__PURE__ */ React.createElement("div", { className: "dev-kpis" }, /* @__PURE__ */ React.createElement(DevKpi, { label: "Users", value: summary.users || 0, icon: "users" }), /* @__PURE__ */ React.createElement(DevKpi, { label: "Resumes", value: summary.with_resume || 0, icon: "file-check-2" }), /* @__PURE__ */ React.createElement(DevKpi, { label: "Applications", value: summary.applications || 0, icon: "send" }), /* @__PURE__ */ React.createElement(DevKpi, { label: "Applied", value: summary.applied || 0, icon: "check-circle-2" }), /* @__PURE__ */ React.createElement(DevKpi, { label: "Manual", value: summary.manual || 0, icon: "hand" }), /* @__PURE__ */ React.createElement(DevKpi, { label: "Errors", value: summary.errors || 0, icon: "alert-triangle", warn: summary.errors > 0 }))), /* @__PURE__ */ React.createElement("div", { className: "dev-card" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "activity", size: 14 }), " Site Status"), /* @__PURE__ */ React.createElement("div", { className: "dev-status" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "App"), /* @__PURE__ */ React.createElement("b", { className: "ok" }, status.app)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Python"), /* @__PURE__ */ React.createElement("b", null, status.python)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Output files"), /* @__PURE__ */ React.createElement("b", null, status.output_files)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Session files"), /* @__PURE__ */ React.createElement("b", null, status.session_files)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "DB size"), /* @__PURE__ */ React.createElement("b", null, status.session_db_mb, " MB")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Free disk"), /* @__PURE__ */ React.createElement("b", null, status.disk_free_gb, " GB")))), /* @__PURE__ */ React.createElement("div", { className: "dev-card" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "wand-sparkles", size: 14 }), " Site Tweaks"), /* @__PURE__ */ React.createElement("div", { className: "dev-tweak-row" }, accents.map((color) => /* @__PURE__ */ React.createElement("button", { key: color, className: "dev-swatch", style: { background: color }, onClick: () => saveTweaks({ accent: color }), title: color }))), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Density"), /* @__PURE__ */ React.createElement("select", { className: "set-select", value: tweaks?.density || "comfortable", onChange: (e) => saveTweaks({ density: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "compact" }, "Compact"), /* @__PURE__ */ React.createElement("option", { value: "comfortable" }, "Comfortable"), /* @__PURE__ */ React.createElement("option", { value: "spacious" }, "Spacious"))), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Experiment mode"), /* @__PURE__ */ React.createElement("select", { className: "set-select", value: tweaks?.experiment || "standard", onChange: (e) => saveTweaks({ experiment: e.target.value }) }, /* @__PURE__ */ React.createElement("option", { value: "standard" }, "Standard"), /* @__PURE__ */ React.createElement("option", { value: "focus" }, "Focus"), /* @__PURE__ */ React.createElement("option", { value: "command" }, "Command"), /* @__PURE__ */ React.createElement("option", { value: "launch" }, "Launch"))), /* @__PURE__ */ React.createElement("div", { className: "set-row" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("div", { className: "set-label" }, "Top banner"), /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, "Use the dev banner as the site-wide strip.")), /* @__PURE__ */ React.createElement("button", { className: "set-toggle" + (tweaks?.show_promo !== false ? " on" : ""), onClick: () => saveTweaks({ show_promo: tweaks?.show_promo === false }) })), /* @__PURE__ */ React.createElement("input", { className: "set-input", value: tweaks?.dev_banner || "", onChange: (e) => setTweaks({ ...tweaks || {}, dev_banner: e.target.value }), onBlur: (e) => saveTweaks({ dev_banner: e.target.value }), placeholder: "Dev banner" })), /* @__PURE__ */ React.createElement("div", { className: "dev-card dev-users" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "users", size: 14 }), " Users"), /* @__PURE__ */ React.createElement("div", { className: "dev-user-list" }, sessions.map((s) => /* @__PURE__ */ React.createElement("button", { key: s.id, className: "dev-user" + (active?.id === s.id ? " active" : ""), onClick: () => setSelected(s) }, /* @__PURE__ */ React.createElement("span", { className: "user-avatar" }, (s.name || "U")[0]), /* @__PURE__ */ React.createElement("span", null, /* @__PURE__ */ React.createElement("b", null, s.name || "Anonymous"), /* @__PURE__ */ React.createElement("small", null, s.email || s.resume_filename || s.id.slice(0, 10))), /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", alignItems: "flex-end", gap: 4 } }, /* @__PURE__ */ React.createElement("em", null, s.done.length, "/7"), s.unread_feedback_count > 0 && /* @__PURE__ */ React.createElement("span", { title: "Unread feedback message", style: { display: "flex", alignItems: "center", gap: 4, background: "var(--warn-d)", color: "var(--warn)", padding: "2px 6px", borderRadius: 10, fontSize: 10, fontWeight: 600 } }, /* @__PURE__ */ React.createElement(Icon, { name: "message-square", size: 10 }), " ", s.unread_feedback_count)))))), /* @__PURE__ */ React.createElement("div", { className: "dev-card dev-span" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h", style: { display: "flex", justifyContent: "space-between", alignItems: "center" } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", gap: 8 } }, /* @__PURE__ */ React.createElement(Icon, { name: "user-cog", size: 14 }), " Selected User Detail"), active && /* @__PURE__ */ React.createElement("div", { style: { display: "flex", gap: 8 } }, /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn-ghost",
        style: { fontSize: 11, padding: "4px 10px", color: "var(--warn)" },
        onClick: async () => {
          if (confirm("Reset this session state? Files will be deleted.")) {
            await api.post(`/api/dev/session/${active.id}/reset`, {});
            refresh();
            setSelected(null);
          }
        }
      },
      /* @__PURE__ */ React.createElement(Icon, { name: "rotate-ccw", size: 12 }),
      " Reset Session"
    ), /* @__PURE__ */ React.createElement(
      "button",
      {
        className: "btn-ghost",
        style: { fontSize: 11, padding: "4px 10px", color: "var(--bad)" },
        onClick: async () => {
          if (confirm("Delete this user entirely? This cannot be undone.")) {
            await fetch(`/api/dev/session/${active.id}`, { method: "DELETE" });
            refresh();
            setSelected(null);
          }
        }
      },
      /* @__PURE__ */ React.createElement(Icon, { name: "trash-2", size: 12 }),
      " Delete Session"
    ), /* @__PURE__ */ React.createElement("button", { className: "btn-primary", style: { fontSize: 11, padding: "4px 10px" }, onClick: () => impersonate(active.id) }, /* @__PURE__ */ React.createElement(Icon, { name: "user-plus", size: 12 }), " View site as ", active.name || "this user"))), active ? /* @__PURE__ */ React.createElement("div", { className: "dev-inspect-grid" }, /* @__PURE__ */ React.createElement("div", { className: "inspect-sec" }, /* @__PURE__ */ React.createElement("h4", null, "Stats & Pipeline"), /* @__PURE__ */ React.createElement("div", { className: "dev-status" }, /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "ID"), /* @__PURE__ */ React.createElement("code", { style: { fontSize: 10 } }, active.id)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Resume"), /* @__PURE__ */ React.createElement("b", null, active.has_resume ? "yes" : "no")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Target"), /* @__PURE__ */ React.createElement("b", null, active.target || "-")), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Jobs"), /* @__PURE__ */ React.createElement("b", null, active.job_count)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Scored"), /* @__PURE__ */ React.createElement("b", null, active.scored_count)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Apps"), /* @__PURE__ */ React.createElement("b", null, active.application_count)), /* @__PURE__ */ React.createElement("div", null, /* @__PURE__ */ React.createElement("span", null, "Applied"), /* @__PURE__ */ React.createElement("b", null, active.applied_count))), /* @__PURE__ */ React.createElement("div", { className: "dev-phases", style: { marginTop: 12 } }, [1, 2, 3, 4, 5, 6, 7].map((n) => /* @__PURE__ */ React.createElement("span", { key: n, className: active.done.includes(n) ? "on" : "" }, n)))), /* @__PURE__ */ React.createElement("div", { className: "inspect-sec", style: { gridRow: "span 2" } }, /* @__PURE__ */ React.createElement("h4", null, "User Feedback"), /* @__PURE__ */ React.createElement("div", { className: "dev-terminal", style: { height: "100%", fontSize: 12, background: "var(--bg-1)" } }, loadingFull ? "Loading..." : (fullState?.feedback || []).length > 0 ? /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", gap: 12 } }, fullState.feedback.map((f) => /* @__PURE__ */ React.createElement("div", { key: f.id, style: { background: "var(--bg-2)", border: "1px solid var(--bdr)", borderRadius: 8, padding: 12 } }, /* @__PURE__ */ React.createElement("div", { style: { display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8, color: "var(--t3)", fontSize: 11, fontFamily: "var(--mono)" } }, /* @__PURE__ */ React.createElement("span", null, new Date(f.created_at).toLocaleString()), !f.read && /* @__PURE__ */ React.createElement("span", { style: { background: "var(--warn)", color: "#000", padding: "2px 6px", borderRadius: 4, fontWeight: 700 } }, "NEW")), /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t1)", whiteSpace: "pre-wrap", lineHeight: 1.5 } }, f.message))), fullState.feedback.some((f) => !f.read) && /* @__PURE__ */ React.createElement("button", { className: "btn-ghost", style: { marginTop: 8 }, onClick: async () => {
      await api.post(`/api/dev/session/${active.id}/feedback/read`, {});
      api.get(`/api/dev/session/${active.id}`).then(setFullState);
      refresh();
    } }, /* @__PURE__ */ React.createElement(Icon, { name: "check-check", size: 13 }), " Mark all as read")) : /* @__PURE__ */ React.createElement("div", { style: { color: "var(--t3)" } }, "No feedback from this user."))), /* @__PURE__ */ React.createElement("div", { className: "inspect-sec" }, /* @__PURE__ */ React.createElement("h4", null, "Resume Text"), /* @__PURE__ */ React.createElement("div", { className: "dev-terminal", style: { height: 200, fontSize: 11 } }, loadingFull ? "Loading..." : fullState?.resume_text || "No resume uploaded.")), /* @__PURE__ */ React.createElement("div", { className: "inspect-sec" }, /* @__PURE__ */ React.createElement("h4", null, "Full State JSON"), /* @__PURE__ */ React.createElement("div", { className: "dev-terminal", style: { height: 200, fontSize: 11 } }, loadingFull ? "Loading..." : JSON.stringify(fullState, null, 2)))) : /* @__PURE__ */ React.createElement("div", { className: "set-helper" }, "No session selected.")), /* @__PURE__ */ React.createElement("div", { className: "dev-card dev-span" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "terminal", size: 14 }), " CLI Output"), /* @__PURE__ */ React.createElement("div", { className: "dev-cli-actions" }, commands.map(([id, label]) => /* @__PURE__ */ React.createElement("button", { key: id, className: "btn-ghost", disabled: cli.running, onClick: () => runCli(id) }, label))), /* @__PURE__ */ React.createElement("pre", { className: "dev-terminal" }, cli.output || "Run a safe command to inspect the app.")), /* @__PURE__ */ React.createElement("div", { className: "dev-card dev-span" }, /* @__PURE__ */ React.createElement("div", { className: "dev-card-h" }, /* @__PURE__ */ React.createElement(Icon, { name: "list-tree", size: 14 }), " Recent Events"), /* @__PURE__ */ React.createElement("div", { className: "dev-events" }, (data.events || []).slice(0, 80).map((e, i) => /* @__PURE__ */ React.createElement("div", { key: i, className: "dev-event" }, /* @__PURE__ */ React.createElement("span", null, new Date(e.ts).toLocaleTimeString()), /* @__PURE__ */ React.createElement("b", null, e.kind), /* @__PURE__ */ React.createElement("p", null, e.message))))))));
  }
  function DevKpi({ label, value, icon, warn }) {
    return /* @__PURE__ */ React.createElement("div", { className: "dev-kpi" + (warn ? " warn" : "") }, /* @__PURE__ */ React.createElement(Icon, { name: icon, size: 15 }), /* @__PURE__ */ React.createElement("span", null, label), /* @__PURE__ */ React.createElement("b", null, value));
  }
  function AuthPage({ onAuth }) {
    const [mode, setMode] = useState("login");
    const [email, setEmail] = useState("");
    const [password, setPassword] = useState("");
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(false);
    const handleSubmit = async (e) => {
      e.preventDefault();
      setError(null);
      setLoading(true);
      try {
        const res = await api.post(`/api/auth/${mode}`, { email, password });
        if (res.ok) {
          onAuth(res.user);
        } else {
          setError(res.error || "Authentication failed");
        }
      } catch (err) {
        setError(err.message || "An error occurred. Please try again.");
      } finally {
        setLoading(false);
      }
    };
    const handleGoogle = async () => {
      setError(null);
      try {
        const res = await api.get("/api/auth/google");
        if (res.url) {
          window.location.href = res.url;
        } else {
          throw new Error("No redirect URL received");
        }
      } catch (err) {
        console.error("Google Auth Error:", err);
        setError(err.message || "Could not initialize Google login");
      }
    };
    return /* @__PURE__ */ React.createElement("div", { className: "auth-page" }, /* @__PURE__ */ React.createElement("div", { className: "auth-card" }, /* @__PURE__ */ React.createElement(BrandMark, null), /* @__PURE__ */ React.createElement("h2", null, mode === "login" ? "Sign in to your account" : "Create your account"), /* @__PURE__ */ React.createElement("p", { className: "auth-sub" }, mode === "login" ? "Welcome back! Please enter your details." : "Start your automated job search today."), /* @__PURE__ */ React.createElement("button", { className: "auth-google", onClick: handleGoogle }, /* @__PURE__ */ React.createElement("img", { src: "https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg", alt: "Google", width: "18" }), "Continue with Google"), /* @__PURE__ */ React.createElement("div", { className: "auth-divider" }, /* @__PURE__ */ React.createElement("span", null, "OR")), /* @__PURE__ */ React.createElement("form", { className: "auth-form", onSubmit: handleSubmit }, /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("label", { className: "set-label" }, "Email address"), /* @__PURE__ */ React.createElement("input", { type: "email", value: email, onChange: (e) => setEmail(e.target.value), placeholder: "name@company.com", required: true })), /* @__PURE__ */ React.createElement("div", { className: "set-field" }, /* @__PURE__ */ React.createElement("label", { className: "set-label" }, "Password"), /* @__PURE__ */ React.createElement("input", { type: "password", value: password, onChange: (e) => setPassword(e.target.value), placeholder: "\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022", required: true })), error && /* @__PURE__ */ React.createElement("div", { className: "auth-error" }, error), /* @__PURE__ */ React.createElement("button", { className: "lp-btn-p", style: { width: "100%", marginTop: 12, padding: "14px" }, disabled: loading }, loading ? /* @__PURE__ */ React.createElement("span", { className: "spin" }) : mode === "login" ? "Sign in" : "Create account")), /* @__PURE__ */ React.createElement("div", { className: "auth-switch" }, mode === "login" ? "Don't have an account?" : "Already have an account?", " ", /* @__PURE__ */ React.createElement("button", { onClick: () => setMode(mode === "login" ? "signup" : "login") }, mode === "login" ? "Sign up" : "Sign in"))));
  }
  function App() {
    const [state, setState] = useState(null);
    const [page, setPage] = useState(() => location.hash === "#dev" ? "dev" : "home");
    const [showPromo, setShowPromo] = useState(true);
    const [booted, setBooted] = useState(false);
    const prefetchStarted = useRef(false);
    const refresh = useCallback(async () => {
      try {
        const next = await api.get("/api/state");
        setState(next);
        applyDevTweaks(next.dev_tweaks);
      } catch {
      } finally {
        setBooted(true);
      }
    }, []);
    useEffect(() => {
      refresh();
    }, [refresh]);
    useEffect(() => {
      const id = setInterval(refresh, 8e3);
      return () => clearInterval(id);
    }, [refresh]);
    useEffect(() => {
      if (!state?.has_resume || state?.scored_summary || prefetchStarted.current || page === "jobs") return;
      prefetchStarted.current = true;
      const id = setTimeout(async () => {
        try {
          const done = new Set(state?.done || []);
          if (!state?.profile) {
            await runPhasePromise(1, { rerun: done.has(1) });
            await refresh();
          }
          if (!done.has(2) || !state?.job_count) {
            await runPhasePromise(2, { rerun: false });
            await refresh();
          }
          if (!done.has(3) || !state?.scored_summary) {
            await runPhasePromise(3, { rerun: false, params: { fast: 1 } });
            await refresh();
          }
        } catch {
          await refresh();
        }
      }, 1200);
      return () => clearTimeout(id);
    }, [state, page, refresh]);
    if (!booted) return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", alignItems: "center", justifyContent: "center", height: "100vh", color: "var(--t3)", fontSize: 13 } }, /* @__PURE__ */ React.createElement("span", { className: "spin", style: { marginRight: 8 } }), " Loading workspace\u2026");
    if (!state?.user && page !== "home" && page !== "dev" && !state?.is_dev) {
      return /* @__PURE__ */ React.createElement(AuthPage, { onAuth: refresh });
    }
    if (!state?.has_resume && page !== "dev") {
      return /* @__PURE__ */ React.createElement("div", { style: { display: "flex", flexDirection: "column", height: "100vh", background: "var(--bg)" } }, /* @__PURE__ */ React.createElement(Onboarding, { onLoaded: refresh }), state?.is_dev && /* @__PURE__ */ React.createElement("button", { className: "dev-float", onClick: () => setPage("dev"), title: "Dev overview" }, /* @__PURE__ */ React.createElement(Icon, { name: "square-terminal", size: 15 }), " Dev"));
    }
    const counts = {
      jobs: state?.scored_summary?.total || null,
      applied: (state?.applications || []).length || null
    };
    const pageEl = (() => {
      switch (page) {
        case "home":
          return /* @__PURE__ */ React.createElement(HomePage, { state, setPage });
        case "jobs":
          return /* @__PURE__ */ React.createElement(JobsPage, { state, refresh, setPage });
        case "resume":
          return /* @__PURE__ */ React.createElement(ResumePage, { state, refresh, setPage });
        case "profile":
          return /* @__PURE__ */ React.createElement(ProfilePage, { state, refresh, setPage });
        case "agent":
          return /* @__PURE__ */ React.createElement(AgentPage, { state, refresh });
        case "dev":
          return /* @__PURE__ */ React.createElement(DevPage, { state, refresh });
        case "feedback":
          return /* @__PURE__ */ React.createElement(FeedbackPage, { refresh });
        case "settings":
          return /* @__PURE__ */ React.createElement(SettingsPage, { state, refresh });
        case "auth":
          return /* @__PURE__ */ React.createElement(AuthPage, { onAuth: () => {
            refresh();
            setPage("home");
          } });
        default:
          return /* @__PURE__ */ React.createElement(HomePage, { state, setPage });
      }
    })();
    const handleLogout = async () => {
      await api.post("/api/auth/logout", {});
      refresh();
      setPage("home");
    };
    return /* @__PURE__ */ React.createElement("div", { className: "shell" }, /* @__PURE__ */ React.createElement("div", { className: "brand-cell" }, /* @__PURE__ */ React.createElement(BrandMark, { onClick: () => setPage("home") })), showPromo && state?.dev_tweaks?.show_promo !== false ? /* @__PURE__ */ React.createElement(PromoStrip, { onClose: () => setShowPromo(false), text: state?.dev_tweaks?.dev_banner }) : /* @__PURE__ */ React.createElement("div", { style: { gridArea: "promo", background: "var(--bg-1)", borderBottom: "1px solid var(--bdr)" } }), /* @__PURE__ */ React.createElement(Rail, { page, setPage, counts, isDev: state?.is_dev, onLogout: handleLogout }), /* @__PURE__ */ React.createElement("main", { className: "main" }, pageEl));
  }
  ReactDOM.createRoot(document.getElementById("root")).render(/* @__PURE__ */ React.createElement(App, null));
})();
