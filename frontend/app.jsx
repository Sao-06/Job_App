/* JobsAI — app.jsx
   Multi-page dark SPA. Purple/indigo brand colors.
   All API contracts preserved: /api/state, /api/phase/*, /api/resume/*, /api/config, /api/reset
*/
const { useState, useEffect, useRef, useCallback, useMemo } = React;

/* ── API ── */
const api = {
  get:  url => fetch(url).then(async r => {
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || data.error || data.message || 'API Error');
    return data;
  }),
  post: (url, body) => fetch(url, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  }).then(async r => {
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || data.error || data.message || 'API Error');
    return data;
  }),
  upload: (url, file) => {
    const fd = new FormData(); fd.append('file', file);
    return fetch(url, { method:'POST', body:fd }).then(async r => {
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || data.error || data.message || 'API Error');
      return data;
    });
  },
  delete: url => fetch(url, { method:'DELETE' }).then(async r => {
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || data.error || data.message || 'API Error');
    return data;
  }),
};

function applyDevTweaks(tweaks = {}) {
  const root = document.documentElement;
  if (tweaks.accent) {
    root.style.setProperty('--accent', tweaks.accent);
    root.style.setProperty('--accent-h', tweaks.accent);
    root.style.setProperty('--accent-d', `${tweaks.accent}22`);
    root.style.setProperty('--accent-b', `${tweaks.accent}55`);
  }
  root.dataset.density = tweaks.density || 'comfortable';
  root.dataset.experiment = tweaks.experiment || 'standard';
}

function runPhaseSSE(n, { onStart, onLog, onDone, onError, rerun = false, params = {} }) {
  const qs = new URLSearchParams(params);
  const url = `/api/phase/${n}/${rerun ? 'rerun' : 'run'}${qs.toString() ? `?${qs}` : ''}`;
  const es = new EventSource(url);
  es.onmessage = e => {
    let m; try { m = JSON.parse(e.data); } catch (err) { return; }
    if (m.type === 'start') onStart && onStart(m);
    if (m.type === 'log')   onLog && onLog(m);
    if (m.type === 'done')  { onDone && onDone(m); es.close(); }
    if (m.type === 'error') { onError && onError(m); es.close(); }
  };
  es.onerror = () => { onError && onError({ message:'Connection lost' }); es.close(); };
  return es;
}

function runPhasePromise(n, { rerun = false, onStart, params = {} } = {}) {
  return new Promise((resolve, reject) => {
    runPhaseSSE(n, {
      rerun,
      params,
      onStart,
      onDone: resolve,
      onError: reject,
    });
  });
}

/* ── Icon helper ── */
function Icon({ name, size=16, color='currentColor', style={} }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !window.lucide) return;
    ref.current.innerHTML = '';
    const el = document.createElement('i');
    el.setAttribute('data-lucide', name);
    el.style.width = size + 'px'; el.style.height = size + 'px'; el.style.color = color;
    ref.current.appendChild(el);
    window.lucide.createIcons({ nodes:[el] });
  }, [name, size, color]);
  return <span ref={ref} className="ic" style={{ width:size, height:size, ...style }}/>;
}

/* ── Brand glyph (original design) ── */
function BrandMark({ onClick }) {
  return (
    <div className="brand-mark" onClick={onClick}>
      <div className="brand-glyph">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
          <path d="M4 6h16M4 12h11M4 18h7" stroke="#fff" strokeWidth="2.5" strokeLinecap="round"/>
          <circle cx="20" cy="18" r="2.5" stroke="#fff" strokeWidth="2" fill="none"/>
        </svg>
      </div>
      <div className="brand-name">jobs<em>ai</em></div>
    </div>
  );
}

/* ── Promo strip ── */
function PromoStrip({ onClose, text }) {
  const [t, setT] = useState({ m:49, s:7 });
  useEffect(() => {
    const id = setInterval(() => setT(p => {
      let s = p.s - 1, m = p.m;
      if (s < 0) { s = 59; m = Math.max(0, m - 1); }
      return { m, s };
    }), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="promo-cell">
      <span>{text || <div>Unlock unlimited applies — offer ends in <strong>{String(t.m).padStart(2,'0')}m {String(t.s).padStart(2,'0')}s</strong></div>}</span>
      <button className="promo-close" onClick={onClose}><Icon name="x" size={13}/></button>
    </div>
  );
}

/* ── Rail nav ── */
const NAV = [
  { id:'home',      label:'Home',      icon:'home' },
  { id:'jobs',      label:'Jobs',      icon:'briefcase' },
  { id:'resume',    label:'Resume',    icon:'file-text' },
  { id:'profile',   label:'Profile',   icon:'user-round' },
  { id:'agent',     label:'Agent',     icon:'sparkles' },
  { id:'dev',       label:'Dev Ops',    icon:'square-terminal' },
];
const NAV_UTIL = [
  { id:'feedback', label:'Feedback', icon:'circle-help' },
  { id:'settings', label:'Settings', icon:'settings' },
  { id:'logout',   label:'Sign out', icon:'log-out' },
];

function Rail({ page, setPage, counts, isDev, onLogout }) {
  return (
    <aside className="rail">
      <nav className="rail-nav">
        {NAV.filter(it => it.id !== 'dev' || isDev).map(it => (
          <div key={it.id}
               className={'rail-item' + (page === it.id ? ' active' : '')}
               onClick={() => setPage(it.id)}>
            <span className="rail-icon">
              <Icon name={it.icon} size={15}/>
            </span>
            <span className="lbl">{it.label}</span>
            {it.badge && <span className="rail-badge">{it.badge}</span>}
            {!it.badge && counts?.[it.id] != null && <span className="rail-count">{counts[it.id]}</span>}
          </div>
        ))}
      </nav>
      <div className="rail-bottom">
        {NAV_UTIL.map(it => (
          <div key={it.id}
               className={'rail-item' + (page === it.id ? ' active' : '')}
               onClick={() => it.id === 'logout' ? onLogout() : setPage(it.id)}>
            <span className="rail-icon"><Icon name={it.icon} size={15}/></span>
            <span className="lbl">{it.label}</span>
          </div>
        ))}
      </div>
    </aside>
  );
}

/* ══════════════════════════════════════════════════════════
   LANDING / HOME PAGE — full marketing page embedded in shell
══════════════════════════════════════════════════════════ */

/* Scroll-reveal hook */
function useLPReveal() {
  const ref = useRef(null);
  useEffect(() => {
    const el = ref.current; if (!el) return;
    const obs = new IntersectionObserver(([e]) => { if (e.isIntersecting) el.classList.add('vis'); }, { threshold: 0.1 });
    obs.observe(el);
    return () => obs.disconnect();
  }, []);
  return ref;
}
function LPReveal({ children, style = {}, className = '' }) {
  const ref = useLPReveal();
  return <div ref={ref} className={'lp-sr ' + className} style={style}>{children}</div>;
}

function Dashboard({ state, setPage }) {
  const jobs = state?.scored_summary?.jobs || [];
  const applied = state?.applications?.length || 0;
  const matches = jobs.filter(j => j.score >= 85).length;
  const done = new Set(state?.done || []);
  const phasePct = Math.round((done.size / 7) * 100);

  return (
    <div className="page-body solo">
      <div className="dash-hero">
        <div className="dash-hero-info">
          <h1 className="dash-hero-h">Welcome back, {state?.profile?.name?.split(' ')[0] || 'Explorer'}.</h1>
          <p className="dash-hero-p">You've reached <strong>{phasePct}%</strong> of your pipeline. {matches} high-confidence roles are waiting for your review.</p>
          <button className="lp-btn-p" onClick={() => setPage('jobs')}>Review matches</button>
        </div>
      </div>
      
      <div className="dash-grid">
        <div className="dash-card">
          <div className="dash-h"><Icon name="target" size={14}/> High matches</div>
          <div className="dash-n">{matches}</div>
          <div className="dash-sub">Score &gt; 85</div>
        </div>
        <div className="dash-card">
          <div className="dash-h"><Icon name="send" size={14}/> Applications</div>
          <div className="dash-n">{applied}</div>
          <div className="dash-sub">Successfully submitted</div>
        </div>
        <div className="dash-card">
          <div className="dash-h"><Icon name="activity" size={14}/> Pipeline</div>
          <div className="dash-n">{done.size}/7</div>
          <div className="dash-sub">Phases completed</div>
        </div>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   ONBOARDING
══════════════════════════════════════════════════════════ */
function Onboarding({ onLoaded, isDev, setPage }) {
  const [tab, setTab]       = useState('paste');
  const [text, setText]     = useState('');
  const [loading, setLoading] = useState(false);
  const [drag, setDrag]     = useState(false);
  const fileRef             = useRef(null);

  const handleFile = async file => {
    if (!file) return;
    setLoading(true);
    try { 
      await api.upload('/api/resume/upload', file); 
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
      const blob = new Blob([text], { type:'text/plain' });
      const file = new File([blob], 'pasted_resume.txt', { type:'text/plain' });
      await api.upload('/api/resume/upload', file);
      onLoaded?.();
    } catch (e) {
      alert(e.message);
    } finally { setLoading(false); }
  };

  const handleDemo = async () => {
    setLoading(true);
    try { await api.post('/api/resume/demo', {}); onLoaded?.(); }
    finally { setLoading(false); }
  };

  return (
    <div className="onboard-wrap">
      <div style={{ marginBottom:28, marginTop:16 }}><BrandMark/></div>
      <div className="ob-card fade-in">
        <div className="ob-eyebrow">Welcome to JobsAI</div>
        <h1 className="ob-h1">Your resume is your<br/><em>starting line.</em></h1>
        <p className="ob-sub">Drop it in and we'll find matching roles, score every opening against your profile, and handle applications automatically.</p>

        <div className="ob-tab-row">
          <button className={'ob-tab' + (tab==='paste' ? ' active' : '')} onClick={() => setTab('paste')}>Paste text</button>
          <button className={'ob-tab' + (tab==='upload' ? ' active' : '')} onClick={() => setTab('upload')}>Upload file</button>
        </div>

        {tab === 'paste' && (
          <textarea className="ob-area"
            placeholder="Paste your resume here…"
            value={text} onChange={e => setText(e.target.value)}/>
        )}

        {tab === 'upload' && (
          <div className={'ob-drop' + (drag ? ' drag' : '')}
            onDragOver={e => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={e => { e.preventDefault(); setDrag(false); handleFile(e.dataTransfer.files?.[0]); }}
            onClick={() => fileRef.current?.click()}>
            <Icon name="upload-cloud" size={28} color="var(--t3)"/>
            <div style={{ marginTop:8, fontSize:13.5, color:'var(--t1)', fontWeight:500 }}>
              Drop your file or click to browse
            </div>
            <div style={{ marginTop:4, fontSize:12, color:'var(--t3)' }}>PDF · DOCX · TXT</div>
            <input ref={fileRef} type="file" accept=".pdf,.docx,.txt" style={{ display:'none' }}
              onChange={e => handleFile(e.target.files?.[0])}/>
          </div>
        )}

        <button className="ob-cta"
          disabled={loading || (tab==='paste' && !text.trim())}
          onClick={tab==='paste' ? handlePaste : () => fileRef.current?.click()}>
          {loading ? <span className="spin"/> : <Icon name="arrow-right" size={15} color="#fff"/>}
          {loading ? 'Processing…' : 'Continue'}
        </button>
        <button className="ob-demo" onClick={handleDemo}>Try with a sample resume →</button>
        {isDev && <button className="dev-float" onClick={() => setPage('dev')} title="Dev overview"><Icon name="square-terminal" size={15}/> Dev</button>}
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   JOBS PAGE — original card design (not copied from Jobright)
══════════════════════════════════════════════════════════ */
const LOGO_VARIANTS = ['v1','v2','v3','v4','v5'];
const POSTED_LABELS = ['2 days ago','1 week ago','3 days ago','Just posted','5 days ago','Reposted today'];
const WORK_MODELS   = ['Onsite','Hybrid','Remote'];
const EXP_LEVELS    = ['Internship','Entry-level','Mid-level','Senior'];

function ScoreRing({ score }) {
  const pct  = Math.max(0, Math.min(100, Math.round(score)));
  const C    = 26, circ = 2 * Math.PI * C;
  const off  = circ - (circ * pct / 100);
  const tone = pct >= 85 ? 'score-high' : pct >= 65 ? 'score-mid' : 'score-low';
  const color = pct >= 85 ? 'var(--good)' : pct >= 65 ? 'var(--accent-h)' : 'var(--t3)';
  const label = pct >= 85 ? 'Strong' : pct >= 65 ? 'Good' : pct >= 50 ? 'Fair' : 'Reach';
  return (
    <div className={'job-score-col ' + tone}>
      <div className="score-ring">
        <svg width="56" height="56" viewBox="0 0 56 56">
          <circle cx="28" cy="28" r={C} fill="none" strokeWidth="4" stroke="rgba(255,255,255,.07)"/>
          <circle cx="28" cy="28" r={C} fill="none" strokeWidth="4" stroke={color}
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={off}
            style={{ transition:'stroke-dashoffset .8s cubic-bezier(.16,1,.3,1)' }}/>
        </svg>
        <div className="score-pct">{pct}</div>
      </div>
      <div className="score-label">{label}</div>
    </div>
  );
}

function JobCard({ job, idx, isLiked, onLike, onHide }) {
  const logo    = LOGO_VARIANTS[idx % LOGO_VARIANTS.length];
  const posted  = POSTED_LABELS[idx % POSTED_LABELS.length];
  const model   = WORK_MODELS[idx % WORK_MODELS.length];
  const exp     = EXP_LEVELS[idx % EXP_LEVELS.length];
  const pct     = Math.round(job.score || 0);
  const stripe  = pct >= 85 ? 'score-high' : pct >= 65 ? 'score-mid' : 'score-low';
  const initial = (job.co || '?').trim().charAt(0).toUpperCase();
  const tags    = (job.skills || '').split(',').map(s => s.trim()).filter(Boolean).slice(0,3);

  return (
    <div className={'job-card ' + stripe}>
      <div className="job-card-inner">
        <div className="job-body">
          <div className="job-header">
            <div className={'co-logo ' + logo}>{initial}</div>
            <div className="job-header-text">
              <div className="job-posted">{posted}</div>
              <div className="job-title" onClick={() => job.url && window.open(job.url, '_blank')} style={{ cursor:'pointer' }}>
                {job.role || 'Untitled Role'}
              </div>
              <div className="job-company">
                <span className="job-co-name">{job.co || '—'}</span>
                {tags[0] && <><span className="job-sep">/</span><span className="job-industry">{tags[0]}</span></>}
              </div>
            </div>
          </div>

          <div className="job-chips">
            {job.loc && (
              <span className="job-chip"><Icon name="map-pin" size={11}/>{job.loc}</span>
            )}
            <span className="job-chip"><Icon name="building-2" size={11}/>{model}</span>
            <span className="job-chip"><Icon name="graduation-cap" size={11}/>{exp}</span>
            {tags.slice(1).map((t, i) => (
              <span key={i} className="job-chip">{t}</span>
            ))}
          </div>

          <div className="job-footer">
            <span className="job-app-count">{(idx * 31 + 47)} applicants</span>
            <div className="job-footer-actions">
              <button className="icon-btn" title="Hide" onClick={() => onHide?.(job)}>
                <Icon name="eye-off" size={13}/>
              </button>
              <button className={'icon-btn' + (isLiked ? ' active' : '')} 
                title={isLiked ? "Unlike" : "Save"} 
                onClick={() => onLike?.(job)}
                style={isLiked ? { color:'var(--accent-h)', background:'var(--accent-d)', borderColor:'var(--accent-b)' } : {}}>
                <Icon name="bookmark" size={13} fill={isLiked ? "currentColor" : "none"}/>
              </button>
              <button className="btn-ghost">
                <Icon name="sparkles" size={12}/> Ask Atlas
              </button>
              <button className="btn-primary" onClick={() => job.url && window.open(job.url, '_blank')}>
                <Icon name="zap" size={12}/> Quick Apply
              </button>
            </div>
          </div>
        </div>
        <ScoreRing score={job.score || 0}/>
      </div>
    </div>
  );
}

function FilterMenu({ label, options, selected, onSelect, onClose }) {
  const ref = useRef(null);
  useEffect(() => {
    const hide = e => { if (ref.current && !ref.current.contains(e.target)) onClose(); };
    document.addEventListener('mousedown', hide);
    return () => document.removeEventListener('mousedown', hide);
  }, [onClose]);

  return (
    <div className="filter-dropdown fade-in" ref={ref}>
      <div className="f-opt-head">{label}</div>
      {options.map(opt => (
        <button key={opt.id} className={'f-opt' + (selected === opt.id ? ' active' : '')}
          onClick={() => { onSelect(opt.id); onClose(); }}>
          {opt.label}
          {selected === opt.id && <Icon name="check" size={12}/>}
        </button>
      ))}
    </div>
  );
}

function JobsPage({ state, refresh, setPage }) {
  const [tab, setTab]           = useState('recommended');
  const [searchQuery, setQuery] = useState('');
  const [running, setRun]   = useState(false);
  const [searchingMore, setSearchingMore] = useState(false);
  const [runLabel, setRunLabel] = useState('');
  const autoStarted = useRef(false);
  const runningRef  = useRef(false);

  const rawJobs = state?.scored_summary?.jobs || [];
  const apps    = state?.applications || [];
  const liked   = new Set(state?.liked_ids || []);
  const hidden  = new Set(state?.hidden_ids || []);

  const filtered = useMemo(() => {
    let list = rawJobs;
    if (tab === 'liked') list = list.filter(j => liked.has(j.id));
    else if (tab === 'applied') {
      const appTitles = new Set(apps.map(a => `${a.co}|${a.role}`));
      list = list.filter(j => appTitles.has(j.id));
    } else if (tab === 'recommended') list = list.filter(j => !hidden.has(j.id));

    if (searchQuery.trim()) {
      const q = searchQuery.toLowerCase();
      list = list.filter(j => (j.co || '').toLowerCase().includes(q) || (j.role || '').toLowerCase().includes(q) || (j.skills || '').toLowerCase().includes(q));
    }
    return list;
  }, [rawJobs, tab, searchQuery, liked, hidden, apps]);

  const tabCounts = {
    recommended: rawJobs.filter(j => !hidden.has(j.id)).length,
    liked:       liked.size,
    applied:     apps.length,
    external:    0,
  };

  const handleAction = async (action, job) => {
    const job_id = job.id || `${job.co}|${job.role}`;
    await api.post('/api/jobs/action', { action, job_id });
    refresh();
  };

  const removeSearch = async (title) => {
    const nextTitles = (state?.profile?.target_titles || []).filter(t => t !== title);
    await api.post('/api/profile', { ...state.profile, target_titles: nextTitles });
    refresh();
  };

  const runDiscovery = useCallback(async ({ force = false, automatic = false, deep = false, more = false } = {}) => {
    if (runningRef.current || !state?.has_resume) return;
    runningRef.current = true;
    if (more) setSearchingMore(true); else setRun(true);
    const done = new Set(state?.done || []);

    try {
      if (!state?.profile) {
        setRunLabel('Reading resume');
        await runPhasePromise(1, { rerun: done.has(1) });
        await refresh();
      }

      setRunLabel(more ? 'Searching more' : (deep ? 'Deep searching' : 'Finding jobs'));
      await runPhasePromise(2, {
        rerun: (force || deep || more) && done.has(2),
        params: more ? { append: 1 } : (deep ? { deep: 1 } : {}),
      });
      await refresh();

      setRunLabel('Ranking matches');
      await runPhasePromise(3, {
        rerun: true,
        params: { fast: 1 },
      });
      await refresh();
    } catch (e) {
      await refresh();
      if (!automatic) console.warn('Job discovery failed', e);
    } finally {
      runningRef.current = false;
      setRun(false);
      setSearchingMore(false);
      setRunLabel('');
    }
  }, [state, refresh]);

  useEffect(() => {
    if (autoStarted.current || !state?.has_resume) return;
    if (state?.scored_summary && state?.scored_summary?.total > 0) return;
    autoStarted.current = true;
    runDiscovery({ automatic: true });
  }, [state, runDiscovery]);

  const onScroll = (e) => {
    if (searchingMore || running || tab !== 'recommended') return;
    const { scrollTop, scrollHeight, clientHeight } = e.target;
    if (scrollHeight - scrollTop - clientHeight < 400) {
      runDiscovery({ more: true });
    }
  };

  const handleRefresh = () => runDiscovery({ force: true });
  const handleDeepSearch = () => runDiscovery({ force: true, deep: true });

  const filters = [
    { label: state?.location || 'United States', dropdown: true, id: 'location' },
    { label: (state?.profile?.target_titles?.[0]) || 'Any title', dropdown: true, active: !!state?.profile?.target_titles?.length, id: 'title' },
    { label: 'Experience level', dropdown: true, id: 'exp' },
    { label: 'Work model', dropdown: true, id: 'model' },
    { label: 'Date posted', dropdown: true, id: 'date' },
    { label: 'Salary', dropdown: true, id: 'salary' },
  ];

  return (
    <>
      <div className="page-head">
        <div className="page-title">JOBS</div>
        <span className="page-tab-sep">›</span>
        <div className="page-tabs">
          {[['recommended','Recommended'],['liked','Liked'],['applied','Applied'],['external','External']].map(([id, label]) => (
            <button key={id} className={'page-tab' + (tab===id ? ' active' : '')} onClick={() => setTab(id)}>
              {label}
              {tabCounts[id] != null && <span className="tab-count">{tabCounts[id]}</span>}
            </button>
          ))}
        </div>
        <div className="head-spacer"/>
        <div className="head-search">
          <Icon name="search" size={13} color="var(--t3)"/>
          <input placeholder="Search roles or companies" value={searchQuery} onChange={e => setQuery(e.target.value)}/>
        </div>
        <button className="head-cta" onClick={handleRefresh} disabled={running}>
          {running ? <><span className="spin"/> {runLabel || 'Finding jobs'}...</> : <><Icon name="refresh-cw" size={13} color="#fff"/> Refresh</>}
        </button>
        <button className="btn-ghost" onClick={handleDeepSearch} disabled={running} style={{ marginLeft:8 }}>
          <Icon name="radar" size={12}/> Deep search
        </button>
      </div>

      <div className="page-body" onScroll={onScroll} style={{ overflowY: 'auto' }}>
        <div className="col-main">
          {/* Filter chips */}
          <div className="filters">
            {filters.map((f, i) => (
              <button key={i} className={'f-chip' + (f.active ? ' on' : '')} onClick={() => setPage('settings')}>
                {f.label}
                {f.dropdown && <Icon name="chevron-down" size={11}/>}
              </button>
            ))}
            <div className="f-divider"/>
            <button className="f-action secondary" onClick={() => setPage('settings')}>
              <Icon name="sliders-horizontal" size={11}/> All filters
            </button>
            {hidden.size > 0 && (
              <button className="f-action primary" onClick={() => { 
                hidden.forEach(id => api.post('/api/jobs/action', { action:'unhide', job_id:id }));
                setTimeout(refresh, 500);
              }}>
                <Icon name="eye" size={11}/> Show {hidden.size} hidden
              </button>
            )}
          </div>

          {filtered.length === 0 ? (
            <div style={{ background:'var(--surface)', border:'1px solid var(--bdr)', borderRadius:14, padding:'52px 32px', textAlign:'center' }}>
              <div style={{ width:52, height:52, margin:'0 auto 16px', borderRadius:14, background:'var(--accent-d)', border:'1px solid var(--accent-b)', display:'flex', alignItems:'center', justifyContent:'center' }}>
                <Icon name={tab === 'liked' ? 'bookmark' : 'briefcase'} size={22} color="var(--accent-h)"/>
              </div>
              <div style={{ fontSize:18, fontWeight:600, marginBottom:6 }}>
                {tab === 'liked' ? 'No saved jobs' : tab === 'applied' ? 'No applications yet' : 'No matched jobs yet'}
              </div>
              <div style={{ fontSize:13, color:'var(--t2)', maxWidth:400, margin:'0 auto 18px', lineHeight:1.55 }}>
                {running || searchingMore
                  ? `${runLabel || 'Finding jobs'} from your resume and profile.`
                  : tab === 'liked' ? 'Jobs you save with the bookmark icon will appear here.' : 'Matched roles will appear here after the scraper checks relevant job boards.'}
              </div>
              {tab === 'recommended' && (
                <button className="btn-primary" onClick={handleRefresh} disabled={running} style={{ margin:'0 auto' }}>
                  {running ? <span className="spin"/> : <Icon name="sparkles" size={13} color="#fff"/>}
                  {running ? `${runLabel || 'Working'}...` : 'Find jobs now'}
                </button>
              )}
            </div>
          ) : (
            <div className="job-list">
              {filtered.map((j, i) => (
                <JobCard key={j.id || i} idx={i} job={j} 
                  isLiked={liked.has(j.id)}
                  onLike={() => handleAction(liked.has(j.id) ? 'unlike' : 'like', j)}
                  onHide={() => handleAction('hide', j)}/>
              ))}
              
              {searchingMore && (
                <div style={{ padding: 24, textAlign: 'center', color: 'var(--t3)' }}>
                  <span className="spin" style={{ marginRight: 8 }}/> Finding more roles for you...
                </div>
              )}
              
              {!searchingMore && tab === 'recommended' && rawJobs.length < 500 && (
                <div style={{ padding: 32, textAlign: 'center' }}>
                  <button className="btn-ghost" onClick={() => runDiscovery({ more: true })}>
                    <Icon name="chevron-down" size={14}/> Load more jobs
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Right rail */}
        <div className="col-rail">
          {/* User card */}
          <div className="rcard">
            <div className="user-row">
              <div className="user-avatar">
                {(state?.profile?.name || 'U').charAt(0).toUpperCase()}
              </div>
              <div>
                <div className="user-name" onClick={() => setPage('profile')} style={{ cursor:'pointer' }}>{state?.profile?.name ? state.profile.name.split(' ')[0] : 'My Account'}</div>
              </div>
            </div>

            <div className="rcard-h">
              Saved searches
              <span className="rcard-add" onClick={() => setPage('profile')} title="Add search role"><Icon name="plus" size={13}/></span>
            </div>
            {state?.profile?.target_titles?.slice(0,4).map((t, i) => (
              <div key={i} className="saved-filter">
                <Icon name="bookmark" size={13} color="var(--accent-h)"/>
                <span onClick={() => { setQuery(t); setTab('recommended'); }} style={{ cursor:'pointer' }}>{t} · {state?.location || 'US'}</span>
                <div style={{ display:'flex', gap:4 }}>
                  <span className="saved-filter-edit" onClick={() => setPage('profile')} title="Edit titles"><Icon name="pencil" size={11}/></span>
                  <span className="saved-filter-edit" onClick={() => removeSearch(t)} title="Remove"><Icon name="trash-2" size={11}/></span>
                </div>
              </div>
            ))}
            {!state?.profile?.target_titles?.length && (
              <div className="saved-filter" onClick={() => setPage('profile')} style={{ cursor:'pointer' }}>
                <Icon name="plus-circle" size={13}/>
                <span style={{ color:'var(--t3)' }}>Add a search filter</span>
              </div>
            )}
          </div>

          {/* Pipeline status */}
          <div className="rcard">
            <div className="rcard-h"><Icon name="activity" size={14}/> Pipeline status</div>
            <div style={{ display:'flex', flexDirection:'column', gap:7 }}>
              {['Profile','Find jobs','Score','Tailor','Apply'].map((label, i) => {
                const n = i + 1;
                const isDone = (state?.done || []).includes(n);
                return (
                  <div key={n} style={{ display:'flex', alignItems:'center', gap:10, fontSize:12.5 }}>
                    <div style={{ width:22, height:22, borderRadius:6, flexShrink:0, display:'flex', alignItems:'center', justifyContent:'center', fontFamily:'var(--mono)', fontSize:10, fontWeight:600, background: isDone ? 'var(--accent-d)' : 'var(--bg-3)', color: isDone ? 'var(--accent-h)' : 'var(--t4)', border:'1px solid ' + (isDone ? 'var(--accent-b)' : 'var(--bdr)') }}>
                      {isDone ? <Icon name="check" size={11} color="var(--accent-h)"/> : n}
                    </div>
                    <span style={{ color: isDone ? 'var(--t1)' : 'var(--t3)' }}>{label}</span>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

/* ── Action menu helper ── */
function ActionMenu({ items = [] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const hide = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener('mousedown', hide);
    return () => document.removeEventListener('mousedown', hide);
  }, [open]);

  return (
    <div className="action-menu-wrap" ref={ref}>
      <button className="icon-btn" onClick={() => setOpen(!open)} style={{ borderColor:'transparent' }}>
        <Icon name="more-horizontal" size={14}/>
      </button>
      {open && (
        <div className="action-menu fade-in">
          {items.map((it, i) => (
            <button key={i} className={'menu-item' + (it.danger ? ' danger' : '')} onClick={() => { setOpen(false); it.onClick(); }}>
              <Icon name={it.icon} size={13}/>
              <span>{it.label}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   RESUME PAGE
══════════════════════════════════════════════════════════ */
function ResumePage({ state, refresh, setPage }) {
  const [resumeText, setResumeText] = useState('');
  const [tab, setTab] = useState('analysis');
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const fileRef = useRef(null);

  const resumes = state?.resumes || [];
  const primary = resumes.find(r => r.primary) || resumes[0];
  const has = !!resumes.length;
  const phase1 = (state?.done || []).includes(1);
  const p = state?.profile || {};

  useEffect(() => {
    if (primary && !resumeText && !loading) {
      setLoading(true);
      api.get('/api/resume/content')
        .then(res => {
          setResumeText(res.text);
          setEditText(res.text);
        })
        .catch(() => {})
        .finally(() => setLoading(false));
    }
    if (!primary) {
      setResumeText('');
      setEditText('');
    }
  }, [primary?.id]);

  const stats = useMemo(() => {
    if (!phase1) return null;
    return {
      skills: (p.top_hard_skills || []).length,
      exp: (p.experience || []).length,
      gaps: (p.resume_gaps || []).length,
    };
  }, [p, phase1]);

  const handleUpload = async (file) => {
    if (!file) return;
    setUploading(true);
    try {
      await api.upload('/api/resume/upload', file);
      refresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setUploading(false);
    }
  };

  const handleDelete = async (id) => {
    if (!confirm('Are you sure you want to delete this resume?')) return;
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
      setResumeText(''); // Clear to trigger re-fetch
      refresh();
    } catch (e) {
      alert(e.message);
    }
  };

  const handleRename = async (id, oldName) => {
    const next = prompt('Enter new filename:', oldName);
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
      await api.post('/api/resume/text', { id: primary.id, text: editText });
      setResumeText(editText);
      setIsEditing(false);
      refresh();
    } catch (e) {
      alert(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Resume</div>
        <div className="head-spacer"/>
        <button className="head-cta" onClick={() => fileRef.current?.click()} disabled={uploading}>
          {uploading ? <span className="spin"/> : <Icon name="plus" size={13} color="#fff"/>}
          {uploading ? 'Uploading...' : 'Add resume'}
        </button>
        <input ref={fileRef} type="file" style={{ display:'none' }} accept=".pdf,.docx,.txt,.md,.tex" onChange={e => handleUpload(e.target.files?.[0])}/>
      </div>

      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="col-main">
          <div className="notice-strip">
            <Icon name="shield-check" size={13}/>
            You have {resumes.length} of 5 resume slots used. Files are encrypted at rest.
          </div>

          <div className="data-card">
            <div className="dt-head">
              <div>Resume</div>
              <div>Target role</div>
              <div>Modified</div>
              <div>Created</div>
              <div></div>
            </div>
            {has ? resumes.map(r => (
              <div key={r.id} className="dt-row">
                <div className="dt-name">
                  <div className="dt-icon" style={!r.primary ? { background:'var(--bg-3)', color:'var(--t3)' } : {}}>{r.filename.charAt(0).toUpperCase()}</div>
                  <span title={r.filename}>{r.filename.replace(/\.[^.]+$/, '')}</span>
                  {r.primary && <span className="badge b-accent">Primary</span>}
                  {r.primary && phase1 && <span className="badge b-good">Analyzed</span>}
                </div>
                <div style={{ color:'var(--t2)' }}>{r.primary ? (state?.profile?.target_titles?.[0] || <span style={{ color:'var(--t3)' }}>—</span>) : <span style={{ color:'var(--t3)' }}>—</span>}</div>
                <div style={{ color:'var(--t3)', fontFamily:'var(--mono)', fontSize:11.5 }}>{r.created_at ? new Date(r.created_at).toLocaleDateString() : 'just now'}</div>
                <div style={{ color:'var(--t3)', fontFamily:'var(--mono)', fontSize:11.5 }}>{r.created_at ? new Date(r.created_at).toLocaleDateString() : 'just now'}</div>
                <div>
                  <ActionMenu items={[
                    { icon:'star', label:'Set as primary', onClick: () => handleSetPrimary(r.id) },
                    { icon:'pencil', label:'Rename', onClick: () => handleRename(r.id, r.filename) },
                    { icon:'edit-3', label:'Edit text', onClick: () => { setTab('preview'); setIsEditing(true); } },
                    { icon:'trash-2', label:'Delete', danger: true, onClick: () => handleDelete(r.id) },
                  ]}/>
                </div>
              </div>
            )) : (
              <div className="dt-empty">No resumes yet — add one to start matching jobs.</div>
            )}
          </div>

          {primary && (
            <div style={{ marginTop:24 }}>
              <div className="prof-tabs" style={{ marginBottom:14 }}>
                <button className={'prof-tab' + (tab==='analysis' ? ' active' : '')} onClick={() => { setTab('analysis'); setIsEditing(false); }}>
                  <Icon name="bar-chart-3" size={13} style={{ marginRight:6 }}/> Analysis
                </button>
                <button className={'prof-tab' + (tab==='preview' ? ' active' : '')} onClick={() => setTab('preview')}>
                  <Icon name="eye" size={13} style={{ marginRight:6 }}/> Preview
                </button>
              </div>

              {tab === 'preview' && (
                <div className="data-card fade-in" style={{ padding:0, overflow:'hidden' }}>
                  <div style={{ padding:'10px 16px', background:'var(--bg-2)', borderBottom:'1px solid var(--bdr)', display:'flex', alignItems:'center', justifyContent:'space-between' }}>
                    <div style={{ fontSize:12, color:'var(--t3)', fontFamily:'var(--mono)' }}>{primary.filename}</div>
                    <div style={{ display:'flex', gap:8 }}>
                      {isEditing ? (
                        <>
                          <button className="btn-ghost" onClick={() => { setIsEditing(false); setEditText(resumeText); }}>Cancel</button>
                          <button className="btn-primary" onClick={handleSaveText} disabled={loading}>
                            {loading ? <span className="spin"/> : <Icon name="save" size={12} color="#fff"/>} Save
                          </button>
                        </>
                      ) : (
                        <>
                          <button className="icon-btn" title="Edit text" onClick={() => setIsEditing(true)}><Icon name="edit-3" size={12}/></button>
                          <button className="icon-btn" title="Copy text" onClick={() => { navigator.clipboard.writeText(resumeText); alert('Copied!'); }}><Icon name="copy" size={12}/></button>
                          <button className="icon-btn" title="Download"><Icon name="download" size={12}/></button>
                        </>
                      )}
                    </div>
                  </div>
                  <div style={{ padding:isEditing ? 0 : 20, maxHeight:600, overflowY:'auto', background:'#0f0f13' }}>
                    {loading && !isEditing ? (
                      <div style={{ padding:40, textAlign:'center', color:'var(--t4)' }}><span className="spin"/> Loading content…</div>
                    ) : isEditing ? (
                      <textarea
                        className="ob-area"
                        style={{ margin:0, width:'100%', minHeight:500, border:'none', borderRadius:0, background:'transparent' }}
                        value={editText}
                        onChange={e => setEditText(e.target.value)}
                        placeholder="Resume text..."
                      />
                    ) : (
                      <pre style={{ margin:0, whiteSpace:'pre-wrap', fontSize:13, lineHeight:1.6, color:'#d1d1d6', fontFamily:'"JetBrains Mono", Menlo, monospace' }}>
                        {resumeText || 'No text content available.'}
                      </pre>
                    )}
                  </div>
                </div>
              )}

              {tab === 'analysis' && (
                <div className="fade-in">
                  {!phase1 ? (
                    <div className="data-card" style={{ padding:40, textAlign:'center' }}>
                      <div style={{ width:48, height:48, borderRadius:12, background:'var(--bg-3)', display:'flex', alignItems:'center', justifyContent:'center', margin:'0 auto 16px' }}>
                        <Icon name="sparkles" size={24} color="var(--t4)"/>
                      </div>
                      <h3 style={{ marginBottom:8 }}>Not yet analyzed</h3>
                      <p style={{ color:'var(--t3)', fontSize:13, maxWidth:300, margin:'0 auto 20px' }}>
                        Run the extraction agent to see a detailed analysis of your skills and improvements.
                      </p>
                      <button className="head-cta" onClick={() => setPage('agent')}>
                        Go to Agent
                      </button>
                    </div>
                  ) : (
                    <div className="settings-grid">
                      <div className="set-sec">
                        <div className="set-sec-h"><Icon name="info" size={14}/> Resume Quality</div>
                        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr 1fr', gap:12, marginBottom:20 }}>
                          <div className="rcard" style={{ textAlign:'center' }}>
                            <div style={{ fontSize:20, fontWeight:700, color:'var(--accent-h)' }}>{stats.skills}</div>
                            <div style={{ fontSize:11, color:'var(--t3)', marginTop:2 }}>Skills found</div>
                          </div>
                          <div className="rcard" style={{ textAlign:'center' }}>
                            <div style={{ fontSize:20, fontWeight:700, color:'var(--accent-h)' }}>{stats.exp}</div>
                            <div style={{ fontSize:11, color:'var(--t3)', marginTop:2 }}>Experiences</div>
                          </div>
                          <div className="rcard" style={{ textAlign:'center' }}>
                            <div style={{ fontSize:20, fontWeight:700, color: stats.gaps > 0 ? 'var(--warn)' : 'var(--good)' }}>{stats.gaps}</div>
                            <div style={{ fontSize:11, color:'var(--t3)', marginTop:2 }}>Issues</div>
                          </div>
                        </div>

                        <div className="set-field">
                          <div className="set-label">Critical Analysis</div>
                          <div className="analysis-text" style={{ fontSize:13, color:'var(--t2)', lineHeight:1.7, marginTop:8, whiteSpace:'pre-wrap' }}>
                            {p.critical_analysis || "No detailed analysis available yet. Run the extraction agent to generate a deep critique."}
                          </div>
                        </div>
                      </div>

                      <div className="set-sec">
                        <div className="set-sec-h" style={{ color:'var(--warn)' }}><Icon name="alert-circle" size={14}/> Things to Improve</div>
                        {stats.gaps > 0 ? (
                          <div style={{ display:'flex', flexDirection:'column', gap:10 }}>
                            {p.resume_gaps.map((gap, i) => (
                              <div key={i} className="notice-strip" style={{ background:'rgba(251, 191, 36, 0.05)', borderColor:'rgba(251, 191, 36, 0.2)', color:'var(--warn)', margin:0 }}>
                                <Icon name="chevron-right" size={12}/>
                                {gap}
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="notice-strip" style={{ color:'var(--good)', background:'rgba(34, 197, 94, 0.05)', borderColor:'rgba(34, 197, 94, 0.2)', margin:0 }}>
                            <Icon name="check-circle-2" size={13}/>
                            No major issues detected. Great job!
                          </div>
                        )}
                        <div className="set-helper" style={{ marginTop:12 }}>
                          These improvements are identified based on typical ATS requirements for {state?.profile?.target_titles?.[0] || 'technical'} roles.
                        </div>
                      </div>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   PROFILE PAGE
══════════════════════════════════════════════════════════ */
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

  if (!p) return (
    <div className="placeholder-page">
      <div className="placeholder-icon"><Icon name="user" size={22}/></div>
      <div style={{ fontSize:18, fontWeight:600 }}>No profile found</div>
      <div style={{ fontSize:13, color:'var(--t2)' }}>Extract your resume or create the profile manually.</div>
      <div style={{ display:'flex', gap:10, marginTop:16 }}>
        <button className="btn-primary" onClick={async () => { await api.post('/api/profile/extract', {}); refresh?.(); }}>
          <Icon name="scan-text" size={14}/> Extract from resume
        </button>
        <button className="btn-ghost" onClick={async () => { await api.post('/api/profile', { name:'', target_titles:[], top_hard_skills:[], top_soft_skills:[], education:[], experience:[], research:[], projects:[] }); refresh?.(); }}>
          <Icon name="pencil" size={14}/> Create manually
        </button>
      </div>
    </div>
  );

  const updateField = (key, value) => { setForm(prev => ({ ...prev, [key]: value })); setDirty(true); };
  const updateRow = (key, index, field, value) => {
    setForm(prev => ({
      ...prev,
      [key]: prev[key].map((item, i) => i === index ? { ...item, [field]: value } : item),
    }));
    setDirty(true);
  };
  const addRow = (key, row) => { setForm(prev => ({ ...prev, [key]: [...prev[key], row] })); setDirty(true); };
  const removeRow = (key, index) => { setForm(prev => ({ ...prev, [key]: prev[key].filter((_, i) => i !== index) })); setDirty(true); };

  const saveProfile = async () => {
    setSaving(true);
    try {
      await api.post('/api/profile', formToProfile(form));
      setDirty(false);
      await refresh?.();
    } finally {
      setSaving(false);
    }
  };

  const rerunExtraction = async () => {
    setExtracting(true);
    try {
      await api.post('/api/profile/extract', { preferred_titles: splitList(form.target_titles) });
      refresh?.();
    } finally {
      setExtracting(false);
    }
  };

  const syncSearch = async () => {
    await saveProfile();
    await api.post('/api/config', { job_titles: form.target_titles });
    setPage?.('jobs');
  };

  const completion = p.completion || { percent:0, missing:[] };
  const settings = p.settings || {};
  const [activeTab, setActiveTab] = useState('personal');

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Profile</div>
        <div className="head-spacer"/>
        <button className="btn-ghost" onClick={rerunExtraction} disabled={extracting}>
          <Icon name="scan-text" size={13}/> {extracting ? 'Extracting...' : 'Re-scrape resume'}
        </button>
        <button className="btn-ghost" onClick={saveProfile} disabled={saving} style={{ marginLeft:8 }}>
          <Icon name="save" size={13}/> {saving ? 'Saving...' : 'Save profile'}
        </button>
        <button className="lp-btn-p" onClick={syncSearch} disabled={saving} style={{ marginLeft:8, padding:'6px 14px', fontSize:13 }}>
          <Icon name="search" size={13}/> Explore jobs
        </button>
      </div>

      <div className="page-body solo">
        <div className="prof-nav-tabs">
          {[
            { id:'personal', label:'Personal' },
            { id:'experience', label:'Experience' },
            { id:'projects', label:'Projects' },
            { id:'targets', label:'Targets & Skills' },
            { id:'education', label:'Education' }
          ].map(t => (
            <button key={t.id} className={'prof-nav-tab' + (activeTab === t.id ? ' active' : '')} onClick={() => setActiveTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        <div className="col-main" style={{ width:'100%', maxWidth:'none', marginTop:24 }}>
          {activeTab === 'personal' && (
            <div className="data-card" style={{ padding:24 }}>
              <div className="profile-grid">
                <ProfileInput label="Name" value={form.name} onChange={v => updateField('name', v)}/>
                <ProfileInput label="Email" value={form.email} onChange={v => updateField('email', v)}/>
                <ProfileInput label="Phone" value={form.phone} onChange={v => updateField('phone', v)}/>
                <ProfileInput label="Location" value={form.location} onChange={v => updateField('location', v)}/>
                <ProfileInput label="LinkedIn URL" value={form.linkedin} onChange={v => updateField('linkedin', v)}/>
                <ProfileInput label="GitHub URL" value={form.github} onChange={v => updateField('github', v)}/>
                <ProfileInput label="Work authorization" value={form.work_authorization} onChange={v => updateField('work_authorization', v)}/>
                <ProfileInput label="Target salary" value={form.target_salary} onChange={v => updateField('target_salary', v)}/>
              </div>
              <ProfileInput label="Professional summary" textarea value={form.summary} onChange={v => updateField('summary', v)}/>
            </div>
          )}
          {activeTab === 'experience' && (
            <div className="data-card" style={{ padding:24 }}>
              <EditableRoles items={form.experience} onChange={(i, f, v) => updateRow('experience', i, f, v)} onAdd={() => addRow('experience', emptyRole())} onRemove={i => removeRow('experience', i)}/>
            </div>
          )}
          {activeTab === 'projects' && (
            <div className="data-card" style={{ padding:24 }}>
              <EditableProjects items={form.projects} onChange={(i, f, v) => updateRow('projects', i, f, v)} onAdd={() => addRow('projects', { name:'', description:'', skills_used:[] })} onRemove={i => removeRow('projects', i)}/>
            </div>
          )}
          {activeTab === 'targets' && (
            <div className="settings-grid">
               <div className="data-card" style={{ padding:24 }}>
                 <h3 className="prof-h" style={{ fontSize:14, marginBottom:16 }}><Icon name="target" size={14}/> Target Roles</h3>
                 <ProfileInput label="Comma-separated titles" value={form.target_titles} onChange={v => updateField('target_titles', v)}/>
                 <ProfileInput label="Critical analysis" textarea value={form.critical_analysis} onChange={v => updateField('critical_analysis', v)}/>
                 <ProfileInput label="ATS gaps, comma-separated" value={form.resume_gaps} onChange={v => updateField('resume_gaps', v)}/>
               </div>
               <div className="data-card" style={{ padding:24 }}>
                 <h3 className="prof-h" style={{ fontSize:14, marginBottom:16 }}><Icon name="list-checks" size={14}/> Skills</h3>
                 <ProfileInput label="Top Hard Skills" textarea value={form.top_hard_skills} onChange={v => updateField('top_hard_skills', v)}/>
                 <ProfileInput label="Soft Skills" value={form.top_soft_skills} onChange={v => updateField('top_soft_skills', v)}/>
               </div>
               <div className="data-card" style={{ padding:24 }}>
                 <h3 className="prof-h" style={{ fontSize:14, marginBottom:16 }}><Icon name="search" size={14}/> Discovery & Search</h3>
                 <ProfileInput label="Max jobs to scrape" type="number" value={form.max_scrape_jobs} onChange={v => updateField('max_scrape_jobs', parseInt(v))}/>
                 <ProfileInput label="Posting age (days)" type="number" value={form.days_old} onChange={v => updateField('days_old', parseInt(v))}/>
               </div>
            </div>
          )}
          {activeTab === 'education' && (
            <div className="data-card" style={{ padding:24 }}>
               <EditableEducation items={form.education} onChange={(i, f, v) => updateRow('education', i, f, v)} onAdd={() => addRow('education', { degree:'', institution:'', year:'', gpa:'' })} onRemove={i => removeRow('education', i)}/>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

function splitList(value) {
  return String(value || '').split(',').map(s => s.trim());
}
function emptyRole() {
  return { title:'', company:'', dates:'', bullets:[] };
}
function profileToForm(p = {}) {
  return {
    name:p?.name || '', email:p?.email || '', phone:p?.phone || '',
    location:p?.location || '', linkedin:p?.linkedin || '', github:p?.github || '',
    summary:p?.summary || '', work_authorization:p?.work_authorization || '',
    target_salary:p?.target_salary || '', critical_analysis:p?.critical_analysis || '',
    target_titles:(p?.target_titles || []).join(', '),
    top_hard_skills:(p?.top_hard_skills || []).join(', '),
    top_soft_skills:(p?.top_soft_skills || []).join(', '),
    resume_gaps:(p?.resume_gaps || []).join(', '),
    education:p?.education || [],
    experience:p?.experience || [],
    research:p?.research || [],
    projects:p?.projects || [],
    // New fields from settings
    max_scrape_jobs: p?.settings?.max_scrape_jobs || 20,
    days_old: p?.settings?.days_old || 30,
    threshold: p?.settings?.threshold || 75,
    experience_levels: p?.settings?.experience_levels || [],
    citizenship_filter: p?.settings?.citizenship_filter || 'none',
    blacklist: (p?.settings?.blacklist || []).join(', '),
    whitelist: (p?.settings?.whitelist || []).join(', '),
  };
}

function formToProfile(form) {
  const roleList = rows => rows.map(r => ({ ...r, bullets: (Array.isArray(r.bullets) ? r.bullets : splitBullets(r.bullets)).filter(Boolean) }));
  return {
    ...form,
    target_titles: splitList(form.target_titles).filter(Boolean),
    top_hard_skills: splitList(form.top_hard_skills).filter(Boolean),
    top_soft_skills: splitList(form.top_soft_skills).filter(Boolean),
    resume_gaps: splitList(form.resume_gaps).filter(Boolean),
    experience: roleList(form.experience),
    research: roleList(form.research),
    education: form.education,
    projects: form.projects.map(p => ({ ...p, skills_used: (Array.isArray(p.skills_used) ? p.skills_used : splitList(p.skills_used)).filter(Boolean) })),
    // Send back settings fields
    settings: {
        max_scrape_jobs: form.max_scrape_jobs,
        days_old: form.days_old,
        threshold: form.threshold,
        experience_levels: form.experience_levels,
        citizenship_filter: form.citizenship_filter,
        blacklist: splitList(form.blacklist).filter(Boolean),
        whitelist: splitList(form.whitelist).filter(Boolean),
    }
  };
}
function splitBullets(value) {
  return String(value || '').split('\n').map(s => s.trim());
}
function ProfileInput({ label, value, onChange, textarea=false }) {
  const Tag = textarea ? 'textarea' : 'input';
  return (
    <label className="set-field">
      <span className="set-label">{label}</span>
      <Tag className={'profile-input' + (textarea ? ' profile-textarea' : '')} value={value || ''} onChange={e => onChange(e.target.value)}/>
    </label>
  );
}
function EditableRoles({ items, onChange, onAdd, onRemove }) {
  return (
    <div className="profile-stack">
      {(items || []).map((item, i) => (
        <div className="profile-edit-row" key={i}>
          <button className="edit-trigger always" onClick={() => onRemove(i)}><Icon name="trash-2" size={13}/></button>
          <div className="profile-grid">
            <ProfileInput label="Title" value={item.title} onChange={v => onChange(i, 'title', v)}/>
            <ProfileInput label="Organization" value={item.company || item.institution} onChange={v => onChange(i, 'company', v)}/>
            <ProfileInput label="Dates" value={item.dates} onChange={v => onChange(i, 'dates', v)}/>
          </div>
          <ProfileInput label="Bullets, one per line" textarea value={(item.bullets || []).join('\n')} onChange={v => onChange(i, 'bullets', splitBullets(v))}/>
        </div>
      ))}
      <button className="btn-ghost" onClick={onAdd}><Icon name="plus" size={13}/> Add role</button>
    </div>
  );
}
function EditableProjects({ items, onChange, onAdd, onRemove }) {
  return (
    <div className="profile-stack">
      {(items || []).map((item, i) => (
        <div className="profile-edit-row" key={i}>
          <button className="edit-trigger always" onClick={() => onRemove(i)}><Icon name="trash-2" size={13}/></button>
          <ProfileInput label="Project name" value={item.name} onChange={v => onChange(i, 'name', v)}/>
          <ProfileInput label="Description" textarea value={item.description} onChange={v => onChange(i, 'description', v)}/>
          <ProfileInput label="Skills used" value={(item.skills_used || []).join(', ')} onChange={v => onChange(i, 'skills_used', splitList(v))}/>
        </div>
      ))}
      <button className="btn-ghost" onClick={onAdd}><Icon name="plus" size={13}/> Add project</button>
    </div>
  );
}
function EditableEducation({ items, onChange, onAdd, onRemove }) {
  return (
    <div className="profile-stack">
      {(items || []).map((item, i) => (
        <div className="profile-edit-row compact" key={i}>
          <button className="edit-trigger always" onClick={() => onRemove(i)}><Icon name="trash-2" size={13}/></button>
          <ProfileInput label="Institution" value={item.institution} onChange={v => onChange(i, 'institution', v)}/>
          <ProfileInput label="Degree" value={item.degree} onChange={v => onChange(i, 'degree', v)}/>
          <ProfileInput label="Year" value={item.year} onChange={v => onChange(i, 'year', v)}/>
          <ProfileInput label="GPA" value={item.gpa} onChange={v => onChange(i, 'gpa', v)}/>
        </div>
      ))}
      <button className="btn-ghost" onClick={onAdd}><Icon name="plus" size={13}/> Add education</button>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   COACHING PAGE
══════════════════════════════════════════════════════════ */
function CoachingPage() {
  const tiles = [
    { icon:'compass',         h:"I'm trying to find my first job",         p:'Get a strong resume, LinkedIn profile, and a game plan that actually gets you interviews.' },
    { icon:'mail-question',   h:"I'm applying but hearing nothing",         p:'Diagnose what is blocking your search and fix the specific issue — whether it\'s targeting, resume, or outreach.' },
    { icon:'message-circle',  h:"I have an interview coming up",            p:'Practice with a real recruiter. Walk in prepared, confident, and ready to close the offer.' },
  ];
  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Coaching</div>
        <span className="page-tab-sep">›</span>
        <div className="page-tabs">
          <button className="page-tab active">Discover</button>
        </div>
      </div>
      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="col-main">
          <div className="coach-hero">
            <h1 className="coach-h">1-on-1 coaching with senior recruiters,<br/>built to <em>land your next interview</em></h1>
            <div className="coach-pills">
              <span className="coach-pill"><span className="pchk"><Icon name="check" size={9}/></span> Personalized, not generic</span>
              <span className="coach-pill"><span className="pchk"><Icon name="check" size={9}/></span> Find your blocker fast</span>
              <span className="coach-pill"><span className="pchk"><Icon name="check" size={9}/></span> Actionable fix, not theory</span>
            </div>
          </div>
          <div className="coach-cards">
            <h3>Where do you need the most help?</h3>
            <div className="coach-grid">
              {tiles.map((t, i) => (
                <div key={i} className="coach-tile">
                  <div className="coach-tile-icon"><Icon name={t.icon} size={22}/></div>
                  <div className="coach-tile-h">{t.h}</div>
                  <div className="coach-tile-p">{t.p}</div>
                  <button className="coach-tile-btn">
                    Meet my coach <Icon name="arrow-right" size={12} color="#fff"/>
                  </button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   INTERVIEW PAGE
══════════════════════════════════════════════════════════ */
function InterviewPage() {
  const recents = ['Stripe','Meta (E4)','Google (L4)','Bloomberg','Databricks'];
  const sections = [
    {
      h:'Top tier',
      feature:{ name:'Google', init:'G', desc:'Build products used by billions. Experience the culture and scale of Silicon Valley\'s most iconic engineering org.', total:368, delta:'+52' },
      cards:[{ name:'Apple', init:'A', q:113 },{ name:'Meta', init:'M', q:434 },{ name:'Netflix', init:'N', q:79 },{ name:'Amazon', init:'A', q:242 }],
    },
    {
      h:'AI Frontier',
      feature:{ name:'OpenAI', init:'O', desc:'Work at the center of the AI revolution and help define what comes next.', total:320, delta:'+95' },
      cards:[{ name:'Anthropic', init:'A', q:117 },{ name:'xAI', init:'X', q:47 },{ name:'NVIDIA', init:'N', q:62 },{ name:'Databricks', init:'D', q:137 }],
    },
  ];

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Interview Prep</div>
        <div className="head-spacer"/>
      </div>
      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="col-main">
          <div className="iv-statbar">
            <div className="iv-stat"><div className="iv-stat-num">311</div><div className="iv-stat-lbl">Companies</div></div>
            <div className="iv-stat"><div className="iv-stat-num">5,887</div><div className="iv-stat-lbl">Total questions</div></div>
            <div className="iv-stat"><div className="iv-stat-num"><em>+</em>1,238</div><div className="iv-stat-lbl">Last 30 days</div></div>
          </div>

          <div className="iv-search">
            <Icon name="search" size={15} color="var(--t3)"/>
            <input placeholder="Search 311+ companies…"/>
          </div>

          <div className="iv-pillbar">
            {recents.map((r, i) => (
              <span key={i} className="iv-pill">
                <span className="iv-pill-co">{r}</span>
                <span className="iv-pill-time">Coding · 6d</span>
              </span>
            ))}
          </div>

          {sections.map((sec, si) => (
            <div key={si} className="iv-section">
              <h2 className="iv-section-h">{sec.h}</h2>
              <div className="iv-grid">
                <div className="iv-feature">
                  <div className="iv-feature-h">
                    <div className="iv-feature-logo">{sec.feature.init}</div>
                    <div className="iv-feature-name">{sec.feature.name}</div>
                    <div className="iv-feature-upd">Updated 6d ago</div>
                  </div>
                  <p className="iv-feature-p">{sec.feature.desc}</p>
                  <div className="iv-feature-stats">
                    <div>
                      <div className="iv-feature-stat-n">{sec.feature.total}</div>
                      <div className="iv-feature-stat-l">Total questions</div>
                    </div>
                    <div>
                      <div className="iv-feature-stat-n delta">{sec.feature.delta}</div>
                      <div className="iv-feature-stat-l">Last 30 days</div>
                    </div>
                  </div>
                </div>
                {sec.cards.map((c, i) => (
                  <div key={i} className="iv-card">
                    <div className="iv-card-h">
                      <div className="iv-card-logo">{c.init}</div>
                      <div className="iv-card-name">{c.name}</div>
                      <div className="iv-card-upd">6d ago</div>
                    </div>
                    <div className="iv-card-q">
                      <strong>{c.q} questions</strong>
                      <Icon name="chevron-right" size={14} color="var(--t3)"/>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   AGENT PAGE
══════════════════════════════════════════════════════════ */
const PHASE_INFO = {
  1: { n:'Profile extraction',  s:'Parse resume → structured profile' },
  2: { n:'Find jobs',           s:'Scrape job boards for live openings' },
  3: { n:'Score & filter',      s:'Rank roles by skill alignment' },
  4: { n:'Tailor resumes',      s:'Generate ATS-tuned variants per role' },
  5: { n:'Apply',               s:'Submit to high-confidence roles' },
  6: { n:'Track',               s:'Update application tracker' },
  7: { n:'Report',              s:'Generate session summary' },
};

const CLI_LINES = {
  1: ['$ agent.py --phase 1', '  parsing resume -> extracting text...', '  extracting skills and experience...', '  auditing skill evidence...', '  ranking target titles...', 'OK phase_1 complete'],
  2: ['$ agent.py --phase 2', '  sources: linkedin / indeed / glassdoor / ziprecruiter', '  simplify dataset enabled', '  deduplicating postings...', '  applying education + citizenship filters...', 'OK phase_2 complete'],
  3: ['$ agent.py --phase 3', '  scoring jobs against profile...', '  weighting skills, industry, location...', '  filtering by experience level...', 'OK phase_3 complete'],
  4: ['$ agent.py --phase 4', '  tailoring resumes for shortlisted jobs...', '  reordering skills to match job descriptions...', '  running ATS gap analysis...', '  saving resume variants...', 'OK phase_4 complete'],
  5: ['$ agent.py --phase 5', '  submitting auto-eligible applications...', '  flagging manual-review applications...', 'OK phase_5 complete'],
  6: ['$ agent.py --phase 6', '  writing Job_Applications_Tracker...', '  status colors and dashboard applied', 'OK phase_6 complete'],
  7: ['$ agent.py --phase 7', '  generating run report...', '  saving final summary...', 'OK phase_7 complete'],
};

function PhaseLog({ n, logs = [], running }) {
  const lines = logs.length ? logs : CLI_LINES[n] || [];
  if (!lines.length && !running) return null;
  return (
    <div className="agent-log">
      {(running && !logs.length ? lines.slice(0, -1) : lines).map((line, i) => (
        <div key={i} className={line.trim().startsWith('OK') ? 'ok' : ''}>{line}</div>
      ))}
      {running && <div><span className="spin" style={{ width:10, height:10, marginRight:6 }}/>streaming backend output...</div>}
    </div>
  );
}

function KVList({ items }) {
  return (
    <div className="detail-kv">
      {items.filter(Boolean).map(([k, v], i) => (
        <div key={i}><span>{k}</span><b>{v || '-'}</b></div>
      ))}
    </div>
  );
}

function DetailTable({ columns, rows, empty = 'No rows yet.' }) {
  if (!rows?.length) return <div className="wait-state">{empty}</div>;
  return (
    <div className="detail-table-wrap">
      <table className="detail-table">
        <thead><tr>{columns.map(c => <th key={c.key}>{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i}>
              {columns.map(c => <td key={c.key} className={c.strong ? 't1' : ''}>{c.render ? c.render(row, i) : row[c.key]}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PhaseDetails({ n, data = {}, state = {}, threshold }) {
  if (n === 1) {
    const p = data.name || data.email || data.top_hard_skills ? data : (state.profile || {});
    return (
      <div className="phase-detail">
        <KVList items={[['Name', p.name], ['Email', p.email], ['Location', p.location], ['LinkedIn', p.linkedin]]}/>
        <div className="detail-grid">
          <div><h4>Target titles</h4><div className="csv-text">{(p.target_titles || []).join(', ')}</div></div>
          <div><h4>Hard skills</h4><div className="csv-text">{(p.top_hard_skills || []).join(', ')}</div></div>
          <div><h4>Soft skills</h4><div className="csv-text">{(p.top_soft_skills || []).join(', ')}</div></div>
          <div><h4>Resume gaps</h4><div className="csv-text" style={{ color:'var(--warn)' }}>{(p.resume_gaps || []).join(', ')}</div></div>
        </div>
      </div>
    );
  }
  if (n === 2) {
    const jobs = data.jobs || [];
    return <div className="phase-detail"><div className="metrics"><div className="met"><b>{data.total ?? state.job_count ?? jobs.length}</b><span>Jobs discovered</span></div></div><DetailTable columns={[{key:'co',label:'Company',strong:true},{key:'role',label:'Role'},{key:'loc',label:'Location'},{key:'experience',label:'Level'},{key:'education',label:'Education'},{key:'posted',label:'Posted'},{key:'url',label:'URL',render:j=>j.url?<a href={j.url} target="_blank" rel="noreferrer">Open</a>:'-'}]} rows={jobs} empty="Run or re-run Phase 2 to see every discovered posting."/></div>;
  }
  if (n === 3) {
    const summary = state.scored_summary || {};
    const jobs = data.jobs || summary.jobs || [];
    return <div className="phase-detail"><div className="metrics"><div className="met"><b>{data.total ?? summary.total ?? 0}</b><span>Scored</span></div><div className="met"><b>{data.auto ?? summary.auto ?? 0}</b><span>Auto at {threshold}</span></div><div className="met"><b>{data.manual ?? summary.manual ?? 0}</b><span>Manual review</span></div><div className="met"><b>{data.filtered ?? summary.filtered ?? 0}</b><span>Filtered</span></div></div><DetailTable columns={[{key:'co',label:'Company',strong:true},{key:'role',label:'Role'},{key:'score',label:'Score',strong:true},{key:'status',label:'Status'},{key:'matching',label:'Matching',render:j=>Array.isArray(j.matching)?j.matching.join(', '):(j.skills || '')},{key:'missing',label:'Missing',render:j=>Array.isArray(j.missing)?j.missing.join(', '):''},{key:'reason',label:'Reason'}]} rows={jobs}/></div>;
  }
  if (n === 4) {
    const items = data.items || [];
    return <div className="phase-detail"><div className="metrics"><div className="met"><b>{data.count ?? items.length}</b><span>Resume variants</span></div></div><DetailTable columns={[{key:'co',label:'Company',strong:true},{key:'role',label:'Role'},{key:'score',label:'Match'},{key:'ats_after',label:'ATS after',strong:true},{key:'ats_gaps',label:'Remaining gaps',render:x=>(x.ats_gaps || []).join(', ')},{key:'resume_file',label:'Resume',render:x=>x.resume_file?<a href={`/output/${x.resume_file}`} download>{x.resume_file}</a>:'-'}]} rows={items} empty="Run or re-run Phase 4 to see tailored resume details."/></div>;
  }
  if (n === 5) {
    const apps = data.apps || state.applications || [];
    return <div className="phase-detail"><div className="metrics"><div className="met"><b>{data.applied ?? apps.filter(a=>a.app_status==='Applied'||a.status==='Applied').length}</b><span>Applied</span></div><div className="met"><b>{data.manual ?? apps.filter(a=>a.app_status==='Manual Required'||a.status==='Manual Required').length}</b><span>Manual</span></div></div><DetailTable columns={[{key:'co',label:'Company',strong:true},{key:'role',label:'Role'},{key:'score',label:'Score'},{key:'status',label:'Status',render:x=>x.status || x.app_status},{key:'confirmation',label:'Confirmation'},{key:'resume',label:'Resume',render:x=>x.resume || x.resume_version || '-'},{key:'url',label:'URL',render:x=>x.url?<a href={x.url} target="_blank" rel="noreferrer">Open</a>:'-'}]} rows={apps}/></div>;
  }
  if (n === 6) {
    const tracker = data.tracker || state.output_files?.find(f => f.phase === 6)?.name;
    return <div className="phase-detail">{tracker ? <a className="detail-file" href={`/output/${tracker}`} download><Icon name="download" size={13}/> {tracker}</a> : <div className="wait-state">Tracker not generated yet.</div>}</div>;
  }
  if (n === 7) {
    return <div className="phase-detail">{data.report ? <pre className="report-pre">{data.report}</pre> : <div className="wait-state">Run report not generated yet.</div>}</div>;
  }
  return null;
}

function AgentPage({ state, refresh }) {
  const [open,    setOpen]    = useState({});
  const [running, setRunning] = useState(null);
  const [errors,  setErrors]  = useState({});
  const [phaseResults, setPhaseResults] = useState({});
  const [phaseLogs, setPhaseLogs] = useState({});

  const done = new Set(state?.done || []);
  const pct  = Math.round((done.size / 7) * 100);
  const C = 65, circ = 2 * Math.PI * C;
  const off = circ - (circ * pct / 100);

  const startPhase = (n, rerun=false) => {
    if (running) return;
    setRunning(n);
    setErrors(p => ({ ...p, [n]:null }));
    setOpen(o => ({ ...o, [n]:true }));
    setPhaseLogs(p => ({ ...p, [n]:[] }));
    runPhaseSSE(n, {
      rerun,
      onLog:   m  => setPhaseLogs(p => ({ ...p, [n]:[...(p[n] || []), m.text || m.line || ''] })),
      onDone:  m  => { setPhaseResults(p => ({ ...p, [n]:m.data || {} })); setRunning(null); refresh(); },
      onError: e  => { setRunning(null); setErrors(p => ({ ...p, [n]:e.message || 'failed' })); refresh(); },
    });
  };

  const runAll = async () => {
    if (running) return;
    for (let n = 1; n <= 7; n++) {
      if (done.has(n)) continue;
      await new Promise((res, rej) => {
        setRunning(n);
        setOpen(o => ({ ...o, [n]:true }));
        setPhaseLogs(p => ({ ...p, [n]:[] }));
        runPhaseSSE(n, {
          onLog:   m  => setPhaseLogs(p => ({ ...p, [n]:[...(p[n] || []), m.text || m.line || ''] })),
          onDone:  m  => { setPhaseResults(p => ({ ...p, [n]:m.data || {} })); setRunning(null); refresh(); res(); },
          onError: e  => { setRunning(null); setErrors(p => ({ ...p, [n]:e.message })); refresh(); rej(e); },
        });
      }).catch(() => {});
    }
  };

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Agent</div>
        <div className="head-spacer"/>
        <button className="btn-ghost" onClick={() => api.post('/api/reset', {}).then(refresh)}>
          <Icon name="rotate-ccw" size={12}/> Reset
        </button>
        <button className="head-cta" onClick={runAll} disabled={!!running} style={{ marginLeft:8 }}>
          {running ? <><span className="spin"/> Running phase {running}…</> : <><Icon name="play" size={13} color="#fff"/> Run all phases</>}
        </button>
      </div>

      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="col-main">
          <div className="agent-hero">
            <div style={{ flex:1, minWidth:0 }}>
              <div className="agent-eyebrow">Autonomous mode</div>
              <h1 className="agent-h">Atlas runs your entire job&#8209;search pipeline.</h1>
              <p className="agent-p">From resume parsing to one-click applies, seven chained phases handle everything. Pause and inspect any step, or let it run end-to-end.</p>
            </div>
            <div className="agent-ring">
              <svg width="120" height="120" viewBox="0 0 150 150">
                <circle cx="75" cy="75" r={C} fill="none" strokeWidth="7" stroke="rgba(255,255,255,.06)"/>
                <circle cx="75" cy="75" r={C} fill="none" strokeWidth="7" stroke="var(--accent-h)" strokeLinecap="round"
                  strokeDasharray={circ} strokeDashoffset={off} style={{ transition:'stroke-dashoffset .8s' }}/>
              </svg>
              <div className="agent-ring-pct">
                <b>{pct}%</b>
                <span>{done.size}/7 phases</span>
              </div>
            </div>
          </div>

          <div className="phases">
            {[1,2,3,4,5,6,7].map(n => {
              const isDone = done.has(n);
              const isRun  = running === n;
              const err    = errors[n] || state?.error?.[n];
              const elapsed = state?.elapsed?.[n];
              const cls = isRun ? 'run' : err ? 'err' : isDone ? 'done' : '';

              return (
                <div key={n} className={'ph ' + cls}>
                  <div className="ph-hd" onClick={() => setOpen(o => ({ ...o, [n]:!o[n] }))}>
                    <div className="ph-num">{n}</div>
                    <div style={{ flex:1, minWidth:0 }}>
                      <div className="ph-name">{PHASE_INFO[n].n}</div>
                      <div className="ph-sub">{PHASE_INFO[n].s}</div>
                    </div>
                    {isRun  && <span className="ph-badge" style={{ background:'var(--warn-d)', color:'var(--warn)' }}>Running</span>}
                    {!isRun && isDone && <span className="ph-badge" style={{ background:'var(--good-d)', color:'var(--good)' }}>Done</span>}
                    {!isRun && !isDone && !err && <span className="ph-badge" style={{ background:'rgba(255,255,255,.04)', color:'var(--t3)' }}>Waiting</span>}
                    {!isRun && err && <span className="ph-badge" style={{ background:'var(--bad-d)', color:'var(--bad)' }}>Error</span>}
                    {elapsed != null && <span className="ph-elapsed">{elapsed.toFixed(1)}s</span>}
                    <button className="btn-ghost" style={{ marginLeft:6 }}
                      onClick={e => { e.stopPropagation(); startPhase(n, isDone); }}
                      disabled={!!running}>
                      <Icon name={isDone ? 'rotate-ccw' : 'play'} size={11}/>
                      {isDone ? 'Re-run' : 'Run'}
                    </button>
                    <span className={'ph-chev' + (open[n] ? ' open' : '')} style={{ marginLeft:6 }}>
                      <Icon name="chevron-down" size={14}/>
                    </span>
                  </div>
                  {isRun && <div className="ph-loading-bar"><div className="ph-loading-fill"/></div>}
                  {open[n] && (
                    <div className="ph-body fade-in">
                      <PhaseLog n={n} logs={phaseLogs[n]} running={isRun}/>
                      {err && <div className="err-block">Warning: {err}</div>}
                      {!err && isDone && (
                        <PhaseDetails n={n} data={phaseResults[n] || state?.phase_results?.[n] || state?.phase_results?.[String(n)] || {}} state={state} threshold={state?.threshold || 75}/>
                      )}
                      {!err && !isDone && !isRun && (
                        <div className="wait-state">Waiting for phase {n-1} to complete first.</div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   SETTINGS PAGE
══════════════════════════════════════════════════════════ */
function SettingsPage({ state, refresh }) {
  const [cfg, setCfg] = useState(state || {});
  const [saving, setSaving] = useState(false);
  const [ollamaModels, setOllamaModels] = useState([]);
  const [ollamaOk, setOllamaOk] = useState(null);

  const update = async (newCfg) => {
    setCfg(p => ({ ...p, ...newCfg }));
    setSaving(true);
    try {
      await api.post('/api/config', newCfg);
      refresh();
    } finally {
      setTimeout(() => setSaving(false), 600);
    }
  };

  useEffect(() => {
    if (cfg.mode !== 'ollama') return;
    api.get('/api/ollama/status').then(s => {
      setOllamaOk(s.running);
      setOllamaModels(s.models || []);
      if (s.models && s.models.length > 0 && !s.models.find(m => m.name === cfg.ollama_model)) {
        update({ ollama_model: s.models[0].name });
      }
    }).catch(() => setOllamaOk(false));
  }, [cfg.mode]);

  const Toggle = ({ field, label, sub }) => (
    <div className="set-row">
      <div style={{ flex:1 }}>
        <div className="set-label" style={{ marginBottom:2 }}>{label}</div>
        {sub && <div className="set-helper">{sub}</div>}
      </div>
      <button className={'set-toggle' + (cfg[field] ? ' on' : '')}
        onClick={() => update({ [field]: !cfg[field] })}/>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Settings</div>
        <div className="head-spacer"/>
        {saving && <div style={{ fontSize:12, color:'var(--accent-h)', marginRight:12, display:'flex', alignItems:'center', gap:6 }}><span className="spin"/> Saving…</div>}
      </div>

      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="settings-grid">
          {/* LLM Backend */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="cpu" size={14}/> LLM Provider</div>
            <div className="set-field">
              <div className="set-label">Model mode</div>
              <select className="set-select" value={cfg.mode} onChange={e => update({ mode: e.target.value })}>
                <option value="anthropic">Anthropic Claude (High quality)</option>
                <option value="ollama">Local Ollama (Free/Private)</option>
                <option value="demo">Demo mode (Offline/Template)</option>
              </select>
            </div>
            {cfg.mode === 'anthropic' && (
              <div className="set-field">
                <div className="set-label">Anthropic API Key</div>
                <input className="set-input" type="password" placeholder="sk-ant-…" value={cfg.api_key || ''}
                  onChange={e => update({ api_key: e.target.value })}/>
              </div>
            )}
            {cfg.mode === 'ollama' && (
              <div className="set-field">
                <div className="set-label">Ollama Model</div>
                {ollamaOk === false && (
                  <div className="set-helper" style={{ color:'#f87171', marginBottom:8 }}>
                    Ollama not reachable — run: <code>ollama serve</code>
                  </div>
                )}
                {ollamaModels.length > 0 ? (
                  <>
                    <select className="set-select" value={cfg.ollama_model || ''} 
                      onChange={e => update({ ollama_model: e.target.value })}>
                      {ollamaModels.map(m => (
                        <option key={m.name} value={m.name}>{m.name}</option>
                      ))}
                    </select>
                  </>
                ) : ollamaOk && (
                  <div className="set-helper" style={{ color:'#fbbf24' }}>
                    No models pulled — run: <code>ollama pull llama3.2</code>
                  </div>
                )}
              </div>
            )}
            <div className="set-field">
              <div className="set-row">
                <div className="set-label">LLM score limit</div>
                <span className="set-range-val">{cfg.llm_score_limit}</span>
              </div>
              <input type="range" className="set-range" min="1" max="50" value={cfg.llm_score_limit || 10}
                onChange={e => update({ llm_score_limit: parseInt(e.target.value) })}/>
              <div className="set-helper">Only top N jobs from fast-score will use LLM (saves time/cost).</div>
            </div>
          </div>

          {/* General User Settings */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="user" size={14}/> General Settings</div>
            <Toggle field="dark_mode" label="Dark mode" sub="Toggle dark mode theme."/>
            <Toggle field="email_notifications" label="Email notifications" sub="Receive updates on pipeline completion."/>
            <Toggle field="auto_export" label="Auto-export tracker" sub="Automatically save Excel tracker on every run."/>
          </div>

          {/* Account/Data */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="database" size={14}/> Data Management</div>
            <button className="btn-ghost" style={{ width:'100%', justifyContent:'flex-start', color:'var(--bad)' }} onClick={() => { if(confirm('Delete all data?')) { api.post('/api/reset', {}); refresh(); } }}>
              <Icon name="trash-2" size={14}/> Reset all data
            </button>
            <div className="set-helper" style={{ marginTop:8 }}>This will clear your resume, jobs, and all application data permanently.</div>
          </div>
          
          {/* Advanced */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="cpu" size={14}/> Advanced</div>
            <Toggle field="quick_score_only" label="Quick score only" sub="Skip LLM rubric scoring (faster, less accurate)."/>
            <Toggle field="force_dev_mode" label="Force Developer Mode" sub="Show Dev Ops tools regardless of connection origin."/>
          </div>

        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   FEEDBACK PAGE
══════════════════════════════════════════════════════════ */
function FeedbackPage({ refresh }) {
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!message.trim()) return;
    setSubmitting(true);
    try {
      await api.post('/api/feedback', { message });
      setSuccess(true);
      setMessage('');
      refresh?.();
    } catch (e) {
      alert(e.message || 'Failed to submit feedback');
    } finally {
      setSubmitting(false);
    }
  };

  if (success) {
    return (
      <div className="placeholder-page">
        <div className="placeholder-icon" style={{ background:'var(--good-d)', border:'1px solid var(--good-b)' }}>
          <Icon name="check" size={22} color="var(--good)"/>
        </div>
        <div style={{ fontSize:18, fontWeight:600 }}>Thank You</div>
        <div style={{ fontSize:13, color:'var(--t2)', maxWidth:400, textAlign:'center', lineHeight:1.55, marginTop:8 }}>
          Your feedback has been sent directly to the development team. We read every message and use it to improve Atlas.
        </div>
        <button className="btn-primary" style={{ marginTop:24 }} onClick={() => setSuccess(false)}>
          Send another message
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Feedback</div>
      </div>
      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="col-main">
          <div className="data-card" style={{ padding:32, maxWidth:600, margin:'0 auto', width:'100%' }}>
            <div style={{ textAlign:'center', marginBottom:32 }}>
              <div style={{ width:56, height:56, borderRadius:14, background:'var(--accent-d)', border:'1px solid var(--accent-b)', display:'flex', alignItems:'center', justifyContent:'center', margin:'0 auto 16px' }}>
                <Icon name="message-square" size={24} color="var(--accent-h)"/>
              </div>
              <h2 style={{ fontSize:20, fontWeight:700, color:'var(--t1)' }}>Tell us what you think</h2>
              <p style={{ fontSize:14, color:'var(--t2)', marginTop:8, lineHeight:1.6 }}>
                Have a feature request, found a bug, or just want to share your experience? We want to hear from you.
              </p>
            </div>

            <form onSubmit={handleSubmit}>
              <div className="set-field">
                <textarea 
                  className="profile-input profile-textarea" 
                  style={{ minHeight:150, padding:16 }}
                  placeholder="Your message..."
                  value={message}
                  onChange={e => setMessage(e.target.value)}
                  required
                />
              </div>
              <button 
                type="submit" 
                className="lp-btn-p" 
                style={{ width:'100%', marginTop:16, padding:'14px' }} 
                disabled={submitting || !message.trim()}
              >
                {submitting ? <span className="spin"/> : <Icon name="send" size={15}/>}
                {submitting ? 'Sending...' : 'Send Feedback'}
              </button>
            </form>
          </div>
        </div>
      </div>
    </>
  );
}

/* Dev overview */
function DevPage({ state: globalState, refresh: globalRefresh }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [fullState, setFullState] = useState(null);
  const [cli, setCli] = useState({ command:'git_status', output:'', running:false });
  const [tweaks, setTweaks] = useState(null);
  const [loadingFull, setLoadingFull] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const [next] = await Promise.all([
        api.get('/api/dev/overview'),
        globalRefresh?.(),
      ]);
      
      if (next.detail === 'Developer access denied' || next.error === 'Developer access denied') {
        setError(403);
      } else {
        setData(next);
        setTweaks(next.status?.tweaks || {});
        setError(null);
      }
    } catch (e) {
      console.error('Dev refresh failed:', e);
      setError(500);
    } finally {
      setTimeout(() => setRefreshing(false), 400);
    }
  }, [globalRefresh]);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (selected) {
      setLoadingFull(true);
      api.get(`/api/dev/session/${selected.id}`)
        .then(res => setFullState(res))
        .finally(() => setLoadingFull(false));
    } else {
      setFullState(null);
    }
  }, [selected]);

  const enableDev = async () => {
    await api.post('/api/config', { force_dev_mode: true, force_customer_mode: false });
    globalRefresh();
    refresh();
  };

  const impersonate = async (id) => {
    await api.post(`/api/dev/session/${id}/impersonate`, {});
    window.location.href = '/app';
  };

  const stopImpersonating = async () => {
    await api.post('/api/dev/session/stop-impersonating', {});
    window.location.href = '/app#dev';
    window.location.reload();
  };

  const testAsCustomer = async () => {
    await api.post('/api/config', { force_customer_mode: true, force_dev_mode: false });
    window.location.href = '/app';
  };

  const runCli = async command => {
    setCli({ command, output:'Running...', running:true });
    try {
      const res = await api.post('/api/dev/cli', { command });
      setCli({ command, output:res.output, running:false });
      refresh();
    } catch (e) {
      setCli({ command, output:'Command failed.', running:false });
    }
  };

  const saveTweaks = async patch => {
    const next = { ...(tweaks || {}), ...patch };
    setTweaks(next);
    const res = await api.post('/api/dev/tweaks', next);
    setTweaks(res.tweaks);
    applyDevTweaks(res.tweaks);
    refresh();
  };

  const summary = data?.summary || {};
  const status = data?.status || {};
  const sessions = data?.sessions || [];
  const active = selected || sessions[0];
  const commands = [
    ['git_status', 'Git'],
    ['recent_outputs', 'Outputs'],
    ['session_db', 'DB'],
    ['pip_freeze', 'Deps'],
  ];
  const accents = ['#5e6ad2', '#0ea5e9', '#14b8a6', '#f97316', '#e11d48'];

  if (error === 403) return (
    <div className="placeholder-page">
      <div className="placeholder-icon" style={{ background:'var(--warn-d)', border:'1px solid var(--warn-b)' }}>
        <Icon name="lock" size={22} color="var(--warn)"/>
      </div>
      <div style={{ fontSize:18, fontWeight:600 }}>Developer Access Required</div>
      <div style={{ fontSize:13, color:'var(--t2)', maxWidth:400, textAlign:'center', lineHeight:1.55, marginTop:8 }}>
        This page contains diagnostic tools and session data. Access is restricted to local connections or authorized developer sessions.
      </div>
      <button className="btn-primary" style={{ marginTop:24 }} onClick={enableDev}>
        Authorize this session
      </button>
    </div>
  );

  if (!data) return (
    <div className="placeholder-page">
      <span className="spin"/>
      <div style={{ color:'var(--t2)' }}>Loading dev console...</div>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Operations</div>
          <div className="page-title-big">Dev Overview</div>
        </div>
        <div className="head-spacer"/>
        {document.cookie.includes('dev_impersonate_id') && (
          <button className="btn-primary" style={{ marginRight:12, background:'var(--warn)', color:'#000' }} onClick={stopImpersonating}>
            <Icon name="user-minus" size={14}/> Stop Impersonating
          </button>
        )}
        <button className="btn-ghost" onClick={testAsCustomer} style={{ marginRight:12 }}>
          <Icon name="user" size={13}/> Test as Customer
        </button>
        <button className="btn-ghost" onClick={refresh} disabled={refreshing}>
          {refreshing ? <span className="spin" style={{marginRight:6, width:13, height:13}}/> : <Icon name="refresh-cw" size={13}/>} 
          {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>

      <div className="dev-wrap">
        <div className="dev-grid">
          <div className="dev-card dev-span">
            <div className="dev-kpis">
              <DevKpi label="Users" value={summary.users || 0} icon="users"/>
              <DevKpi label="Resumes" value={summary.with_resume || 0} icon="file-check-2"/>
              <DevKpi label="Applications" value={summary.applications || 0} icon="send"/>
              <DevKpi label="Applied" value={summary.applied || 0} icon="check-circle-2"/>
              <DevKpi label="Manual" value={summary.manual || 0} icon="hand"/>
              <DevKpi label="Errors" value={summary.errors || 0} icon="alert-triangle" warn={summary.errors > 0}/>
            </div>
          </div>

          <div className="dev-card">
            <div className="dev-card-h"><Icon name="activity" size={14}/> Site Status</div>
            <div className="dev-status">
              <div><span>App</span><b className="ok">{status.app}</b></div>
              <div><span>Python</span><b>{status.python}</b></div>
              <div><span>Output files</span><b>{status.output_files}</b></div>
              <div><span>Session files</span><b>{status.session_files}</b></div>
              <div><span>DB size</span><b>{status.session_db_mb} MB</b></div>
              <div><span>Free disk</span><b>{status.disk_free_gb} GB</b></div>
            </div>
          </div>

          <div className="dev-card">
            <div className="dev-card-h"><Icon name="wand-sparkles" size={14}/> Site Tweaks</div>
            <div className="dev-tweak-row">
              {accents.map(color => (
                <button key={color} className="dev-swatch" style={{ background:color }} onClick={() => saveTweaks({ accent:color })} title={color}/>
              ))}
            </div>
            <div className="set-field">
              <div className="set-label">Density</div>
              <select className="set-select" value={tweaks?.density || 'comfortable'} onChange={e => saveTweaks({ density:e.target.value })}>
                <option value="compact">Compact</option>
                <option value="comfortable">Comfortable</option>
                <option value="spacious">Spacious</option>
              </select>
            </div>
            <div className="set-field">
              <div className="set-label">Experiment mode</div>
              <select className="set-select" value={tweaks?.experiment || 'standard'} onChange={e => saveTweaks({ experiment:e.target.value })}>
                <option value="standard">Standard</option>
                <option value="focus">Focus</option>
                <option value="command">Command</option>
                <option value="launch">Launch</option>
              </select>
            </div>
            <div className="set-row">
              <div>
                <div className="set-label">Top banner</div>
                <div className="set-helper">Use the dev banner as the site-wide strip.</div>
              </div>
              <button className={'set-toggle' + (tweaks?.show_promo !== false ? ' on' : '')} onClick={() => saveTweaks({ show_promo: tweaks?.show_promo === false })}/>
            </div>
            <input className="set-input" value={tweaks?.dev_banner || ''} onChange={e => setTweaks({ ...(tweaks || {}), dev_banner:e.target.value })} onBlur={e => saveTweaks({ dev_banner:e.target.value })} placeholder="Dev banner"/>
          </div>

          <div className="dev-card dev-users">
            <div className="dev-card-h"><Icon name="users" size={14}/> Users</div>
            <div className="dev-user-list">
              {sessions.map(s => (
                <button key={s.id} className={'dev-user' + (active?.id === s.id ? ' active' : '')} onClick={() => setSelected(s)}>
                  <span className="user-avatar">{(s.name || 'U')[0]}</span>
                  <span>
                    <b>{s.name || 'Anonymous'}</b>
                    <small>{s.email || s.resume_filename || s.id.slice(0, 10)}</small>
                  </span>
                  <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:4 }}>
                    <em>{s.done.length}/7</em>
                    {s.unread_feedback_count > 0 && (
                      <span title="Unread feedback message" style={{ display:'flex', alignItems:'center', gap:4, background:'var(--warn-d)', color:'var(--warn)', padding:'2px 6px', borderRadius:10, fontSize:10, fontWeight:600 }}>
                        <Icon name="message-square" size={10}/> {s.unread_feedback_count}
                      </span>
                    )}
                  </div>
                </button>
              ))}
            </div>
          </div>

          <div className="dev-card dev-span">
            <div className="dev-card-h" style={{ display:'flex', justifyContent:'space-between', alignItems:'center' }}>
              <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                <Icon name="user-cog" size={14}/> Selected User Detail
              </div>
              {active && (
                <div style={{ display:'flex', gap:8 }}>
                  <button className="btn-ghost" style={{ fontSize:11, padding:'4px 10px', color:'var(--warn)' }} 
                    onClick={async () => { if(confirm('Reset this session state? Files will be deleted.')) { await api.post(`/api/dev/session/${active.id}/reset`, {}); refresh(); setSelected(null); } }}>
                    <Icon name="rotate-ccw" size={12}/> Reset Session
                  </button>
                  <button className="btn-ghost" style={{ fontSize:11, padding:'4px 10px', color:'var(--bad)' }}
                    onClick={async () => { if(confirm('Delete this user entirely? This cannot be undone.')) { await fetch(`/api/dev/session/${active.id}`, { method:'DELETE' }); refresh(); setSelected(null); } }}>
                    <Icon name="trash-2" size={12}/> Delete Session
                  </button>
                  <button className="btn-primary" style={{ fontSize:11, padding:'4px 10px' }} onClick={() => impersonate(active.id)}>
                    <Icon name="user-plus" size={12}/> View site as {active.name || 'this user'}
                  </button>
                </div>
              )}
            </div>
            {active ? (
              <div className="dev-inspect-grid">
                <div className="inspect-sec">
                  <h4>Stats & Pipeline</h4>
                  <div className="dev-status">
                    <div><span>ID</span><code style={{ fontSize:10 }}>{active.id}</code></div>
                    <div><span>Resume</span><b>{active.has_resume ? 'yes' : 'no'}</b></div>
                    <div><span>Target</span><b>{active.target || '-'}</b></div>
                    <div><span>Jobs</span><b>{active.job_count}</b></div>
                    <div><span>Scored</span><b>{active.scored_count}</b></div>
                    <div><span>Apps</span><b>{active.application_count}</b></div>
                    <div><span>Applied</span><b>{active.applied_count}</b></div>
                  </div>
                  <div className="dev-phases" style={{ marginTop:12 }}>
                    {[1,2,3,4,5,6,7].map(n => <span key={n} className={active.done.includes(n) ? 'on' : ''}>{n}</span>)}
                  </div>
                </div>

                <div className="inspect-sec" style={{ gridRow:'span 2' }}>
                  <h4>User Feedback</h4>
                  <div className="dev-terminal" style={{ height:'100%', fontSize:12, background:'var(--bg-1)' }}>
                    {loadingFull ? 'Loading...' : (
                      (fullState?.feedback || []).length > 0 ? (
                        <div style={{ display:'flex', flexDirection:'column', gap:12 }}>
                          {fullState.feedback.map(f => (
                            <div key={f.id} style={{ background:'var(--bg-2)', border:'1px solid var(--bdr)', borderRadius:8, padding:12 }}>
                              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:8, color:'var(--t3)', fontSize:11, fontFamily:'var(--mono)' }}>
                                <span>{new Date(f.created_at).toLocaleString()}</span>
                                {!f.read && <span style={{ background:'var(--warn)', color:'#000', padding:'2px 6px', borderRadius:4, fontWeight:700 }}>NEW</span>}
                              </div>
                              <div style={{ color:'var(--t1)', whiteSpace:'pre-wrap', lineHeight:1.5 }}>{f.message}</div>
                            </div>
                          ))}
                          {fullState.feedback.some(f => !f.read) && (
                            <button className="btn-ghost" style={{ marginTop:8 }} onClick={async () => {
                              await api.post(`/api/dev/session/${active.id}/feedback/read`, {});
                              api.get(`/api/dev/session/${active.id}`).then(setFullState);
                              refresh();
                            }}>
                              <Icon name="check-check" size={13}/> Mark all as read
                            </button>
                          )}
                        </div>
                      ) : <div style={{ color:'var(--t3)' }}>No feedback from this user.</div>
                    )}
                  </div>
                </div>

                <div className="inspect-sec">
                  <h4>Resume Text</h4>
                  <div className="dev-terminal" style={{ height:200, fontSize:11 }}>
                    {loadingFull ? 'Loading...' : (fullState?.resume_text || 'No resume uploaded.')}
                  </div>
                </div>

                <div className="inspect-sec">
                  <h4>Full State JSON</h4>
                  <div className="dev-terminal" style={{ height:200, fontSize:11 }}>
                    {loadingFull ? 'Loading...' : JSON.stringify(fullState, null, 2)}
                  </div>
                </div>
              </div>
            ) : <div className="set-helper">No session selected.</div>}
          </div>

          <div className="dev-card dev-span">
            <div className="dev-card-h"><Icon name="terminal" size={14}/> CLI Output</div>
            <div className="dev-cli-actions">
              {commands.map(([id, label]) => <button key={id} className="btn-ghost" disabled={cli.running} onClick={() => runCli(id)}>{label}</button>)}
            </div>
            <pre className="dev-terminal">{cli.output || 'Run a safe command to inspect the app.'}</pre>
          </div>

          <div className="dev-card dev-span">
            <div className="dev-card-h"><Icon name="list-tree" size={14}/> Recent Events</div>
            <div className="dev-events">
              {(data.events || []).slice(0, 80).map((e, i) => (
                <div key={i} className="dev-event">
                  <span>{new Date(e.ts).toLocaleTimeString()}</span>
                  <b>{e.kind}</b>
                  <p>{e.message}</p>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

function DevKpi({ label, value, icon, warn }) {
  return (
    <div className={'dev-kpi' + (warn ? ' warn' : '')}>
      <Icon name={icon} size={15}/>
      <span>{label}</span>
      <b>{value}</b>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   ROOT
══════════════════════════════════════════════════════════ */
function AuthPage({ onAuth }) {
  const [mode, setMode] = useState('login'); // 'login' | 'signup'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
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
        setError(res.error || 'Authentication failed');
      }
    } catch (err) {
      setError(err.message || 'An error occurred. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  const handleGoogle = async () => {
    setError(null);
    try {
      const res = await api.get('/api/auth/google');
      if (res.url) {
        window.location.href = res.url;
      } else {
        throw new Error('No redirect URL received');
      }
    } catch (err) {
      console.error('Google Auth Error:', err);
      setError(err.message || 'Could not initialize Google login');
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <BrandMark />
        <h2>{mode === 'login' ? 'Sign in to your account' : 'Create your account'}</h2>
        <p className="auth-sub">{mode === 'login' ? 'Welcome back! Please enter your details.' : 'Start your automated job search today.'}</p>
        
        <button className="auth-google" onClick={handleGoogle}>
          <img src="https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg" alt="Google" width="18"/>
          Continue with Google
        </button>

        <div className="auth-divider"><span>OR</span></div>

        <form className="auth-form" onSubmit={handleSubmit}>
          <div className="set-field">
            <label className="set-label">Email address</label>
            <input type="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="name@company.com" required/>
          </div>
          <div className="set-field">
            <label className="set-label">Password</label>
            <input type="password" value={password} onChange={e => setPassword(e.target.value)} placeholder="••••••••" required/>
          </div>
          
          {error && <div className="auth-error">{error}</div>}
          
          <button className="lp-btn-p" style={{ width:'100%', marginTop:12, padding:'14px' }} disabled={loading}>
            {loading ? <span className="spin"/> : (mode === 'login' ? 'Sign in' : 'Create account')}
          </button>
        </form>

        <div className="auth-switch">
          {mode === 'login' ? "Don't have an account?" : "Already have an account?"}{' '}
          <button onClick={() => setMode(mode === 'login' ? 'signup' : 'login')}>
            {mode === 'login' ? 'Sign up' : 'Sign in'}
          </button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [state,     setState]     = useState(null);
  const [page,      setPage]      = useState(() => location.hash === '#dev' ? 'dev' : 'home');
  const [showPromo, setShowPromo] = useState(true);
  const [booted,    setBooted]    = useState(false);
  const prefetchStarted = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const next = await api.get('/api/state');
      setState(next);
      applyDevTweaks(next.dev_tweaks);
    }
    catch (err) {}
    finally { setBooted(true); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => {
    const id = setInterval(refresh, 8000);
    return () => clearInterval(id);
  }, [refresh]);

  useEffect(() => {
    if (!state?.has_resume || state?.scored_summary || prefetchStarted.current || page === 'jobs') return;
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

  if (!booted) return (
    <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:'100vh', color:'var(--t3)', fontSize:13 }}>
      <span className="spin" style={{ marginRight:8 }}/> Loading workspace…
    </div>
  );

  /* Auth Gate */
  if (!state?.user && page !== 'home' && page !== 'dev' && !state?.is_dev) {
    return <AuthPage onAuth={refresh} />;
  }

  /* Onboarding gate */
  if (!state?.has_resume && page !== 'dev') {
    return (
      <div style={{ display:'flex', flexDirection:'column', height:'100vh', background:'var(--bg)' }}>
        <Onboarding onLoaded={refresh} isDev={state?.is_dev} setPage={setPage}/>
      </div>
    );
  }

  const counts = {
    jobs:    state?.scored_summary?.total || null,
    applied: (state?.applications || []).length || null,
  };

  const pageEl = (() => {
    switch (page) {
      case 'home':      return <Dashboard state={state} setPage={setPage}/>;
      case 'jobs':      return <JobsPage state={state} refresh={refresh} setPage={setPage}/>;
      case 'resume':    return <ResumePage state={state} refresh={refresh} setPage={setPage}/>;
      case 'profile':   return <ProfilePage state={state} refresh={refresh} setPage={setPage}/>;
      case 'agent':     return <AgentPage state={state} refresh={refresh}/>;
      case 'dev':       return <DevPage state={state} refresh={refresh}/>;
      case 'feedback':  return <FeedbackPage refresh={refresh}/>;
      case 'settings':  return <SettingsPage state={state} refresh={refresh}/>;
      case 'auth':      return <AuthPage onAuth={() => { refresh(); setPage('home'); }} />;
      default:          return <Dashboard state={state} setPage={setPage}/>;
    }
  })();

  const handleLogout = async () => {
    await api.post('/api/auth/logout', {});
    window.location.href = '/app#auth';
    window.location.reload();
  };

  return (
    <div className="shell">
      {/* Brand cell — clicking logo goes home */}
      <div className="brand-cell">
        <BrandMark onClick={() => window.location.href = '/'}/>
      </div>

      {/* Promo strip (dismissable) */}
      {showPromo && state?.dev_tweaks?.show_promo !== false ? (
        <PromoStrip onClose={() => setShowPromo(false)} text={state?.dev_tweaks?.dev_banner}/>
      ) : (
        <div style={{ gridArea:'promo', background:'var(--bg-1)', borderBottom:'1px solid var(--bdr)' }}/>
      )}

      <Rail page={page} setPage={setPage} counts={counts} isDev={state?.is_dev} onLogout={handleLogout}/>

      <main className="main">{pageEl}</main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
