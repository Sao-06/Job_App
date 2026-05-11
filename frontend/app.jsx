/* JobsAI — app.jsx
   Multi-page dark SPA. Purple/indigo brand colors.
   All API contracts preserved: /api/state, /api/phase/*, /api/resume/*, /api/config, /api/reset
*/
const { useState, useEffect, useRef, useCallback, useMemo } = React;

/* ── API ──
   All requests carry a wall-clock timeout via AbortController. Without it,
   a hung server (e.g. restart warm-up while the ingestion backfill saturates
   SQLite) leaves the JobsPage spinner stuck indefinitely — the user can't
   tell the difference between "loading" and "broken". 30s is generous for
   any normal request and short enough that the empty-state takes over before
   the user gives up. */
const _withTimeout = (ms = 30000) => {
  if (typeof AbortController === 'undefined') return { signal: undefined, cancel: () => {} };
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), ms);
  return { signal: ctrl.signal, cancel: () => clearTimeout(timer) };
};
const _handle = async r => {
  let data;
  try { data = await r.json(); }
  catch (_) { data = {}; }
  if (!r.ok) throw new Error(data.detail || data.error || data.message || `API ${r.status}`);
  return data;
};
const api = {
  get: (url, { timeoutMs = 30000 } = {}) => {
    const t = _withTimeout(timeoutMs);
    return fetch(url, { signal: t.signal }).then(_handle).finally(t.cancel);
  },
  post: (url, body, { timeoutMs = 30000 } = {}) => {
    const t = _withTimeout(timeoutMs);
    return fetch(url, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body), signal: t.signal,
    }).then(_handle).finally(t.cancel);
  },
  upload: (url, file, { timeoutMs = 120000 } = {}) => {
    const t = _withTimeout(timeoutMs);
    const fd = new FormData(); fd.append('file', file);
    return fetch(url, { method:'POST', body:fd, signal: t.signal })
      .then(_handle).finally(t.cancel);
  },
  delete: (url, { timeoutMs = 30000 } = {}) => {
    const t = _withTimeout(timeoutMs);
    return fetch(url, { method:'DELETE', signal: t.signal })
      .then(async r => {
        if (r.status === 204) return { ok: true };
        return _handle(r);
      })
      .finally(t.cancel);
  },
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

/* ── Company logo (Clearbit → Google favicon → letter fallback) ── */
const COMPANY_DOMAIN_OVERRIDES = {
  'meta':'meta.com', 'meta platforms':'meta.com', 'facebook':'meta.com',
  'alphabet':'abc.xyz', 'google':'google.com', 'youtube':'youtube.com',
  'x':'x.com', 'twitter':'x.com',
  'amazon web services':'aws.amazon.com', 'aws':'aws.amazon.com',
  'jpmorgan chase':'jpmorganchase.com', 'jp morgan':'jpmorganchase.com',
  'goldman sachs':'goldmansachs.com',
  'tsmc':'tsmc.com', 'samsung semiconductors':'samsung.com',
  'micron technology':'micron.com', 'microchip technology':'microchip.com',
  'curtiss-wright corporation':'curtisswright.com',
  'pennsylvania state university':'psu.edu',
  'two six technologies':'twosixtech.com',
  'sider & byers associates':'sba-eng.com',
  'coherent corp':'coherent.com', 'coherent corp.':'coherent.com',
  'enovis':'enovis.com',
  'cesium astro':'cesiumastro.com', 'cesiumastro':'cesiumastro.com',
  'apptronik':'apptronik.com',
  'two-six technologies':'twosixtech.com',
  'simplifyjobs':'simplify.jobs',
  // Cross-industry sampler used by TrendingMarquee fallback — slugs that don't auto-resolve
  'mayo clinic':'mayoclinic.org',
  'procter & gamble':'pg.com', 'p&g':'pg.com',
  'johnson & johnson':'jnj.com',
  'coca-cola':'coca-cola.com', 'coca cola':'coca-cola.com',
};
function companyDomain(raw) {
  if (!raw) return null;
  let s = String(raw).trim().toLowerCase();
  if (!s || s === 'nan' || s === '?') return null;
  if (COMPANY_DOMAIN_OVERRIDES[s]) return COMPANY_DOMAIN_OVERRIDES[s];
  // strip noise: "(...)" suffixes and corp-suffix words
  s = s.replace(/\s*\([^)]*\)\s*/g, ' ').trim();
  s = s.replace(/[,\.—–].*$/, '').trim();
  s = s.replace(/\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|sa|s\.a\.|plc|holdings|group|labs?|technologies|technology)\.?\s*$/i, '').trim();
  if (COMPANY_DOMAIN_OVERRIDES[s]) return COMPANY_DOMAIN_OVERRIDES[s];
  // collapse whitespace and non-letters into nothing
  const slug = s.replace(/&/g, 'and').replace(/[^a-z0-9]+/g, '');
  if (!slug) return null;
  return slug + '.com';
}

const _logoFailed = new Set();    // domains that errored (avoid retry flicker)
function CompanyLogo({ company, className = '', fallbackVariant = 'v1', size = 38 }) {
  const domain = useMemo(() => companyDomain(company), [company]);
  const initial = (String(company || '?').trim().charAt(0) || '?').toUpperCase();
  const initiallyFailed = !domain || _logoFailed.has(domain);
  const [step, setStep] = useState(initiallyFailed ? 2 : 0);
  // step 0 = Clearbit, 1 = Google favicon, 2 = letter fallback
  useEffect(() => { setStep(initiallyFailed ? 2 : 0); }, [domain, initiallyFailed]);

  if (step === 2) {
    return (
      <div className={'co-logo ' + fallbackVariant + ' ' + className}
           style={{ width: size, height: size, fontSize: Math.max(11, Math.round(size * 0.38)) }}>
        {initial}
      </div>
    );
  }
  const src = step === 0
    ? `https://logo.clearbit.com/${domain}?size=128`
    : `https://www.google.com/s2/favicons?domain=${domain}&sz=64`;
  return (
    <div className={'co-logo logo-img ' + className}
         style={{ width: size, height: size }}
         title={company || ''}>
      <img src={src} alt=""
           loading="lazy"
           referrerPolicy="no-referrer"
           onError={() => {
             if (step === 0) setStep(1);
             else { _logoFailed.add(domain); setStep(2); }
           }}/>
    </div>
  );
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

/* ── Inline Markdown renderer ──────────────────────────────────────────
   Handles the subset of Markdown the Anthropic / Ollama responses use:
     # / ## / ### headings
     - / *   bullet lists       1. 2. 3. ordered lists
     **bold**  __bold__          *italic*  _italic_
     `code`                       ```code blocks```
     [text](url)                  raw http(s)://… links
     > blockquote                 paragraph breaks on blank lines

   No external dep — the SPA is Babel-transpiled in-browser, so we keep
   it small. Streaming partial messages render incrementally because the
   parser is pure (no state outside the input string).
*/
function _mdInlineSpans(text, keyPrefix) {
  // Inline pass: code, bold, italic, links. Token-by-token so we don't
  // accidentally bold inside code spans.
  const out = [];
  let cursor = 0;
  let key = 0;
  const push = (node) => out.push(typeof node === 'string'
    ? <React.Fragment key={`${keyPrefix}-${key++}`}>{node}</React.Fragment>
    : React.cloneElement(node, { key: `${keyPrefix}-${key++}` }));

  // Match (in order of priority): inline `code`, **bold**/__bold__,
  //   *italic*/_italic_, [text](url), bare URL.
  const RE = /(`[^`]+`)|(\*\*[^*\n]+?\*\*|__[^_\n]+?__)|(\*[^*\s][^*\n]*?\*|_[^_\s][^_\n]*?_)|(\[[^\]]+\]\(https?:\/\/[^\s)]+\))|(https?:\/\/[^\s)]+)/g;
  let m;
  while ((m = RE.exec(text)) !== null) {
    if (m.index > cursor) push(text.slice(cursor, m.index));
    if (m[1]) {
      push(<code className="md-code">{m[1].slice(1, -1)}</code>);
    } else if (m[2]) {
      const inner = m[2].startsWith('**') ? m[2].slice(2, -2) : m[2].slice(2, -2);
      push(<strong>{inner}</strong>);
    } else if (m[3]) {
      const inner = m[3].slice(1, -1);
      push(<em>{inner}</em>);
    } else if (m[4]) {
      const lm = m[4].match(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/);
      if (lm) push(<a href={lm[2]} target="_blank" rel="noopener noreferrer">{lm[1]}</a>);
    } else if (m[5]) {
      push(<a href={m[5]} target="_blank" rel="noopener noreferrer">{m[5]}</a>);
    }
    cursor = m.index + m[0].length;
  }
  if (cursor < text.length) push(text.slice(cursor));
  return out;
}

function Markdown({ text }) {
  if (!text) return null;
  const lines = String(text).replace(/\r\n/g, '\n').split('\n');
  const blocks = [];
  let i = 0;
  let blockIdx = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Skip blank lines (paragraph separators)
    if (!line.trim()) { i++; continue; }

    // Fenced code block ``` … ```
    if (/^```/.test(line)) {
      const lang = line.replace(/^```/, '').trim();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i]); i++;
      }
      if (i < lines.length) i++;     // consume closing fence
      blocks.push(
        <pre key={`b${blockIdx++}`} className="md-pre">
          <code className={lang ? `md-lang-${lang}` : undefined}>{buf.join('\n')}</code>
        </pre>
      );
      continue;
    }

    // Headings: # / ## / ### / ####
    const h = line.match(/^(#{1,4})\s+(.+?)\s*#*\s*$/);
    if (h) {
      const level = h[1].length;
      const Tag = `h${Math.min(6, level + 1)}`;   // h2..h5 inside chat bubble
      blocks.push(
        <Tag key={`b${blockIdx++}`} className={`md-h md-h${level}`}>
          {_mdInlineSpans(h[2], `b${blockIdx}`)}
        </Tag>
      );
      i++; continue;
    }

    // Blockquote run (> text, > text, …)
    if (/^>\s?/.test(line)) {
      const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, '')); i++;
      }
      blocks.push(
        <blockquote key={`b${blockIdx++}`} className="md-quote">
          {_mdInlineSpans(buf.join(' '), `b${blockIdx}`)}
        </blockquote>
      );
      continue;
    }

    // Unordered list (- foo / * foo) — peel off contiguous lines
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        const t = lines[i].replace(/^\s*[-*]\s+/, '');
        items.push(<li key={`li${i}`}>{_mdInlineSpans(t, `li${i}`)}</li>);
        i++;
      }
      blocks.push(<ul key={`b${blockIdx++}`} className="md-ul">{items}</ul>);
      continue;
    }

    // Ordered list (1. foo / 2. foo)
    if (/^\s*\d+\.\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        const t = lines[i].replace(/^\s*\d+\.\s+/, '');
        items.push(<li key={`oli${i}`}>{_mdInlineSpans(t, `oli${i}`)}</li>);
        i++;
      }
      blocks.push(<ol key={`b${blockIdx++}`} className="md-ol">{items}</ol>);
      continue;
    }

    // Paragraph: collect contiguous non-blank, non-list, non-heading lines
    const buf = [line];
    i++;
    while (i < lines.length
        && lines[i].trim()
        && !/^```|^#{1,4}\s|^\s*[-*]\s|^\s*\d+\.\s|^>/.test(lines[i])) {
      buf.push(lines[i]); i++;
    }
    blocks.push(
      <p key={`b${blockIdx++}`} className="md-p">
        {_mdInlineSpans(buf.join(' '), `b${blockIdx}`)}
      </p>
    );
  }

  return <div className="md">{blocks}</div>;
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
  return (
    <div className="promo-cell">
      <span>{text}</span>
      <button className="promo-close" onClick={onClose}><Icon name="x" size={13}/></button>
    </div>
  );
}

/* ── Hash-based routing ─────────────────────────────────────
   Each page has its own URL hash so the browser back/forward
   buttons work, refresh restores the same page, and links
   like /app#jobs go directly to a page.

   Home is the canonical empty hash so /app and /app#home both
   work (we never write #home — only read it). */
const VALID_PAGES = new Set([
  'home', 'jobs', 'resume', 'documents', 'profile', 'agent', 'dev',
  'feedback', 'settings', 'plans', 'auth',
]);
function pageFromHash() {
  if (typeof location === 'undefined') return 'home';
  const h = (location.hash || '').replace(/^#/, '').split(/[?/]/)[0];
  if (!h) return 'home';
  return VALID_PAGES.has(h) ? h : 'home';
}
function hashFromPage(page) {
  return page && page !== 'home' ? '#' + page : '';
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
// Documents lives in the utility section right under Plans because it's a
// library / archive surface, not a primary workflow step. The pipeline is
// the workflow (Home → Jobs → Resume → Profile → Agent); Documents is
// where everything the pipeline produces ends up.
const NAV_UTIL = [
  { id:'plans',     label:'Plans',     icon:'gem' },
  { id:'documents', label:'Documents', icon:'folder-open' },
  { id:'feedback',  label:'Feedback',  icon:'circle-help' },
  { id:'settings',  label:'Settings',  icon:'settings' },
  { id:'logout',    label:'Sign out',  icon:'log-out' },
];

function Rail({ page, setPage, counts, isDev, onLogout, navOpen, closeNav }) {
  // Selecting any item closes the mobile drawer. Desktop ignores closeNav
  // because the rail isn't transformed there.
  const select = (id) => { setPage(id); closeNav?.(); };
  const utilSelect = (it) => {
    if (it.id === 'logout') onLogout();
    else setPage(it.id);
    closeNav?.();
  };
  return (
    <aside className={'rail' + (navOpen ? ' is-open' : '')}>
      <nav className="rail-nav">
        {NAV.filter(it => it.id !== 'dev' || isDev).map(it => (
          <div key={it.id}
               className={'rail-item' + (page === it.id ? ' active' : '')}
               onClick={() => select(it.id)}>
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
               className={'rail-item' + (page === it.id ? ' active' : '')
                        + (it.id === 'plans' ? ' rail-item-plans' : '')}
               onClick={() => utilSelect(it)}>
            <span className="rail-icon"><Icon name={it.icon} size={15}/></span>
            <span className="lbl">{it.label}</span>
            {/* Plans gets a subtle gold "Pro" glint and a shimmer sweep so
                it actually catches the eye in the rail-bottom utility row. */}
            {it.id === 'plans' && <span className="rail-plans-glint" aria-hidden="true"/>}
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

function CountUp({ to, duration = 900, suffix = '' }) {
  const [n, setN] = useState(0);
  useEffect(() => {
    const start = performance.now();
    let raf;
    const tick = (t) => {
      const p = Math.min(1, (t - start) / duration);
      const eased = 1 - Math.pow(1 - p, 3);
      setN(to * eased);
      if (p < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [to, duration]);
  return <>{Math.round(n)}{suffix}</>;
}

function Sparkline({ values, color = 'var(--accent-h)', w = 88, h = 24 }) {
  if (!values || values.length < 2) values = [0, 0];
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = Math.max(1, max - min);
  const step = w / (values.length - 1);
  const pts = values.map((v, i) => [i * step, h - ((v - min) / span) * (h - 2) - 1]);
  const d = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
  const area = d + ` L ${w},${h} L 0,${h} Z`;
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} style={{ display:'block' }}>
      <path d={area} fill={color} opacity=".12"/>
      <path d={d} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
      <circle cx={pts[pts.length-1][0]} cy={pts[pts.length-1][1]} r="2.5" fill={color}/>
    </svg>
  );
}

function ScoreHisto({ jobs }) {
  const buckets = [
    { label: '90+',   range: [90, 101], color: 'var(--good)',     n: 0 },
    { label: '80–89', range: [80, 90],  color: 'var(--good)',     n: 0 },
    { label: '70–79', range: [70, 80],  color: 'var(--accent-h)', n: 0 },
    { label: '60–69', range: [60, 70],  color: 'var(--t3)',       n: 0 },
    { label: '<60',   range: [0,  60],  color: 'var(--t4)',       n: 0 },
  ];
  jobs.forEach(j => {
    const s = Math.round(j.score || 0);
    const b = buckets.find(b => s >= b.range[0] && s < b.range[1]);
    if (b) b.n++;
  });
  const max = Math.max(1, ...buckets.map(b => b.n));
  const total = jobs.length || 1;
  return (
    <div className="hc-bars">
      {buckets.map((b, i) => (
        <div key={i} className="hc-bar-row">
          <div className="hc-bar-lbl">{b.label}</div>
          <div className="hc-bar-track">
            <div className="hc-bar-fill"
              style={{
                width: `${(b.n / max) * 100}%`,
                background: `linear-gradient(90deg, ${b.color}, ${b.color}99)`,
                boxShadow: `0 0 12px ${b.color}66`,
                animationDelay: `${i * 80}ms`,
              }}/>
            <span className="hc-bar-n">{b.n}</span>
          </div>
          <div className="hc-bar-pct">{Math.round((b.n / total) * 100)}%</div>
        </div>
      ))}
    </div>
  );
}

function SkillDonut({ profileSkills, jobs }) {
  const skills = (profileSkills || []).slice(0, 12).map(s => String(s).toLowerCase());
  const corpus = jobs.map(j => String(j.skills || '').toLowerCase()).join(' | ');
  const matched = skills.filter(s => s && corpus.includes(s)).length;
  const pct = skills.length ? Math.round((matched / skills.length) * 100) : 0;
  const C = 44, circ = 2 * Math.PI * C;
  const off = circ - (circ * pct / 100);
  return (
    <div className="donut-wrap">
      <div className="donut-svg">
        <svg width="140" height="140" viewBox="0 0 120 120">
          <defs>
            <linearGradient id="donutGrad" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%"  stopColor="#7c5cff"/>
              <stop offset="55%" stopColor="#ff3d9a"/>
              <stop offset="100%" stopColor="#22e5ff"/>
            </linearGradient>
          </defs>
          <circle cx="60" cy="60" r={C} fill="none" stroke="var(--bdr)" strokeWidth="10"/>
          <circle cx="60" cy="60" r={C} fill="none" stroke="url(#donutGrad)" strokeWidth="10"
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={off}
            transform="rotate(-90 60 60)"
            style={{ transition: 'stroke-dashoffset 1.2s cubic-bezier(.16,1,.3,1)' }}/>
        </svg>
        <div className="donut-center">
          <b><CountUp to={pct} suffix="%"/></b>
          <span>coverage</span>
        </div>
      </div>
      <div className="donut-meta">
        <div><b>{matched}</b><span>matched</span></div>
        <div><b>{Math.max(0, skills.length - matched)}</b><span>untouched</span></div>
        <div><b>{skills.length}</b><span>tracked</span></div>
      </div>
    </div>
  );
}

function ActivityFeed({ state }) {
  const items = useMemo(() => {
    const out = [];
    const apps = state?.applications || [];
    apps.slice(-4).reverse().forEach((a) => {
      out.push({
        type: a.status === 'Applied' ? 'apply' : 'tailor',
        title: a.role || a.title || 'Application',
        sub: a.co || a.company || '—',
        time: a.date_applied || 'Today',
        score: Math.round(a.score || 0),
      });
    });
    const jobs = state?.scored_summary?.jobs || [];
    jobs.slice(0, 3).forEach(j => {
      out.push({
        type: 'match',
        title: j.role || 'New match',
        sub: j.co || '—',
        time: 'Just now',
        score: Math.round(j.score || 0),
      });
    });
    if (state?.done?.length) {
      out.push({
        type: 'phase',
        title: `Phase ${Math.max(...state.done)} completed`,
        sub: 'Pipeline checkpoint',
        time: 'Recent',
        score: null,
      });
    }
    return out.slice(0, 6);
  }, [state]);

  const icon = { apply: 'send', tailor: 'wand-2', match: 'sparkles', phase: 'check-circle-2' };
  const tone = { apply: 'good', tailor: 'accent', match: 'accent', phase: 'good' };

  if (!items.length) {
    return <div className="feed-empty">
      <Icon name="radio" size={18}/>
      <span>No activity yet — run discovery to see your pipeline come alive.</span>
    </div>;
  }
  return (
    <div className="feed">
      <div className="feed-rail"/>
      {items.map((it, i) => (
        <div key={i} className="feed-row" style={{ animationDelay:`${i*70}ms` }}>
          <div className={'feed-dot tone-' + tone[it.type]}>
            <Icon name={icon[it.type]} size={11}/>
          </div>
          <div className="feed-body">
            <div className="feed-title">{it.title}</div>
            <div className="feed-sub">{it.sub} · <em>{it.time}</em></div>
          </div>
          {it.score != null && <div className="feed-score">{it.score}</div>}
        </div>
      ))}
    </div>
  );
}

// Cross-industry sampler shown when the user hasn't surfaced enough real listings yet.
// Spans tech, finance, healthcare, media, industrials, consumer, hospitality, and more —
// Jobs AI works for any field, not just tech.
const INDUSTRY_SAMPLER = [
  { c:'Microsoft',        industry:'Software'      },
  { c:'JPMorgan Chase',   industry:'Finance'       },
  { c:'Pfizer',           industry:'Pharma'        },
  { c:'Disney',           industry:'Media'         },
  { c:'Boeing',           industry:'Aerospace'     },
  { c:'Nike',             industry:'Consumer'      },
  { c:'Tesla',            industry:'Automotive'    },
  { c:'McKinsey',         industry:'Consulting'    },
  { c:'Marriott',         industry:'Hospitality'   },
  { c:'Coca-Cola',        industry:'Beverages'     },
  { c:'Mayo Clinic',      industry:'Healthcare'    },
  { c:'Lockheed Martin',  industry:'Defense'       },
  { c:'Netflix',          industry:'Streaming'     },
  { c:'Procter & Gamble', industry:'CPG'           },
  { c:'BlackRock',        industry:'Asset Mgmt'    },
  { c:'Goldman Sachs',    industry:'Finance'       },
];

function TrendingMarquee({ jobs }) {
  const counts = {};
  jobs.forEach(j => { const c = (j.co || '').trim(); if (c) counts[c] = (counts[c] || 0) + 1; });
  const top = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 14)
    .map(([c, n]) => ({ c, n, industry: null }));
  if (top.length < 6) {
    INDUSTRY_SAMPLER.forEach(s => {
      if (!counts[s.c]) top.push({ c: s.c, n: 0, industry: s.industry });
    });
  }
  const palette = ['v1','v2','v3','v4','v5'];
  const loop = [...top, ...top];
  return (
    <div className="marquee-wrap">
      <div className="marquee-track">
        {loop.map((t, i) => (
          <div key={i} className="mq-pill">
            <CompanyLogo company={t.c} fallbackVariant={palette[i % palette.length]} size={28} className="mq-logo"/>
            <div className="mq-text">
              <b>{t.c}</b>
              <span>{t.n ? `${t.n} open` : (t.industry || 'trending')}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function MarketPulse({ jobs, profileSkills }) {
  const stats = useMemo(() => {
    const n = jobs.length;
    if (!n) return null;

    const remote = jobs.filter(j =>
      j.remote === true || /remote|distributed|anywhere/i.test(String(j.location || '') + ' ' + String(j.title || ''))
    ).length;
    const remotePct = Math.round((remote / n) * 100);

    const salaries = jobs.map(j => {
      const s = String(j.salary_range || j.salary || j.compensation || '');
      const m = s.match(/\$?\s*([\d,.]+)\s*k?\s*[-–—to]+\s*\$?\s*([\d,.]+)\s*k?/i);
      if (!m) return null;
      const lo = parseFloat(m[1].replace(/,/g, ''));
      const hi = parseFloat(m[2].replace(/,/g, ''));
      if (!isFinite(lo) || !isFinite(hi)) return null;
      const isK = /k/i.test(s) || (lo < 500 && hi < 500);
      return ((lo + hi) / 2) * (isK ? 1000 : 1);
    }).filter(Boolean).sort((a, b) => a - b);
    const median = salaries.length ? salaries[Math.floor(salaries.length / 2)] : null;

    const counts = {};
    jobs.forEach(j => {
      const c = (j.co || j.company || '').trim();
      if (c) counts[c] = (counts[c] || 0) + 1;
    });
    const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0] || null;

    // Skill-gap suggestion is derived from the user's actual job queue —
    // never a hardcoded list. We tokenize each job's skills/requirements
    // string, count occurrences across the queue, then pick the most
    // frequent token that isn't already in the user's profile. Field-
    // agnostic by construction: a marketing queue suggests marketing
    // skills, a hardware queue suggests hardware skills.
    const userSet = new Set((profileSkills || []).map(s => String(s).toLowerCase()));
    const tokenCounts = new Map();
    const STOP = new Set([
      'and','or','the','a','an','to','of','in','for','with','on','at','by','from','is','as','be',
      'team','teams','work','working','years','year','experience','required','preferred','plus',
      'must','should','will','can','our','your','their','this','that','these','those','more',
      'using','knowledge','strong','good','great','solid','excellent','familiar','familiarity',
      'including','related','similar','etc','any','all','one','two','three','five','ten',
    ]);
    for (const j of jobs) {
      const s = String(j.skills || j.requirements || j.description || '').toLowerCase();
      if (!s) continue;
      // Token shape: alpha + optional + # . - so we keep "c++", "node.js", "c#".
      const toks = s.match(/[a-z][a-z0-9+#.\-]{1,30}/g) || [];
      const seenInJob = new Set();
      for (const tok of toks) {
        if (tok.length < 2 || STOP.has(tok)) continue;
        if (seenInJob.has(tok)) continue;
        seenInJob.add(tok);
        tokenCounts.set(tok, (tokenCounts.get(tok) || 0) + 1);
      }
    }
    // Most frequent token that the user doesn't already list. Need at
    // least 2 jobs mentioning it so we don't surface a one-off oddity.
    const ranked = [...tokenCounts.entries()]
      .filter(([t, n]) => n >= 2 && !userSet.has(t))
      .sort((a, b) => b[1] - a[1]);
    const gap = ranked.length ? ranked[0][0] : null;

    return { remotePct, remote, total: n, median, top, gap };
  }, [jobs, profileSkills]);

  if (!stats) {
    return (
      <div className="viz-empty">
        <Icon name="activity" size={20}/>
        <span>Market signals appear once Atlas finishes Phase 2 discovery.</span>
      </div>
    );
  }

  return (
    <div className="pulse-grid">
      <div className="pulse-tile" style={{ animationDelay: '0ms' }}>
        <div className="pulse-eyebrow"><Icon name="globe" size={10}/> Remote share</div>
        <div className="pulse-num"><CountUp to={stats.remotePct}/><i>%</i></div>
        <div className="pulse-bar">
          <div className="pulse-bar-fill" style={{ width: `${stats.remotePct}%` }}/>
        </div>
        <div className="pulse-foot">{stats.remote} of {stats.total} listings work-from-anywhere</div>
      </div>

      <div className="pulse-tile" style={{ animationDelay: '80ms' }}>
        <div className="pulse-eyebrow"><Icon name="dollar-sign" size={10}/> Median posted</div>
        <div className="pulse-num">
          {stats.median
            ? <>${Math.round(stats.median / 1000)}<i>k</i></>
            : <span className="pulse-na">—</span>}
        </div>
        <div className="pulse-foot">
          {stats.median ? `Across ${stats.total} roles in your queue` : 'Salary not disclosed in queue'}
        </div>
      </div>

      {stats.top && (
        <div className="pulse-tile pulse-co-tile" style={{ animationDelay: '160ms' }}>
          <div className="pulse-eyebrow"><Icon name="building-2" size={10}/> Top hirer</div>
          <div className="pulse-co">
            <CompanyLogo company={stats.top[0]} size={26} fallbackVariant="v1" className="pulse-co-logo"/>
            <b>{stats.top[0]}</b>
          </div>
          <div className="pulse-foot">{stats.top[1]} active opening{stats.top[1] === 1 ? '' : 's'}</div>
        </div>
      )}

      {stats.gap && (
        <div className="pulse-tile pulse-gap-tile" style={{ animationDelay: '240ms' }}>
          <div className="pulse-eyebrow"><Icon name="zap" size={10}/> Skill to add</div>
          <div className="pulse-gap-tag">{stats.gap}</div>
          <div className="pulse-foot">In demand across your queue, missing from your résumé</div>
        </div>
      )}
    </div>
  );
}

/* Industry-curated HN Algolia queries for the market-news widget.
   Bare terms OR; parens group; AND/OR are explicit. When a tighter topic
   returns nothing in 7 days the fetcher widens the window — see the loop
   below — so a sparse industry never strands the user on an empty card. */
const NEWS_TOPICS = [
  { id: 'all',      label: 'All',         icon: 'globe',          q: '"job market" OR hiring OR layoffs OR career OR salary' },
  { id: 'tech',     label: 'Software',    icon: 'terminal',       q: '(software OR developer OR engineer OR coding) AND (hiring OR layoff OR jobs OR market)' },
  { id: 'ai',       label: 'AI / ML',     icon: 'cpu',            q: '(AI OR LLM OR "machine learning" OR OpenAI OR Anthropic OR DeepMind) AND (hiring OR layoff OR jobs OR research)' },
  { id: 'startups', label: 'Startups',    icon: 'rocket',         q: 'startup AND (hiring OR funding OR "Y Combinator" OR seed OR "Series A" OR layoff)' },
  { id: 'design',   label: 'Design',      icon: 'palette',        q: '(designer OR "UX " OR "UI " OR "product design") AND (hiring OR portfolio OR job OR market)' },
  { id: 'finance',  label: 'Finance',     icon: 'banknote',       q: '(finance OR fintech OR banking OR analyst OR trading OR "Wall Street") AND (hiring OR layoff OR job)' },
  { id: 'health',   label: 'Healthcare',  icon: 'heart-pulse',    q: '(healthcare OR pharma OR biotech OR medical OR clinical) AND (hiring OR layoff OR jobs OR research)' },
  { id: 'research', label: 'Research',    icon: 'graduation-cap', q: '(PhD OR postdoc OR research OR academic OR university) AND (hiring OR job OR funding)' },
  { id: 'remote',   label: 'Remote',      icon: 'home',           q: '("remote work" OR WFH OR "work from home" OR hybrid OR "return to office") AND (hiring OR jobs OR future)' },
  { id: 'layoffs',  label: 'Layoffs',     icon: 'trending-down',  q: 'layoffs OR fired OR severance OR "reduction in force" OR "RIF"' },
];

/* Pick a sensible default chip based on the user's profile. A designer
   lands on Design, an AI engineer on AI/ML, etc., without manually
   clicking — keeps the widget feeling tailored. Falls back to "All"
   for anyone whose target_titles don't fingerprint any industry. */
function _pickDefaultNewsTopic(profile) {
  const titles = (profile?.target_titles || []).map(String).join(' ').toLowerCase();
  const skills = (profile?.top_hard_skills || []).map(String).join(' ').toLowerCase();
  const blob = titles + ' ' + skills;
  if (!blob.trim()) return 'all';
  if (/\b(ai|ml|machine\s*learning|data\s*scientist|llm|nlp|deep\s*learning|pytorch|tensorflow)\b/.test(blob)) return 'ai';
  if (/\b(designer|ux|ui|product\s*design|figma|illustrator)\b/.test(blob)) return 'design';
  if (/\b(finance|fintech|analyst|banking|trading|investment)\b/.test(blob)) return 'finance';
  if (/\b(healthcare|nurse|physician|medical|clinical|pharma|biotech)\b/.test(blob)) return 'health';
  if (/\b(phd|postdoc|research\s*scientist|academic|professor)\b/.test(blob)) return 'research';
  if (/\b(software|developer|engineer|backend|frontend|fullstack|swe|sde)\b/.test(blob)) return 'tech';
  if (/\b(startup|founder|product\s*manager|growth)\b/.test(blob)) return 'startups';
  return 'all';
}

function MarketNews({ profile }) {
  const [topic, setTopic]           = useState(() => _pickDefaultNewsTopic(profile));
  const [items, setItems]           = useState(null);
  const [err, setErr]               = useState(null);
  const [windowDays, setWindowDays] = useState(7);

  // Refetch whenever the topic changes. The fetcher auto-widens the window
  // from 7 → 30 → 180 days if the topic is sparse (Healthcare and Research
  // routinely have <3 stories in any given week on HN). Recency wins when
  // it can; we only widen when we'd otherwise show nothing.
  useEffect(() => {
    let cancelled = false;
    setItems(null); setErr(null); setWindowDays(7);

    const t = NEWS_TOPICS.find(x => x.id === topic) || NEWS_TOPICS[0];

    const fetchWindow = async (days) => {
      const since = Math.floor(Date.now() / 1000) - days * 24 * 3600;
      const url = 'https://hn.algolia.com/api/v1/search_by_date'
        + '?query=' + encodeURIComponent(t.q)
        + '&tags=story'
        + '&numericFilters=' + encodeURIComponent(`created_at_i>${since},points>1`)
        + '&hitsPerPage=20';
      const r = await fetch(url);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      return (d.hits || [])
        .filter(h => h.title && h.url)
        .map(h => {
          let host = 'news';
          try { host = new URL(h.url).hostname.replace(/^www\./, ''); } catch (_) {}
          return {
            title: h.title,
            url: h.url,
            host,
            points: h.points || 0,
            comments: h.num_comments || 0,
            when: h.created_at,
            hnUrl: `https://news.ycombinator.com/item?id=${h.objectID}`,
          };
        });
    };

    (async () => {
      try {
        for (const days of [7, 30, 180]) {
          const hits = await fetchWindow(days);
          if (cancelled) return;
          if (hits.length >= 3 || days === 180) {
            setItems(hits.slice(0, 7));
            setWindowDays(days);
            return;
          }
        }
      } catch (e) {
        if (!cancelled) setErr(e.message || 'Network');
      }
    })();

    return () => { cancelled = true; };
  }, [topic]);

  const fmt = iso => {
    const ms = Date.now() - new Date(iso).getTime();
    const h = ms / 3.6e6;
    if (h < 1) return Math.max(1, Math.round(h * 60)) + 'm ago';
    if (h < 24) return Math.round(h) + 'h ago';
    return Math.round(h / 24) + 'd ago';
  };

  const chips = (
    <div className="news-topics" role="tablist" aria-label="News topic">
      {NEWS_TOPICS.map(t => (
        <button
          key={t.id}
          role="tab"
          aria-selected={topic === t.id}
          className={'news-topic' + (topic === t.id ? ' on' : '')}
          onClick={() => setTopic(t.id)}>
          <Icon name={t.icon} size={11}/>
          {t.label}
        </button>
      ))}
    </div>
  );

  let body;
  if (err) {
    body = (
      <div className="news-empty">
        <Icon name="wifi-off" size={18}/>
        <span>News feed offline — {err}</span>
      </div>
    );
  } else if (!items) {
    body = (
      <div className="news-skel">
        {[0, 1, 2, 3].map(i => <div key={i} className="news-skel-row" style={{ animationDelay: `${i * 120}ms` }}/>)}
      </div>
    );
  } else if (!items.length) {
    body = (
      <div className="news-empty">
        <Icon name="search" size={18}/>
        <span>No relevant stories — even the long-window search came up empty.</span>
      </div>
    );
  } else {
    body = (
      <>
        {windowDays > 7 && (
          <div className="news-window-hint">
            <Icon name="info" size={11}/>
            Quiet week — showing the last {windowDays === 30 ? '30 days' : '6 months'} instead.
          </div>
        )}
        <ul className="news-list">
          {items.map((it, i) => (
            <li key={i} className="news-item" style={{ animationDelay: `${i * 70}ms` }}>
              <a className="news-link" href={it.url} target="_blank" rel="noopener noreferrer">
                <div className="news-row">
                  <span className="news-host">{it.host}</span>
                  <span className="news-when">{fmt(it.when)}</span>
                </div>
                <div className="news-title">{it.title}</div>
                <div className="news-meta">
                  <span><Icon name="arrow-up" size={10}/> {it.points}</span>
                  <span className="news-sep">·</span>
                  <span><Icon name="message-circle" size={10}/> {it.comments}</span>
                  <span className="news-sep">·</span>
                  <a className="news-thread" href={it.hnUrl} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()}>
                    thread
                  </a>
                </div>
              </a>
            </li>
          ))}
        </ul>
      </>
    );
  }

  return (
    <div className="news-wrap">
      {chips}
      {body}
    </div>
  );
}

function TipCard({ state }) {
  const tips = useMemo(() => {
    const t = [
      { h: 'Tighten your top fold', b: 'Recruiters scan the first ~80 words. Move your strongest two bullets above the fold.' },
      { h: 'Mirror the job description', b: 'Roles asking for "verification" reward résumés that say "verification" — not "QA".' },
      { h: 'Quantify or it didn\'t happen', b: 'Bullets with numbers convert ~2.4× higher in ATS scoring than bullets without.' },
      { h: 'Apply on Tuesdays', b: 'Job postings see 46% more recruiter triage on Tue/Wed than Mon or Fri.' },
    ];
    if ((state?.scored_summary?.jobs || []).some(j => (j.score || 0) >= 85))
      t.unshift({ h: 'You have strong matches', b: 'Move on auto-eligible roles within 48h — match velocity decays after that.' });
    return t;
  }, [state]);
  const [idx, setIdx] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setIdx(i => (i + 1) % tips.length), 6500);
    return () => clearInterval(id);
  }, [tips.length]);
  const t = tips[idx];
  return (
    <div className="tip-card" key={idx}>
      <div className="tip-aurora"/>
      <div className="tip-tag"><span className="tip-pulse"/> Atlas tip · live</div>
      <div className="tip-h">{t.h}</div>
      <div className="tip-b">{t.b}</div>
      <div className="tip-dots">
        {tips.map((_, i) => <span key={i} className={i === idx ? 'on' : ''}/>)}
      </div>
    </div>
  );
}

/* ── Home — Resume Intelligence panel ────────────────────────────────────── */
function ResumeIntelligencePanel({ profile, resumes, setPage, refresh }) {
  const insights = profile?.insights || null;
  const m = insights?.metrics || {};
  const score = insights ? Math.max(0, Math.min(100, Math.round(insights.overall_score || 0))) : null;
  const verified = insights && insights.verified_by && insights.verified_by !== 'heuristic';
  // Local busy state for the re-scan button — gives immediate feedback
  // before the next /api/state poll surfaces the backend's `extracting`
  // flag (~150 ms request RTT + up to 2 s poll cadence).
  const [rescanning, setRescanning] = useState(false);

  // Big-number ring config (sized for the home dossier card).
  const R = 62, CIRC = 2 * Math.PI * R;
  const off = score == null ? CIRC : CIRC - (CIRC * score / 100);
  const ringColor = score == null ? 'var(--t4)'
                  : score >= 80 ? 'var(--good)'
                  : score >= 60 ? 'var(--accent-h)'
                  : score >= 40 ? 'var(--warn)'
                                : 'var(--bad)';
  const verdict = score == null ? 'Awaiting scan'
                : score >= 85 ? 'Strong'
                : score >= 70 ? 'Solid'
                : score >= 55 ? 'Promising'
                : score >= 40 ? 'Needs work'
                              : 'Reach';
  const verdictColor = ringColor;
  const subline = score == null
    ? 'Drop a resume to see your living dossier.'
    : score >= 85 ? 'Top-decile signal — refine the specifics.'
    : score >= 70 ? 'Above the bar; close the last few gaps.'
    : score >= 55 ? 'Has the bones; sharpen impact and verbs.'
    : score >= 40 ? 'Quantification + action verbs are the unlock.'
                  : 'Restructure: numbers, action verbs, sections.';

  const tiles = [
    { lbl: 'Quantified',   v: m.quantified_pct  != null ? `${m.quantified_pct}%`  : '—',
      hint: 'numeric impact', tone: m.quantified_pct  >= 60 ? 'good' : m.quantified_pct  >= 40 ? 'warn' : 'bad' },
    { lbl: 'Action verbs', v: m.action_verb_pct != null ? `${m.action_verb_pct}%` : '—',
      hint: 'strong leads',   tone: m.action_verb_pct >= 70 ? 'good' : m.action_verb_pct >= 50 ? 'warn' : 'bad' },
    { lbl: 'Skill density', v: m.skill_density  != null ? m.skill_density       : '—',
      hint: '/100w',         tone: m.skill_density   >= 6  ? 'good' : m.skill_density   >= 4  ? 'warn' : 'bad' },
    { lbl: 'Words',        v: m.word_count      != null ? m.word_count          : '—',
      hint: 'total',         tone: (m.word_count >= 350 && m.word_count <= 700) ? 'good' : 'warn' },
  ];

  const topStrength = (insights?.strengths || [])[0];
  const topFlag     = (insights?.red_flags || [])[0];
  const primary = (resumes || []).find(r => r.primary) || (resumes || [])[0];

  return (
    <div className="dossier-card intel-card">
      <div className="dossier-eyebrow">
        <span className="dossier-num">01</span>
        <span>Resume intelligence</span>
        <span className="dossier-sep">·</span>
        <span className="dossier-meta">
          {primary ? primary.filename.replace(/\.[^.]+$/, '') : 'no resume'}
        </span>
        <span className="dossier-grow"/>
        <span className={'dossier-chip ' + (verified ? 'ok' : score == null ? 'mute' : 'info')}>
          <Icon name={verified ? 'shield-check' : 'sparkles'} size={11}/>
          {verified ? 'AI-verified' : score == null ? 'Not scanned' : 'Heuristic'}
        </span>
      </div>

      <div className="intel-grid">
        <div className="intel-ring-wrap">
          <svg width="160" height="160" viewBox="0 0 160 160" className="intel-ring-svg">
            <defs>
              <linearGradient id="intelRingGrad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%"  stopColor={ringColor} stopOpacity=".95"/>
                <stop offset="100%" stopColor={ringColor} stopOpacity=".55"/>
              </linearGradient>
            </defs>
            <circle cx="80" cy="80" r={R} fill="none" stroke="var(--bdr)" strokeWidth="9"/>
            <circle cx="80" cy="80" r={R} fill="none" stroke="url(#intelRingGrad)" strokeWidth="9"
              strokeLinecap="round" strokeDasharray={CIRC} strokeDashoffset={off}
              transform="rotate(-90 80 80)"
              style={{ transition:'stroke-dashoffset 1.2s cubic-bezier(.16,1,.3,1)' }}/>
          </svg>
          <div className="intel-ring-c">
            <div className="intel-ring-num" style={{ color: ringColor }}>
              {score == null ? '—' : score}
            </div>
            <div className="intel-ring-unit">/ 100</div>
          </div>
        </div>

        <div className="intel-body">
          <div className="intel-verdict" style={{ color: verdictColor }}>{verdict}</div>
          <div className="intel-sub">{subline}</div>

          <div className="intel-tile-row">
            {tiles.map((t, i) => (
              <div key={i} className={'intel-tile tone-' + t.tone}>
                <div className="intel-tile-v">{t.v}</div>
                <div className="intel-tile-l">{t.lbl}</div>
                <div className="intel-tile-h">{t.hint}</div>
              </div>
            ))}
          </div>

          <div className="intel-bullets">
            {topStrength && (
              <div className="intel-bullet good">
                <Icon name="check" size={11}/>
                <span>{topStrength}</span>
              </div>
            )}
            {topFlag && (
              <div className="intel-bullet bad">
                <Icon name="alert-triangle" size={11}/>
                <span>{topFlag}</span>
              </div>
            )}
            {!topStrength && !topFlag && (
              <div className="intel-bullet mute">
                <Icon name="info" size={11}/>
                <span>{primary ? 'Re-scan to surface strengths and red flags.' : 'Upload a resume to begin.'}</span>
              </div>
            )}
          </div>

          <div className="intel-actions">
            <button className="intel-btn primary" onClick={() => setPage('resume')}>
              <Icon name="bar-chart-3" size={12}/> Open analysis
            </button>
            {primary && (() => {
              const busy = rescanning || !!primary?.extracting;
              return (
                <button className="intel-btn ghost" disabled={busy}
                  onClick={async () => {
                    setRescanning(true);
                    try {
                      await api.post('/api/profile/extract',
                                      { resume_id: primary.id, force: true });
                      await refresh?.();
                    } catch (e) { /* swallow — UI shows extracting state */ }
                    finally { setRescanning(false); }
                  }}>
                  {busy
                    ? <span className="spin" style={{ width:11, height:11, borderWidth:1.5 }}/>
                    : <Icon name="refresh-cw" size={11}/>}
                  {busy ? 'Scanning…' : 'Re-scan'}
                </button>
              );
            })()}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ── Home — Mission Control quick-actions panel ──────────────────────────── */
function MissionControlPanel({ state, setPage, refresh }) {
  const has_resume = !!state?.has_resume;
  const apps = state?.applications || [];
  const jobs_total = state?.scored_summary?.total || state?.job_count || 0;
  const primary = (state?.resumes || []).find(r => r.primary) || (state?.resumes || [])[0];
  // Local busy state for the Re-scan tile so the user gets immediate
  // feedback. Stays "busy" until the backend's `extracting` flag clears.
  const [rescanning, setRescanning] = useState(false);
  const isScanning = rescanning || !!primary?.extracting;

  const actions = [
    {
      n: '01', icon: 'compass', label: 'Discover roles',
      sub: jobs_total ? `${jobs_total} in queue` : 'find new openings',
      tone: 'accent', onClick: () => setPage('jobs'),
    },
    {
      n: '02', icon: 'sparkles', label: 'Run agent',
      sub: 'all 7 phases',
      tone: 'cyan', onClick: () => setPage('agent'),
    },
    {
      n: '03',
      icon: isScanning ? 'loader-2' : 'scan-text',
      label: isScanning ? 'Scanning…' : 'Re-scan resume',
      sub: isScanning
        ? 'reading bullets, recomputing score'
        : (primary ? primary.filename.replace(/\.[^.]+$/, '').slice(0, 22) : 'analyze resume'),
      tone: 'pink',
      busy: isScanning,
      onClick: async () => {
        if (isScanning) return;
        if (!primary) { setPage('resume'); return; }
        setRescanning(true);
        try {
          await api.post('/api/profile/extract',
                          { resume_id: primary.id, force: true });
          await refresh?.();
        } catch (e) { /* swallow — extracting flag carries the state */ }
        finally { setRescanning(false); }
      },
    },
    {
      n: '04', icon: 'user-round', label: 'Edit profile',
      sub: state?.profile?.target_titles?.[0] || 'identity & targets',
      tone: 'mint', onClick: () => setPage('profile'),
    },
    {
      n: '05', icon: 'send', label: 'Applications',
      sub: apps.length ? `${apps.length} tracked` : 'tracker view',
      tone: 'amber', onClick: () => setPage('jobs'),
    },
    {
      n: '06', icon: 'settings-2', label: 'Settings',
      sub: state?.mode === 'anthropic' ? 'Claude (dev)' : (state?.is_pro ? 'cloud AI' : 'local AI'),
      tone: 'violet', onClick: () => setPage('settings'),
    },
  ];

  return (
    <div className="dossier-card mission-card">
      <div className="dossier-eyebrow">
        <span className="dossier-num">02</span>
        <span>Mission control</span>
        <span className="dossier-sep">·</span>
        <span className="dossier-meta">launchpad</span>
      </div>
      <div className="mission-grid">
        {actions.map((a, i) => (
          <button
            key={a.n}
            className={'mission-tile tone-' + a.tone + (a.busy ? ' busy' : '')}
            onClick={a.onClick}
            disabled={a.busy || (!has_resume && (a.label === 'Discover roles' || a.label === 'Run agent'))}
            style={{ animationDelay: `${i * 60}ms` }}
          >
            <span className="mission-num">{a.n}</span>
            <span className="mission-icon">
              {a.busy
                ? <span className="spin" style={{ width:18, height:18, borderWidth:2 }}/>
                : <Icon name={a.icon} size={20}/>}
            </span>
            <span className="mission-label">{a.label}</span>
            <span className="mission-sub">{a.sub}</span>
            <span className="mission-arrow"><Icon name="arrow-up-right" size={13}/></span>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Home — editorial pull-quote of the AI narrative ─────────────────────── */
function NarrativePullQuote({ profile }) {
  const insights = profile?.insights;
  const narrative = insights?.narrative || '';
  if (!narrative.trim()) return null;
  const verified = insights.verified_by && insights.verified_by !== 'heuristic';
  // Pull the first paragraph for the home page; the full narrative lives on
  // the Resume → Analysis tab.
  const lead = narrative.split(/\n\n+/)[0]?.trim() || narrative;
  return (
    <section className="narrative-section">
      <div className="narrative-card">
        <span className="narrative-mark" aria-hidden="true">&ldquo;</span>
        <div className="narrative-eyebrow">
          <span>03 ·</span>
          <span>{verified ? 'AI-verified analysis' : 'Heuristic analysis'}</span>
          <span className="narrative-sep">·</span>
          <span>this week's read</span>
        </div>
        <p className="narrative-body">{lead}</p>
        <div className="narrative-foot">— Atlas, your career analyst</div>
      </div>
    </section>
  );
}


/* ── Home — Career Cockpit HUD strip ──────────────────────────────────────
   Aviation-instrument-style telemetry panel that sits between the home hero
   and the Resume Intelligence dossier. Surfaces session-live state, a
   time-of-day greeting (mirrors the hero eyebrow but at desktop fidelity),
   the streak, pipeline progress, and a ticking wall clock. The accent hue
   shifts with local time. Mobile hides this strip — the hero already
   foregrounds the same data in a stacked layout there. */
function CockpitStrip({ state, done, apps, jobs, matches }) {
  const [now, setNow] = useState(() => new Date());
  const startedRef = useRef(Date.now());
  const [sessionMs, setSessionMs] = useState(0);

  useEffect(() => {
    const id = setInterval(() => {
      setNow(new Date());
      setSessionMs(Date.now() - startedRef.current);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  const hr = now.getHours();
  const greet = hr < 5  ? 'Burning the midnight oil'
              : hr < 12 ? 'Good morning'
              : hr < 17 ? 'Good afternoon'
              : hr < 21 ? 'Good evening'
                        : 'Up late';
  const tod = hr < 5  ? 'midnight'
            : hr < 12 ? 'morning'
            : hr < 17 ? 'afternoon'
            : hr < 21 ? 'evening'
                      : 'nightfall';
  const firstName = (state?.profile?.name || '').split(' ')[0] || 'Explorer';
  const phaseDone = done?.size || 0;
  const streak    = Math.max(1, phaseDone + ((apps?.length || 0) > 0 ? 2 : 0));
  const jobCount  = jobs?.length || 0;

  const note = matches > 0
    ? <>You have <strong>{matches}</strong> high-fit role{matches === 1 ? '' : 's'} waiting on your move.</>
    : phaseDone >= 7
      ? <>Cycle complete — review the tracker, then rerun discovery.</>
      : phaseDone > 0
        ? <>Atlas finished phase <strong>{phaseDone}/7</strong> — keep the momentum.</>
        : jobCount > 0
          ? <>{jobCount} role{jobCount === 1 ? '' : 's'} in the queue. Score them next.</>
          : <>Ready when you are. Kick off discovery to begin.</>;

  const pad = (n) => String(n).padStart(2, '0');
  const sStr = (() => {
    const s = Math.floor(sessionMs / 1000);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
  })();
  const clock   = `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  const weekday = ['SUN','MON','TUE','WED','THU','FRI','SAT'][now.getDay()];
  const month   = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'][now.getMonth()];
  const day     = pad(now.getDate());

  return (
    <section className={'cockpit-strip cockpit-' + tod} aria-label="Career cockpit">
      <div className="cockpit-grid">
        <div className="cockpit-zone cockpit-status">
          <span className="cockpit-tick">Status</span>
          <div className="cockpit-status-line">
            <span className="cockpit-led" aria-hidden="true"/>
            <span>SESSION ACTIVE</span>
          </div>
          <div className="cockpit-readout">
            <span className="cockpit-readout-lbl">Uptime</span>
            <span className="cockpit-readout-num">{sStr}</span>
          </div>
        </div>

        <div className="cockpit-divider" aria-hidden="true"/>

        <div className="cockpit-zone cockpit-greeting">
          <span className="cockpit-tick">Today</span>
          <h2 className="cockpit-greet">{greet}, <em>{firstName}</em>.</h2>
          <p className="cockpit-note">{note}</p>
        </div>

        <div className="cockpit-divider cockpit-divider--telemetry" aria-hidden="true"/>

        <div className="cockpit-zone cockpit-telemetry">
          <span className="cockpit-tick">Telemetry</span>
          <div className="cockpit-stat-row">
            <div className="cockpit-stat">
              <span className="cockpit-stat-num">
                <Icon name="flame" size={14}/>{streak}
              </span>
              <span className="cockpit-stat-lbl">Day streak</span>
            </div>
            <div className="cockpit-stat">
              <span className="cockpit-stat-num">{phaseDone}<i>/7</i></span>
              <span className="cockpit-stat-lbl">Phases</span>
            </div>
            <div className="cockpit-stat">
              <span className="cockpit-stat-num">{matches}</span>
              <span className="cockpit-stat-lbl">High-fit</span>
            </div>
          </div>
        </div>

        <div className="cockpit-divider" aria-hidden="true"/>

        <div className="cockpit-zone cockpit-time">
          <span className="cockpit-tick">Local</span>
          <div className="cockpit-clock">
            {clock}
            <span className="cockpit-clock-dot" aria-hidden="true"/>
          </div>
          <div className="cockpit-date">
            <span>{weekday}</span>
            <span className="cockpit-date-sep">/</span>
            <span>{month} {day}</span>
          </div>
        </div>
      </div>
    </section>
  );
}


function Dashboard({ state, setPage, refresh }) {
  const jobs       = state?.scored_summary?.jobs || [];
  const apps       = state?.applications || [];
  const applied    = apps.filter(a => a.status === 'Applied').length;
  const matches    = jobs.filter(j => (j.score || 0) >= 85).length;
  const reviewable = jobs.filter(j => (j.score || 0) >= 60 && (j.score || 0) < 85).length;
  const avgScore   = jobs.length
    ? Math.round(jobs.reduce((s, j) => s + (j.score || 0), 0) / jobs.length)
    : 0;
  const done       = new Set(state?.done || []);
  const phasePct   = Math.round((done.size / 7) * 100);

  // Re-scan CTA was here when the hero hosted a button row; the button is
  // now gone (lived inside .hero-cta-row, which dropped out of the visible
  // layout). The Re-scan action remains available in the
  // ResumeIntelligencePanel below — its proper home — and on the dedicated
  // Resume page header.

  // No streak math here — the previous version was
  // `done.size + (apps.length > 0 ? 2 : 0)`, which produced numbers like
  // "3-day streak" with no relation to actual day-over-day login activity.
  // We don't track per-user daily-active timestamps yet, so any "streak"
  // value would be a lie. The eyebrow now stands on its own.

  const hr = new Date().getHours();
  const greet = hr < 5 ? 'Burning the midnight oil'
              : hr < 12 ? 'Good morning'
              : hr < 17 ? 'Good afternoon'
              : hr < 21 ? 'Good evening'
              : 'Up late';
  const firstName = state?.profile?.name?.split(' ')[0] || 'Explorer';

  const seed = jobs.length || 1;
  const spark = (offset, amp = 6) =>
    Array.from({ length: 12 }, (_, i) =>
      Math.max(0, Math.round(amp + amp * Math.sin((i + offset) * 0.55) + (i * 0.3))));

  // Canonical key emitted by pipeline/profile_extractor.py is `top_hard_skills`.
  // Earlier `skills` / `hard_skills` reads were stale — both undefined on every
  // /api/state response, which silently emptied SkillDonut + MarketPulse.
  const profileSkills = (state?.profile?.top_hard_skills || []).map(s =>
    typeof s === 'string' ? s : (s.name || s.skill || ''));


  return (
    <div className="page-body solo home-v2">
      <section className="home-hero">
        <div className="hero-aurora">
          <span className="orb orb-1"/>
          <span className="orb orb-2"/>
          <span className="orb orb-3"/>
          <span className="hero-grain"/>
        </div>
        <div className="hero-grid">
          <div className="hero-left">
            <div className="hero-eyebrow">
              <span className="hero-pulse"/>
              <span>Career cockpit · session live</span>
              {matches > 0 && (
                <>
                  <span className="hero-eyebrow-sep">/</span>
                  <span className="hero-eyebrow-stat">
                    <Icon name="target" size={10}/> {matches} high-fit
                  </span>
                </>
              )}
            </div>
            <h1 className="hero-h">
              {greet}, <em>{firstName}</em>.
            </h1>
            <p className="hero-p">
              {matches > 0
                ? <>You have <strong>{matches}</strong> high-confidence roles open in the queue. Atlas finished phase&nbsp;<strong>{done.size}/7</strong> — <em>your move</em>.</>
                : <>Atlas is warming up. Run discovery to surface the freshest roles tuned to your profile.</>}
            </p>
            {/* The CTA row and 7-step pipeline strip lived here previously
                but were dropping out of the visible layout — even though
                the JSX rendered them, they appeared as zero-height children
                on desktop, leaving the hero with empty space below the
                body paragraph (the "out of line" complaint).

                Both were also duplicates of surfaces elsewhere in the SPA:
                  • The left rail already handles all nav (Home / Jobs /
                    Resume / Profile / Agent / Documents) — adding pill
                    buttons inside the hero just gave the same destinations
                    a second visual weight.
                  • The agent page carries the canonical 7-phase pipeline
                    strip (large, with logs and reruns) — a tiny inline
                    version on the home page didn't add information beyond
                    the percentage in the hero ring on the right.

                Hero is now a compact greeting band: eyebrow + title + body
                paragraph on the left, the percentage ring on the right.
                The dossier-row below (Resume Intelligence + Mission
                Control) is the user's real launchpad. */}
          </div>
          <div className="hero-right">
            <div className="hero-ring">
              <svg width="180" height="180" viewBox="0 0 180 180">
                <defs>
                  <linearGradient id="ringGrad" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%"  stopColor="#7b84e8"/>
                    <stop offset="60%" stopColor="#a855f7"/>
                    <stop offset="100%" stopColor="#34d399"/>
                  </linearGradient>
                </defs>
                <circle cx="90" cy="90" r="74" fill="none" stroke="var(--bdr)" strokeWidth="2"/>
                <circle cx="90" cy="90" r="60" fill="none" stroke="var(--bdr2)" strokeWidth="1" strokeDasharray="2 6"/>
                <circle cx="90" cy="90" r="74" fill="none" stroke="url(#ringGrad)" strokeWidth="6"
                  strokeLinecap="round"
                  strokeDasharray={2 * Math.PI * 74}
                  strokeDashoffset={2 * Math.PI * 74 - (2 * Math.PI * 74 * phasePct / 100)}
                  transform="rotate(-90 90 90)"
                  style={{ transition:'stroke-dashoffset 1.4s cubic-bezier(.16,1,.3,1)', filter:'drop-shadow(0 0 12px rgba(123,132,232,.45))' }}/>
              </svg>
              <div className="hero-ring-c">
                <div className="hrc-pct"><CountUp to={phasePct}/><i>%</i></div>
                <div className="hrc-lbl">pipeline</div>
                <div className="hrc-sub">{done.size} of 7 phases</div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* CockpitStrip removed — duplicated the hero's greeting / streak /
          phase progress and rendered as an empty band on desktop windows
          where its 7-col grid (170+280+240+170 = 860 px min) couldn't fit
          its content cleanly alongside the hero. The hero already carries
          the same telemetry (streak in the eyebrow, pipeline progress in
          the ring + step row). The component is kept in the file in case
          a future iteration wants to bring it back as its own page. */}

      {/* ── Resume Intelligence + Mission Control row ──────────────── */}
      <section className="dossier-row">
        <ResumeIntelligencePanel
          profile={state?.profile}
          resumes={state?.resumes}
          setPage={setPage}
          refresh={refresh}
        />
        <MissionControlPanel state={state} setPage={setPage} refresh={refresh}/>
      </section>

      <NarrativePullQuote profile={state?.profile}/>

      <section className="kpi-row">
        <div className="kpi" style={{ animationDelay:'40ms' }}>
          <div className="kpi-h"><Icon name="target" size={13}/><span>High-fit matches</span><i className="kpi-trend up">+{Math.max(1, Math.round(matches/3))}</i></div>
          <div className="kpi-n"><CountUp to={matches}/></div>
          <div className="kpi-foot">
            <span>Score ≥ 85</span>
            <Sparkline values={spark(seed, 4)} color="var(--good)"/>
          </div>
        </div>
        <div className="kpi" style={{ animationDelay:'120ms' }}>
          <div className="kpi-h"><Icon name="send" size={13}/><span>Applications sent</span><i className="kpi-trend up">+{Math.max(0, Math.round(applied/2))}</i></div>
          <div className="kpi-n"><CountUp to={applied}/></div>
          <div className="kpi-foot">
            <span>{Math.max(0, apps.length - applied)} pending review</span>
            <Sparkline values={spark(seed + 3, 5)} color="var(--accent-h)"/>
          </div>
        </div>
        <div className="kpi" style={{ animationDelay:'200ms' }}>
          <div className="kpi-h"><Icon name="gauge" size={13}/><span>Avg match score</span><i className="kpi-trend">{avgScore >= 70 ? '↑' : '→'} {avgScore}</i></div>
          <div className="kpi-n"><CountUp to={avgScore}/><i className="kpi-unit">/100</i></div>
          <div className="kpi-foot">
            <span>across {jobs.length} roles</span>
            <Sparkline values={spark(seed + 5, 6)} color="var(--accent-h)"/>
          </div>
        </div>
        <div className="kpi" style={{ animationDelay:'280ms' }}>
          <div className="kpi-h"><Icon name="layers" size={13}/><span>Manual review queue</span><i className="kpi-trend">{reviewable}</i></div>
          <div className="kpi-n"><CountUp to={reviewable}/></div>
          <div className="kpi-foot">
            <span>Score 60–84</span>
            <Sparkline values={spark(seed + 8, 4)} color="var(--warn)"/>
          </div>
        </div>
      </section>

      <section className="viz-grid">
        <div className="viz-card viz-histo">
          <div className="viz-head">
            <div>
              <div className="viz-eyebrow">Distribution</div>
              <div className="viz-h">How your matches stack up</div>
            </div>
            <button className="viz-link" onClick={() => setPage('jobs')}>
              See all <Icon name="arrow-right" size={11}/>
            </button>
          </div>
          {jobs.length ? <ScoreHisto jobs={jobs}/> :
            <div className="viz-empty">
              <Icon name="bar-chart-3" size={20}/>
              <span>Score distribution appears once Atlas finishes Phase 3.</span>
            </div>}
        </div>

        <div className="viz-card viz-donut">
          <div className="viz-head">
            <div>
              <div className="viz-eyebrow">Skill coverage</div>
              <div className="viz-h">Your résumé vs. open listings</div>
            </div>
          </div>
          <SkillDonut profileSkills={profileSkills} jobs={jobs}/>
        </div>

        <div className="viz-card viz-pulse">
          <div className="viz-head">
            <div>
              <div className="viz-eyebrow"><span className="hero-pulse"/> Market pulse</div>
              <div className="viz-h">What your queue is paying</div>
            </div>
            <button className="viz-link" onClick={() => setPage('jobs')}>
              Drill in <Icon name="arrow-right" size={11}/>
            </button>
          </div>
          <MarketPulse jobs={jobs} profileSkills={profileSkills}/>
        </div>
      </section>

      <section className="lower-grid">
        <div className="viz-card feed-card">
          <div className="viz-head">
            <div>
              <div className="viz-eyebrow"><span className="hero-pulse"/> Live</div>
              <div className="viz-h">Activity stream</div>
            </div>
            <button className="viz-link" onClick={() => setPage('agent')}>
              Open agent <Icon name="arrow-right" size={11}/>
            </button>
          </div>
          <ActivityFeed state={state}/>
        </div>

        <div className="viz-card news-card">
          <div className="viz-head">
            <div>
              <div className="viz-eyebrow"><span className="news-rss"/> Industry pulse · curated weekly</div>
              <div className="viz-h">Hiring, layoffs &amp; market moves</div>
            </div>
            <a className="viz-link" href="https://hn.algolia.com/?dateRange=pastWeek&query=hiring%20OR%20layoffs" target="_blank" rel="noopener noreferrer">
              View on HN <Icon name="external-link" size={11}/>
            </a>
          </div>
          <MarketNews profile={state?.profile}/>
        </div>
      </section>

      <section className="trending-card">
        <div className="viz-head" style={{ marginBottom: 4 }}>
          <div>
            <div className="viz-eyebrow">Trending in your queue</div>
            <div className="viz-h">Companies hiring for your stack</div>
          </div>
          <button className="viz-link" onClick={() => setPage('jobs')}>
            Browse <Icon name="arrow-right" size={11}/>
          </button>
        </div>
        <TrendingMarquee jobs={jobs}/>
      </section>
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
      onLoaded?.();  // re-check auth state; if session expired, auth gate shows sign-in
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
      onLoaded?.();
    } finally { setLoading(false); }
  };

  const handleDemo = async () => {
    setLoading(true);
    try {
      await api.post('/api/resume/demo', {});
      onLoaded?.();
    } catch (e) {
      alert(e.message || 'Could not load the sample resume. Please try again.');
      onLoaded?.();
    } finally {
      setLoading(false);
    }
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
            <div style={{ marginTop:8, fontSize:16, color:'var(--t1)', fontWeight:500 }}>
              Drop your file or click to browse
            </div>
            <div style={{ marginTop:4, fontSize:14.5, color:'var(--t3)' }}>PDF · DOCX · TEX · TXT · MD</div>
            <div style={{ marginTop:4, fontSize:11.5, color:'var(--t4)', maxWidth:340, lineHeight:1.4 }}>
              For best format match, upload <b style={{ color:'var(--t2)' }}>.tex</b> or <b style={{ color:'var(--t2)' }}>.docx</b> if you have them — Atlas preserves the original layout exactly. PDF works too: it's matched to the closest template.
            </div>
            <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.tex,.md" style={{ display:'none' }}
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

// Score tier — single source of truth used by JobCard's left-border
// stripe AND the ring color/label. Four tiers across the brand palette
// so every score lands in a colored zone (no more gray "low" tier);
// thresholds sit lower than 85/65 so the rerank-composite fallback —
// which naturally peaks around 0.7 — still lights up.
// Score tier — single source of truth used by JobCard's left-border
// stripe AND the ring color/label. Four tiers across the brand palette
// so every score lands in a colored zone (no more gray "low" tier);
// thresholds sit lower than 85/65 so the rerank-composite fallback —
// which naturally peaks around 0.7 — still lights up.
//
// `track` is the dimmed remainder ring behind the active stroke; `glow`
// is an outer drop-shadow for the top tier so "Strong" cards visibly
// punch out of a dense list.
function _scoreTier(score) {
  if (score == null || Number.isNaN(score)) {
    return { key: 'pending', label: 'Scoring…', color: 'var(--t4)',
             track: 'var(--bdr)',                glow: 'none' };
  }
  const pct = Math.max(0, Math.min(100, Math.round(score)));
  if (pct >= 75) return {
    key: 'strong', label: 'Strong', color: 'var(--good)',
    track: 'rgba(61,255,154,.16)',
    glow:  'drop-shadow(0 0 8px rgba(61,255,154,.45))',
  };
  if (pct >= 55) return {
    key: 'solid',  label: 'Solid',  color: 'var(--accent-h)',
    track: 'rgba(167,139,255,.18)',
    glow:  'drop-shadow(0 0 5px rgba(167,139,255,.32))',
  };
  if (pct >= 35) return {
    key: 'fair',   label: 'Fair',   color: 'var(--accent2)',
    track: 'rgba(34,229,255,.16)',
    glow:  'none',
  };
  return {
    key: 'reach',  label: 'Reach',  color: 'var(--accent3)',
    track: 'rgba(255,61,154,.16)',
    glow:  'none',
  };
}

function ScoreRing({ score, tooltip }) {
  const isPending = score == null || Number.isNaN(score);
  const pct       = isPending ? 0 : Math.max(0, Math.min(100, Math.round(score)));
  const tier      = _scoreTier(score);
  const C         = 26, circ = 2 * Math.PI * C;
  const off       = circ - (circ * pct / 100);
  return (
    <div className={'job-score-col score-' + tier.key} title={tooltip || undefined}>
      <div className="score-ring">
        <svg width="56" height="56" viewBox="0 0 56 56" style={{ filter: tier.glow }}>
          <circle cx="28" cy="28" r={C} fill="none" strokeWidth="4" stroke={tier.track}/>
          <circle cx="28" cy="28" r={C} fill="none" strokeWidth="4"
                  stroke={tier.color} strokeLinecap="round"
                  strokeDasharray={circ} strokeDashoffset={off}
                  style={{ transition: 'stroke-dashoffset .8s cubic-bezier(.16,1,.3,1), stroke .25s ease' }}/>
        </svg>
        <div className="score-pct">{isPending ? '—' : pct}</div>
      </div>
      <div className="score-label">{tier.label}</div>
    </div>
  );
}

function JobCard({ job, idx, isLiked, onLike, onHide, onAsk, onTailor, onSelect, scoreData }) {
  // Prefer stable per-job values (set by JobsPage); fall back to idx-based for callers that don't enrich.
  const logo    = job._logo   ?? LOGO_VARIANTS[idx % LOGO_VARIANTS.length];
  const posted  = job._posted ?? POSTED_LABELS[idx % POSTED_LABELS.length];
  const model   = job._model  ?? WORK_MODELS[idx % WORK_MODELS.length];
  const exp     = job._exp    ?? EXP_LEVELS[idx % EXP_LEVELS.length];
  // Card score uses a two-tier display so users never see a blank ring:
  //   1. Immediate fallback = job.score (the rerank composite from the
  //      feed: 0.45*bm25 + 0.30*skill_overlap + 0.15*freshness +
  //      0.10*title_match, scaled 0..100). Same number JobDetailView
  //      shows in its hero ring, so the card and the detail view agree.
  //   2. Once /api/jobs/score-batch returns, scoreData.score (the real
  //      compute_skill_coverage match against the full description)
  //      replaces it. Network failure / null score → keep the fallback.
  const lazyScore = scoreData && typeof scoreData.score === 'number'
                      ? scoreData.score : null;
  const fallback  = (typeof job.score === 'number' && job.score >= 0)
                      ? job.score : null;
  const pct       = lazyScore != null
                      ? Math.round(lazyScore)
                      : (fallback != null ? Math.round(fallback) : null);
  const stripe    = 'score-' + _scoreTier(pct).key;   // strong / solid / fair / reach / pending
  const tags    = (job.skills || '').split(',').map(s => s.trim()).filter(Boolean).slice(0,3);

  // Clicking the card body opens the rich detail sub-page (jobright-style).
  // Action buttons stop propagation so they keep their existing per-action
  // behaviour (bookmark, hide, Ask Atlas, Tailor, Quick Apply).
  const openDetail = (e) => {
    if (e?.target) {
      // Don't hijack clicks on links inside the card body (rare today, but
      // future-proofs us if we add inline links).
      const a = e.target.closest && e.target.closest('a');
      if (a) return;
    }
    onSelect?.(job);
  };

  return (
    <div className={'job-card ' + stripe}
         data-card-id={job.id}
         onClick={openDetail}
         role="button"
         tabIndex={0}
         onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(e); } }}
         style={{ cursor: 'pointer' }}
         title="Click to see full details">
      <div className="job-card-inner">
        <div className="job-body">
          <div className="job-header">
            <CompanyLogo company={job.co} fallbackVariant={logo} size={38}/>
            <div className="job-header-text">
              <div className="job-posted">{posted}</div>
              <div className="job-title">
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
            {job.has_jd === false && (
              <span className="job-chip"
                title="The source feed only carried this job's title — score is preliminary. Open the listing to fetch the full description; the score updates the next time you score this job."
                style={{ borderColor:'var(--warn-b)', color:'var(--warn)', background:'var(--warn-d)' }}>
                <Icon name="info" size={11}/> Title-only score
              </span>
            )}
          </div>

          <div className="job-footer">
            <span className="job-app-count">{(idx * 31 + 47)} applicants</span>
            <div className="job-footer-actions" onClick={e => e.stopPropagation()}>
              <button className="icon-btn" title="Hide"
                onClick={e => { e.stopPropagation(); onHide?.(job); }}>
                <Icon name="eye-off" size={13}/>
              </button>
              <button className={'icon-btn' + (isLiked ? ' active' : '')}
                title={isLiked ? "Unlike" : "Save"}
                onClick={e => { e.stopPropagation(); onLike?.(job); }}
                style={isLiked ? { color:'var(--accent-h)', background:'var(--accent-d)', borderColor:'var(--accent-b)' } : {}}>
                <Icon name="bookmark" size={13} fill={isLiked ? "currentColor" : "none"}/>
              </button>
              <button className="btn-atlas"
                onClick={e => { e.stopPropagation(); onAsk?.(job); }}>
                <Icon name="sparkles" size={12}/> Ask Atlas
              </button>
              <button className="btn-tailor" title="Generate a resume tailored to this job"
                onClick={e => { e.stopPropagation(); onTailor?.(job); }}>
                <Icon name="wand-2" size={12}/> Tailor
              </button>
              <button className="btn-primary"
                onClick={e => { e.stopPropagation(); job.url && window.open(job.url, '_blank'); }}>
                <Icon name="zap" size={12}/> Quick Apply
              </button>
            </div>
          </div>
        </div>
        <ScoreRing score={pct}
          tooltip={
            lazyScore != null ? 'Detailed match · how well your skills match this posting' :
            scoreData       ? `Baseline relevance shown · ${scoreData.reason || 'detailed match unavailable'}` :
            'Baseline relevance — refining detailed match…'
          }/>
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

/* Stable string hash → small int. Used to deterministically pick visual chips per job. */
function stableHash(s) {
  let h = 0;
  s = String(s || '');
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

/* ── Industry filter ──────────────────────────────────────────────────────────
 *
 * Replaces the old "Title" chip. The user's ask:
 *   "the positions should be more open ended too and allow for a general
 *    industry search rather than position search"
 *
 * Two-mode UI inside the popover:
 *   - Curated grid of canonical industries (icons + counts) for fast scanning.
 *   - When the user types, the grid is filtered AND the DB is queried via
 *     /api/jobs/facets so any uncurated bucket (e.g. legacy labels) still
 *     surfaces.
 *
 * Multi-select. Selecting two industries means "engineering OR sales" at the
 * SQL layer. The chip label shows the count when more than one is active.
 * Canonical labels and order are pinned to the same set produced by
 * pipeline/helpers.py::_CATEGORY_KEYWORDS — keep them in sync.
 */
const INDUSTRY_TILES = [
  { value: 'engineering',   label: 'Engineering',   icon: 'code-2',         hint: 'Software, SRE, hardware' },
  { value: 'data',          label: 'Data & AI',     icon: 'database',       hint: 'DS / ML / analytics' },
  { value: 'product',       label: 'Product & PM',  icon: 'rocket',         hint: 'PM, program, project' },
  { value: 'design',        label: 'Design',        icon: 'palette',        hint: 'UX, UI, brand, content' },
  { value: 'sales',         label: 'Sales',         icon: 'handshake',      hint: 'AE, BDR, SDR, CS' },
  { value: 'marketing',     label: 'Marketing',     icon: 'megaphone',      hint: 'Brand, growth, SEO' },
  { value: 'finance',       label: 'Finance',       icon: 'banknote',       hint: 'Accounting, FP&A, audit' },
  { value: 'consulting',    label: 'Consulting',    icon: 'briefcase',      hint: 'Strategy, advisory' },
  { value: 'operations',    label: 'Operations',    icon: 'truck',          hint: 'Supply chain, logistics' },
  { value: 'support',       label: 'Customer support', icon: 'headphones',  hint: 'CX, help desk' },
  { value: 'hr',            label: 'People & HR',   icon: 'users',          hint: 'Recruiting, HRBP' },
  { value: 'healthcare',    label: 'Healthcare',    icon: 'heart-pulse',    hint: 'Clinical, pharmacy' },
  { value: 'education',     label: 'Education',     icon: 'graduation-cap', hint: 'Teaching, curriculum' },
  { value: 'legal',         label: 'Legal',         icon: 'scale',          hint: 'Counsel, paralegal' },
  { value: 'public_sector', label: 'Public sector', icon: 'landmark',       hint: 'Government, civic' },
  { value: 'media',         label: 'Media',         icon: 'film',           hint: 'Editorial, video, audio' },
  { value: 'trades',        label: 'Trades',        icon: 'wrench',         hint: 'Technician, electrician' },
  { value: 'general',       label: 'General / other', icon: 'layers',       hint: 'Uncategorized roles' },
];
const INDUSTRY_BY_VALUE = INDUSTRY_TILES.reduce((m, t) => (m[t.value] = t, m), {});

function IndustryFilter({ value, onChange }) {
  // value is an array of canonical industry codes (strings); never null.
  const selected = Array.isArray(value) ? value : [];
  const [open, setOpen]     = useState(false);
  const [q, setQ]           = useState('');
  const [counts, setCounts] = useState({});       // { value: count }
  const [extraBuckets, setExtraBuckets] = useState([]); // non-curated facets returned by DB
  const [loading, setLoading] = useState(false);
  const wrapRef  = useRef(null);
  const inputRef = useRef(null);

  // Close on outside click / Escape.
  useEffect(() => {
    if (!open) return;
    const onDoc = e => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    const onKey = e => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  // Focus search box on open.
  useEffect(() => {
    if (open && inputRef.current) {
      const id = setTimeout(() => inputRef.current?.focus(), 30);
      return () => clearTimeout(id);
    }
  }, [open]);

  // Load curated counts on open. We query the full set once (limit=200 covers
  // all 18 canonical labels with room for any drift) so the tile order matches
  // real inventory — biggest first inside the curated row, then long-tail.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    api.get('/api/jobs/facets?kind=industry&limit=200')
      .then(data => {
        if (cancelled) return;
        const c = {};
        const extras = [];
        for (const b of data.buckets || []) {
          c[b.value] = b.count;
          if (!INDUSTRY_BY_VALUE[b.value] && b.value !== 'general') {
            extras.push({ value: b.value, label: b.value, icon: 'layers', count: b.count, hint: 'DB label' });
          }
        }
        setCounts(c);
        setExtraBuckets(extras);
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open]);

  const toggle = (val) => {
    const next = selected.includes(val) ? selected.filter(v => v !== val) : [...selected, val];
    onChange(next.length ? next : null);
  };

  const fmt = (n) => n == null ? '—' : (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n));

  // Canonical tiles ordered by live count (desc), with zero-count buckets
  // pushed to the bottom but still shown so the user sees the full taxonomy.
  const orderedTiles = useMemo(() => {
    const withCount = INDUSTRY_TILES.map(t => ({ ...t, count: counts[t.value] || 0 }));
    withCount.sort((a, b) => (b.count - a.count) || a.label.localeCompare(b.label));
    return withCount.concat(extraBuckets);
  }, [counts, extraBuckets]);

  const norm = q.trim().toLowerCase();
  const filteredTiles = norm
    ? orderedTiles.filter(t =>
        t.label.toLowerCase().includes(norm) ||
        (t.hint || '').toLowerCase().includes(norm) ||
        t.value.toLowerCase().includes(norm))
    : orderedTiles;

  const triggerLabel = selected.length === 0
    ? 'Industry'
    : selected.length === 1
      ? (INDUSTRY_BY_VALUE[selected[0]]?.label || selected[0])
      : `${selected.length} industries`;
  const isActive = selected.length > 0;

  return (
    <div className="fd-wrap" ref={wrapRef}>
      <button className={'f-chip fd-trigger' + (isActive ? ' on' : '') + (open ? ' open' : '')}
              onClick={() => setOpen(o => !o)}>
        <Icon name="layers" size={11}/>
        <span className="fd-trigger-lbl">{triggerLabel}</span>
        {isActive && <span className="fd-clear-x"
                           onClick={e => { e.stopPropagation(); onChange(null); }}
                           title="Clear all">
          <Icon name="x" size={10}/>
        </span>}
        <Icon name="chevron-down" size={11}/>
      </button>
      {open && (
        <div className="fd-pop fd-left fd-wide">
          <div className="fd-search">
            <Icon name="search" size={11} color="var(--t3)"/>
            <input ref={inputRef} placeholder="Search industries — e.g. design, finance"
                   value={q} onChange={e => setQ(e.target.value)}/>
            {q && <button className="fd-search-x" onClick={() => setQ('')}><Icon name="x" size={10}/></button>}
          </div>
          {selected.length > 0 && (
            <div className="fd-section-h">
              <span><Icon name="check-square" size={10}/> Active</span>
              <button className="fd-mini-link" onClick={() => onChange(null)}>Clear</button>
            </div>
          )}
          {selected.length > 0 && (
            <div className="fd-chips-row">
              {selected.map(v => (
                <button key={v} className="fd-active-chip" onClick={() => toggle(v)} title="Remove">
                  <Icon name={(INDUSTRY_BY_VALUE[v] || extraBuckets.find(e => e.value === v))?.icon || 'layers'} size={10}/>
                  {(INDUSTRY_BY_VALUE[v] || extraBuckets.find(e => e.value === v))?.label || v}
                  <Icon name="x" size={9}/>
                </button>
              ))}
            </div>
          )}
          <div className="fd-section-h">
            <span><Icon name="grid-3x3" size={10}/> {norm ? 'Matches' : 'Browse industries'}</span>
            {loading && <em className="fd-mini-meta">loading…</em>}
          </div>
          <div className="fd-grid">
            {filteredTiles.length === 0 && (
              <div className="fd-empty" style={{ gridColumn: '1 / -1' }}>
                No industries match "{q}". Try "engineering" or "finance".
              </div>
            )}
            {filteredTiles.map(t => {
              const sel = selected.includes(t.value);
              const c   = counts[t.value] != null ? counts[t.value] : t.count;
              return (
                <button key={t.value} className={'fd-tile' + (sel ? ' on' : '')}
                        onClick={() => toggle(t.value)} title={t.hint || ''}>
                  <span className="fd-tile-ico"><Icon name={t.icon} size={14}/></span>
                  <span className="fd-tile-body">
                    <span className="fd-tile-lbl">{t.label}</span>
                    <span className="fd-tile-meta">{fmt(c)}</span>
                  </span>
                  {sel && <span className="fd-tile-tick"><Icon name="check" size={11}/></span>}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ── BackendFacetFilter — single-select chip backed by /api/jobs/facets ──────
 *
 * Replaces the old client-side Location chip. Lets the user pick from a small
 * curated list of common defaults OR live-search the DB for any value
 * (cities, regions, countries — whatever the location column actually
 * contains). Counts come from the same facet endpoint. Used for the Location
 * chip; could be reused for "Company" later without changes.
 */
function BackendFacetFilter({ placeholder, kind, value, onChange, icon, defaults = [] }) {
  const [open, setOpen]       = useState(false);
  const [q, setQ]             = useState('');
  const [debouncedQ, setDQ]   = useState('');
  const [buckets, setBuckets] = useState([]);
  const [loading, setLoading] = useState(false);
  const wrapRef  = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = e => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    const onKey = e => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  useEffect(() => {
    if (open && inputRef.current) {
      const id = setTimeout(() => inputRef.current?.focus(), 30);
      return () => clearTimeout(id);
    }
  }, [open]);

  // Debounce search to avoid hammering the facet endpoint while typing.
  useEffect(() => {
    const id = setTimeout(() => setDQ(q.trim()), 200);
    return () => clearTimeout(id);
  }, [q]);

  // Fetch facets when the popover is open. Empty `q` returns top buckets by
  // count, which is what we want for the default landing view.
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    const url = `/api/jobs/facets?kind=${encodeURIComponent(kind)}&limit=40&q=${encodeURIComponent(debouncedQ)}`;
    api.get(url)
      .then(data => { if (!cancelled) setBuckets(data.buckets || []); })
      .catch(() => { if (!cancelled) setBuckets([]); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open, kind, debouncedQ]);

  const fmt = (n) => n == null ? '' : (n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n));
  const isActive = !!value;

  // Hide curated defaults the moment the user starts typing — they want to
  // see DB matches at that point, not our hand-picked shortcuts.
  const showDefaults = debouncedQ === '';

  return (
    <div className="fd-wrap" ref={wrapRef}>
      <button className={'f-chip fd-trigger' + (isActive ? ' on' : '') + (open ? ' open' : '')}
              onClick={() => setOpen(o => !o)}>
        {icon && <Icon name={icon} size={11}/>}
        <span className="fd-trigger-lbl">{value || placeholder}</span>
        {isActive && <span className="fd-clear-x"
                           onClick={e => { e.stopPropagation(); onChange(null); }}
                           title="Clear">
          <Icon name="x" size={10}/>
        </span>}
        <Icon name="chevron-down" size={11}/>
      </button>
      {open && (
        <div className="fd-pop fd-left">
          <div className="fd-search">
            <Icon name="search" size={11} color="var(--t3)"/>
            <input ref={inputRef}
                   placeholder={`Search ${placeholder.toLowerCase()} — type any value`}
                   value={q} onChange={e => setQ(e.target.value)}/>
            {q && <button className="fd-search-x" onClick={() => setQ('')}><Icon name="x" size={10}/></button>}
          </div>
          {showDefaults && defaults.length > 0 && (
            <>
              <div className="fd-section-h">
                <span><Icon name="bookmark" size={10}/> Quick picks</span>
              </div>
              <div className="fd-list">
                {defaults.map((d, i) => {
                  const sel = value === d.value;
                  return (
                    <button key={'d' + i} className={'fd-opt' + (sel ? ' selected' : '')}
                            onClick={() => { onChange(sel ? null : d.value); setOpen(false); setQ(''); }}>
                      {d.icon && <Icon name={d.icon} size={12} color="var(--t3)"/>}
                      <span className="fd-opt-lbl">{d.label}</span>
                      {sel && <Icon name="check" size={12}/>}
                    </button>
                  );
                })}
              </div>
            </>
          )}
          <div className="fd-section-h">
            <span><Icon name="database" size={10}/> {showDefaults ? 'Most active in index' : 'Live DB matches'}</span>
            {loading && <em className="fd-mini-meta">loading…</em>}
          </div>
          <div className="fd-list">
            {!loading && buckets.length === 0 && (
              <div className="fd-empty">
                {debouncedQ
                  ? `No ${placeholder.toLowerCase()} matches "${debouncedQ}".`
                  : `Index empty — try again in a moment.`}
              </div>
            )}
            {buckets.map((b, i) => {
              const sel = value === b.value;
              return (
                <button key={'b' + i} className={'fd-opt' + (sel ? ' selected' : '')}
                        onClick={() => { onChange(sel ? null : b.value); setOpen(false); setQ(''); }}>
                  <span className="fd-opt-lbl">{b.value}</span>
                  <em className="fd-meta">{fmt(b.count)}</em>
                  {sel && <Icon name="check" size={12}/>}
                </button>
              );
            })}
          </div>
          {isActive && (
            <button className="fd-clear-row" onClick={() => { onChange(null); setOpen(false); setQ(''); }}>
              <Icon name="x" size={11}/> Clear selection
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ── FilterDropdown — chip trigger + searchable popover ── */
function FilterDropdown({ placeholder, value, options, onChange, searchable = true, align = 'left', icon }) {
  const [open, setOpen] = useState(false);
  const [q, setQ]       = useState('');
  const wrapRef         = useRef(null);
  const inputRef        = useRef(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = e => { if (wrapRef.current && !wrapRef.current.contains(e.target)) setOpen(false); };
    const onKey = e => { if (e.key === 'Escape') setOpen(false); };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  useEffect(() => {
    if (open && inputRef.current) {
      // tiny delay so click that opened it doesn't immediately steal focus
      const id = setTimeout(() => inputRef.current?.focus(), 30);
      return () => clearTimeout(id);
    }
  }, [open]);

  const norm = q.trim().toLowerCase();
  const list = options.filter(opt => {
    if (!norm) return true;
    return (opt.label || '').toLowerCase().includes(norm)
        || (opt.meta  || '').toLowerCase().includes(norm);
  });

  const selected = options.find(o => o.value === value && value !== null && value !== undefined);
  const isActive = !!selected;
  const display  = selected ? selected.label : placeholder;

  return (
    <div className="fd-wrap" ref={wrapRef}>
      <button className={'f-chip fd-trigger' + (isActive ? ' on' : '') + (open ? ' open' : '')}
              onClick={() => setOpen(o => !o)}>
        {icon && <Icon name={icon} size={11}/>}
        <span className="fd-trigger-lbl">{display}</span>
        {isActive && <span className="fd-clear-x"
                           onClick={e => { e.stopPropagation(); onChange(null); }}
                           title="Clear">
          <Icon name="x" size={10}/>
        </span>}
        <Icon name="chevron-down" size={11}/>
      </button>
      {open && (
        <div className={'fd-pop fd-' + align}>
          {searchable && (
            <div className="fd-search">
              <Icon name="search" size={11} color="var(--t3)"/>
              <input ref={inputRef} placeholder={`Search ${placeholder.toLowerCase()}…`}
                     value={q} onChange={e => setQ(e.target.value)}/>
              {q && <button className="fd-search-x" onClick={() => setQ('')}><Icon name="x" size={10}/></button>}
            </div>
          )}
          <div className="fd-list">
            {list.length === 0 && <div className="fd-empty">No matches for "{q}"</div>}
            {list.map((opt, i) => {
              const sel = opt.value === value && value !== null && value !== undefined;
              return (
                <button key={i} className={'fd-opt' + (sel ? ' selected' : '')}
                        onClick={() => { onChange(sel ? null : opt.value); setOpen(false); setQ(''); }}>
                  <span className="fd-opt-lbl">{opt.label}</span>
                  {opt.meta != null && <em className="fd-meta">{opt.meta}</em>}
                  {sel && <Icon name="check" size={12}/>}
                </button>
              );
            })}
          </div>
          {isActive && (
            <button className="fd-clear-row" onClick={() => { onChange(null); setOpen(false); setQ(''); }}>
              <Icon name="x" size={11}/> Clear selection
            </button>
          )}
        </div>
      )}
    </div>
  );
}

/* ──────────────────────────────────────────────────────────
   Job-detail helpers — shared by JobsPage + JobDetailView.
   ────────────────────────────────────────────────────────── */
function _formatPostedAgo(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (!t) return '';
  const ms = Math.max(0, Date.now() - t);
  const s = Math.max(1, Math.round(ms / 1000));
  if (s < 60)         return s + 's ago';
  const m = Math.round(s / 60);
  if (m < 60)         return m + 'm ago';
  const h = Math.round(m / 60);
  if (h < 24)         return h + 'h ago';
  const d = Math.round(h / 24);
  if (d < 30)         return d + 'd ago';
  return Math.round(d / 30) + 'mo ago';
}

const _PLATFORM_BY_PREFIX = [
  ['ats:greenhouse',     'Greenhouse'],
  ['ats:lever',          'Lever'],
  ['ats:ashby',          'Ashby'],
  ['ats:workable',       'Workable'],
  ['api:themuse',        'The Muse'],
  ['api:remoteok',       'RemoteOK'],
  ['api:jobicy',         'Jobicy'],
  ['api:himalayas',      'Himalayas'],
  ['api:remotive',       'Remotive'],
  ['api:arbeitnow',      'Arbeitnow'],
  ['api:weworkremotely', 'We Work Remotely'],
  ['api:usajobs',        'USAJobs'],
  ['api:adzuna',         'Adzuna'],
  ['api:reed',           'Reed'],
  ['api:jooble',         'Jooble'],
  ['api:findwork',       'Findwork'],
  ['gh:simplify',        'SimplifyJobs'],
  ['gh:jobright',        'Jobright'],
  ['gh:speedyapply',     'SpeedyApply'],
  ['gh:vanshb03',        'Vanshb03'],
  ['gh:ouckah',          'Ouckah'],
  ['gh:pittcsc',         'PittCSC'],
];
function _prettyPlatform(source) {
  if (!source) return 'Direct';
  const s = String(source);
  for (const [pref, name] of _PLATFORM_BY_PREFIX) {
    if (s.startsWith(pref)) return name;
  }
  // ats:foo:bar → "Foo"
  const head = s.split(':')[1] || s.split(':')[0] || s;
  return head.charAt(0).toUpperCase() + head.slice(1);
}

function _humanLevel(value, fallback = '—') {
  if (!value || value === 'unknown') return fallback;
  return String(value).split(/[-_]/g)
    .map(p => p.charAt(0).toUpperCase() + p.slice(1))
    .join(' ');
}

/* Inline-highlight any user-skill mentions inside arbitrary text.
   Whole-word case-insensitive match, longest-skill-first so "C" doesn't
   pre-empt "C++". Returns a React fragment of plain strings + chip spans. */
function _highlightSkills(text, userSkills) {
  if (!text) return null;
  if (!Array.isArray(userSkills) || userSkills.length === 0) return text;
  const skills = [...userSkills]
    .filter(s => typeof s === 'string' && s.trim().length >= 2)
    .sort((a, b) => b.length - a.length);
  if (skills.length === 0) return text;
  const escaped = skills.map(s => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  // Lookbehind/ahead avoid matching across alphanumeric or punctuation
  // glued to the skill (so "c" doesn't match inside "scala", and "c++"
  // matches "C++ programming" cleanly).
  const re = new RegExp(`(?<![\\w+#.])(${escaped.join('|')})(?![\\w+#.])`, 'gi');
  const parts = [];
  let lastIndex = 0;
  let match;
  let key = 0;
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index));
    }
    parts.push(
      <span key={`hl-${key++}`} className="jd-skill-inline match">{match[0]}</span>
    );
    lastIndex = match.index + match[0].length;
    if (match[0].length === 0) re.lastIndex += 1;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return <>{parts}</>;
}

/* Pull a small set of "tagged" skills from the qualification bullet lines
   for the chip cloud above the Required/Preferred columns — jobright's
   "click on the tags" UI. Picks: every profile skill that appears in the
   qualifications text (=match, green) + a few obvious tech tokens that
   don't (=gap, gray). */
function _extractQualSkills(bullets, userSkills, max = 14) {
  const haystack = (bullets || []).join('\n');
  if (!haystack.trim()) return { matched: [], gaps: [] };

  const matched = [];
  const seenLower = new Set();
  for (const s of userSkills || []) {
    const sl = String(s).toLowerCase().trim();
    if (!sl || sl.length < 2 || seenLower.has(sl)) continue;
    const escaped = sl.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(`(?<![\\w+#.])${escaped}(?![\\w+#.])`, 'i');
    if (re.test(haystack)) {
      matched.push(s);
      seenLower.add(sl);
    }
  }
  const STOP = new Set([
    'the','and','for','with','from','our','your','their','this','that',
    'these','those','will','have','has','use','using','work','team','more',
    'who','what','when','where','which','required','preferred','plus',
    'years','year','must','should','can','any','all','one','two','three',
    'four','five','etc','strong','solid','great','good','familiar','related',
    'similar','including','design','development','experience','knowledge',
    'understanding','ability','skills','degree','field','area','various',
  ]);
  const gaps = [];
  const tokenRe = /\b([A-Za-z][A-Za-z0-9+#.\-]{1,28})\b/g;
  let tm;
  while ((tm = tokenRe.exec(haystack)) !== null) {
    const tok = tm[1];
    const tl  = tok.toLowerCase();
    if (seenLower.has(tl) || STOP.has(tl) || tl.length < 3) continue;
    const looksTech = (
      /[A-Z][a-z]+[A-Z]/.test(tok)               // CamelCase
      || /[+#.]/.test(tok)                        // C++, C#, .NET
      || (tok === tok.toUpperCase() && tok.length >= 2 && tok.length <= 6)
    );
    if (!looksTech) continue;
    seenLower.add(tl);
    gaps.push(tok);
    if (matched.length + gaps.length >= max) break;
  }
  return { matched, gaps };
}

function JobsPage({ state, refresh, setPage }) {
  const [tab, setTab]           = useState('recommended');
  const [searchQuery, setQuery] = useState('');
  const [running, setRun]       = useState(false);
  const [searchingMore, setSearchingMore] = useState(false);
  const [runLabel, setRunLabel] = useState('');
  const [askJob,  setAskJob]    = useState(null);  // active "Ask Atlas" target
  const [tailorJob, setTailorJob] = useState(null); // active "Tailor for this job" target
  // active "open the rich Overview/Company detail" target. When set, the page
  // swaps the list view for <JobDetailView/> while keeping the page-head
  // visible so the user can still hop between Recommended/Liked/etc.
  const [detailJob, setDetailJob] = useState(null);

  // Filter selections — null means "no filter for this dimension"
  const [fLocation,   setFLocation]   = useState(null);
  // fIndustries replaces the old "Title" chip. Multi-select array of canonical
  // job_category codes (engineering / sales / healthcare / …) — see
  // INDUSTRY_TILES above. null means "no industry filter".
  const [fIndustries, setFIndustries] = useState(null);
  const [fExp,        setFExp]        = useState(null);
  const [fModel,      setFModel]      = useState(null);
  const [fDateMax,    setFDateMax]    = useState(null);   // max age in days
  const [fSalary,     setFSalary]     = useState(null);   // min k$/yr
  const runningRef  = useRef(false);

  /* ── Feed v2: locally-owned, fed by /api/jobs/feed ── */
  const [feedJobs, setFeedJobs]       = useState([]);
  const [feedCursor, setFeedCursor]   = useState(null);
  const [feedTotal, setFeedTotal]     = useState(0);
  const [feedLoading, setFeedLoading] = useState(false);
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const seenIds     = useRef(new Set());
  const feedRequest = useRef(0);          // monotonic id; race-safe

  // Per-card on-demand scores (id → {score, has_jd, matched, missing}).
  // The feed's `score` field is now used only for ORDERING the list. The
  // displayed match score comes from this map and is populated lazily as
  // cards scroll into view — POST /api/jobs/score-batch fetches the
  // description on demand, scores via compute_skill_coverage, caches for
  // 1 h. Cards not yet scored render "—" rather than a misleading
  // title-only number.
  const [cardScores, setCardScores] = useState({});
  const cardScoresRef = useRef(cardScores);
  useEffect(() => { cardScoresRef.current = cardScores; }, [cardScores]);
  const scoreQueueRef    = useRef(new Set());
  const scoreInflightRef = useRef(new Set());
  const scoreFlushTimer  = useRef(null);

  const flushScoreQueue = useCallback(async () => {
    scoreFlushTimer.current = null;
    const queue = scoreQueueRef.current;
    const inflight = scoreInflightRef.current;
    const ids = [];
    for (const id of queue) {
      if (ids.length >= 30) break;
      if (!inflight.has(id)) ids.push(id);
    }
    if (!ids.length) return;
    ids.forEach(id => { queue.delete(id); inflight.add(id); });
    try {
      const data = await api.post('/api/jobs/score-batch', { job_ids: ids }, { timeoutMs: 20000 });
      const next = {};
      for (const r of (data?.scores || [])) {
        if (r && r.id) next[r.id] = r;
      }
      // Defensive: if the server didn't return an entry for some IDs we
      // requested (truncation, partial failure, schema mismatch), mark
      // those as failed locally so the cards don't stay in "Scoring…"
      // forever. Same shape the endpoint emits so JobCard handles them
      // identically.
      for (const id of ids) {
        if (!next[id]) {
          next[id] = { id, score: null, has_jd: false, reason: 'no result returned' };
        }
      }
      setCardScores(prev => ({ ...prev, ...next }));
    } catch (err) {
      // Network error / timeout / 5xx. Mark these IDs as failed so the
      // UI shows "—" with a tooltip rather than spinning forever. Cards
      // stay clickable; the next time the user filters or scrolls back
      // we re-queue and retry.
      const failed = {};
      const reason = (err && err.message) ? `request failed: ${err.message}` : 'request failed';
      for (const id of ids) {
        failed[id] = { id, score: null, has_jd: false, reason };
      }
      setCardScores(prev => ({ ...prev, ...failed }));
    } finally {
      ids.forEach(id => inflight.delete(id));
      // If more were queued during the in-flight call, schedule another
      // flush so we drain the backlog.
      if (queue.size > 0) {
        scoreFlushTimer.current = setTimeout(flushScoreQueue, 60);
      }
    }
  }, []);

  const queueScore = useCallback((jobId) => {
    if (!jobId) return;
    if (cardScoresRef.current[jobId]) return;            // already scored
    if (scoreInflightRef.current.has(jobId)) return;      // currently fetching
    scoreQueueRef.current.add(jobId);
    if (scoreFlushTimer.current) clearTimeout(scoreFlushTimer.current);
    scoreFlushTimer.current = setTimeout(flushScoreQueue, 220);
  }, [flushScoreQueue]);

  // Reset the score map whenever the search query / filters change — the
  // displayed match score must reflect THIS user's latest profile-vs-
  // posting comparison, and the server-side cache key is per-user-per-job
  // (1h TTL) so re-queueing is cheap.
  useEffect(() => {
    setCardScores({});
    scoreQueueRef.current = new Set();
    scoreInflightRef.current = new Set();
    if (scoreFlushTimer.current) {
      clearTimeout(scoreFlushTimer.current);
      scoreFlushTimer.current = null;
    }
  }, [debouncedQuery, fLocation, fIndustries, fExp, fModel, fDateMax]);

  const apps    = state?.applications || [];
  const liked   = new Set(state?.liked_ids || []);
  const hidden  = new Set(state?.hidden_ids || []);

  // Debounce free-text search to avoid querying on every keystroke
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQuery(searchQuery), 250);
    return () => clearTimeout(id);
  }, [searchQuery]);

  // Build the feed query string from the active filter chips. Industry,
  // location, exp, work-model, and date-posted are server-filtered. Salary is
  // applied client-side (no server filter for that yet — most rows have an
  // unparseable "Unknown" anyway).
  const feedQS = useMemo(() => {
    const params = new URLSearchParams();
    params.set('limit', '30');
    if (debouncedQuery.trim())               params.set('q', debouncedQuery.trim());
    if (fLocation && fLocation !== 'Anywhere') params.set('location', fLocation);
    if (Array.isArray(fIndustries) && fIndustries.length > 0) {
      // CSV — backend splits on comma in app.py::_csv. Keeps order stable.
      params.set('industry', fIndustries.join(','));
    }
    if (fExp) {
      const map = { 'Internship':'internship', 'Entry-level':'entry-level',
                    'Mid-level':'mid-level',   'Senior':'senior' };
      const v = map[fExp]; if (v) params.set('exp', v);
    }
    if (fModel === 'Remote')                 params.set('remote', '1');
    if (fDateMax != null)                    params.set('days', String(fDateMax));
    return params.toString();
  }, [debouncedQuery, fLocation, fIndustries, fExp, fModel, fDateMax]);

  // Load page 1 whenever the filters change. Tracks whether the last attempt
  // ended in error so the empty-state can auto-retry during server warm-up.
  const [feedError, setFeedError] = useState(false);
  const loadFirstPage = useCallback(async () => {
    const reqId = ++feedRequest.current;
    setFeedLoading(true);
    try {
      const data = await api.get('/api/jobs/feed?' + feedQS);
      if (reqId !== feedRequest.current) return;   // a newer request supersedes
      seenIds.current = new Set((data.jobs || []).map(j => j.id));
      setFeedJobs(data.jobs || []);
      setFeedCursor(data.next_cursor || null);
      setFeedTotal(data.total_estimate || 0);
      setFeedError(false);
    } catch (e) {
      // Feed load failed (timeout, 401, 5xx). Mark error so the empty-state
      // can show a useful message and the auto-retry below kicks in.
      if (reqId === feedRequest.current) setFeedError(true);
    }
    finally { if (reqId === feedRequest.current) setFeedLoading(false); }
  }, [feedQS]);

  useEffect(() => { loadFirstPage(); }, [loadFirstPage]);

  // Auto-retry empty/errored first-loads on a back-off. Without this, a
  // server hung during ingestion warm-up leaves the user permanently on the
  // empty state until they click Refresh — exactly the "stuck on loading"
  // symptom we just debugged. Stops once we have any jobs.
  useEffect(() => {
    if (feedJobs.length > 0) return undefined;        // got jobs; nothing to do
    if (feedLoading) return undefined;                // a fetch is already in flight
    if (!feedError && feedTotal > 0) return undefined; // healthy 0-result page (filters)
    const delay = feedError ? 5000 : 8000;
    const id = setTimeout(() => { loadFirstPage(); }, delay);
    return () => clearTimeout(id);
  }, [feedJobs.length, feedLoading, feedError, feedTotal, loadFirstPage]);

  // Cursor-based "load more" — used by both the scroll handler and the button.
  // Tracks consecutive empty responses so the IntersectionObserver doesn't
  // fire forever when the server returns a cursor but no new (post-dedup)
  // rows. After two empty pages in a row, drop the cursor and let the 25s
  // polling pick up any future ingestions.
  const emptyStreakRef = useRef(0);
  const loadMore = useCallback(async () => {
    if (!feedCursor || searchingMore) return;
    setSearchingMore(true);
    try {
      const data = await api.get(
        '/api/jobs/feed?' + feedQS + '&cursor=' + encodeURIComponent(feedCursor)
      );
      const fresh = (data.jobs || []).filter(j => !seenIds.current.has(j.id));
      fresh.forEach(j => seenIds.current.add(j.id));
      const nextCursor = data.next_cursor || null;
      if (fresh.length === 0) {
        emptyStreakRef.current += 1;
        if (emptyStreakRef.current >= 2 || nextCursor === feedCursor) {
          // Two empty pages in a row, OR a cursor that didn't advance →
          // we've genuinely exhausted the index. Stop the observer loop.
          setFeedCursor(null);
        } else {
          setFeedCursor(nextCursor);
        }
      } else {
        emptyStreakRef.current = 0;
        setFeedJobs(prev => [...prev, ...fresh]);
        setFeedCursor(nextCursor);
      }
    } catch (e) { /* load more failed — cursor resets on next filter change */ }
    finally { setSearchingMore(false); }
  }, [feedCursor, feedQS, searchingMore]);

  // 25 s background polling — prepend new rows the ingester just discovered.
  useEffect(() => {
    if (!feedJobs.length) return undefined;
    const tick = async () => {
      if (document.hidden) return;
      const topId = feedJobs[0]?.id;
      if (!topId) return;
      try {
        const data = await api.get(
          '/api/jobs/feed?since_id=' + encodeURIComponent(topId) + '&limit=20'
        );
        const fresh = (data.jobs || []).filter(j => !seenIds.current.has(j.id));
        if (fresh.length) {
          fresh.forEach(j => seenIds.current.add(j.id));
          setFeedJobs(prev => [...fresh, ...prev]);
        }
      } catch (_) { /* swallow — next tick will retry */ }
    };
    const id = setInterval(tick, 25_000);
    return () => clearInterval(id);
  }, [feedJobs]);

  // Lazy-score IntersectionObserver: queue a score request for any
  // [data-card-id] element that scrolls within ~400 px of the viewport.
  // Each card requests at most once per filter-set; the server-side cache
  // (per-user, 1 h TTL) absorbs duplicate requests across sessions.
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return undefined;
    const obs = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const id = e.target.getAttribute('data-card-id');
        if (id) queueScore(id);
      }
    }, { rootMargin: '400px 0px', threshold: 0.05 });
    // Observe every job card currently on the page. Re-runs when filtered
    // changes so newly-rendered cards (next page, search results, etc.)
    // get observed.
    const cards = document.querySelectorAll('.job-card[data-card-id]');
    cards.forEach(c => obs.observe(c));
    return () => obs.disconnect();
  }, [queueScore, feedJobs.length, debouncedQuery, fLocation, fIndustries, fExp, fModel, fDateMax]);

  const rawJobs = feedJobs;

  const POSTED_DAYS_BY_INDEX = [2, 7, 3, 0, 5, 0];

  const enrichedJobs = useMemo(() => rawJobs.map(j => {
    const h    = stableHash(j.id || `${j.co || ''}|${j.role || ''}`);
    const pIdx = h % POSTED_LABELS.length;
    return {
      ...j,
      _logo:        LOGO_VARIANTS[h % LOGO_VARIANTS.length],
      _model:       WORK_MODELS[h % WORK_MODELS.length],
      _exp:         EXP_LEVELS[h % EXP_LEVELS.length],
      _posted:      POSTED_LABELS[pIdx],
      _posted_days: POSTED_DAYS_BY_INDEX[pIdx] ?? 0,
    };
  }), [rawJobs]);

  const filtered = useMemo(() => {
    let list = enrichedJobs;
    if (tab === 'liked') list = list.filter(j => liked.has(j.id));
    else if (tab === 'applied') {
      const appTitles = new Set(apps.map(a => `${a.co}|${a.role}`));
      list = list.filter(j => appTitles.has(j.id));
    } else if (tab === 'recommended') list = list.filter(j => !hidden.has(j.id));

    if (searchQuery.trim()) {
      // Punctuation-insensitive client-side filter. The previous version
      // did a literal `String.includes(q)` against the company/role/skills
      // strings, which meant typing "mcdonalds" never matched "McDonald's"
      // (the apostrophe broke the substring check) — same problem with
      // "AT&T" / "att", "Kaiser-Permanente" / "kaiserpermanente",
      // "L'Oréal" / "loreal", "Procter & Gamble" / "procterandgamble",
      // etc. The server-side FTS5 already tokenizes through these via
      // unicode61 (treats non-alnum as separators) — the client filter
      // needs the same forgiveness so it doesn't strip results the
      // server correctly returned.
      //
      // We also tokenize the query into words and require ALL of them
      // to appear in the haystack (matches FTS5's implicit AND), so
      // multi-word queries like "software engineer" don't get filtered
      // away because the company name is "Atlas Software".
      // Unicode normalize first so accented chars decompose into base + combining
      // mark, then strip the combining marks. Effect: "L'Oréal" → "loreal",
      // "Nestlé" → "nestle", "Crédit Agricole" → "creditagricole". Without
      // this, typing "loreal" silently filters out the L'Oréal listing.
      // Two normalized forms per text — "&" maps two ways at once:
      //   form1 (strip-all): "AT&T" → "att", "Procter & Gamble" → "proctergamble"
      //   form2 (& → "and"): "AT&T" → "atandt", "Procter & Gamble" → "procterandgamble"
      // ANY-of-ANY substring match catches "att" / "atandt" both hitting
      // AT&T, and "procterandgamble" / "proctergamble" both hitting Procter
      // & Gamble. Then a word-by-word fallback catches multi-token queries
      // whose order differs ("software engineer atlas" vs "Atlas Software").
      const _flat = (s) => String(s || '')
        .normalize('NFD').replace(/[̀-ͯ]/g, '')
        .toLowerCase();
      const _norms = (s) => {
        const f = _flat(s);
        return [
          f.replace(/[^a-z0-9]+/g, ''),
          f.replace(/&/g, 'and').replace(/[^a-z0-9]+/g, ''),
        ].filter(Boolean);
      };
      const _wordsForm = (s) =>
        _flat(s).replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim();

      const qNorms = _norms(searchQuery);
      const qWords = _wordsForm(searchQuery).split(/\s+/).filter(Boolean);
      list = list.filter(j => {
        const hayNorms = [
          ..._norms(j.co), ..._norms(j.role), ..._norms(j.skills),
        ];
        const hayWords = _wordsForm(j.co) + ' ' + _wordsForm(j.role) + ' ' + _wordsForm(j.skills);
        if (qNorms.some(q => hayNorms.some(h => h.includes(q)))) return true;
        return qWords.length > 0 && qWords.every(w => hayWords.includes(w));
      });
    }

    if (fLocation && fLocation !== 'Anywhere') {
      const q = fLocation.toLowerCase();
      list = list.filter(j => (j.loc || '').toLowerCase().includes(q));
    }
    // Industry is server-filtered (job_category column) — no client mirror
    // needed. Doing it on the client too would require fetching every job's
    // category which we don't ship in the JSON shape today.
    if (fExp)             list = list.filter(j => j._exp === fExp);
    if (fModel)           list = list.filter(j => j._model === fModel);
    if (fDateMax != null) list = list.filter(j => (j._posted_days ?? 0) <= fDateMax);
    return list;
  }, [enrichedJobs, tab, searchQuery, liked, hidden, apps,
      fLocation, fExp, fModel, fDateMax]);

  // Curated quick-pick locations shown above the live DB facets in the chip
  // popover. Aligned with the global ingestion's coverage: every country we
  // now pull jobs from gets a country-level entry, plus the 1-3 highest-
  // density employer cities for that country. The DB-facet list below this
  // surfaces everything else (smaller cities, hybrid clusters, single-city
  // listings) sorted by inventory count. The dropdown is searchable so
  // volume here is fine — users type "berlin" or "tokyo" rather than
  // scanning. Regionally grouped for those who do scan.
  const locationDefaults = useMemo(() => [
    // Special intent
    { value: 'Remote',               label: 'Remote',          icon: 'globe' },
    { value: 'Anywhere',             label: 'Anywhere',        icon: 'globe' },

    // North America
    { value: 'United States',        label: 'United States',   icon: 'flag' },
    { value: 'San Francisco',        label: 'San Francisco',   icon: 'map-pin' },
    { value: 'New York',             label: 'New York',        icon: 'map-pin' },
    { value: 'Seattle',              label: 'Seattle',         icon: 'map-pin' },
    { value: 'Austin',               label: 'Austin',          icon: 'map-pin' },
    { value: 'Boston',               label: 'Boston',          icon: 'map-pin' },
    { value: 'Los Angeles',          label: 'Los Angeles',     icon: 'map-pin' },
    { value: 'Chicago',              label: 'Chicago',         icon: 'map-pin' },
    { value: 'Canada',               label: 'Canada',          icon: 'flag' },
    { value: 'Toronto',              label: 'Toronto',         icon: 'map-pin' },
    { value: 'Vancouver',            label: 'Vancouver',       icon: 'map-pin' },

    // UK & Western Europe
    { value: 'United Kingdom',       label: 'United Kingdom',  icon: 'flag' },
    { value: 'London',               label: 'London',          icon: 'map-pin' },
    { value: 'Edinburgh',            label: 'Edinburgh',       icon: 'map-pin' },
    { value: 'Ireland',              label: 'Ireland',         icon: 'flag' },
    { value: 'Dublin',               label: 'Dublin',          icon: 'map-pin' },
    { value: 'Germany',              label: 'Germany',         icon: 'flag' },
    { value: 'Berlin',               label: 'Berlin',          icon: 'map-pin' },
    { value: 'Munich',               label: 'Munich',          icon: 'map-pin' },
    { value: 'France',               label: 'France',          icon: 'flag' },
    { value: 'Paris',                label: 'Paris',           icon: 'map-pin' },
    { value: 'Netherlands',          label: 'Netherlands',     icon: 'flag' },
    { value: 'Amsterdam',            label: 'Amsterdam',       icon: 'map-pin' },
    { value: 'Spain',                label: 'Spain',           icon: 'flag' },
    { value: 'Madrid',               label: 'Madrid',          icon: 'map-pin' },
    { value: 'Barcelona',            label: 'Barcelona',       icon: 'map-pin' },
    { value: 'Italy',                label: 'Italy',           icon: 'flag' },
    { value: 'Milan',                label: 'Milan',           icon: 'map-pin' },
    { value: 'Sweden',               label: 'Sweden',          icon: 'flag' },
    { value: 'Stockholm',            label: 'Stockholm',       icon: 'map-pin' },
    { value: 'Switzerland',          label: 'Switzerland',     icon: 'flag' },
    { value: 'Zurich',               label: 'Zurich',          icon: 'map-pin' },
    { value: 'Poland',               label: 'Poland',          icon: 'flag' },
    { value: 'Warsaw',               label: 'Warsaw',          icon: 'map-pin' },

    // Asia-Pacific
    { value: 'Singapore',            label: 'Singapore',       icon: 'flag' },
    { value: 'Hong Kong',            label: 'Hong Kong',       icon: 'flag' },
    { value: 'Japan',                label: 'Japan',           icon: 'flag' },
    { value: 'Tokyo',                label: 'Tokyo',           icon: 'map-pin' },
    { value: 'South Korea',          label: 'South Korea',     icon: 'flag' },
    { value: 'Seoul',                label: 'Seoul',           icon: 'map-pin' },
    { value: 'Taiwan',               label: 'Taiwan',          icon: 'flag' },
    { value: 'India',                label: 'India',           icon: 'flag' },
    { value: 'Bangalore',            label: 'Bangalore',       icon: 'map-pin' },
    { value: 'Mumbai',               label: 'Mumbai',          icon: 'map-pin' },
    { value: 'Delhi',                label: 'Delhi',           icon: 'map-pin' },
    { value: 'Hyderabad',            label: 'Hyderabad',       icon: 'map-pin' },
    { value: 'Australia',            label: 'Australia',       icon: 'flag' },
    { value: 'Sydney',               label: 'Sydney',          icon: 'map-pin' },
    { value: 'Melbourne',            label: 'Melbourne',       icon: 'map-pin' },
    { value: 'New Zealand',          label: 'New Zealand',     icon: 'flag' },

    // Middle East & Africa
    { value: 'United Arab Emirates', label: 'UAE',             icon: 'flag' },
    { value: 'Dubai',                label: 'Dubai',           icon: 'map-pin' },
    { value: 'Israel',               label: 'Israel',          icon: 'flag' },
    { value: 'Tel Aviv',             label: 'Tel Aviv',        icon: 'map-pin' },
    { value: 'South Africa',         label: 'South Africa',    icon: 'flag' },
    { value: 'Cape Town',            label: 'Cape Town',       icon: 'map-pin' },

    // Latin America
    { value: 'Brazil',               label: 'Brazil',          icon: 'flag' },
    { value: 'São Paulo',            label: 'São Paulo',       icon: 'map-pin' },
    { value: 'Mexico',               label: 'Mexico',          icon: 'flag' },
    { value: 'Mexico City',          label: 'Mexico City',     icon: 'map-pin' },
    { value: 'Argentina',            label: 'Argentina',       icon: 'flag' },
    { value: 'Colombia',             label: 'Colombia',        icon: 'flag' },
  ], []);

  const expOptions = [
    { value:'Internship',  label:'Internship'  },
    { value:'Entry-level', label:'Entry-level' },
    { value:'Mid-level',   label:'Mid-level'   },
    { value:'Senior',      label:'Senior'      },
  ];
  const modelOptions = [
    { value:'Onsite', label:'Onsite' },
    { value:'Hybrid', label:'Hybrid' },
    { value:'Remote', label:'Remote' },
  ];
  const dateOptions = [
    { value: 1,  label: 'Last 24 hours' },
    { value: 3,  label: 'Last 3 days'   },
    { value: 7,  label: 'Last 7 days'   },
    { value: 14, label: 'Last 14 days'  },
    { value: 30, label: 'Last 30 days'  },
  ];
  const salaryOptions = [
    { value: 40,  label: '$40k+',  meta: 'entry'  },
    { value: 60,  label: '$60k+',  meta: 'mid'    },
    { value: 100, label: '$100k+', meta: 'senior' },
    { value: 150, label: '$150k+', meta: 'staff'  },
    { value: 200, label: '$200k+', meta: 'lead'   },
  ];

  const activeFilterCount =
    (fLocation && fLocation !== 'Anywhere' ? 1 : 0) +
    (Array.isArray(fIndustries) && fIndustries.length > 0 ? 1 : 0) +
    (fExp ? 1 : 0) + (fModel ? 1 : 0) +
    (fDateMax != null ? 1 : 0) + (fSalary != null ? 1 : 0);
  const clearAllFilters = () => {
    setFLocation(null); setFIndustries(null); setFExp(null);
    setFModel(null); setFDateMax(null); setFSalary(null);
  };

  const tabCounts = {
    recommended: rawJobs.filter(j => !hidden.has(j.id)).length,
    liked:       liked.size,
    applied:     apps.length,
    external:    0,
  };

  const handleAction = async (action, job) => {
    const job_id = job.id || `${job.co}|${job.role}`;
    try {
      await api.post('/api/jobs/action', { action, job_id });
      refresh();
    } catch (e) {
      alert(e.message || `Could not ${action} this job. Please try again.`);
    }
  };

  const removeSearch = async (title) => {
    const nextTitles = (state?.profile?.target_titles || []).filter(t => t !== title);
    try {
      await api.post('/api/profile', { ...state.profile, target_titles: nextTitles });
      refresh();
    } catch (e) {
      alert(e.message || 'Could not remove search title.');
    }
  };

  const anyExtracting = (state?.resumes || []).some(r => r.extracting);

  // Force the ingestion worker to tick every source (dev-only) and reload
  // the feed. The source-tick fires in the BACKGROUND because force_run() in
  // the ingestion worker waits for every registered source (16+ external
  // APIs) to finish — that's a 30-90 s wall-clock. Awaiting it made the
  // Refresh button look hung. Instead we fire-and-forget, return the
  // user's existing inventory immediately, and tail the feed with a few
  // follow-up reloads so freshly-ingested rows surface within ~12 s
  // without a second click.
  //
  // A monotonic refreshSeq lets a fresh click pre-empt the in-flight
  // polling loop without us double-clearing the running flag.
  const refreshSeqRef = useRef(0);
  const [refreshToast, setRefreshToast] = useState(null);
  const handleRefresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current;
    runningRef.current = true; setRun(true); setRunLabel('Refreshing');
    // Snapshot the currently-visible job ids so we can show "+N new" /
    // "Up to date" after the refresh completes — without this, a refresh
    // that returns identical rows looks broken to the user.
    const beforeIds = new Set(seenIds.current);
    try {
      if (state?.is_dev) {
        // Fire-and-forget. Server-side locks already serialize per-source
        // ticks, so even rapid spam-clicks can't overload anything.
        api.post('/api/jobs/source-status', {}).catch(() => {});
      }
      // Immediate refresh from whatever's already indexed — this is the
      // ~250 ms response the user actually wants from the button.
      await loadFirstPage();
      if (seq !== refreshSeqRef.current) return;  // newer click superseded us

      // Dev only: poll a few times so newly-ingested rows show up. The
      // intervals (3 s / 4 s / 5 s) front-load updates while sources are
      // racing to complete their fetches and back off as the wave settles.
      if (state?.is_dev) {
        setRunLabel('Re-indexing');
        for (const wait of [3000, 4000, 5000]) {
          await new Promise(r => setTimeout(r, wait));
          if (seq !== refreshSeqRef.current) return;
          await loadFirstPage();
          if (seq !== refreshSeqRef.current) return;
        }
      }
      // Compute what changed so the user gets a concrete confirmation.
      let newCount = 0;
      for (const id of seenIds.current) if (!beforeIds.has(id)) newCount++;
      setRefreshToast(newCount > 0
        ? { kind: 'new', text: `+${newCount} new ${newCount === 1 ? 'job' : 'jobs'}` }
        : { kind: 'ok',  text: 'Up to date' });
    } finally {
      // Only the LATEST refresh sequence resets the running flag; an
      // older sequence pre-empted by a newer click does nothing here.
      if (seq === refreshSeqRef.current) {
        runningRef.current = false; setRun(false); setRunLabel('');
      }
    }
  }, [state?.is_dev, loadFirstPage]);

  // Auto-dismiss the refresh toast after a few seconds.
  useEffect(() => {
    if (!refreshToast) return undefined;
    const id = setTimeout(() => setRefreshToast(null), 3500);
    return () => clearTimeout(id);
  }, [refreshToast]);

  // "Deep search" used to flip a JobSpy flag. With the new aggregated pipeline
  // the difference is just a wider date window, so we drop the date chip and
  // refresh.
  const handleDeepSearch = useCallback(async () => {
    setFDateMax(null);
    await handleRefresh();
  }, [handleRefresh]);

  // Backup scroll-event handler (in case IntersectionObserver isn't supported
  // or the sentinel hasn't mounted yet). The real driver is the sentinel
  // below — this just handles edge cases where the observer doesn't fire.
  const onScroll = (e) => {
    if (searchingMore || feedLoading || tab !== 'recommended') return;
    if (!feedCursor) return;
    const { scrollTop, scrollHeight, clientHeight } = e.target;
    if (scrollHeight - scrollTop - clientHeight < 800) {
      loadMore();
    }
  };

  // IntersectionObserver-driven infinite scroll. Reliable across nested
  // scroll containers, off-screen detection, and pointer-event quirks that
  // make the scroll-event approach miss triggers near the bottom.
  const sentinelRef = useRef(null);
  useEffect(() => {
    if (tab !== 'recommended') return undefined;
    if (!feedCursor) return undefined;
    const node = sentinelRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') return undefined;
    const io = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting && !searchingMore && !feedLoading) {
          loadMore();
          break;
        }
      }
    }, { rootMargin: '600px 0px 600px 0px', threshold: 0 });
    io.observe(node);
    return () => io.disconnect();
  }, [feedCursor, searchingMore, feedLoading, tab, loadMore]);


  return (
    <>
      <div className="page-head">
        <div className="page-title">JOBS</div>
        <span className="page-tab-sep">›</span>
        <div className="page-tabs">
          {[['recommended','Recommended'],['liked','Liked'],['applied','Applied'],['external','External']].map(([id, label]) => (
            <button key={id} className={'page-tab' + (tab===id ? ' active' : '')}
                    onClick={() => { setTab(id); setDetailJob(null); }}>
              {label}
              {tabCounts[id] != null && <span className="tab-count">{tabCounts[id]}</span>}
            </button>
          ))}
        </div>
        <div className="head-spacer"/>
        {!detailJob && (
          <>
            <div className="head-search">
              <Icon name="search" size={13} color="var(--t3)"/>
              <input placeholder="Search roles or companies" value={searchQuery} onChange={e => setQuery(e.target.value)}/>
            </div>
            <button className="head-cta" onClick={handleRefresh}>
              {running ? <><span className="spin"/> {runLabel || 'Refreshing'}…</> : <><Icon name="refresh-cw" size={13} color="#fff"/> Refresh</>}
            </button>
            {refreshToast && !running && (
              <span className={'refresh-toast ' + refreshToast.kind} aria-live="polite">
                <Icon name={refreshToast.kind === 'new' ? 'sparkles' : 'check'} size={11}/>
                {refreshToast.text}
              </span>
            )}
            <button className="btn-ghost" onClick={handleDeepSearch} disabled={running} style={{ marginLeft:8 }}>
              <Icon name="radar" size={12}/> Deep search
            </button>
          </>
        )}
      </div>

      {detailJob ? (
        <JobDetailView
          job={detailJob}
          profile={state?.profile}
          allJobs={feedJobs}
          scoreData={cardScores[detailJob.id]}
          allScores={cardScores}
          isLiked={liked.has(detailJob.id)}
          isHidden={hidden.has(detailJob.id)}
          onClose={() => setDetailJob(null)}
          onAsk={(j) => setAskJob(j)}
          onTailor={(j) => setTailorJob(j)}
          onLike={(j) => handleAction(liked.has(j.id) ? 'unlike' : 'like', j)}
          onHide={(j) => handleAction('hide', j)}
          onSwitchTo={(j) => setDetailJob(j)}
        />
      ) : (
      <div className="page-body" onScroll={onScroll} style={{ overflowY: 'auto' }}>
        <div className="col-main">
          {/* Profile-incomplete banner — when the user has < 2 hard skills
              AND no target titles, scoring against jobs is meaningless;
              every match number would be derived purely from text-search
              relevance. We surface "—" on each card and explain why here
              so the user knows what to fix instead of seeing fake numbers
              like "68% match" on a senior hardware role from a blank
              resume. Mirrors the server-side `_profile_is_meaningful`
              gate so client + server stay aligned. */}
          {(() => {
            const sk = (state?.profile?.top_hard_skills || []).filter(s => s && String(s).trim());
            const tt = (state?.profile?.target_titles || []).filter(t => t && String(t).trim());
            if (sk.length >= 2 || tt.length >= 1) return null;
            return (
              <div className="profile-gate-banner" role="status">
                <span className="profile-gate-icon"><Icon name="user-plus" size={16}/></span>
                <div className="profile-gate-body">
                  <div className="profile-gate-h">Match scoring is paused</div>
                  <div className="profile-gate-p">
                    Add at least <b>2 hard skills</b> or <b>1 target title</b> to your profile so Atlas can compute real fit scores. Until then every card shows a neutral <code>—</code> instead of a fabricated percentage.
                  </div>
                </div>
                <button className="profile-gate-cta" onClick={() => setPage('profile')}>
                  Complete profile
                  <span className="profile-gate-arrow">→</span>
                </button>
              </div>
            );
          })()}

          {/* Filter chips — searchable dropdowns */}
          <div className="filters">
            <BackendFacetFilter placeholder="Location" kind="location" value={fLocation} onChange={setFLocation}
                                icon="map-pin" defaults={locationDefaults}/>
            <IndustryFilter        value={fIndustries} onChange={setFIndustries}/>
            <FilterDropdown placeholder="Experience level" value={fExp}      onChange={setFExp}      options={expOptions}    searchable={false} icon="graduation-cap"/>
            <FilterDropdown placeholder="Work model"       value={fModel}    onChange={setFModel}    options={modelOptions}  searchable={false} icon="building-2"/>
            <FilterDropdown placeholder="Date posted"      value={fDateMax}  onChange={setFDateMax}  options={dateOptions}   searchable={false} icon="calendar"/>
            <FilterDropdown placeholder="Salary"           value={fSalary}   onChange={setFSalary}   options={salaryOptions} searchable={false} icon="banknote"/>
            {activeFilterCount > 0 && (
              <>
                <div className="f-divider"/>
                <button className="f-action secondary" onClick={clearAllFilters}>
                  <Icon name="x" size={11}/> Clear {activeFilterCount}
                </button>
              </>
            )}
            <div className="f-divider"/>
            <button className="f-action secondary" onClick={() => setPage('profile')} title="Open your profile to refine targeting">
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
              <div style={{ fontSize:20, fontWeight:600, marginBottom:6 }}>
                {tab === 'liked' ? 'No saved jobs' : tab === 'applied' ? 'No applications yet' : 'No matched jobs yet'}
              </div>
              <div style={{ fontSize:15.5, color:'var(--t2)', maxWidth:400, margin:'0 auto 18px', lineHeight:1.55 }}>
                {feedLoading || running
                  ? 'Loading jobs from the live index…'
                  : tab === 'liked'
                    ? 'Jobs you save with the bookmark icon will appear here.'
                    : tab === 'applied'
                      ? 'Roles you\'ve applied to will appear here once submitted.'
                      : feedTotal > 0
                        ? 'No jobs matched these filters — try clearing the chips or widening the date window.'
                        : 'The job index is warming up — first results land within a few seconds. The page auto-refreshes every 25 seconds.'}
              </div>
              {tab === 'recommended' && (
                <button className="btn-primary" onClick={handleRefresh} disabled={running} style={{ margin:'0 auto' }}>
                  {running ? <span className="spin"/> : <Icon name="sparkles" size={13} color="#fff"/>}
                  {running ? `${runLabel || 'Refreshing'}…` : 'Refresh feed'}
                </button>
              )}
            </div>
          ) : (
            <div className="job-list">
              {filtered.map((j, i) => (
                <JobCard key={j.id || i} idx={i} job={j}
                  scoreData={cardScores[j.id]}
                  isLiked={liked.has(j.id)}
                  onLike={() => handleAction(liked.has(j.id) ? 'unlike' : 'like', j)}
                  onHide={() => handleAction('hide', j)}
                  onAsk={() => setAskJob(j)}
                  onTailor={() => setTailorJob(j)}
                  onSelect={() => setDetailJob(j)}/>
              ))}

              {/* Sentinel — IntersectionObserver fires loadMore when this
                  scrolls into view (with a 600px rootMargin lookahead).
                  Always rendered while there's more to fetch so the scroll
                  driver can re-attach after each load. */}
              {tab === 'recommended' && feedCursor && (
                <div ref={sentinelRef} style={{ padding: 24, textAlign: 'center', color: 'var(--t3)' }}>
                  <span className="spin" style={{ marginRight: 8 }}/> Loading more roles…
                </div>
              )}

              {/* Manual fallback — only shown when the observer was disabled
                  (e.g. no cursor) but jobs still exist. Lets the user kick
                  the polling tick to fetch any newly-ingested rows. */}
              {!searchingMore && tab === 'recommended' && !feedCursor && feedJobs.length > 0 && (
                <div style={{ padding: 28, textAlign: 'center', color: 'var(--t3)', fontSize: 13.5 }}>
                  <div style={{ marginBottom: 10 }}>
                    Showing {feedJobs.length} of {feedTotal} jobs. New roles surface every 25 s as the ingester finds them.
                  </div>
                  <button className="btn-ghost" onClick={loadFirstPage}>
                    <Icon name="refresh-cw" size={12}/> Check for new jobs
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
              <button
                key={i}
                type="button"
                className="ss-row"
                onClick={() => { setQuery(t); setTab('recommended'); }}
                title={`Apply this search · ${t} in ${state?.location || 'US'}`}
              >
                <span className="ss-num">{String(i + 1).padStart(2, '0')}</span>
                <span className="ss-spine" aria-hidden="true"/>
                <span className="ss-body">
                  <span className="ss-title">{t}</span>
                  <span className="ss-loc">
                    <Icon name="map-pin" size={9}/>{state?.location || 'US'}
                  </span>
                </span>
                <span className="ss-actions" onClick={(e) => e.stopPropagation()}>
                  <span
                    className="ss-act"
                    onClick={(e) => { e.stopPropagation(); setPage('profile'); }}
                    title="Edit titles"
                  ><Icon name="pencil" size={11}/></span>
                  <span
                    className="ss-act ss-act-del"
                    onClick={(e) => { e.stopPropagation(); removeSearch(t); }}
                    title="Remove"
                  ><Icon name="trash-2" size={11}/></span>
                </span>
              </button>
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
                  <div key={n} style={{ display:'flex', alignItems:'center', gap:10, fontSize:15 }}>
                    <div style={{ width:22, height:22, borderRadius:6, flexShrink:0, display:'flex', alignItems:'center', justifyContent:'center', fontFamily:'var(--mono)', fontSize:13, fontWeight:600, background: isDone ? 'var(--accent-d)' : 'var(--bg-3)', color: isDone ? 'var(--accent-h)' : 'var(--t4)', border:'1px solid ' + (isDone ? 'var(--accent-b)' : 'var(--bdr)') }}>
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
      )}

      {askJob && (
        <AskAtlas job={askJob} mode={state?.mode} isPro={!!state?.is_pro}
                  isDev={!!state?.is_dev}
                  scoreData={cardScores[askJob.id]}
                  onClose={() => setAskJob(null)}/>
      )}
      {tailorJob && (
        <TailorDrawer job={tailorJob} mode={state?.mode} isPro={!!state?.is_pro}
                      isDev={!!state?.is_dev}
                      hasResume={!!state?.profile}
                      scoreData={cardScores[tailorJob.id]}
                      onOpenDocuments={() => { setTailorJob(null); setPage('documents'); }}
                      onClose={() => setTailorJob(null)}/>
      )}
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   JOB DETAIL VIEW — Overview + Company sub-page
   ══════════════════════════════════════════════════════════
   Replaces the JobsPage list when a card is clicked. All data
   flows from the existing /api/jobs/feed shape — no extra
   fetches. Match analysis is derived client-side from the
   current profile (top_hard_skills + target_titles + location)
   so the score breakdown stays in sync with the score on
   the JobCard ring without round-tripping the LLM rubric. */

function JobDetailView({ job, profile, allJobs, scoreData, allScores,
                          isLiked, isHidden,
                          onClose, onAsk, onTailor, onLike, onHide,
                          onSwitchTo }) {
  const [tab, setTab] = useState('overview');
  const bodyRef = useRef(null);

  // ── Live-fetched detail payload — full description + parsed sections + Wikipedia ──
  // The list view's job DTOs are intentionally metadata-only (description
  // bodies were dropped at ingest to keep the SQLite index lean). This
  // detail view re-fetches the full posting from the upstream source on
  // demand and parses it into responsibilities/required/preferred buckets,
  // plus a Wikipedia-derived company summary. Both are cached server-side
  // so opening the same posting twice never re-hits the upstream APIs.
  const [details,        setDetails]        = useState(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError,   setDetailsError]   = useState(null);

  // Esc closes — but skip when AskAtlas / TailorDrawer is open over us
  // (those drawers manage their own Esc and would double-fire).
  useEffect(() => {
    const onKey = e => {
      if (e.key !== 'Escape') return;
      if (document.querySelector('.ask-overlay, .tailor-overlay')) return;
      onClose?.();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Reset to Overview tab + scroll to top whenever the active job changes
  // (covers both first-open and "switch to another role" from the Company tab).
  useEffect(() => {
    setTab('overview');
    if (bodyRef.current) bodyRef.current.scrollTop = 0;
  }, [job?.id]);

  // Fetch the live detail payload whenever the active job id changes.
  // The /api/jobs/{id}/details endpoint composes (a) full description from
  // the source's per-job API and (b) Wikipedia company summary. Skip the
  // fetch when the id is empty (defensive — shouldn't happen in practice).
  useEffect(() => {
    if (!job?.id) {
      setDetails(null);
      setDetailsError(null);
      setDetailsLoading(false);
      return;
    }
    let cancelled = false;
    setDetails(null);
    setDetailsError(null);
    setDetailsLoading(true);
    api.get(`/api/jobs/${encodeURIComponent(job.id)}/details`, { timeoutMs: 25000 })
      .then(d => { if (!cancelled) setDetails(d); })
      .catch(err => { if (!cancelled) setDetailsError(err?.message || 'Could not load job details'); })
      .finally(() => { if (!cancelled) setDetailsLoading(false); });
    return () => { cancelled = true; };
  }, [job?.id]);

  // ── Skill alignment ──────────────────────────────────────────────────
  // Two sources, in priority order:
  //   1. scoreData (from /api/jobs/score-batch) — the SAME description-aware
  //      scorer the JobCard uses. Reads title + requirements + full
  //      description against profile.top_hard_skills. Returns: score (0-100),
  //      matched (top 6), missing (top 6), coverage (0-1). This is the
  //      authoritative number we want the user to see.
  //   2. Client-side recompute against `job.skills` — only when scoreData
  //      hasn't arrived yet (first paint, network in-flight, scoreData
  //      failed). Keeps the panel populated instead of showing zeros.
  // Profile-meaningfulness gate — mirrors the backend's
  // _profile_is_meaningful (≥2 hard skills OR ≥1 target title). If the
  // profile is below the threshold (e.g., a blank 2-word resume that
  // extracted nothing useful), refuse to compute fit signals at all.
  // The previous version had per-signal client-side fallbacks that
  // fabricated "60% title alignment" and "100% location & seniority"
  // for any blank profile — exactly the bug the user reported. Now
  // every signal is 0 (and the panels render an honest "—" with
  // copy directing the user to the Profile page).
  const userSkills = (profile?.top_hard_skills || [])
    .map(s => String(s).toLowerCase().trim()).filter(Boolean);
  const userSkillsSet = new Set(userSkills);
  const jobReqs = String(job.skills || '')
    .split(',').map(s => s.trim()).filter(Boolean);
  // Target titles: prefer the explicit `target_titles` extraction, but fall
  // back to verbatim work-experience titles so a profile that didn't fire
  // the title-rules still has a real title-alignment signal (otherwise
  // every job reads 0% title alignment even for clear matches).
  let targetTitlesAll = (profile?.target_titles || [])
    .map(t => String(t).trim()).filter(Boolean);
  if (!targetTitlesAll.length) {
    const seen = new Set();
    for (const bucket of ['work_experience', 'experience', 'research_experience']) {
      for (const row of (profile?.[bucket] || [])) {
        const t = row && typeof row === 'object' ? String(row.title || '').trim() : '';
        if (t && !seen.has(t.toLowerCase())) {
          seen.add(t.toLowerCase());
          targetTitlesAll.push(t);
        }
        if (targetTitlesAll.length >= 6) break;
      }
      if (targetTitlesAll.length >= 6) break;
    }
  }
  // Has any meaningful work history? Even before target_titles get
  // extracted, a resume with actual roles has enough signal to score.
  const hasWorkSignal = [
    'work_experience', 'experience', 'research_experience'
  ].some(b => (profile?.[b] || []).some(r => r && (r.title || r.company)));
  const profileMeaningful = userSkills.length >= 2
                          || targetTitlesAll.length >= 1
                          || hasWorkSignal;

  const haveLazy = scoreData && typeof scoreData.score === 'number';
  let matched, gaps, skillPct;
  if (!profileMeaningful) {
    // No fit math when the profile lacks signal. Zero everything; the
    // rail panels render explicit "Profile incomplete" copy below.
    matched  = [];
    gaps     = [];
    skillPct = 0;
  } else if (haveLazy && (Array.isArray(scoreData.matched) || Array.isArray(scoreData.missing))) {
    matched = scoreData.matched || [];
    gaps    = scoreData.missing || [];
    skillPct = (typeof scoreData.coverage === 'number')
      ? Math.round(scoreData.coverage * 100)
      : (matched.length + gaps.length > 0
          ? Math.round((matched.length / (matched.length + gaps.length)) * 100)
          : 0);
  } else {
    matched = [];
    gaps    = [];
    for (const r of jobReqs) {
      const lr = r.toLowerCase();
      const hit = userSkillsSet.has(lr) ||
                  userSkills.some(s => s && (lr.includes(s) || s.includes(lr)));
      (hit ? matched : gaps).push(r);
    }
    // No "userSkills.length ? 50 : 0" fallback — that previously
    // injected a flat 50% for any profile-with-skills regardless of
    // job-fit, which lit up the rail panel for unrelated roles.
    skillPct = jobReqs.length
      ? Math.round((matched.length / jobReqs.length) * 100)
      : 0;
  }

  // ── Title alignment vs. profile target_titles ────────────────────────
  // Prefer the server's `title_match` value (same logic the score-batch
  // endpoint applied) so the panel agrees with the headline score; fall
  // back to a local token-overlap compute against the (now broadened)
  // target_titles when scoreData hasn't arrived yet.
  const targetTitles = profileMeaningful
    ? targetTitlesAll.map(t => t.toLowerCase())
    : [];
  const titleLower   = String(job.role || '').toLowerCase();
  let titlePct = 0;
  if (haveLazy && typeof scoreData.title_match === 'number') {
    titlePct = Math.round(Math.max(0, Math.min(1, scoreData.title_match)) * 100);
  } else {
    for (const t of targetTitles) {
      const toks = t.split(/\s+/).filter(w => w.length > 2);
      if (!toks.length) continue;
      const hits = toks.filter(w => titleLower.includes(w)).length;
      titlePct = Math.max(titlePct, Math.round((hits / toks.length) * 100));
    }
  }

  // ── Location & seniority fit ─────────────────────────────────────────
  // Returns 0 when the profile has no location AND the job isn't
  // explicitly remote-friendly to a profile-with-known-region. The
  // previous `job.remote ? 100 : ...` produced "100% location" for
  // every remote role regardless of whether we knew anything about
  // the user's location preferences — fabricated again for blank
  // resumes. Now: remote alone isn't enough — we need a profile
  // location to confirm the user actually wants remote OR a generic
  // US/global qualifier the user has on file.
  const profileLoc = (profile?.location || '').toLowerCase().split(',')[0].trim();
  const jobLoc     = String(job.loc || '').toLowerCase();
  const locFitGeneric = /united states|usa|us|north america|remote|anywhere/i.test(job.loc || '');
  const profileLocHit = profileLoc && jobLoc.includes(profileLoc);
  let locPct;
  if (haveLazy && typeof scoreData.loc_seniority === 'number') {
    locPct = Math.round(Math.max(0, Math.min(1, scoreData.loc_seniority)) * 100);
  } else if (!profileMeaningful) {
    locPct = 0;
  } else if (profileLocHit) {
    locPct = 95;
  } else if (job.remote && profileLoc) {
    // Remote helps when we know the user has a base region — they
    // can take it. Without a known region, we don't know if they'd
    // accept remote-only, so we don't claim a high fit.
    locPct = 90;
  } else if (locFitGeneric && profileLoc) {
    locPct = 70;
  } else if (profileLoc) {
    locPct = 30;
  } else {
    // Profile meaningful (has titles or skills) but no location set —
    // mid-low signal, not zero (we have *some* fit signal from titles)
    // but not the inflated 100% the previous code produced.
    locPct = job.remote ? 50 : 25;
  }

  // ── Overall match metadata ───────────────────────────────────────────
  // Single source of truth: prefer the description-aware lazy score
  // (same number JobCard shows after the IntersectionObserver fetch).
  // Fall back to job.score (the rerank composite from /api/jobs/feed,
  // already 0-100 via _dto_to_json) only when the lazy score hasn't
  // arrived yet. This keeps card-vs-detail score consistent.
  const score = haveLazy
    ? Math.max(0, Math.min(100, Math.round(scoreData.score)))
    : Math.max(0, Math.min(100, Math.round(job.score || 0)));

  // Weighted point breakdown — mirrors providers.RUBRIC_WEIGHTS so the
  // user sees exactly the same 50/30/20 math the scorer applied. With
  // lazy data we use `coverage` directly; without it we fall back to the
  // client-side skillPct ratio above.
  const coverageFraction = haveLazy && typeof scoreData.coverage === 'number'
    ? scoreData.coverage
    : skillPct / 100;
  const titleFraction    = titlePct / 100;
  const locFraction      = locPct   / 100;
  const breakdown = [
    { key:'skills',   label:'Required skills',    weight:50, raw:coverageFraction,
      points:Math.round(50 * coverageFraction),
      hint:'How many of the job’s requirements your profile already covers' },
    { key:'industry', label:'Title · industry', weight:30, raw:titleFraction,
      points:Math.round(30 * titleFraction),
      hint:'Match between your target titles and the job title' },
    { key:'location', label:'Location · seniority', weight:20, raw:locFraction,
      points:Math.round(20 * locFraction),
      hint:'Geographic fit and seniority alignment' },
  ];
  // Sum of the rubric components — the panel's header + footer show this
  // so the panel is internally consistent. Differs from `score` (the hero
  // ring) when no lazy data has arrived: the rerank composite (job.score)
  // and the rubric sum measure different things, so showing both honestly
  // is more useful than papering over the gap.
  const breakdownTotal = breakdown.reduce((s, b) => s + b.points, 0);
  const breakdownTone =
      breakdownTotal >= 85 ? 'good'
    : breakdownTotal >= 70 ? 'accent'
    : breakdownTotal >= 55 ? 'warn'
    :                        'bad';
  const verdict =
      score >= 85 ? { label: 'Strong Match', tone: 'good' }
    : score >= 70 ? { label: 'Good Match',   tone: 'accent' }
    : score >= 55 ? { label: 'Fair Match',   tone: 'warn' }
    :              { label: 'Reach Match',  tone: 'bad' };
  const ringColor =
      score >= 85 ? 'var(--good)'
    : score >= 70 ? 'var(--accent-h)'
    : score >= 55 ? 'var(--warn)'
    :              'var(--bad)';

  const RING_R = 86, RING_C = 2 * Math.PI * RING_R;
  const ringOff = RING_C - (RING_C * score / 100);
  const gradId  = `jd-ring-grad-${(job.id || '').replace(/[^a-zA-Z0-9]/g, '') || 'x'}`;

  // ── Quick chips & meta ───────────────────────────────────────────────
  const postedAgo      = job.posted ? _formatPostedAgo(job.posted) : (job._posted || 'recently');
  const platformPretty = _prettyPlatform(job.source);
  const expHuman       = _humanLevel(job.exp || job._exp || '', 'Open level');
  const eduHuman       = _humanLevel(job.edu || '', 'Any education');
  const remoteLabel    = job.remote ? 'Remote-friendly' : (job._model || 'Onsite / Hybrid');
  const cit            = String(job.cit || '').toLowerCase();

  // ── Industry chips (first few requirements as a proxy) ───────────────
  const industryRaw = jobReqs.slice(0, 5);

  // ── Other roles at this company from the loaded feed ─────────────────
  const otherRoles = (allJobs || [])
    .filter(j => (j.co || '').toLowerCase() === (job.co || '').toLowerCase()
                  && j.id !== job.id)
    .slice(0, 6);

  // ── Company external lookups ─────────────────────────────────────────
  const companyHost = companyDomain(job.co);
  const companyUrl  = companyHost ? `https://${companyHost}` : null;
  const lookups = [
    { id: 'web',        ic: 'globe',     label: 'Company website',
      url: companyUrl,  hint: companyHost || 'Direct site',  disabled: !companyUrl },
    { id: 'linkedin',   ic: 'briefcase', label: 'LinkedIn',
      url: `https://www.linkedin.com/company/${encodeURIComponent(job.co || '')}`,
      hint: 'Profile · employees · followers' },
    { id: 'crunchbase', ic: 'building-2', label: 'Crunchbase',
      url: `https://www.crunchbase.com/textsearch?q=${encodeURIComponent(job.co || '')}`,
      hint: 'Funding · investors · status' },
    { id: 'wiki',       ic: 'book-open', label: 'Wikipedia',
      url: `https://en.wikipedia.org/wiki/Special:Search?search=${encodeURIComponent(job.co || '')}`,
      hint: 'Background · history' },
    { id: 'glassdoor',  ic: 'star',      label: 'Glassdoor',
      url: `https://www.glassdoor.com/Search/results.htm?keyword=${encodeURIComponent(job.co || '')}`,
      hint: 'Reviews · ratings · salary data' },
    { id: 'news',       ic: 'newspaper', label: 'Recent news',
      url: `https://news.google.com/search?q=${encodeURIComponent((job.co || '') + ' company')}`,
      hint: 'Press · announcements' },
  ];

  // ── Action handlers ──────────────────────────────────────────────────
  const handleApply = () => job.url && window.open(job.url, '_blank');
  const handleShare = async () => {
    if (job.url && navigator.clipboard) {
      try { await navigator.clipboard.writeText(job.url); } catch (_) {}
    }
  };

  const reasonText = matched.length
    ? <>You match <b>{matched.length}</b> of <b>{jobReqs.length}</b> tagged requirements
        {targetTitles.length && titlePct >= 80 ? <>, and the role aligns with your target titles.</> : <>.</>}
        {' '}Tailor your résumé to surface those skills first.</>
    : (jobReqs.length === 0
        ? <>Atlas couldn't tag any requirements from this listing — open the original posting for the full requirements list.</>
        : <>None of your top-hard skills match the listed tags. Consider adding the missing keywords (right column) before applying.</>);

  return (
    <div className="jd-shell" role="region" aria-label="Job detail">
      <div className="jd-bar">
        <button className="jd-back" onClick={onClose} title="Back to jobs (Esc)">
          <Icon name="arrow-left" size={13}/>
          <span>Back</span>
        </button>

        <div className="jd-tabs" role="tablist">
          <button role="tab" aria-selected={tab === 'overview'}
                  className={'jd-tab' + (tab === 'overview' ? ' active' : '')}
                  onClick={() => setTab('overview')}>
            <Icon name="layout-grid" size={11}/> Overview
          </button>
          <button role="tab" aria-selected={tab === 'company'}
                  className={'jd-tab' + (tab === 'company' ? ' active' : '')}
                  onClick={() => setTab('company')}>
            <Icon name="building-2" size={11}/> Company
          </button>
        </div>

        <span className="jd-bar-meta" title="Composite match score">
          <span>match</span><b style={{ color: ringColor }}>{score}</b>
        </span>

        <div className="jd-spacer"/>

        <button className="jd-bar-icon" title="Copy job link" onClick={handleShare}>
          <Icon name="share-2" size={13}/>
        </button>
        <button className="jd-bar-icon" title="Open original posting"
                onClick={() => job.url && window.open(job.url, '_blank')}>
          <Icon name="external-link" size={13}/>
        </button>
        <button className={'jd-bar-icon' + (isLiked ? ' active' : '')}
                title={isLiked ? 'Saved — click to remove' : 'Save for later'}
                onClick={() => onLike?.(job)}>
          <Icon name="bookmark" size={13} fill={isLiked ? 'currentColor' : 'none'}/>
        </button>
        <button className="jd-apply" onClick={handleApply} disabled={!job.url}>
          <Icon name="zap" size={13} color="#fff"/>
          <span>Apply now</span>
          <Icon name="arrow-up-right" size={11} color="#fff"/>
        </button>
      </div>

      <div className="jd-body" ref={bodyRef}>
        <div className="jd-page">
          {tab === 'overview' && (
            <>
              {/* Hero */}
              <section className="jd-hero">
                <div className="jd-hero-eyebrow">
                  <span className="jd-eb-dot"/>
                  <b>Open role</b>
                  <span className="jd-dot">·</span>
                  <span>{postedAgo} on {platformPretty}</span>
                </div>

                <div className="jd-hero-top">
                  <div className="jd-hero-logo">
                    <CompanyLogo company={job.co} size={64} fallbackVariant="v2"/>
                  </div>
                  <div className="jd-hero-meta">
                    <div className="jd-hero-co">
                      <b>{job.co || 'Unknown company'}</b>
                      <span className="jd-dot">·</span>
                      <span>{postedAgo}</span>
                      {platformPretty && (<>
                        <span className="jd-dot">·</span>
                        <span className="jd-source-tag">
                          <Icon name="link-2" size={9}/>{platformPretty}
                        </span>
                      </>)}
                    </div>
                    <h1 className="jd-hero-title">{job.role || 'Untitled role'}</h1>
                  </div>
                </div>

                <div className="jd-hero-quick">
                  {job.loc && (
                    <span className="jd-quick-chip">
                      <Icon name="map-pin" size={12}/>{job.loc}
                    </span>
                  )}
                  <span className={'jd-quick-chip' + (job.remote ? ' cyan' : '')}>
                    <Icon name="building-2" size={12}/>{remoteLabel}
                  </span>
                  <span className="jd-quick-chip accent">
                    <Icon name="graduation-cap" size={12}/>{expHuman}
                  </span>
                  {eduHuman !== 'Any education' && (
                    <span className="jd-quick-chip">
                      <Icon name="book-open" size={12}/>{eduHuman}
                    </span>
                  )}
                  {cit === 'yes' && (
                    <span className="jd-quick-chip warn">
                      <Icon name="shield" size={12}/>US citizenship required
                    </span>
                  )}
                  {cit === 'no' && (
                    <span className="jd-quick-chip good">
                      <Icon name="globe" size={12}/>Open to non-citizens
                    </span>
                  )}
                  {job.salary && job.salary !== 'Unknown' && (
                    <span className="jd-quick-chip good">
                      <Icon name="dollar-sign" size={12}/>{job.salary}
                    </span>
                  )}
                </div>

                {industryRaw.length > 0 && (
                  <div className="jd-hero-industry">
                    {industryRaw.map((tag, i) => (
                      <span key={i} className="jd-industry-chip">{tag}</span>
                    ))}
                  </div>
                )}
              </section>

              {/* Match analysis — only renders when the profile has enough
                  signal to score against. A blank/two-word resume hits the
                  fallback below: a single CTA card explaining that scoring
                  is paused until the user adds skills or a target title.
                  Previously we computed bogus per-component percentages
                  client-side (titlePct=60, locPct=100) and rendered them
                  as full-color bars, which is what produced the user's
                  "100% title alignment + 100% location & seniority on a
                  blank resume" report. */}
              {profileMeaningful ? (
                <section className="jd-match">
                  <div className="jd-match-ring">
                    <svg viewBox="0 0 200 200">
                      <defs>
                        <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
                          <stop offset="0%"  stopColor={ringColor} stopOpacity=".95"/>
                          <stop offset="100%" stopColor={ringColor} stopOpacity=".55"/>
                        </linearGradient>
                      </defs>
                      <circle cx="100" cy="100" r={RING_R} fill="none"
                              stroke="var(--bdr)" strokeWidth="9"/>
                      <circle cx="100" cy="100" r={RING_R} fill="none"
                              stroke={`url(#${gradId})`}
                              strokeWidth="9" strokeLinecap="round"
                              strokeDasharray={RING_C} strokeDashoffset={ringOff}
                              transform="rotate(-90 100 100)"
                              style={{ transition: 'stroke-dashoffset 1.4s cubic-bezier(.16,1,.3,1)' }}/>
                    </svg>
                    <div className="jd-match-ring-c">
                      <div className="jd-match-num" style={{ color: ringColor }}>
                        <CountUp to={score}/><i>%</i>
                      </div>
                      <div className={'jd-match-verdict ' + verdict.tone}>
                        {verdict.label}
                      </div>
                    </div>
                  </div>

                  <div>
                    <div className="jd-match-rows">
                      <div className="jd-match-row">
                        <div className="jd-match-row-l">
                          <Icon name="check-square" size={12}/>Skill match
                        </div>
                        <div className="jd-match-bar">
                          <div className="jd-match-bar-fill" style={{
                            width: skillPct + '%',
                            background: 'linear-gradient(90deg, var(--good), var(--accent2))',
                            boxShadow: '0 0 12px var(--good-d)',
                          }}/>
                        </div>
                        <div className="jd-match-row-r" style={{ color: 'var(--good)' }}>{skillPct}%</div>
                      </div>
                      <div className="jd-match-row">
                        <div className="jd-match-row-l">
                          <Icon name="target" size={12}/>Title alignment
                        </div>
                        <div className="jd-match-bar">
                          <div className="jd-match-bar-fill" style={{
                            width: titlePct + '%',
                            background: 'linear-gradient(90deg, var(--accent), var(--accent3))',
                            boxShadow: '0 0 12px var(--accent-d)',
                          }}/>
                        </div>
                        <div className="jd-match-row-r" style={{ color: 'var(--accent-h)' }}>{titlePct}%</div>
                      </div>
                      <div className="jd-match-row">
                        <div className="jd-match-row-l">
                          <Icon name="map-pin" size={12}/>Location & seniority
                        </div>
                        <div className="jd-match-bar">
                          <div className="jd-match-bar-fill" style={{
                            width: locPct + '%',
                            background: 'linear-gradient(90deg, var(--accent2), var(--accent-h))',
                            boxShadow: '0 0 12px var(--accent2-d)',
                          }}/>
                        </div>
                        <div className="jd-match-row-r" style={{ color: 'var(--accent2)' }}>{locPct}%</div>
                      </div>
                    </div>

                    <div className="jd-match-reason">{reasonText}</div>
                  </div>
                </section>
              ) : (
                <section className="jd-match jd-match-locked">
                  <div className="jd-match-ring jd-match-ring-locked" aria-hidden="true">
                    <svg viewBox="0 0 200 200">
                      <circle cx="100" cy="100" r={RING_R} fill="none"
                              stroke="var(--bdr)" strokeWidth="9"
                              strokeDasharray="3 6"/>
                    </svg>
                    <div className="jd-match-ring-c">
                      <div className="jd-match-num jd-match-num-empty">—</div>
                      <div className="jd-match-verdict jd-match-verdict-locked">
                        Scoring paused
                      </div>
                    </div>
                  </div>
                  <div className="jd-match-locked-body">
                    <div className="jd-match-locked-eyebrow">
                      <Icon name="user-plus" size={11}/>
                      <span>Profile incomplete</span>
                    </div>
                    <h3 className="jd-match-locked-h">
                      Add skills or a target title to see real fit scores.
                    </h3>
                    <p className="jd-match-locked-p">
                      Atlas refuses to fabricate match percentages for a profile that doesn’t have the signal to compute them. Add at least <b>2 hard skills</b> or <b>1 target title</b> to your profile and every job will score against the description and your background.
                    </p>
                    <div className="jd-match-locked-actions">
                      <button className="jd-match-locked-cta primary"
                              type="button"
                              onClick={() => onClose?.() /* close detail; user navigates to profile via rail */}>
                        <Icon name="user" size={13}/>
                        Complete profile
                        <span className="jd-match-locked-arrow">→</span>
                      </button>
                      {job.url && (
                        <a className="jd-match-locked-cta ghost"
                           href={job.url} target="_blank" rel="noopener noreferrer">
                          <Icon name="external-link" size={13}/>
                          Read posting on source
                        </a>
                      )}
                    </div>
                  </div>
                </section>
              )}

              {/* ── DESCRIPTION + SKILLS — editorial 2-column layout ──
                  Left: reading column for the JD body (lead / responsibilities
                  / qualifications / benefits). Right: sticky rail with the
                  skill match panel and key facts. The rail is intentionally
                  narrow so the reading column gets ~64ch — comfortable for
                  the 6-12 line bullets that dominate JDs. */}
              <div className="jd-detail-grid">
                <div className="jd-detail-main">

                  {/* Loading skeleton */}
                  {detailsLoading && (
                    <section className="jd-section">
                      <h2 className="jd-section-h">
                        <span className="jd-section-icon"><Icon name="file-text" size={14}/></span>
                        Loading the full posting…
                      </h2>
                      <div className="jd-jd-loading">
                        <div className="jd-jd-skel title"/>
                        <div className="jd-jd-skel"/>
                        <div className="jd-jd-skel"/>
                        <div className="jd-jd-skel short"/>
                        <div className="jd-jd-skel"/>
                        <div className="jd-jd-skel short"/>
                      </div>
                    </section>
                  )}

                  {/* About this role */}
                  {!detailsLoading && details?.lead_paragraph && (
                    <section className="jd-lead-card">
                      <div className="jd-lead-eyebrow">
                        <Icon name="file-text" size={11}/>
                        <span>About this role</span>
                      </div>
                      <div className="jd-lead-text">
                        {_highlightSkills(details.lead_paragraph, profile?.top_hard_skills || [])}
                      </div>
                    </section>
                  )}

                  {/* What you'll do */}
                  {!detailsLoading && (details?.responsibilities || []).length > 0 && (
                    <section className="jd-section">
                      <h2 className="jd-section-h">
                        <span className="jd-section-num">01</span>
                        What you'll do
                      </h2>
                      <ul className="jd-bullet-list">
                        {details.responsibilities.map((b, i) => (
                          <li key={i} className="jd-bullet-item">
                            <span className="jd-bullet-marker accent">{i + 1}</span>
                            <span>{_highlightSkills(b, profile?.top_hard_skills || [])}</span>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}

                  {/* Qualifications — single column for reading width. Required
                      and Preferred stack vertically, separated by a subtle rule. */}
                  {!detailsLoading && details && (
                    ((details.required_qualifications  || []).length > 0)
                    || ((details.preferred_qualifications || []).length > 0)
                  ) && (
                    <section className="jd-section">
                      <h2 className="jd-section-h">
                        <span className="jd-section-num">02</span>
                        Qualifications
                      </h2>
                      {(details.required_qualifications || []).length > 0 && (
                        <div className="jd-qual-block required">
                          <div className="jd-qual-block-h">
                            <Icon name="shield-check" size={11}/>
                            <span>Required</span>
                            <span className="jd-qual-count">
                              {details.required_qualifications.length}
                            </span>
                          </div>
                          <ul className="jd-bullet-list">
                            {details.required_qualifications.map((b, i) => (
                              <li key={i} className="jd-bullet-item">
                                <span className="jd-bullet-marker accent">
                                  <Icon name="check" size={11}/>
                                </span>
                                <span>{_highlightSkills(b, profile?.top_hard_skills || [])}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                      {(details.preferred_qualifications || []).length > 0
                        && (details.required_qualifications || []).length > 0 && (
                        <div className="jd-qual-divider"><span/></div>
                      )}
                      {(details.preferred_qualifications || []).length > 0 && (
                        <div className="jd-qual-block preferred">
                          <div className="jd-qual-block-h">
                            <Icon name="star" size={11}/>
                            <span>Preferred</span>
                            <span className="jd-qual-count">
                              {details.preferred_qualifications.length}
                            </span>
                          </div>
                          <ul className="jd-bullet-list">
                            {details.preferred_qualifications.map((b, i) => (
                              <li key={i} className="jd-bullet-item">
                                <span className="jd-bullet-marker">
                                  <Icon name="plus" size={11}/>
                                </span>
                                <span>{_highlightSkills(b, profile?.top_hard_skills || [])}</span>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </section>
                  )}

                  {/* Empty-state: render a useful fallback when Atlas couldn't
                      fetch / parse the description (most often: source site
                      requires a captcha, JS-rendered HTML, or auth). The page
                      previously collapsed to dead space; now we surface what
                      we DO have (tagged requirements, key facts) and route
                      the user to read it on the source. */}
                  {!detailsLoading && (
                    !details ||
                    (
                      !details.lead_paragraph &&
                      (details.responsibilities || []).length === 0 &&
                      (details.required_qualifications || []).length === 0 &&
                      (details.preferred_qualifications || []).length === 0 &&
                      (details.benefits || []).length === 0
                    )
                  ) && (
                    <section className="jd-empty-card">
                      <div className="jd-empty-eyebrow">
                        <span className="jd-empty-pulse"/>
                        <span>Description not available</span>
                      </div>
                      <h2 className="jd-empty-h">
                        Atlas couldn’t pull the full posting from the source.
                      </h2>
                      <p className="jd-empty-lead">
                        {detailsError
                          ? <>The fetcher returned <code className="jd-empty-code">{detailsError}</code> — usually a CAPTCHA, JS-only render, or auth wall on the source.</>
                          : <>This posting’s body wasn’t parseable by Atlas. The posting itself is still live on <b>{platformPretty || job.source}</b>.</>}
                        {' '}You can still apply directly, and below is what we know from the listing card.
                      </p>

                      <div className="jd-empty-grid">
                        {jobReqs.length > 0 && (
                          <div className="jd-empty-block">
                            <div className="jd-empty-block-h">
                              <Icon name="tag" size={11}/>
                              <span>Tagged requirements</span>
                              <span className="jd-empty-count">{jobReqs.length}</span>
                            </div>
                            <div className="jd-empty-chips">
                              {jobReqs.map((r, i) => {
                                const isMatch = matched.includes(r) || userSkills.some(s => s && (r.toLowerCase().includes(s) || s.includes(r.toLowerCase())));
                                return (
                                  <span key={i} className={'jd-empty-chip' + (isMatch ? ' on' : '')}>
                                    {isMatch && <Icon name="check" size={10}/>}
                                    {r}
                                  </span>
                                );
                              })}
                            </div>
                          </div>
                        )}

                        <div className="jd-empty-block">
                          <div className="jd-empty-block-h">
                            <Icon name="layout-list" size={11}/>
                            <span>Key facts</span>
                          </div>
                          <dl className="jd-empty-facts">
                            {job.loc && (<><dt>Location</dt><dd>{job.loc}</dd></>)}
                            <dt>Work model</dt><dd>{remoteLabel}</dd>
                            {expHuman && expHuman !== 'Open level' && (<><dt>Experience</dt><dd>{expHuman}</dd></>)}
                            {eduHuman && eduHuman !== 'Any education' && (<><dt>Education</dt><dd>{eduHuman}</dd></>)}
                            {job.salary && job.salary !== 'Unknown' && (<><dt>Compensation</dt><dd>{job.salary}</dd></>)}
                            <dt>Posted</dt><dd>{postedAgo}</dd>
                            <dt>Source</dt><dd className="jd-empty-source">{platformPretty || job.source || '—'}</dd>
                          </dl>
                        </div>
                      </div>

                      <div className="jd-empty-actions">
                        {job.url && (
                          <a className="jd-empty-cta primary" href={job.url} target="_blank" rel="noopener noreferrer">
                            <Icon name="external-link" size={13}/>
                            Read on {platformPretty || 'source'}
                            <span className="jd-empty-cta-arrow">→</span>
                          </a>
                        )}
                        <button className="jd-empty-cta ghost" type="button" onClick={() => onAsk?.(job)}>
                          <Icon name="message-circle" size={13}/>
                          Ask Atlas anyway
                        </button>
                        <button className="jd-empty-cta ghost" type="button" onClick={() => onTailor?.(job)}>
                          <Icon name="wand-2" size={13}/>
                          Tailor from title
                        </button>
                      </div>
                      <p className="jd-empty-hint">
                        Atlas falls back to scoring against the title + tagged requirements. The match panel on the right reflects that title-only score until the description arrives.
                      </p>
                    </section>
                  )}

                  {/* Benefits */}
                  {!detailsLoading && (details?.benefits || []).length > 0 && (
                    <section className="jd-section">
                      <h2 className="jd-section-h">
                        <span className="jd-section-num">03</span>
                        Benefits &amp; perks
                      </h2>
                      <ul className="jd-bullet-list">
                        {details.benefits.map((b, i) => (
                          <li key={i} className="jd-bullet-item">
                            <span className="jd-bullet-marker good">
                              <Icon name="check" size={11}/>
                            </span>
                            <span>{b}</span>
                          </li>
                        ))}
                      </ul>
                    </section>
                  )}
                </div>

                {/* Sticky right rail: score breakdown + skill match panel + key facts */}
                <aside className="jd-detail-rail" aria-label="Score breakdown, skill match and key facts">
                  {/* "Why this score?" — shows the 50/30/20 weighted math the
                      backend rubric scorer applied. Pulls coverage from
                      scoreData when available (description-aware) and falls
                      back to client-side computed signals when the lazy
                      score hasn't arrived. The user can finally see WHY a
                      job is 78 vs 56. Hidden entirely when the profile is
                      not meaningful — the hero match section already
                      surfaces the "scoring paused" state, no point in
                      rendering empty bars in the rail too. */}
                  {!profileMeaningful && (
                    <section className="jd-rail-card jd-rail-locked">
                      <div className="jd-rail-eyebrow">
                        <span className="jd-rail-bar"/>
                        <span>Scoring paused</span>
                      </div>
                      <p className="jd-rail-locked-p">
                        Match math runs once your profile has at least <b>2 hard skills</b> or <b>1 target title</b>. Until then, nothing to break down.
                      </p>
                    </section>
                  )}
                  {profileMeaningful && (<>
                  <section className="jd-rail-card jd-rail-breakdown">
                    <div className="jd-rail-eyebrow">
                      <span className="jd-rail-bar"/>
                      <span>Why this score</span>
                      <span className={'jd-rail-pct jd-rail-pct-' + breakdownTone}>{breakdownTotal}</span>
                    </div>
                    <div className="jd-bd-source">
                      {haveLazy
                        ? <>Computed against the full job description.</>
                        : <>Preview math from title + listing tags. The hero ring shows the live ranking score (<b>{score}</b>); these three rows show how the description-aware rubric weighs your fit.</>}
                    </div>
                    <ul className="jd-bd-rows">
                      {breakdown.map(b => (
                        <li key={b.key} className="jd-bd-row" title={b.hint}>
                          <span className="jd-bd-row-h">
                            <span className="jd-bd-label">{b.label}</span>
                            <span className="jd-bd-pts">
                              <b>{b.points}</b><i>/{b.weight}</i>
                            </span>
                          </span>
                          <div className="jd-bd-meter" aria-hidden="true">
                            <div className="jd-bd-meter-fill"
                                 style={{ width: `${Math.min(100, Math.round(b.raw * 100))}%` }}/>
                          </div>
                        </li>
                      ))}
                    </ul>
                    <div className="jd-bd-total">
                      <span>Total</span>
                      <b>{breakdownTotal}<i>/100</i></b>
                    </div>
                  </section>

                  <section className="jd-rail-card jd-rail-skills">
                    <div className="jd-rail-eyebrow">
                      <span className="jd-rail-bar"/>
                      <span>Skill match</span>
                      <span className="jd-rail-pct">{skillPct}%</span>
                    </div>
                    <div className="jd-rail-meter" aria-hidden="true">
                      <div className="jd-rail-meter-fill" style={{ width: `${skillPct}%` }}/>
                    </div>
                    <div className="jd-rail-skill-summary">
                      {haveLazy
                        ? (matched.length > 0
                            ? <><b>{matched.length}</b> of your top skills found in this posting</>
                            : <>None of your top skills appear in this posting yet</>)
                        : <><b>{matched.length}</b> of <b>{jobReqs.length || '—'}</b> tagged
                            requirement{jobReqs.length === 1 ? '' : 's'} match your profile</>}
                    </div>

                    {matched.length > 0 && (
                      <div className="jd-rail-skill-group on">
                        <div className="jd-rail-skill-h">
                          <Icon name="check-circle-2" size={10}/>
                          You bring
                        </div>
                        <div className="jd-rail-skill-chips">
                          {matched.map((s, i) => (
                            <span key={`m-${i}`} className="jd-rail-chip on">
                              <Icon name="check" size={10}/>{s}
                            </span>
                          ))}
                        </div>
                      </div>
                    )}
                    {gaps.length > 0 && (
                      <div className="jd-rail-skill-group off">
                        <div className="jd-rail-skill-h">
                          <Icon name="zap" size={10}/>
                          Consider adding
                        </div>
                        <div className="jd-rail-skill-chips">
                          {gaps.map((s, i) => (
                            <span key={`g-${i}`} className="jd-rail-chip off">{s}</span>
                          ))}
                        </div>
                      </div>
                    )}
                    {!haveLazy && jobReqs.length === 0 && (
                      <div className="jd-rail-skill-empty">
                        Atlas couldn't tag any requirements from this listing.
                        Open the original posting for the full list.
                      </div>
                    )}
                  </section>
                  </>)}

                  <section className="jd-rail-card jd-rail-facts">
                    <div className="jd-rail-eyebrow">
                      <span className="jd-rail-bar cyan"/>
                      <span>Key facts</span>
                    </div>
                    <dl className="jd-rail-facts-list">
                      {job.loc && (
                        <div className="jd-rail-fact">
                          <dt><Icon name="map-pin" size={11}/>Location</dt>
                          <dd>{job.loc}</dd>
                        </div>
                      )}
                      <div className="jd-rail-fact">
                        <dt><Icon name="building-2" size={11}/>Work model</dt>
                        <dd>{remoteLabel}</dd>
                      </div>
                      <div className="jd-rail-fact">
                        <dt><Icon name="graduation-cap" size={11}/>Level</dt>
                        <dd>{expHuman}</dd>
                      </div>
                      {eduHuman !== 'Any education' && (
                        <div className="jd-rail-fact">
                          <dt><Icon name="book-open" size={11}/>Education</dt>
                          <dd>{eduHuman}</dd>
                        </div>
                      )}
                      {job.salary && job.salary !== 'Unknown' && (
                        <div className="jd-rail-fact">
                          <dt><Icon name="dollar-sign" size={11}/>Salary</dt>
                          <dd className="good">{job.salary}</dd>
                        </div>
                      )}
                      <div className="jd-rail-fact">
                        <dt><Icon name="link-2" size={11}/>Source</dt>
                        <dd>{platformPretty}</dd>
                      </div>
                      <div className="jd-rail-fact">
                        <dt><Icon name="clock" size={11}/>Posted</dt>
                        <dd>{postedAgo}</dd>
                      </div>
                      {cit === 'yes' && (
                        <div className="jd-rail-fact">
                          <dt><Icon name="shield" size={11}/>Citizenship</dt>
                          <dd className="warn">US required</dd>
                        </div>
                      )}
                    </dl>
                  </section>

                  <button className="jd-rail-apply" onClick={handleApply} disabled={!job.url}>
                    <Icon name="zap" size={13} color="#fff"/>
                    Apply on {platformPretty}
                    <Icon name="arrow-up-right" size={11} color="#fff"/>
                  </button>
                </aside>
              </div>

              {/* Source-not-supported / fetch-error fallback. Shown when the live
                  fetch returned no description and we have nothing parsed. */}
              {!detailsLoading && details && !details.has_description
                  && !details.lead_paragraph
                  && (details.responsibilities || []).length === 0 && (
                <section className="jd-no-desc">
                  <div className="jd-no-desc-icon"><Icon name="file-text" size={20}/></div>
                  <div className="jd-no-desc-h">
                    The full posting lives on {platformPretty}
                  </div>
                  <div className="jd-no-desc-p">
                    Atlas indexes <b>{platformPretty}</b> as a metadata-only source —
                    the responsibilities and qualifications aren't pulled into the
                    local store. Open the original posting to read the full JD,
                    or use the skill-alignment block below for the tagged-skills view.
                  </div>
                  {job.url && (
                    <a className="jd-no-desc-cta" href={job.url}
                       target="_blank" rel="noopener noreferrer">
                      <Icon name="external-link" size={12} color="#fff"/>
                      Open on {platformPretty}
                      <Icon name="arrow-up-right" size={11} color="#fff"/>
                    </a>
                  )}
                </section>
              )}

              {/* Network-error fallback — distinct from "no source support" */}
              {!detailsLoading && detailsError && !details && (
                <section className="jd-no-desc">
                  <div className="jd-no-desc-icon"
                       style={{ background:'var(--bad-d)', borderColor:'var(--bad-b)', color:'var(--bad)' }}>
                    <Icon name="wifi-off" size={20}/>
                  </div>
                  <div className="jd-no-desc-h">Couldn't load the full posting</div>
                  <div className="jd-no-desc-p">{detailsError}</div>
                  {job.url && (
                    <a className="jd-no-desc-cta" href={job.url}
                       target="_blank" rel="noopener noreferrer">
                      <Icon name="external-link" size={12} color="#fff"/>
                      Open original
                    </a>
                  )}
                </section>
              )}

              {/* (The standalone Skill alignment block was consolidated into
                  the sticky rail inside .jd-detail-grid above so the skill
                  match stays visible while the user reads the JD body.) */}

              {/* Listed requirements — fallback for sources that don't expose
                  per-job description APIs (RemoteOK / Adzuna / Jobicy / etc.).
                  Hidden once we successfully parsed Required/Preferred from a
                  live ATS fetch since those bullets are richer. */}
              {jobReqs.length > 0 && !(
                details && (
                  (details.required_qualifications  || []).length > 0
                  || (details.preferred_qualifications || []).length > 0
                )
              ) && (
                <section className="jd-section">
                  <h2 className="jd-section-h">
                    <span className="jd-section-icon"><Icon name="list-checks" size={14}/></span>
                    Tagged requirements
                  </h2>
                  <ul className="jd-reqs-list">
                    {jobReqs.map((r, i) => {
                      const isMatch = matched.includes(r);
                      return (
                        <li key={i} className={'jd-reqs-item' + (isMatch ? ' match' : '')}>
                          <span className="jd-reqs-item-marker">
                            {isMatch ? <Icon name="check" size={11}/> : (i + 1)}
                          </span>
                          <span>{r}</span>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              )}

              {/* Action footer */}
              <section className="jd-actions-card">
                <div className="jd-actions-text">
                  <div className="jd-actions-h">Move on this role</div>
                  <div className="jd-actions-sub">
                    Tailor your résumé in seconds, ask Atlas for an angle, then apply directly.
                  </div>
                </div>
                <div className="jd-actions-row">
                  <button className="jd-action-btn cyan" onClick={() => onTailor?.(job)}>
                    <Icon name="wand-2" size={12}/> Tailor résumé
                  </button>
                  <button className="jd-action-btn" onClick={() => onAsk?.(job)}>
                    <Icon name="sparkles" size={12}/> Ask Atlas
                  </button>
                  <button className="jd-action-btn"
                          onClick={() => { onHide?.(job); onClose?.(); }}>
                    <Icon name="eye-off" size={12}/> Hide
                  </button>
                  <button className="jd-action-btn primary"
                          onClick={handleApply} disabled={!job.url}>
                    <Icon name="zap" size={12} color="#fff"/> Apply now
                    <Icon name="arrow-up-right" size={11} color="#fff"/>
                  </button>
                </div>
              </section>
            </>
          )}

          {tab === 'company' && (
            <>
              <section className="jd-company-hero">
                <div className="jd-company-logo">
                  <CompanyLogo company={job.co} size={84} fallbackVariant="v3"/>
                </div>
                <div className="jd-company-text">
                  <div className="jd-company-name">{job.co || 'Unknown company'}</div>
                  {industryRaw.length > 0 && (
                    <div className="jd-company-tags">
                      {industryRaw.map((tag, i) => (
                        <span key={i} className="jd-industry-chip">{tag}</span>
                      ))}
                    </div>
                  )}
                  {industryRaw.length === 0 && (
                    <div className="jd-company-tags">
                      <span className="jd-industry-chip">Indexed via {platformPretty}</span>
                    </div>
                  )}
                  {companyUrl && (
                    <a className="jd-company-link" href={companyUrl}
                       target="_blank" rel="noopener noreferrer">
                      <Icon name="external-link" size={11}/>{companyHost}
                    </a>
                  )}
                </div>
              </section>

              {/* Wikipedia-derived company summary. Shown only when the live
                  fetch returned a real summary; otherwise the curated lookup
                  cards below stand in for the missing background. */}
              {detailsLoading && (
                <section className="jd-section">
                  <h2 className="jd-section-h">
                    <span className="jd-section-icon"><Icon name="book-open" size={14}/></span>
                    Loading company background…
                  </h2>
                  <div className="jd-jd-loading">
                    <div className="jd-jd-skel title"/>
                    <div className="jd-jd-skel"/>
                    <div className="jd-jd-skel"/>
                    <div className="jd-jd-skel short"/>
                  </div>
                </section>
              )}

              {!detailsLoading && details?.company_summary && (
                <section className="jd-co-summary">
                  {details.company_image && (
                    <img className="jd-co-summary-img"
                         src={details.company_image}
                         alt={job.co || 'Company image'}
                         loading="lazy"
                         referrerPolicy="no-referrer"/>
                  )}
                  <div className="jd-co-summary-text">
                    <div className="jd-co-summary-eyebrow">
                      <Icon name="book-open" size={10}/>
                      About {job.co || 'this company'}
                    </div>
                    <div className="jd-co-summary-h">
                      {job.co}
                      {details.company_short_description && (
                        <span className="jd-co-summary-tag">
                          — {details.company_short_description}
                        </span>
                      )}
                    </div>
                    <div className="jd-co-summary-body">
                      {details.company_summary}
                    </div>
                    {details.company_wiki_url && (
                      <div className="jd-co-summary-cite">
                        Source: <a href={details.company_wiki_url}
                                    target="_blank" rel="noopener noreferrer">Wikipedia</a>
                        {' · cached locally for 24 h'}
                      </div>
                    )}
                  </div>
                </section>
              )}

              <section className="jd-section">
                <h2 className="jd-section-h">
                  <span className="jd-section-icon"><Icon name="info" size={14}/></span>
                  At a glance
                </h2>
                <div className="jd-fact-grid">
                  <div className="jd-fact">
                    <span className="jd-fact-l">Source platform</span>
                    <span className="jd-fact-v">{platformPretty}</span>
                    <span className="jd-fact-h">Indexed via the public API</span>
                  </div>
                  <div className="jd-fact">
                    <span className="jd-fact-l">Posted</span>
                    <span className="jd-fact-v">{postedAgo}</span>
                    {job.posted && <span className="jd-fact-h">{job.posted}</span>}
                  </div>
                  <div className="jd-fact">
                    <span className="jd-fact-l">Open roles in feed</span>
                    <span className="jd-fact-v mono">{otherRoles.length + 1}</span>
                    <span className="jd-fact-h">From the current Jobs index</span>
                  </div>
                  <div className="jd-fact">
                    <span className="jd-fact-l">Salary range</span>
                    <span className="jd-fact-v">
                      {job.salary && job.salary !== 'Unknown' ? job.salary : 'Not disclosed'}
                    </span>
                    <span className="jd-fact-h">From the original posting</span>
                  </div>
                  <div className="jd-fact">
                    <span className="jd-fact-l">Work location</span>
                    <span className="jd-fact-v">{job.loc || 'Unspecified'}</span>
                    <span className="jd-fact-h">{job.remote ? 'Remote-friendly' : 'On-site / hybrid'}</span>
                  </div>
                  <div className="jd-fact">
                    <span className="jd-fact-l">Citizenship</span>
                    <span className="jd-fact-v">
                      {cit === 'yes' ? 'US citizenship required'
                        : cit === 'no' ? 'Open to non-citizens'
                        : 'Not specified'}
                    </span>
                    <span className="jd-fact-h">From the JD inference</span>
                  </div>
                </div>
              </section>

              <section className="jd-section">
                <h2 className="jd-section-h">
                  <span className="jd-section-icon"><Icon name="search" size={14}/></span>
                  Look up the company
                </h2>
                <div className="jd-lookup-grid">
                  {lookups.map(l => l.disabled
                    ? <div key={l.id} className="jd-lookup"
                            style={{ opacity: .45, cursor: 'not-allowed' }}>
                        <span className="jd-lookup-ic"><Icon name={l.ic} size={13}/></span>
                        <div className="jd-lookup-text">
                          <span className="jd-lookup-l">{l.label}</span>
                          <span className="jd-lookup-h">Domain not resolved</span>
                        </div>
                      </div>
                    : <a key={l.id} className="jd-lookup"
                          href={l.url} target="_blank" rel="noopener noreferrer">
                        <span className="jd-lookup-ic"><Icon name={l.ic} size={13}/></span>
                        <div className="jd-lookup-text">
                          <span className="jd-lookup-l">{l.label}</span>
                          <span className="jd-lookup-h">{l.hint}</span>
                        </div>
                        <Icon name="arrow-up-right" size={11} color="var(--t4)"/>
                      </a>)}
                </div>
              </section>

              <section className="jd-section">
                <h2 className="jd-section-h">
                  <span className="jd-section-icon"><Icon name="briefcase" size={14}/></span>
                  Other roles at {job.co || 'this company'}
                </h2>
                {otherRoles.length === 0 ? (
                  <div className="jd-skills-empty" style={{ padding: '8px 4px', lineHeight: 1.55 }}>
                    No additional roles for this company in the loaded feed.
                    The ingester refreshes every 25 seconds — try Refresh on the Jobs tab to widen the pool.
                  </div>
                ) : (
                  <div className="jd-roles-list">
                    {otherRoles.map(r => {
                      const sc = Math.round(r.score || 0);
                      const tone = sc >= 85 ? 'good' : sc >= 70 ? 'accent' : '';
                      return (
                        <div key={r.id} className="jd-role-card"
                             role="button" tabIndex={0}
                             onClick={() => onSwitchTo?.(r)}
                             onKeyDown={e => {
                               if (e.key === 'Enter' || e.key === ' ') {
                                 e.preventDefault(); onSwitchTo?.(r);
                               }
                             }}
                             title="Open this role">
                          <CompanyLogo company={r.co} size={32} fallbackVariant="v1"/>
                          <div className="jd-role-card-text">
                            <div className="jd-role-card-title">{r.role || 'Untitled role'}</div>
                            <div className="jd-role-card-meta">
                              <span>{r.loc || '—'}</span>
                              {r.posted && <>
                                <span className="jd-dot">·</span>
                                <span>{_formatPostedAgo(r.posted)}</span>
                              </>}
                            </div>
                          </div>
                          <span className={'jd-role-card-score ' + tone}>{sc}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            </>
          )}
        </div>
      </div>
    </div>
  );
}


/* ──────────────────────────────────────────────────────────
   Ask Atlas — per-job chat advisor. Slides in from the right;
   thread is owned locally and reset when the drawer closes.
   ────────────────────────────────────────────────────────── */
function AskAtlas({ job, mode, isPro, isDev, scoreData, onClose }) {
  const jobId = job.id || `${job.co || job.company || ''}|${job.role || job.title || ''}`;
  const [history, setHistory] = useState([]);
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState(null);
  const scrollRef = useRef(null);
  const inputRef = useRef(null);

  // Esc closes; click outside also closes (handled by overlay click).
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    // Pin to bottom on every new turn or while pending shows the typing dots
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }, [history.length, pending]);

  const send = async () => {
    const message = draft.trim();
    if (!message || pending) return;
    setError(null);
    setDraft('');
    const next = [...history, { role: 'user', content: message }];
    setHistory(next);
    setPending(true);
    try {
      const res = await api.post('/api/jobs/ask', { job_id: jobId, message, history });
      setHistory([...next, { role: 'assistant', content: res.reply || '(no reply)' }]);
    } catch (e) {
      setError(e.message || 'Atlas could not respond.');
      setHistory(next);   // keep the user's question even on failure
    } finally {
      setPending(false);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  };

  const onKey = e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  };

  const co = job.co || job.company || '—';
  const role = job.role || job.title || 'Untitled role';
  // Prefer the description-aware lazy score (same number JobCard +
  // JobDetailView show) so opening AskAtlas never reveals a different
  // number than the card the user clicked on.
  const haveLazy = scoreData && typeof scoreData.score === 'number';
  const score = haveLazy
    ? Math.max(0, Math.min(100, Math.round(scoreData.score)))
    : Math.max(0, Math.min(100, Math.round(job.score || 0)));
  // Neutral label — the chat advisor uses whichever provider is configured;
  // surfacing the brand here is just noise.
  const providerLabel = 'AI';

  const suggestions = [
    'How well do I fit this role based on my profile?',
    'What résumé bullets should I emphasize for this posting?',
    'What gaps should I expect in the interview, and how do I handle them?',
    'What\'s a smart question to ask the recruiter for this role?',
  ];

  return (
    <div className="ask-overlay" onClick={onClose}>
      <aside className="ask-drawer" onClick={e => e.stopPropagation()}>
        <header className="ask-head">
          <div className="ask-head-l">
            <CompanyLogo company={co} fallbackVariant="v1" size={36}/>
            <div className="ask-head-meta">
              <div className="ask-head-eyebrow">
                <Icon name="sparkles" size={10}/> Atlas · {providerLabel}
                {mode === 'anthropic' && !isDev && <span className="ask-pro-pill">Soon</span>}
              </div>
              <div className="ask-head-role">{role}</div>
              <div className="ask-head-co">
                <span>{co}</span>
                {score > 0 && <span className="ask-head-score">{score}<i>/100</i></span>}
              </div>
            </div>
          </div>
          <button className="ask-close" onClick={onClose} title="Close (Esc)">
            <Icon name="x" size={16}/>
          </button>
        </header>

        <div className="ask-thread" ref={scrollRef}>
          {history.length === 0 && (
            <div className="ask-empty">
              <div className="ask-empty-eyebrow">
                <Icon name="brain" size={11}/> Atlas has read the full posting and your résumé
              </div>
              <div className="ask-empty-h">
                Ask anything about <em>{role}</em> at <em>{co}</em>.
              </div>
              <div className="ask-suggestions">
                {suggestions.map((s, i) => (
                  <button key={i} className="ask-suggestion"
                    style={{ animationDelay: `${i * 60}ms` }}
                    onClick={() => { setDraft(s); setTimeout(() => inputRef.current?.focus(), 0); }}>
                    <Icon name="sparkle" size={11}/> {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {history.map((m, i) => (
            <div key={i} className={'ask-msg ask-msg-' + m.role}>
              {m.role === 'assistant' && (
                <div className="ask-msg-icon"><Icon name="sparkles" size={12}/></div>
              )}
              <div className="ask-msg-body">
                {m.role === 'assistant'
                  ? <Markdown text={m.content}/>
                  : m.content}
              </div>
            </div>
          ))}

          {pending && (
            <div className="ask-msg ask-msg-assistant">
              <div className="ask-msg-icon"><Icon name="sparkles" size={12}/></div>
              <div className="ask-msg-body ask-typing">
                <span/><span/><span/>
              </div>
            </div>
          )}

          {error && (
            <div className="ask-error">
              <Icon name="alert-triangle" size={13}/>
              <span>{error}</span>
            </div>
          )}
        </div>

        <footer className="ask-input-row">
          <textarea
            ref={inputRef}
            className="ask-input"
            placeholder={`Ask Atlas about ${role}…`}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={onKey}
            rows={1}
            disabled={pending}/>
          <button className="ask-send" onClick={send} disabled={pending || !draft.trim()}>
            {pending ? <span className="spin"/> : <Icon name="send" size={14}/>}
          </button>
        </footer>
        <div className="ask-foot-hint">
          <Icon name="corner-down-left" size={10}/> Enter to send · Shift+Enter for newline · Esc to close
        </div>
      </aside>
    </div>
  );
}

/* ──────────────────────────────────────────────────────────
   Tailor Drawer — per-job, on-demand resume tailoring.
   Mirrors jobright.ai: user clicks one job, gets a tailored
   resume in seconds without running the full 7-phase pipeline
   first. Hits POST /api/resume/tailor on mount, renders the
   same TailoredResumeCard the phase-4 batch view uses.
   ────────────────────────────────────────────────────────── */
function TailorDrawer({ job, mode, isPro, isDev, hasResume, scoreData, onClose, onOpenDocuments }) {
  const jobId = job.id || `${job.co || job.company || ''}|${job.role || job.title || ''}`;
  // stage: 'analyzing' | 'review' | 'generating' | 'result' | 'error'
  const [stage, setStage] = useState('analyzing');
  const [analysis, setAnalysis] = useState(null);
  const [selectedKws, setSelectedKws] = useState({});
  const [item, setItem] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Stage 0 → /tailor/analyze on mount. Heuristic-only, ~30s timeout.
  useEffect(() => {
    let cancelled = false;
    if (!hasResume) {
      setStage('error');
      setError('Upload a resume first — Atlas needs your profile to tailor against this posting.');
      return () => { cancelled = true; };
    }
    setStage('analyzing');
    setError(null);
    setItem(null);
    api.post('/api/resume/tailor/analyze', { job_id: jobId }, { timeoutMs: 30000 })
      .then(res => {
        if (cancelled) return;
        setAnalysis(res);
        const initial = {};
        (res.must_have || []).forEach(c => { if (!c.present) initial[c.keyword] = true; });
        (res.nice_to_have || []).forEach(c => { initial[c.keyword] = false; });
        setSelectedKws(initial);
        setStage('review');
      })
      .catch(e => {
        if (cancelled) return;
        let msg = e?.message || 'Analysis failed.';
        if (/Job not found|API 404/i.test(msg)) {
          msg = 'This job is no longer in the index. Refresh the feed and try again.';
        }
        setError(msg);
        setStage('error');
      });
    return () => { cancelled = true; };
  }, [jobId, hasResume]);

  const generate = (skipReview = false) => {
    setStage('generating');
    setError(null);
    const selected = skipReview
      ? (analysis?.must_have || []).filter(c => !c.present).map(c => c.keyword)
      : Object.entries(selectedKws).filter(([_, v]) => v).map(([k]) => k);
    api.post('/api/resume/tailor',
            { job_id: jobId, selected_keywords: selected },
            { timeoutMs: 120000 })
      .then(res => { setItem(res?.item || null); setStage('result'); })
      .catch(e => {
        let msg = e?.message || 'Tailoring failed.';
        if (/Job not found|API 404/i.test(msg)) {
          msg = 'This job is no longer in the index. Refresh the feed and try again.';
        }
        setError(msg);
        setStage('error');
      });
  };

  const co       = job.co || job.company || '—';
  const role     = job.role || job.title || 'Untitled role';
  // Same single-source-of-truth pattern as JobDetailView + AskAtlas:
  // prefer the lazy description-aware score, fall back to job.score.
  const haveLazy = scoreData && typeof scoreData.score === 'number';
  const score    = haveLazy
    ? Math.max(0, Math.min(100, Math.round(scoreData.score)))
    : Math.max(0, Math.min(100, Math.round(job.score || 0)));
  const provLbl  = mode === 'anthropic' ? 'Claude' : 'Ollama';

  const selectedCount = Object.values(selectedKws).filter(Boolean).length;

  return (
    <div className="ask-overlay" onClick={onClose}>
      <aside className="ask-drawer tailor-drawer" onClick={e => e.stopPropagation()}>
        <header className="ask-head">
          <div className="ask-head-l">
            <CompanyLogo company={co} fallbackVariant="v2" size={36}/>
            <div className="ask-head-meta">
              <div className="ask-head-eyebrow">
                <Icon name="wand-2" size={10}/> Tailor · {provLbl}
                {mode === 'anthropic' && !isDev && <span className="ask-pro-pill">Soon</span>}
              </div>
              <div className="ask-head-role">{role}</div>
              <div className="ask-head-co">
                <span>{co}</span>
                {score > 0 && <span className="ask-head-score">{score}<i>/100</i></span>}
              </div>
            </div>
          </div>
          <button className="ask-close" onClick={onClose} title="Close (Esc)">
            <Icon name="x" size={16}/>
          </button>
        </header>

        <div className="tailor-body">
          {(stage === 'analyzing' || stage === 'generating') && (
            <div className="tailor-loading">
              <div className="tailor-loading-eyebrow">
                <span className="spin"/>
                {stage === 'analyzing'
                  ? ' Reading the job description…'
                  : ' Generating tailored resume…'}
              </div>
              <div className="tailor-loading-hint">
                {stage === 'analyzing'
                  ? 'Comparing JD requirements against your resume — finding the keywords you might want to weave in.'
                  : (mode === 'anthropic'
                      ? 'Claude is rewriting bullets and skills with your selected keywords. Usually 15–25 s.'
                      : 'Your local model is running — speed depends on the model size and your hardware.')}
              </div>
            </div>
          )}

          {stage === 'error' && (
            <div className="tailor-error">
              <Icon name="alert-triangle" size={14}/>
              <div>
                <div className="tailor-error-h">Couldn't tailor this job</div>
                <div className="tailor-error-msg">{error}</div>
              </div>
            </div>
          )}

          {stage === 'review' && analysis && (
            <div className="tailor-review">
              <h4 className="must">Must-have keywords ({(analysis.must_have || []).length})</h4>
              {(analysis.must_have || []).map((c, i) => (
                <label key={'m' + i} className="tailor-keyword-row">
                  <input type="checkbox" checked={!!selectedKws[c.keyword]}
                         disabled={c.present}
                         onChange={e => setSelectedKws(s => ({ ...s, [c.keyword]: e.target.checked }))}/>
                  <span className="kw-name">{c.keyword}</span>
                  {c.present
                    ? <span className="kw-pill">already on resume</span>
                    : <span className="kw-meta">→ {c.suggested_section || 'skills'}</span>}
                </label>
              ))}

              <h4 className="nice" style={{ marginTop: 14 }}>Nice-to-have ({(analysis.nice_to_have || []).length})</h4>
              {(analysis.nice_to_have || []).map((c, i) => (
                <label key={'n' + i} className="tailor-keyword-row">
                  <input type="checkbox" checked={!!selectedKws[c.keyword]}
                         disabled={c.present}
                         onChange={e => setSelectedKws(s => ({ ...s, [c.keyword]: e.target.checked }))}/>
                  <span className="kw-name">{c.keyword}</span>
                  {c.present
                    ? <span className="kw-pill">already on resume</span>
                    : <span className="kw-meta">→ {c.suggested_section || 'skills'}</span>}
                </label>
              ))}

              <div className="tailor-review-stats">
                ATS score: <b>{analysis.ats_score_current}</b>
                {' → estimated after: '}
                <b className="good">{analysis.estimated_after}</b>
              </div>
              <div className="tailor-review-actions">
                <button onClick={() => generate(true)}>Skip review — generate now</button>
                <button className="primary" onClick={() => generate(false)}>
                  Generate with selected ({selectedCount})
                </button>
              </div>
            </div>
          )}

          {stage === 'result' && item && (
            <div className="tailor-result">
              {item.resume_file && (
                <div role="status"
                  style={{
                    display: 'flex', alignItems: 'center', gap: 10,
                    padding: '10px 13px',
                    marginBottom: 12,
                    borderRadius: 9,
                    background: 'var(--good-d)',
                    border: '1px solid var(--good-b)',
                    fontSize: 13.5, color: 'var(--t1)',
                  }}>
                  <Icon name="check-circle-2" size={14} color="var(--good)"/>
                  <span style={{ flex: 1, lineHeight: 1.45 }}>
                    Saved to your <b>Documents</b> library —
                    <span style={{ color: 'var(--t3)', marginLeft: 4 }}>
                      <code style={{ fontFamily: 'var(--mono)', fontSize: 12.5 }}>
                        {String(item.resume_file).split('/').pop()}
                      </code>
                    </span>
                  </span>
                  {item.final_pdf_url && (
                    <a className="btn-primary"
                      href={item.final_pdf_url}
                      download
                      style={{ padding: '5px 11px', fontSize: 13, textDecoration: 'none' }}
                      title="Clean (all-black) PDF for sending to employers — no diff highlights">
                      <Icon name="download" size={11}/>
                      Final PDF
                    </a>
                  )}
                  {onOpenDocuments && (
                    <button className="btn-ghost"
                      style={{ padding: '5px 11px', fontSize: 13 }}
                      onClick={onOpenDocuments}>
                      <Icon name="folder-open" size={11}/>
                      View
                    </button>
                  )}
                </div>
              )}
              <TailoredResumePreview item={{
                ...item,
                co: item.co || co, role: item.role || role,
                loc: item.loc || job.loc || job.location || '',
                score: item.score || score || 0,
              }}/>
            </div>
          )}
        </div>
      </aside>
    </div>
  );
}

function TailoredResumePreview({ item }) {
  // The HTML preview file is the EXACT content WeasyPrint rendered to PDF —
  // green highlights, layout, fonts. Embedding it as an iframe gives a
  // pixel-equivalent in-page preview without re-rendering on the client.
  const previewUrl = item.html_preview_url || null;
  const tplLabel = item.template_id ? String(item.template_id).replace(/_/g, ' ') : null;
  const isInPlace = item.template_id === 'in_place_latex' || item.template_id === 'in_place_docx';
  return (
    <>
      {previewUrl && (
        <div style={{ marginBottom: 12 }}>
          <iframe className="tailor-preview-frame"
                  src={previewUrl}
                  title="Tailored resume preview"/>
          {tplLabel && (
            <div className="tailor-template-pick">
              <span>{isInPlace ? 'Mode:' : 'Template:'}</span>
              <b>{tplLabel}</b>
              {!isInPlace && item.template_confidence != null && (
                <span>· {Math.round(item.template_confidence * 100)}% match</span>
              )}
            </div>
          )}
        </div>
      )}
      <TailoredResumeCard item={item}/>
    </>
  );
}

/* ── Action menu helper ── */
function ActionMenu({ items = [] }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState(null);  // { top, right } in viewport coords
  const wrapRef = useRef(null);
  const menuRef = useRef(null);

  // Recompute the menu's viewport position from the trigger's rect. The menu
  // is rendered with position: fixed so it cannot be clipped by any ancestor
  // overflow (e.g. .data-card has overflow:hidden for its rounded corners).
  const place = useCallback(() => {
    const btn = wrapRef.current?.querySelector('button');
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const MENU_WIDTH = 180;
    const MENU_HEIGHT_EST = 40 * Math.max(1, items.length) + 8;
    const gap = 6;
    // Default: anchor below the trigger, right-aligned.
    let top = r.bottom + gap;
    let right = window.innerWidth - r.right;
    // Flip up if there isn't room below.
    if (top + MENU_HEIGHT_EST > window.innerHeight - 8) {
      top = Math.max(8, r.top - MENU_HEIGHT_EST - gap);
    }
    // Keep on-screen horizontally.
    right = Math.max(8, Math.min(right, window.innerWidth - MENU_WIDTH - 8));
    setPos({ top, right });
  }, [items.length]);

  useEffect(() => {
    if (!open) return;
    place();
    const hide = e => {
      if (wrapRef.current?.contains(e.target)) return;
      if (menuRef.current?.contains(e.target)) return;
      setOpen(false);
    };
    const dismiss = () => setOpen(false);
    document.addEventListener('mousedown', hide);
    // Close on scroll/resize rather than tracking — the user moved away.
    window.addEventListener('scroll', dismiss, true);
    window.addEventListener('resize', dismiss);
    return () => {
      document.removeEventListener('mousedown', hide);
      window.removeEventListener('scroll', dismiss, true);
      window.removeEventListener('resize', dismiss);
    };
  }, [open, place]);

  return (
    <div className="action-menu-wrap" ref={wrapRef}>
      <button className="icon-btn" onClick={e => { e.stopPropagation(); setOpen(!open); }} style={{ borderColor:'transparent' }}>
        <Icon name="more-horizontal" size={14}/>
      </button>
      {open && pos && (
        <div
          ref={menuRef}
          className="action-menu fade-in"
          style={{ position:'fixed', top: pos.top, right: pos.right }}
          onClick={e => e.stopPropagation()}
        >
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
   RESUME ANALYSIS — score ring + metric tiles + insight rows
══════════════════════════════════════════════════════════ */
function ScoreHero({ score, verifiedBy, notes, verifiedError }) {
  const pct = Math.max(0, Math.min(100, Math.round(score || 0)));
  const C = 56, circ = 2 * Math.PI * C;
  const off = circ - (circ * pct / 100);
  const color = pct >= 80 ? 'var(--good)' : pct >= 60 ? 'var(--accent-h)' : pct >= 40 ? 'var(--warn)' : 'var(--bad)';
  const label = pct >= 85 ? 'Strong' : pct >= 70 ? 'Solid' : pct >= 55 ? 'Promising' : pct >= 40 ? 'Needs work' : 'Reach';
  const sub = pct >= 85 ? 'Top-decile resume — refine specifics.'
            : pct >= 70 ? 'Above the bar; close the last few gaps.'
            : pct >= 55 ? 'Has the bones; sharpen impact and verbs.'
            : pct >= 40 ? 'Quantification & action verbs are the unlocks.'
            :              'Restructure: numbers, action verbs, sections.';
  // Any non-"heuristic" verified_by counts as a successful AI verification.
  // The specific provider name (e.g. "ollama:llama3.2", "anthropic:opus") is
  // intentionally not surfaced — the user shouldn't have to know which
  // backend ran the check, and they shouldn't see an apology when the LLM
  // step fails. The heuristic output is itself a complete, valid analysis.
  const verified = !!verifiedBy && verifiedBy !== 'heuristic';
  return (
    <div className="rs-hero">
      <div className="rs-hero-ring">
        <svg width="140" height="140" viewBox="0 0 140 140">
          <circle cx="70" cy="70" r={C} fill="none" strokeWidth="8" stroke="var(--bdr)"/>
          <circle cx="70" cy="70" r={C} fill="none" strokeWidth="8" stroke={color}
            strokeLinecap="round" strokeDasharray={circ} strokeDashoffset={off}
            transform="rotate(-90 70 70)"
            style={{ transition:'stroke-dashoffset 1.1s cubic-bezier(.16,1,.3,1)' }}/>
        </svg>
        <div className="rs-hero-num">
          <div style={{ fontSize:40, fontWeight:700, lineHeight:1, color }}>{pct}</div>
          <div style={{ fontSize:14, color:'var(--t3)', marginTop:2, letterSpacing:'.5px' }}>/ 100</div>
        </div>
      </div>
      <div className="rs-hero-text">
        <div className="rs-hero-rating" style={{ color }}>{label}</div>
        <div className="rs-hero-tag">{sub}</div>
        <div className={'rs-verify ' + (verified ? 'ok' : 'info')}>
          <Icon name={verified ? 'shield-check' : 'sparkles'} size={11}/>
          {verified
            ? <>AI-verified analysis</>
            : <>Heuristic analysis</>}
        </div>
        {/* Only surface verifier notes when the AI verification actually
            succeeded; failure messages would just be alarming noise on top
            of an already-valid heuristic analysis. */}
        {verified && notes ? <div className="rs-verify-notes">{notes}</div> : null}
      </div>
    </div>
  );
}

function MetricTile({ label, value, hint, tone }) {
  const color = tone === 'good' ? 'var(--good)' : tone === 'bad' ? 'var(--bad)' : tone === 'warn' ? 'var(--warn)' : 'var(--accent-h)';
  return (
    <div className="rs-tile">
      <div className="rs-tile-val" style={{ color }}>{value}</div>
      <div className="rs-tile-lbl">{label}</div>
      {hint ? <div className="rs-tile-hint">{hint}</div> : null}
    </div>
  );
}

function InsightRow({ kind, children }) {
  // kind: 'good' | 'bad' | 'hint'
  const map = {
    good: { icon: 'check', color: 'var(--good)', bg: 'var(--good-d)', bd: 'var(--good-b)' },
    bad:  { icon: 'alert-triangle', color: 'var(--bad)', bg: 'var(--bad-d)', bd: 'var(--bad-b)' },
    hint: { icon: 'lightbulb', color: 'var(--accent-h)', bg: 'var(--accent-d)', bd: 'var(--accent-b)' },
  }[kind] || {};
  return (
    <div className="rs-insight" style={{ background: map.bg, borderColor: map.bd }}>
      <span className="rs-insight-icon" style={{ color: map.color }}>
        <Icon name={map.icon} size={12}/>
      </span>
      <span className="rs-insight-text">{children}</span>
    </div>
  );
}

/* ── Resume analysis: shared metric tile generator ── */
function getMetricTiles(metrics = {}) {
  const m = metrics || {};
  return {
    volume: [
      { label: 'Words',     value: m.word_count ?? '—',
        hint: (m.word_count >= 350 && m.word_count <= 700) ? 'in the sweet spot'
              : (m.word_count > 700 ? 'trim toward 600' : 'on the thin side'),
        tone: (m.word_count >= 350 && m.word_count <= 700) ? 'good' : 'warn' },
      { label: 'Bullets',   value: m.bullet_count ?? '—',
        hint: m.bullet_count >= 8 ? 'enough surface' : 'add more',
        tone: m.bullet_count >= 8 ? 'good' : 'warn' },
      { label: 'Sections',  value: m.section_count ?? '—',
        hint: (m.sections || []).slice(0,3).map(s => s.replace(/\b\w/g, c => c.toUpperCase())).join(' · ') || 'baseline',
        tone: m.section_count >= 4 ? 'good' : 'warn' },
      { label: 'Read time', value: (m.reading_seconds != null ? `${m.reading_seconds}s` : '—'),
        hint: 'recruiters give 6–15s', tone: 'warn' },
    ],
    impact: [
      { label: 'Quantified',    value: (m.quantified_pct != null ? `${m.quantified_pct}%` : '—'),
        hint: `${m.quantified_count || 0} / ${m.bullet_count || 0} bullets · target ≥60%`,
        tone: m.quantified_pct >= 60 ? 'good' : m.quantified_pct >= 40 ? 'warn' : 'bad' },
      { label: 'Action verbs',  value: (m.action_verb_pct != null ? `${m.action_verb_pct}%` : '—'),
        hint: 'target ≥70%',
        tone: m.action_verb_pct >= 70 ? 'good' : m.action_verb_pct >= 50 ? 'warn' : 'bad' },
      { label: 'Avg bullet',    value: (m.avg_bullet_len != null ? `${m.avg_bullet_len}w` : '—'),
        hint: 'sweet spot 12–20w',
        tone: (m.avg_bullet_len >= 12 && m.avg_bullet_len <= 22) ? 'good' : 'warn' },
      { label: 'Skill density', value: (m.skill_density != null ? `${m.skill_density}` : '—'),
        hint: 'per 100 words · target ≥6',
        tone: m.skill_density >= 6 ? 'good' : m.skill_density >= 4 ? 'warn' : 'bad' },
    ],
    hygiene: [
      { label: 'Weak phrases',  value: m.weak_phrase_count ?? '—',
        hint: m.weak_phrase_count === 0 ? 'clean' : 'replace each',
        tone: m.weak_phrase_count === 0 ? 'good' : 'bad' },
      { label: 'Buzzwords',     value: m.buzzword_count ?? '—',
        hint: m.buzzword_count === 0 ? 'no clichés' : 'cut the fluff',
        tone: m.buzzword_count === 0 ? 'good' : 'warn' },
    ],
  };
}

function deriveTopActions(insights = {}) {
  const flags = (insights.red_flags || []).slice(0, 1);
  const tips  = (insights.suggestions || []).slice(0, 3 - flags.length);
  return [
    ...flags.map(t => ({ kind: 'flag', text: t })),
    ...tips.map(t  => ({ kind: 'tip',  text: t })),
  ];
}

/* ── Sub-view 1/4: Overview (score + top priorities + key signals) ── */
function RsOverview({ insights }) {
  const groups   = getMetricTiles(insights?.metrics);
  const keyTiles = [groups.impact[0], groups.impact[1], groups.volume[0], groups.volume[2]];
  const actions  = deriveTopActions(insights);
  return (
    <div className="rs-overview fade-in">
      <div className="rs-overview-top">
        <ScoreHero
          score={insights?.overall_score}
          verifiedBy={insights?.verified_by}
          verifiedError={insights?.verification_error}
          notes={insights?.verification_notes}
        />
        <div className="rs-tldr">
          <div className="rs-tldr-h">Top priorities</div>
          {actions.length === 0 ? (
            <div className="rs-tldr-empty">
              <Icon name="check-circle-2" size={14}/>
              No critical fixes — your resume hits the major rubric points. Refine specifics on Deep dive.
            </div>
          ) : (
            <div className="rs-tldr-list">
              {actions.map((a, i) => (
                <div key={i} className="rs-tldr-item">
                  <span className={'rs-tldr-num ' + a.kind}>{i + 1}</span>
                  <span className="rs-tldr-text">{a.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
      <div className="rs-keysec">
        <div className="rs-metric-group-h">Key signals</div>
        <div className="rs-keystrip">
          {keyTiles.map((t, i) => <MetricTile key={i} {...t}/>)}
        </div>
      </div>
    </div>
  );
}

/* ── Sub-view 2/4: Metrics (full grid, grouped) ── */
function RsMetrics({ insights }) {
  const g = getMetricTiles(insights?.metrics);
  const groups = [
    { key:'volume',  title:'Volume & Structure', tiles: g.volume,  hint:'How long your resume is and how it reads' },
    { key:'impact',  title:'Impact & Substance', tiles: g.impact,  hint:'How tangibly your bullets argue your case' },
    { key:'hygiene', title:'Hygiene',            tiles: g.hygiene, hint:'Phrases and clichés to revise' },
  ];
  return (
    <div className="fade-in">
      {groups.map(group => (
        <div key={group.key} className="rs-metric-group">
          <div className="rs-metric-group-h">
            <span>{group.title}</span>
            <span className="rs-metric-group-hint">{group.hint}</span>
          </div>
          <div className="rs-tile-grid">
            {group.tiles.map((t, i) => <MetricTile key={i} {...t}/>)}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ── Sub-view 3/4: Insights (strengths · red flags · improvements) ── */
function RsInsights({ insights }) {
  const strengths   = insights?.strengths   || [];
  const redFlags    = insights?.red_flags   || [];
  const suggestions = insights?.suggestions || [];
  const sections = [
    { tone:'good',   title:"What's working",        icon:'trending-up',   items:strengths,
      empty:'No standout strengths surfaced yet — see Targeted improvements below.' },
    { tone:'bad',    title:'Red flags',             icon:'alert-octagon', items:redFlags,
      empty:'No structural red flags found.', emptyOk:true },
    { tone:'accent', title:'Targeted improvements', icon:'target',        items:suggestions,
      empty:'Resume hits the major rubric points.' },
  ];
  return (
    <div className="rs-insights-page fade-in">
      {sections.map((sec, idx) => {
        const color = sec.tone === 'good' ? 'var(--good)'
                    : sec.tone === 'bad'  ? 'var(--bad)'
                    : 'var(--accent-h)';
        return (
          <div key={idx} className="rs-insights-sec">
            <div className="rs-insights-sec-h" style={{ color }}>
              <Icon name={sec.icon} size={13}/>
              <span>{sec.title}</span>
              <span className="rs-insights-sec-count">{sec.items.length}</span>
            </div>
            {sec.items.length > 0
              ? <div className="rs-insights-list">
                  {sec.items.map((s, i) => (
                    <InsightRow key={i} kind={sec.tone === 'accent' ? 'hint' : sec.tone}>{s}</InsightRow>
                  ))}
                </div>
              : <div className="set-helper" style={sec.emptyOk ? { color:'var(--good)' } : {}}>{sec.empty}</div>}
          </div>
        );
      })}
    </div>
  );
}

/* ── Sub-view 4/4: Deep dive (narrative + target roles + rescan) ── */
function RsDeepDive({ insights, profile, onRescan, rescanning }) {
  const text   = insights?.narrative || profile?.critical_analysis || '';
  const titles = profile?.target_titles || [];
  const metrics     = insights?.metrics     || {};
  const strengths   = insights?.strengths   || [];
  const redFlags    = insights?.red_flags   || [];
  const suggestions = insights?.suggestions || [];
  const score       = insights?.overall_score;
  const verified    = !!(insights?.verified_by && insights.verified_by !== 'heuristic');
  const verifNotes  = (insights?.verification_notes || '').trim();

  // Split narrative on blank lines so each paragraph is a real <p>. Lets the
  // CSS drop-cap apply only to the first paragraph and gives proper rhythm
  // between paragraphs without relying on white-space:pre-wrap.
  const paragraphs = String(text || '')
    .split(/\n\s*\n+/g)
    .map(p => p.trim())
    .filter(Boolean);

  // Pick a pull-quote — the punchiest sentence from the narrative for the
  // sidecar's editorial highlight slot. Falls back to the verification
  // notes when the narrative is empty / sparse.
  const pullQuote = (() => {
    const src = paragraphs[0] || verifNotes || '';
    if (!src) return '';
    const sentences = src.split(/(?<=[.!?])\s+/).filter(s => s.length > 30 && s.length < 220);
    return sentences[0] || '';
  })();

  const tileFmt = (val, suffix = '') =>
    val == null || (typeof val === 'number' && !Number.isFinite(val))
      ? '—' : `${val}${suffix}`;

  const stats = [
    { label: 'Quantified',   value: tileFmt(metrics.quantified_pct,  '%'),
      hint: 'bullets w/ numbers', tone: (metrics.quantified_pct  ?? 0) >= 50 ? 'good' : (metrics.quantified_pct  ?? 0) >= 30 ? 'warn' : 'bad' },
    { label: 'Action verbs', value: tileFmt(metrics.action_verb_pct, '%'),
      hint: 'strong leads',       tone: (metrics.action_verb_pct ?? 0) >= 60 ? 'good' : (metrics.action_verb_pct ?? 0) >= 40 ? 'warn' : 'bad' },
    { label: 'Skill density', value: tileFmt(metrics.skill_density),
      hint: '/100w',              tone: (metrics.skill_density   ?? 0) >= 6  ? 'good' : (metrics.skill_density   ?? 0) >= 4  ? 'warn' : 'bad' },
    { label: 'Words',        value: tileFmt(metrics.word_count),
      hint: 'total',              tone: (metrics.word_count >= 350 && metrics.word_count <= 700) ? 'good' : 'warn' },
  ];

  return (
    <div className="rs-deep fade-in">
      {/* Editorial header — sits above the magazine spread */}
      <header className="rs-deep-head">
        <div className="rs-narrative-sub">Critical analysis</div>
        <h3 className="rs-narrative-h">A reading of <em>your resume</em></h3>
        <div className="rs-deep-folio">
          <span className="rs-deep-folio-rule"/>
          <span className="rs-deep-folio-label">
            {verified ? 'AI-verified · cross-checked against the original resume text' : 'Heuristic reading · scan tuned for resume quality signals'}
          </span>
          {score != null && (
            <span className="rs-deep-folio-score">
              <b>{Math.round(score)}</b><i>/100</i>
            </span>
          )}
        </div>
      </header>

      {/* Two-column magazine spread: narrative + supporting sidecar */}
      <div className="rs-deep-grid">
        <article className="rs-narrative">
          {paragraphs.length > 0
            ? paragraphs.map((p, i) => (
                <p key={i} className={'rs-narrative-p' + (i === 0 ? ' rs-lede' : '')}>
                  {p}
                </p>
              ))
            : <div className="rs-narrative-empty">
                <Icon name="file-text" size={20} color="var(--t3)"/>
                <div>
                  <div className="rs-narrative-empty-h">No narrative yet</div>
                  <div className="rs-narrative-empty-p">
                    Re-scan to produce a detailed editorial read of your resume — quantified bullets, action-verb cadence, structural notes, and a recommended next move.
                  </div>
                </div>
              </div>}
          {pullQuote && paragraphs.length >= 2 && (
            <aside className="rs-narrative-pull" aria-hidden="true">
              <span className="rs-narrative-pull-mark">&ldquo;</span>
              <span>{pullQuote}</span>
            </aside>
          )}
        </article>

        <aside className="rs-deep-aside">
          {/* Score tile — anchors the sidecar */}
          {score != null && (
            <section className="rs-aside-block rs-aside-score">
              <div className="rs-aside-h"><Icon name="gauge" size={11}/>At a glance</div>
              <div className="rs-aside-score-body">
                <div className="rs-aside-score-num">
                  {Math.round(score)}<i>/100</i>
                </div>
                <span className={'rs-aside-score-tag ' + (verified ? 'ok' : 'info')}>
                  <Icon name={verified ? 'shield-check' : 'sparkles'} size={11}/>
                  {verified ? 'AI verified' : 'Heuristic'}
                </span>
              </div>
              <ul className="rs-aside-stats">
                {stats.map(s => (
                  <li key={s.label} className={'tone-' + s.tone}>
                    <span className="rs-aside-stat-l">{s.label}</span>
                    <span className="rs-aside-stat-v">{s.value}</span>
                    <span className="rs-aside-stat-h">{s.hint}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Target roles inferred */}
          {titles.length > 0 && (
            <section className="rs-aside-block">
              <div className="rs-aside-h"><Icon name="briefcase" size={11}/>Target roles</div>
              <div className="rs-aside-pills">
                {titles.slice(0, 8).map((t, i) => (
                  <span key={i} className="skill-pill hard">{t}</span>
                ))}
              </div>
            </section>
          )}

          {/* Strengths — green dot list */}
          {strengths.length > 0 && (
            <section className="rs-aside-block">
              <div className="rs-aside-h"><Icon name="check-circle-2" size={11}/>Strengths</div>
              <ul className="rs-aside-list good">
                {strengths.slice(0, 4).map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </section>
          )}

          {/* Red flags — pink dot list */}
          {redFlags.length > 0 && (
            <section className="rs-aside-block">
              <div className="rs-aside-h"><Icon name="alert-triangle" size={11}/>Red flags</div>
              <ul className="rs-aside-list bad">
                {redFlags.slice(0, 4).map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </section>
          )}

          {/* Suggestions — accent dot list */}
          {suggestions.length > 0 && (
            <section className="rs-aside-block">
              <div className="rs-aside-h"><Icon name="zap" size={11}/>Suggestions</div>
              <ul className="rs-aside-list accent">
                {suggestions.slice(0, 4).map((s, i) => <li key={i}>{s}</li>)}
              </ul>
            </section>
          )}

          {/* Action card — Re-scan CTA + verification meta */}
          <section className="rs-aside-block rs-aside-action">
            <button className="rs-aside-rescan" onClick={onRescan} disabled={rescanning}>
              {rescanning
                ? <span className="spin" style={{ width:12, height:12, borderWidth:1.5 }}/>
                : <Icon name="refresh-cw" size={12} color="#fff"/>}
              {rescanning ? 'Re-scanning…' : 'Re-scan & re-verify'}
            </button>
            <div className="rs-aside-action-hint">
              {verified
                ? 'Re-runs the heuristic scanner and asks the configured AI to double-check every claim.'
                : 'Switch to Anthropic or Ollama in Settings to upgrade this reading from heuristic to AI-verified.'}
            </div>
          </section>
        </aside>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   RESUME PAGE
══════════════════════════════════════════════════════════ */
function ResumePage({ state, refresh, setPage }) {
  const [resumeText, setResumeText] = useState('');
  const [tab, setTab] = useState('analysis');
  const [analysisView, setAnalysisView] = useState('overview'); // overview | metrics | insights | detail
  const [loading, setLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [rescanning, setRescanning] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const [selectedId, setSelectedId] = useState(null);  // which row is selected
  // 'document' embeds the original PDF in an iframe; 'text' shows the
  // extracted plaintext (still the only option for .docx/.tex/.txt).
  // Default to 'document' so a fresh upload renders the PDF the user just
  // sent — that's the explicit behavior they asked for.
  const [previewMode, setPreviewMode] = useState('document');
  const [deleteConfirmId, setDeleteConfirmId] = useState(null);
  // "Add resume" splits into two paths: upload a file, or paste text into
  // a modal that round-trips the same /api/resume/upload endpoint as a
  // pasted_resume.txt blob.
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [pasteName, setPasteName] = useState('pasted_resume.txt');
  const fileRef = useRef(null);
  const addMenuRef = useRef(null);
  // Close the Add menu when the user clicks outside it.
  useEffect(() => {
    if (!addMenuOpen) return undefined;
    const off = (e) => {
      if (addMenuRef.current && !addMenuRef.current.contains(e.target)) {
        setAddMenuOpen(false);
      }
    };
    document.addEventListener('mousedown', off);
    return () => document.removeEventListener('mousedown', off);
  }, [addMenuOpen]);
  // Track per-upload extraction polls so we can clear them when the user
  // navigates away or kicks off another upload — otherwise setInterval keeps
  // ticking for the full 2-min safety cap and triggers stale refreshes.
  const pollsRef = useRef(new Set());
  useEffect(() => () => {
    for (const id of pollsRef.current) clearInterval(id);
    pollsRef.current.clear();
  }, []);

  const resumes = state?.resumes || [];
  const primary = resumes.find(r => r.primary) || resumes[0];
  // Active selection: user-clicked or falls back to primary
  const selected = resumes.find(r => r.id === selectedId) || primary;
  const has = !!resumes.length;

  // Per-resume profile (not the global pipeline profile)
  const sp = selected?.profile || null;
  const isAnalyzed = !!sp;

  // When selected resume changes, reset text + editing state and fetch content
  useEffect(() => {
    if (!selected) { setResumeText(''); setEditText(''); return; }
    setIsEditing(false);
    setAnalysisView('overview');
    setResumeText('');
    setEditText('');
    setLoading(true);
    api.get(`/api/resume/content?id=${selected.id}`)
      .then(res => { setResumeText(res.text); setEditText(res.text); })
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [selected?.id]);

  // Back-fill the preview PDF for legacy records (uploaded before the
  // auto-render landed). Fires once per resume when we have a record
  // with no embeddable PDF but text content exists. The render itself
  // is server-side; we just refresh after it's done so the iframe picks
  // up the new preview_pdf_url.
  const previewBackfillTriedRef = useRef(new Set());
  useEffect(() => {
    if (!selected) return;
    if (previewBackfillTriedRef.current.has(selected.id)) return;
    const hasOriginalPdf = selected.original_kind === 'pdf' && selected.original_url;
    const hasPreview = !!selected.preview_pdf_url;
    if (hasOriginalPdf || hasPreview) return;
    if (selected.extracting) return;        // wait until extraction settles
    previewBackfillTriedRef.current.add(selected.id);
    api.post(`/api/resume/${selected.id}/render-preview`, {})
      .then(() => refresh?.())
      .catch(() => {/* graceful: text fallback still works */});
  }, [selected?.id, selected?.original_url, selected?.preview_pdf_url, selected?.extracting]);

  const stats = useMemo(() => {
    if (!sp) return null;
    return {
      skills: (sp.top_hard_skills || []).length,
      exp: (sp.experience || []).length,
      gaps: (sp.resume_gaps || []).length,
    };
  }, [sp]);

  // Shared upload pipeline used by both the file-picker and the paste-text
  // modal. Both end up calling /api/resume/upload with a File object — the
  // backend can't tell the difference and the same extraction polling runs.
  const submitResume = async (file) => {
    if (!file) return;
    setUploading(true);
    try {
      const result = await api.upload('/api/resume/upload', file);
      if (result?.id) setSelectedId(result.id);
      refresh();
      // Poll while extraction is in flight so the analysis lights up live.
      if (result?.extracting) {
        const poll = setInterval(async () => {
          const s = await refresh();
          const r = (s?.resumes || []).find(x => x.id === result.id);
          if (!r || !r.extracting) {
            clearInterval(poll);
            pollsRef.current.delete(poll);
          }
        }, 2000);
        pollsRef.current.add(poll);
        setTimeout(() => {
          clearInterval(poll);
          pollsRef.current.delete(poll);
        }, 120000);
      }
    } catch (e) {
      alert(e.message);
    } finally {
      setUploading(false);
    }
  };

  const handleUpload = (file) => submitResume(file);

  const handlePasteSubmit = async () => {
    const text = (pasteText || '').trim();
    if (!text) {
      alert('Paste your resume text first.');
      return;
    }
    // Sanitize the filename — strip path separators, ensure .txt suffix.
    let fname = (pasteName || 'pasted_resume.txt').trim().replace(/[/\\]/g, '_');
    if (!/\.[a-z0-9]{1,5}$/i.test(fname)) fname += '.txt';
    const blob = new Blob([text], { type: 'text/plain' });
    const file = new File([blob], fname, { type: 'text/plain' });
    setPasteOpen(false);
    setPasteText('');
    setPasteName('pasted_resume.txt');
    await submitResume(file);
  };

  const handleDelete = (id) => setDeleteConfirmId(id);

  const handleDeleteExecute = async () => {
    const id = deleteConfirmId;
    setDeleteConfirmId(null);
    try {
      await api.delete(`/api/resume/${id}`);
      refresh();
    } catch (e) {
      alert(e.message);
    }
  };

  const handleSetPrimary = async (id) => {
    try {
      // Backend copies the resume's stored profile into _S["profile"] so the
      // Profile page populates immediately. If the resume hasn't been scanned
      // yet, the backend kicks off extraction in the background and returns
      // { extracting: true } — poll until it lands so the Profile page
      // transitions out of "analyzing" without the user refreshing.
      const res = await api.post(`/api/resume/primary/${id}`, {});
      setResumeText(''); // Clear to trigger re-fetch
      await refresh();
      if (res?.extracting) {
        const poll = setInterval(async () => {
          const s = await refresh();
          const r = (s?.resumes || []).find(x => x.id === id);
          if (!r || !r.extracting) clearInterval(poll);
        }, 2000);
        setTimeout(() => clearInterval(poll), 120000);
      }
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
    if (!selected) return;
    setLoading(true);
    try {
      await api.post('/api/resume/text', { id: selected.id, text: editText });
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
        {/* Re-scan the currently-selected resume with the configured AI.
            Mirrors the home-page hero CTA (Dashboard) so the same action is
            reachable without bouncing back to home. Disabled while a scan
            is already in flight (server-side `extracting` flag OR our local
            optimistic flag). Hidden when there's no resume yet — nothing
            to scan. */}
        {selected && (
          <button
            className="btn-ghost"
            disabled={rescanning || !!selected.extracting}
            onClick={async () => {
              if (rescanning || selected.extracting) return;
              setRescanning(true);
              try {
                await api.post('/api/profile/extract',
                                { resume_id: selected.id, force: true });
                await refresh?.();
              } catch (_) { /* server-side extracting state surfaces via polling */ }
              finally { setRescanning(false); }
            }}
            style={{ marginRight: 8 }}
            title={(rescanning || selected.extracting)
              ? 'Re-scan in progress…'
              : `Re-scan ${selected.filename} with the configured AI`}
          >
            {(rescanning || selected.extracting)
              ? <><span className="spin"/> Scanning…</>
              : <><Icon name="refresh-cw" size={13} color="var(--cyan, #22e5ff)"/> Re-scan</>}
          </button>
        )}
        <div ref={addMenuRef} style={{ position:'relative', display:'inline-flex' }}>
          <button
            className="head-cta"
            onClick={() => setAddMenuOpen(o => !o)}
            disabled={uploading}
            style={{ display:'inline-flex', alignItems:'center', gap:6 }}
          >
            {uploading ? <span className="spin"/> : <Icon name="plus" size={13} color="#fff"/>}
            {uploading ? 'Uploading…' : 'Add resume'}
            {!uploading && <Icon name="chevron-down" size={11} color="#fff"/>}
          </button>
          {addMenuOpen && !uploading && (
            <div
              className="action-menu fade-in"
              style={{ position:'absolute', top:'calc(100% + 6px)', right:0, minWidth:240, zIndex:50 }}
            >
              <button
                className="menu-item"
                onClick={() => { setAddMenuOpen(false); fileRef.current?.click(); }}
              >
                <Icon name="upload-cloud" size={13}/>
                <span>Upload file</span>
              </button>
              <button
                className="menu-item"
                onClick={() => { setAddMenuOpen(false); setPasteOpen(true); }}
              >
                <Icon name="clipboard" size={13}/>
                <span>Paste text</span>
              </button>
              <div style={{ padding:'8px 12px', borderTop:'1px solid var(--bdr)', fontSize:11, color:'var(--t4)', lineHeight:1.4 }}>
                For best format match, upload <b style={{ color:'var(--t3)' }}>.tex</b> or <b style={{ color:'var(--t3)' }}>.docx</b>. PDF works too — matched to the closest template.
              </div>
            </div>
          )}
        </div>
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
            {has ? resumes.map(r => {
              const isSelected = r.id === selected?.id;
              return (
              <div key={r.id} className="dt-row"
                style={{ cursor:'pointer', background: isSelected ? 'var(--accent-d)' : undefined }}
                onClick={() => setSelectedId(r.id)}>
                <div className="dt-name">
                  <div className="dt-icon" style={!r.primary ? { background:'var(--bg-3)', color:'var(--t3)' } : {}}>{r.filename.charAt(0).toUpperCase()}</div>
                  <span title={r.filename}>{r.filename.replace(/\.[^.]+$/, '')}</span>
                  {r.primary && <span className="badge b-accent">Primary</span>}
                  {r.extracting && <span className="badge b-warn" style={{ display:'inline-flex', alignItems:'center', gap:4 }}><span className="spin" style={{ width:8, height:8, borderWidth:1.5 }}/> Analyzing</span>}
                  {r.analyzed && !r.extracting && <span className="badge b-good">Analyzed</span>}
                  {r.extract_error && <span className="badge b-warn" title={r.extract_error}>Failed</span>}
                </div>
                <div style={{ color:'var(--t2)', fontSize:14.5 }}>
                  {r.profile?.target_titles?.[0] || <span style={{ color:'var(--t3)' }}>—</span>}
                </div>
                <div style={{ color:'var(--t3)', fontFamily:'var(--mono)', fontSize:14 }}>{r.updated_at ? new Date(r.updated_at).toLocaleDateString() : 'just now'}</div>
                <div style={{ color:'var(--t3)', fontFamily:'var(--mono)', fontSize:14 }}>{r.created_at ? new Date(r.created_at).toLocaleDateString() : 'just now'}</div>
                <div onClick={e => e.stopPropagation()}>
                  <ActionMenu items={[
                    { icon:'star', label:'Set as primary', onClick: () => handleSetPrimary(r.id) },
                    { icon:'pencil', label:'Rename', onClick: () => handleRename(r.id, r.filename) },
                    { icon:'edit-3', label:'Edit text', onClick: () => { setSelectedId(r.id); setTab('preview'); setIsEditing(true); } },
                    { icon:'trash-2', label:'Delete', danger: true, onClick: () => handleDelete(r.id) },
                  ]}/>
                </div>
              </div>
            );}) : (
              <div className="dt-empty">No resumes yet — add one to start matching jobs.</div>
            )}
          </div>

          {selected && (
            <div className="rs-detail-area">
              {/* "Now viewing" banner */}
              <div className="rs-viewing">
                <div className="rs-viewing-icon">{selected.filename.charAt(0).toUpperCase()}</div>
                <div className="rs-viewing-meta-col">
                  <div className="rs-viewing-name">{selected.filename.replace(/\.[^.]+$/, '')}</div>
                  <div className="rs-viewing-meta">
                    {selected.primary ? 'Primary' : 'Saved'}
                    <span className="rs-viewing-dot">·</span>
                    Updated {selected.updated_at ? new Date(selected.updated_at).toLocaleDateString() : 'just now'}
                    {isAnalyzed && sp?.insights?.overall_score != null && (
                      <>
                        <span className="rs-viewing-dot">·</span>
                        Score {Math.round(sp.insights.overall_score)}/100
                      </>
                    )}
                  </div>
                </div>
                {!selected.primary && (
                  <button className="btn-ghost rs-viewing-action"
                    onClick={() => handleSetPrimary(selected.id)}>
                    <Icon name="star" size={11}/> Set as primary
                  </button>
                )}
              </div>

              <div className="prof-tabs" style={{ marginBottom:14 }}>
                <button className={'prof-tab' + (tab==='analysis' ? ' active' : '')} onClick={() => { setTab('analysis'); setIsEditing(false); }}>
                  <Icon name="bar-chart-3" size={13} style={{ marginRight:6 }}/> Analysis
                </button>
                <button className={'prof-tab' + (tab==='preview' ? ' active' : '')} onClick={() => setTab('preview')}>
                  <Icon name="eye" size={13} style={{ marginRight:6 }}/> Preview
                </button>
              </div>

              {tab === 'preview' && (() => {
                // Pick which PDF (if any) to embed. Original wins when the
                // user uploaded a real .pdf; otherwise the rendered preview
                // (auto-generated for .txt / .docx / .tex / .md) is used.
                const originalIsPdf = !!selected.original_url && selected.original_kind === 'pdf';
                const embedUrl = originalIsPdf
                  ? selected.original_url
                  : (selected.preview_pdf_url || '');
                const embedLabel = originalIsPdf
                  ? 'Original PDF'
                  : (embedUrl ? 'Generated preview' : '');
                const downloadUrl = selected.original_url || selected.preview_pdf_url || '';
                const downloadLabel = selected.original_url
                  ? 'Download original'
                  : (selected.preview_pdf_url ? 'Download preview PDF' : 'No file to download');
                const showDocumentToggle = !!embedUrl && !isEditing;
              return (
                <div className="data-card fade-in" style={{ padding:0, overflow:'hidden' }}>
                  <div style={{ padding:'10px 16px', background:'var(--bg-2)', borderBottom:'1px solid var(--bdr)', display:'flex', alignItems:'center', justifyContent:'space-between', gap:12, flexWrap:'wrap' }}>
                    <div style={{ display:'flex', alignItems:'center', gap:12, minWidth:0 }}>
                      <div style={{ fontSize:14.5, color:'var(--t3)', fontFamily:'var(--mono)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{selected.filename}</div>
                      {embedLabel && (
                        <span className="badge b-accent" style={{ fontSize:11, fontWeight:600 }}>{embedLabel}</span>
                      )}
                      {/* Document / Text toggle — visible whenever we have
                          an embeddable PDF (original or generated). */}
                      {showDocumentToggle && (
                        <div className="prof-tabs" style={{ margin:0 }}>
                          <button
                            className={'prof-tab' + (previewMode === 'document' ? ' active' : '')}
                            onClick={() => setPreviewMode('document')}
                            style={{ padding:'5px 11px', fontSize:13 }}>
                            <Icon name="file" size={12} style={{ marginRight:5 }}/> Document
                          </button>
                          <button
                            className={'prof-tab' + (previewMode === 'text' ? ' active' : '')}
                            onClick={() => setPreviewMode('text')}
                            style={{ padding:'5px 11px', fontSize:13 }}>
                            <Icon name="align-left" size={12} style={{ marginRight:5 }}/> Text
                          </button>
                        </div>
                      )}
                    </div>
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
                          <button className="icon-btn" title="Edit text" onClick={() => { setPreviewMode('text'); setIsEditing(true); }}><Icon name="edit-3" size={12}/></button>
                          <button className="icon-btn" title="Copy text" onClick={() => { navigator.clipboard.writeText(resumeText); alert('Copied!'); }}><Icon name="copy" size={12}/></button>
                          {downloadUrl ? (
                            <a className="icon-btn" title={downloadLabel} href={downloadUrl}
                               download={selected.filename || true} target="_blank" rel="noopener noreferrer">
                              <Icon name="download" size={12}/>
                            </a>
                          ) : (
                            <button className="icon-btn" title={downloadLabel} disabled style={{ opacity:.4, cursor:'not-allowed' }}><Icon name="download" size={12}/></button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  {(!isEditing && embedUrl && previewMode === 'document') ? (
                    <div style={{ padding:0, height:720, background:'var(--bg-1)' }}>
                      {/* Browsers render PDFs natively in an iframe. The
                          #toolbar=0&navpanes=0 hash hides the Chrome/Edge
                          built-in toolbar so the embed sits flush; Firefox
                          ignores the params, harmless. The key forces a
                          remount when the user switches resumes. */}
                      <iframe
                        key={embedUrl}
                        src={embedUrl + '#toolbar=0&navpanes=0'}
                        title="Resume preview"
                        style={{ width:'100%', height:'100%', border:'none', background:'var(--bg-1)' }}/>
                    </div>
                  ) : (
                  <div style={{ padding:isEditing ? 0 : 20, maxHeight:600, overflowY:'auto', background:'var(--bg-1)' }}>
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
                      <pre style={{ margin:0, whiteSpace:'pre-wrap', fontSize:15.5, lineHeight:1.6, color:'var(--t2)', fontFamily:'"JetBrains Mono", Menlo, monospace' }}>
                        {resumeText || 'No text content available.'}
                      </pre>
                    )}
                  </div>
                  )}
                </div>
              );
              })()}

              {tab === 'analysis' && (
                <div className="fade-in">
                  {!isAnalyzed ? (
                    <div className="rs-empty">
                      {selected.extracting ? (
                        <>
                          <span className="spin" style={{ width:28, height:28, borderWidth:3, margin:'0 auto 16px', display:'block' }}/>
                          <h3 className="rs-empty-h">Scanning resume & verifying with AI…</h3>
                          <p className="rs-empty-sub">
                            Reading every bullet for action verbs, quantification, weak phrasing,
                            and section structure — then asking your configured AI to double-check.
                            Usually 5–30 seconds.
                          </p>
                        </>
                      ) : (
                        <>
                          <div className="rs-empty-icon">
                            <Icon name="sparkles" size={22} color="var(--accent-h)"/>
                          </div>
                          <h3 className="rs-empty-h">Not yet analyzed</h3>
                          <p className="rs-empty-sub">
                            {selected.extract_error
                              ? `Extraction failed: ${selected.extract_error}`
                              : 'Run a scan to surface metrics, strengths, red flags, and a critical reading of this resume.'}
                          </p>
                          <button className="head-cta" onClick={async () => {
                            try {
                              await api.post('/api/profile/extract', { resume_id: selected.id });
                              refresh();
                            } catch (e) { alert(e.message); }
                          }}>
                            <Icon name="scan-text" size={13} color="#fff"/> Scan this resume
                          </button>
                        </>
                      )}
                    </div>
                  ) : (
                    <div className="rs-analysis">
                      {/* Sub-nav — paginated analysis */}
                      <div className="rs-subnav-row">
                        <div className="rs-subnav" role="tablist">
                          {[
                            { id:'overview', label:'Overview',   icon:'gauge'        },
                            { id:'metrics',  label:'Metrics',    icon:'bar-chart-3'  },
                            { id:'insights', label:'Insights',   icon:'lightbulb'    },
                            { id:'detail',   label:'Deep dive',  icon:'file-text'    },
                          ].map(v => (
                            <button key={v.id} role="tab"
                              aria-selected={analysisView === v.id}
                              className={'rs-subnav-pill' + (analysisView === v.id ? ' active' : '')}
                              onClick={() => setAnalysisView(v.id)}>
                              <Icon name={v.icon} size={13}/>
                              <span>{v.label}</span>
                            </button>
                          ))}
                        </div>
                        <div className="rs-subnav-step">
                          {{ overview:'1', metrics:'2', insights:'3', detail:'4' }[analysisView]} / 4
                        </div>
                      </div>

                      {!sp.insights ? (
                        <div className="rs-empty">
                          <div className="rs-empty-icon"><Icon name="info" size={22} color="var(--accent-h)"/></div>
                          <h3 className="rs-empty-h">Legacy analysis</h3>
                          <p className="rs-empty-sub">
                            This resume was scanned before the structured-insights pipeline. Re-scan to generate metrics, strengths, and red flags.
                          </p>
                          {sp.critical_analysis && (
                            <pre style={{ textAlign:'left', whiteSpace:'pre-wrap', fontFamily:'var(--sans)', fontSize:15.5, color:'var(--t2)', lineHeight:1.7, marginTop:18, maxWidth:640 }}>{sp.critical_analysis}</pre>
                          )}
                          <button className="head-cta" style={{ marginTop:18 }}
                            onClick={async () => {
                              setRescanning(true);
                              try {
                                await api.post('/api/profile/extract', { resume_id: selected.id, force: true });
                                refresh();
                              } catch (e) { alert(e.message); }
                              finally { setRescanning(false); }
                            }}>
                            <Icon name="refresh-cw" size={13} color="#fff"/> Re-scan now
                          </button>
                        </div>
                      ) : (
                        <>
                          {analysisView === 'overview' && <RsOverview insights={sp.insights}/>}
                          {analysisView === 'metrics'  && <RsMetrics  insights={sp.insights}/>}
                          {analysisView === 'insights' && <RsInsights insights={sp.insights}/>}
                          {analysisView === 'detail'   && (
                            <RsDeepDive
                              insights={sp.insights}
                              profile={sp}
                              rescanning={rescanning}
                              onRescan={async () => {
                                setRescanning(true);
                                try {
                                  await api.post('/api/profile/extract', { resume_id: selected.id, force: true });
                                  refresh();
                                } catch (e) { alert(e.message); }
                                finally { setRescanning(false); }
                              }}
                            />
                          )}
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {deleteConfirmId && (() => {
        const target = resumes.find(r => r.id === deleteConfirmId);
        return (
          <div className="ask-overlay" onClick={() => setDeleteConfirmId(null)}>
            <div style={{ position:'fixed', top:'50%', left:'50%', transform:'translate(-50%,-50%)',
                          background:'var(--bg-2)', border:'1px solid var(--bdr2)', borderRadius:12,
                          padding:'24px 28px', minWidth:300, maxWidth:420, display:'flex', flexDirection:'column', gap:16 }}
                 onClick={e => e.stopPropagation()}>
              <div style={{ fontWeight:600, fontSize:16 }}>Delete resume?</div>
              <div style={{ color:'var(--t2)', fontSize:14.5, lineHeight:1.5 }}>
                <b style={{ color:'var(--t1)' }}>{target?.filename || 'This resume'}</b> will be permanently removed.
                {target?.primary && <div style={{ marginTop:8, color:'var(--warn)', fontSize:13.5 }}>This is your primary resume. The next resume in the list will become primary.</div>}
              </div>
              <div style={{ display:'flex', gap:8, justifyContent:'flex-end' }}>
                <button className="btn-ghost" style={{ padding:'7px 14px', fontSize:14 }} onClick={() => setDeleteConfirmId(null)}>Cancel</button>
                <button className="btn-primary" style={{ padding:'7px 14px', fontSize:14, background:'var(--bad)', borderColor:'var(--bad)' }} onClick={handleDeleteExecute}>Delete</button>
              </div>
            </div>
          </div>
        );
      })()}

      {/* Paste-text modal — alternative to file upload. Uses the same
          /api/resume/upload endpoint by wrapping the text in a Blob. */}
      {pasteOpen && (
        <div
          style={{
            position:'fixed', inset:0, background:'rgba(0,0,0,0.55)',
            display:'flex', alignItems:'center', justifyContent:'center',
            zIndex:9999, padding:20,
          }}
          onClick={(e) => { if (e.target === e.currentTarget) setPasteOpen(false); }}
        >
          <div
            style={{
              background:'var(--surface)', border:'1px solid var(--bdr)',
              borderRadius:14, padding:24, width:'min(720px, 100%)',
              boxShadow:'0 24px 48px rgba(0,0,0,0.45)',
            }}
          >
            <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:14 }}>
              <Icon name="clipboard" size={18} color="var(--accent-h)"/>
              <h2 style={{ fontSize:18, fontWeight:600, margin:0 }}>Paste resume text</h2>
            </div>
            <p style={{ fontSize:13, color:'var(--t2)', margin:'0 0 16px', lineHeight:1.6 }}>
              Drop the text of your resume here. We extract skills, target roles, and
              experience the same way as an uploaded PDF — just without the PDF parser
              step.
            </p>

            <label className="set-field" style={{ marginBottom:12 }}>
              <span className="set-label">Filename (optional)</span>
              <input
                className="set-input"
                value={pasteName}
                onChange={e => setPasteName(e.target.value)}
                placeholder="pasted_resume.txt"
              />
            </label>

            <label className="set-field" style={{ marginBottom:16 }}>
              <span className="set-label">Resume content</span>
              <textarea
                className="profile-input profile-textarea"
                style={{ minHeight:300, fontFamily:'var(--mono, monospace)', fontSize:12.5 }}
                value={pasteText}
                onChange={e => setPasteText(e.target.value)}
                placeholder={'Jane Doe\njane@example.com  |  linkedin.com/in/janedoe\n\nEDUCATION\n…\n\nEXPERIENCE\n…'}
                autoFocus
              />
            </label>

            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', gap:12 }}>
              <span style={{ fontSize:12, color:'var(--t3)' }}>
                {pasteText.length.toLocaleString()} characters
              </span>
              <div style={{ display:'flex', gap:8 }}>
                <button
                  className="btn-ghost"
                  style={{ padding:'7px 14px', fontSize:14 }}
                  onClick={() => { setPasteOpen(false); setPasteText(''); setPasteName('pasted_resume.txt'); }}
                >
                  Cancel
                </button>
                <button
                  className="btn-primary"
                  style={{ padding:'7px 14px', fontSize:14 }}
                  onClick={handlePasteSubmit}
                  disabled={uploading || !pasteText.trim()}
                >
                  {uploading ? <span className="spin"/> : <Icon name="upload" size={13} color="#fff"/>}
                  {uploading ? 'Saving…' : 'Save resume'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   DOCUMENTS PAGE
   Manages every artifact the pipeline produces (tailored resumes,
   trackers, run reports, cover letters). Distinct from the Resume
   page, which manages the user's INPUT resumes. Survives a pipeline
   reset — only the destructive Settings → "Reset all data" wipes
   these.
══════════════════════════════════════════════════════════ */

const _DOC_KIND_META = {
  resume:       { label: 'Tailored resumes', icon: 'file-text',        tone: 'accent' },
  cover_letter: { label: 'Cover letters',    icon: 'mail',             tone: 'accent2' },
  tracker:      { label: 'Trackers',         icon: 'file-spreadsheet', tone: 'good' },
  report:       { label: 'Run reports',      icon: 'file-line-chart',  tone: 'warn' },
  other:        { label: 'Other',            icon: 'file',             tone: 'mute' },
};
const _DOC_KIND_ORDER = ['resume', 'cover_letter', 'tracker', 'report', 'other'];

function _docExtIcon(ext) {
  const e = (ext || '').toLowerCase();
  if (e === 'pdf')  return 'file-text';
  if (e === 'tex')  return 'file-code-2';
  if (e === 'md')   return 'file-line-chart';
  if (e === 'xlsx' || e === 'xls' || e === 'csv') return 'file-spreadsheet';
  if (e === 'docx' || e === 'doc') return 'file-text';
  return 'file';
}

function _formatRelativeTime(iso) {
  if (!iso) return '';
  const ms = Date.now() - new Date(iso).getTime();
  if (!isFinite(ms) || ms < 0) return '';
  const s = ms / 1000;
  if (s < 60)        return 'just now';
  if (s < 3600)      return `${Math.round(s / 60)}m ago`;
  if (s < 86400)     return `${Math.round(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function DocumentsPage({ state, refresh, setPage }) {
  const [docs, setDocs]           = useState(null);
  const [error, setError]         = useState(null);
  const [filter, setFilter]       = useState('all');
  const [search, setSearch]       = useState('');
  const [busyName, setBusyName]   = useState(null);
  const [editing, setEditing]     = useState(null);  // {name, content, dirty, saving, ext}
  const [renaming, setRenaming]   = useState(null);  // {oldName, newName}
  const [flash, setFlash]         = useState(null);  // {kind, text}

  const loadDocs = useCallback(async () => {
    try {
      const r = await api.get('/api/documents');
      setDocs(r.documents || []);
      setError(null);
    } catch (e) {
      setError(e?.message || 'Failed to load documents');
    }
  }, []);

  useEffect(() => { loadDocs(); }, [loadDocs]);

  // Auto-clear the flash banner so it doesn't accumulate ghost confirmations.
  useEffect(() => {
    if (!flash) return undefined;
    const id = setTimeout(() => setFlash(null), 3500);
    return () => clearTimeout(id);
  }, [flash]);

  const counts = useMemo(() => {
    const out = { all: 0 };
    _DOC_KIND_ORDER.forEach(k => { out[k] = 0; });
    (docs || []).forEach(d => {
      out.all += 1;
      out[d.kind] = (out[d.kind] || 0) + 1;
    });
    return out;
  }, [docs]);

  const filtered = useMemo(() => {
    if (!docs) return [];
    let list = docs;
    if (filter !== 'all') list = list.filter(d => d.kind === filter);
    const q = search.trim().toLowerCase();
    if (q) list = list.filter(d => d.name.toLowerCase().includes(q));
    return list;
  }, [docs, filter, search]);

  // Group filtered list back into kind buckets so the body always shows the
  // user's documents organized by purpose, even when the chip selection is
  // "All". Keeps related artifacts from a single run visually adjacent.
  const grouped = useMemo(() => {
    const buckets = new Map();
    filtered.forEach(d => {
      if (!buckets.has(d.kind)) buckets.set(d.kind, []);
      buckets.get(d.kind).push(d);
    });
    return _DOC_KIND_ORDER
      .map(kind => ({ kind, items: buckets.get(kind) || [] }))
      .filter(g => g.items.length > 0);
  }, [filtered]);

  const handleDelete = async (doc) => {
    if (busyName) return;
    if (!confirm(`Delete ${doc.name}?\n\nThis cannot be undone.`)) return;
    setBusyName(doc.name);
    try {
      await api.delete(`/api/documents/${encodeURIComponent(doc.name)}`);
      setFlash({ kind: 'ok', text: `Deleted ${doc.name}` });
      await loadDocs();
      refresh?.();
    } catch (e) {
      setFlash({ kind: 'err', text: e?.message || 'Delete failed' });
    } finally {
      setBusyName(null);
    }
  };

  const startRename = (doc) => setRenaming({ oldName: doc.name, newName: doc.name });
  const cancelRename = () => setRenaming(null);
  const commitRename = async () => {
    if (!renaming) return;
    const newName = renaming.newName.trim();
    if (!newName || newName === renaming.oldName) { setRenaming(null); return; }
    setBusyName(renaming.oldName);
    try {
      await api.post(
        `/api/documents/${encodeURIComponent(renaming.oldName)}/rename`,
        { name: newName },
      );
      setFlash({ kind: 'ok', text: `Renamed to ${newName}` });
      setRenaming(null);
      await loadDocs();
    } catch (e) {
      setFlash({ kind: 'err', text: e?.message || 'Rename failed' });
    } finally {
      setBusyName(null);
    }
  };

  const startEdit = async (doc) => {
    if (!doc.editable || busyName) return;
    setBusyName(doc.name);
    try {
      const r = await api.get(`/api/documents/${encodeURIComponent(doc.name)}/content`);
      setEditing({
        name:    doc.name,
        ext:     doc.ext,
        content: r.content || '',
        dirty:   false,
        saving:  false,
      });
    } catch (e) {
      setFlash({ kind: 'err', text: e?.message || 'Could not open editor' });
    } finally {
      setBusyName(null);
    }
  };

  const cancelEdit = () => {
    if (editing?.dirty && !confirm('Discard unsaved changes?')) return;
    setEditing(null);
  };

  const saveEdit = async () => {
    if (!editing || editing.saving) return;
    setEditing(e => ({ ...e, saving: true }));
    try {
      await api.post(
        `/api/documents/${encodeURIComponent(editing.name)}/content`,
        { content: editing.content },
      );
      setFlash({ kind: 'ok', text: `Saved ${editing.name}` });
      setEditing(null);
      await loadDocs();
    } catch (e) {
      setFlash({ kind: 'err', text: e?.message || 'Save failed' });
      setEditing(prev => prev ? { ...prev, saving: false } : prev);
    }
  };

  // Empty state — depends on whether the user has anything generated yet.
  const isEmpty = docs && docs.length === 0;

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Library</div>
          <div className="page-title-big">Documents</div>
        </div>
        <div className="head-spacer"/>
        <div className="head-search" style={{ width: 220 }}>
          <Icon name="search" size={13} color="var(--t3)"/>
          <input
            placeholder="Filter by name…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <button
          className="btn-ghost"
          onClick={loadDocs}
          title="Re-scan the session output directory">
          <Icon name="refresh-cw" size={12}/> Refresh
        </button>
      </div>

      <div className="page-body solo" style={{ paddingTop: 14 }}>
        {/* Filter chips — surfaces buckets with non-zero counts only. */}
        <div
          className="filters"
          style={{ flexWrap: 'wrap', alignItems: 'center', marginBottom: 4 }}>
          <button
            className={'page-tab' + (filter === 'all' ? ' active' : '')}
            onClick={() => setFilter('all')}>
            All <span className="tab-count">{counts.all}</span>
          </button>
          {_DOC_KIND_ORDER.filter(k => counts[k] > 0).map(k => {
            const meta = _DOC_KIND_META[k];
            return (
              <button
                key={k}
                className={'page-tab' + (filter === k ? ' active' : '')}
                onClick={() => setFilter(k)}>
                <Icon name={meta.icon} size={12}/>
                {meta.label}
                <span className="tab-count">{counts[k]}</span>
              </button>
            );
          })}
        </div>

        {/* Soft note when the run-data was reset but documents survived —
            reassures the user that resetting the pipeline didn't destroy
            their library. */}
        <div
          className="notice-strip"
          style={{
            background: 'var(--accent2-d)',
            borderColor: 'var(--accent2-b)',
            color: 'var(--t2)',
          }}>
          <Icon name="info" size={13} color="var(--accent2)"/>
          <span>
            Documents survive a pipeline reset.
            Use the Agent page's "Reset run" to clear jobs and scoring without
            losing what you've already generated. The destructive
            <em style={{ fontStyle: 'normal', fontWeight: 600, margin: '0 4px' }}>
              Reset all data
            </em>
            on Settings <em style={{ fontStyle: 'normal' }}>does</em> wipe these.
          </span>
        </div>

        {/* Inline flash for save / rename / delete confirmations + errors. */}
        {flash && (
          <div
            role="status"
            style={{
              padding: '9px 12px',
              borderRadius: 9,
              fontSize: 13.5,
              display: 'inline-flex',
              alignItems: 'center',
              gap: 7,
              background: flash.kind === 'ok' ? 'var(--good-d)' : 'var(--bad-d)',
              border: `1px solid ${flash.kind === 'ok' ? 'var(--good-b)' : 'var(--bad-b)'}`,
              color:  flash.kind === 'ok' ? 'var(--good)' : 'var(--bad)',
              alignSelf: 'flex-start',
            }}>
            <Icon name={flash.kind === 'ok' ? 'check' : 'alert-triangle'} size={12}/>
            <span>{flash.text}</span>
          </div>
        )}

        {error && (
          <div className="notice-strip"
               style={{ background: 'var(--bad-d)', borderColor: 'var(--bad-b)', color: 'var(--bad)' }}>
            <Icon name="alert-triangle" size={13}/>
            <span>{error}</span>
          </div>
        )}

        {/* Loading skeleton — the list is small (typically <50 items)
            so a single shimmer block is enough. */}
        {docs == null && !error && (
          <div
            className="data-card"
            style={{
              padding: 60, textAlign: 'center', color: 'var(--t3)',
              fontSize: 14,
            }}>
            <span className="spin" style={{ marginRight: 8 }}/>
            Loading documents…
          </div>
        )}

        {/* True empty state — distinguishes "nothing generated yet" from
            "search/filter returned nothing". */}
        {isEmpty && (
          <div
            className="data-card"
            style={{
              padding: '52px 32px', textAlign: 'center', display: 'flex',
              flexDirection: 'column', alignItems: 'center', gap: 12,
            }}>
            <div
              style={{
                width: 56, height: 56, borderRadius: 14,
                background: 'var(--accent-d)', border: '1px solid var(--accent-b)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
              }}>
              <Icon name="folder-open" size={24} color="var(--accent-h)"/>
            </div>
            <div style={{ fontSize: 19, fontWeight: 600 }}>No documents yet</div>
            <div style={{ fontSize: 14.5, color: 'var(--t2)', maxWidth: 480, lineHeight: 1.55 }}>
              Tailored resumes, cover letters, trackers, and run reports show up here
              after you run the agent. Head to the Agent page and click <b>Run all phases</b>.
            </div>
            <button className="btn-primary" onClick={() => setPage?.('agent')}>
              <Icon name="sparkles" size={13} color="#fff"/>
              Open Agent
            </button>
          </div>
        )}

        {/* No-results state when filters are too tight. */}
        {docs && docs.length > 0 && filtered.length === 0 && (
          <div
            className="data-card"
            style={{ padding: '36px 24px', textAlign: 'center', color: 'var(--t3)', fontSize: 14 }}>
            No documents match this filter.
            {' '}
            <button
              className="btn-ghost"
              style={{ marginLeft: 8 }}
              onClick={() => { setFilter('all'); setSearch(''); }}>
              Clear filters
            </button>
          </div>
        )}

        {/* Grouped table view. Each kind gets its own header + rows.
            Edit / Rename / Download / Delete on the right; the row's
            click target is the filename which opens in a new tab. */}
        {grouped.map(({ kind, items }) => {
          const meta = _DOC_KIND_META[kind];
          return (
            <section key={kind} className="data-card" style={{ marginBottom: 4 }}>
              <header
                style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '12px 18px', borderBottom: '1px solid var(--bdr)',
                  background: 'var(--bg-2)',
                }}>
                <Icon name={meta.icon} size={14} color="var(--accent-h)"/>
                <span
                  style={{
                    fontSize: 11.5, fontWeight: 600, letterSpacing: '.08em',
                    textTransform: 'uppercase', color: 'var(--t2)',
                    fontFamily: 'var(--mono)',
                  }}>
                  {meta.label}
                </span>
                <span style={{ fontSize: 13, color: 'var(--t4)' }}>
                  {items.length} {items.length === 1 ? 'file' : 'files'}
                </span>
              </header>
              <ul
                style={{
                  listStyle: 'none', padding: 0, margin: 0,
                  display: 'flex', flexDirection: 'column',
                }}>
                {items.map((d, idx) => {
                  const isBusy = busyName === d.name;
                  const isRenaming = renaming?.oldName === d.name;
                  return (
                    <li
                      key={d.name}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 12,
                        padding: '11px 18px',
                        borderTop: idx === 0 ? 'none' : '1px solid var(--bdr)',
                        opacity: isBusy ? 0.55 : 1,
                        transition: 'opacity .15s, background .15s',
                      }}
                      onMouseEnter={e => { if (!isBusy) e.currentTarget.style.background = 'var(--bg-2)'; }}
                      onMouseLeave={e => { e.currentTarget.style.background = ''; }}>
                      <span
                        style={{
                          width: 32, height: 32, borderRadius: 8,
                          background: 'var(--bg-3)', border: '1px solid var(--bdr)',
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          flexShrink: 0,
                        }}>
                        <Icon name={_docExtIcon(d.ext)} size={14} color="var(--t2)"/>
                      </span>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        {isRenaming ? (
                          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                            <input
                              autoFocus
                              className="set-input"
                              style={{ width: 360, maxWidth: '60vw' }}
                              value={renaming.newName}
                              onChange={e => setRenaming(r => ({ ...r, newName: e.target.value }))}
                              onKeyDown={e => {
                                if (e.key === 'Enter') commitRename();
                                if (e.key === 'Escape') cancelRename();
                              }}
                            />
                            <button className="btn-primary" onClick={commitRename} disabled={!renaming.newName.trim()}>
                              <Icon name="check" size={11} color="#fff"/> Rename
                            </button>
                            <button className="btn-ghost" onClick={cancelRename}>
                              Cancel
                            </button>
                          </div>
                        ) : (
                          <>
                            <a
                              href={d.url}
                              target="_blank"
                              rel="noopener noreferrer"
                              title={`Open ${d.name}`}
                              style={{
                                color: 'var(--t1)',
                                fontWeight: 500,
                                fontSize: 14.5,
                                textDecoration: 'none',
                                wordBreak: 'break-all',
                              }}
                              onMouseEnter={e => { e.currentTarget.style.color = 'var(--accent-h)'; }}
                              onMouseLeave={e => { e.currentTarget.style.color = 'var(--t1)'; }}>
                              {d.name}
                            </a>
                            <div
                              style={{
                                fontSize: 12, color: 'var(--t4)', marginTop: 2,
                                display: 'flex', gap: 10, fontFamily: 'var(--mono)',
                              }}>
                              <span>.{d.ext}</span>
                              <span>·</span>
                              <span>{d.size_kb} KB</span>
                              <span>·</span>
                              <span title={d.modified}>{_formatRelativeTime(d.modified)}</span>
                            </div>
                          </>
                        )}
                      </div>
                      {!isRenaming && (
                        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                          {d.editable && (
                            <button
                              className="icon-btn"
                              title="Edit content"
                              onClick={() => startEdit(d)}
                              disabled={isBusy}>
                              <Icon name="pencil" size={13}/>
                            </button>
                          )}
                          <button
                            className="icon-btn"
                            title="Rename"
                            onClick={() => startRename(d)}
                            disabled={isBusy}>
                            <Icon name="text-cursor" size={13}/>
                          </button>
                          <a
                            className="icon-btn"
                            title={`Download ${d.name}`}
                            href={d.url}
                            download={d.name}>
                            <Icon name="download" size={13}/>
                          </a>
                          <button
                            className="icon-btn"
                            title="Delete"
                            onClick={() => handleDelete(d)}
                            disabled={isBusy}
                            style={{ color: 'var(--bad)' }}>
                            <Icon name="trash-2" size={13}/>
                          </button>
                        </div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>

      {/* In-app editor modal — full-screen overlay so the user has plenty
          of room. Plain monospace textarea; no syntax highlighting because
          the SPA is in-browser-Babel and bringing in Monaco/CodeMirror
          would multiply the bundle weight for what's typically light edits. */}
      {editing && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={`Editing ${editing.name}`}
          onClick={e => { if (e.target === e.currentTarget) cancelEdit(); }}
          style={{
            position: 'fixed', inset: 0, zIndex: 1000,
            background: 'rgba(7, 7, 14, 0.78)',
            backdropFilter: 'blur(3px)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            padding: 24, animation: 'fade-in-up .18s cubic-bezier(.16,1,.3,1)',
          }}>
          <div
            style={{
              background: 'var(--surface)',
              border: '1px solid var(--bdr2)',
              borderRadius: 14,
              width: 'min(960px, 100%)',
              height: 'min(85vh, 760px)',
              display: 'flex', flexDirection: 'column',
              boxShadow: '0 20px 60px -10px rgba(0, 0, 0, 0.55)',
            }}>
            <header
              style={{
                display: 'flex', alignItems: 'center', gap: 12,
                padding: '14px 20px',
                borderBottom: '1px solid var(--bdr)',
              }}>
              <Icon name={_docExtIcon(editing.ext)} size={16} color="var(--accent-h)"/>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 14.5, fontWeight: 600, color: 'var(--t1)', wordBreak: 'break-all' }}>
                  {editing.name}
                </div>
                <div style={{ fontSize: 12, color: 'var(--t4)', marginTop: 2, fontFamily: 'var(--mono)' }}>
                  Editing · .{editing.ext} · {(editing.content?.length || 0).toLocaleString()} chars
                  {editing.dirty && <span style={{ color: 'var(--warn)', marginLeft: 8 }}>● unsaved</span>}
                </div>
              </div>
              <button className="btn-ghost" onClick={cancelEdit} disabled={editing.saving}>
                <Icon name="x" size={13}/> Cancel
              </button>
              <button
                className="btn-primary"
                onClick={saveEdit}
                disabled={editing.saving || !editing.dirty}>
                {editing.saving
                  ? <><span className="spin"/> Saving…</>
                  : <><Icon name="save" size={13} color="#fff"/> Save</>}
              </button>
            </header>
            <textarea
              autoFocus
              spellCheck={false}
              value={editing.content}
              onChange={e => setEditing(prev => prev ? {
                ...prev, content: e.target.value, dirty: true,
              } : prev)}
              onKeyDown={e => {
                // Cmd/Ctrl+S saves without leaving the editor.
                if ((e.metaKey || e.ctrlKey) && e.key === 's') {
                  e.preventDefault();
                  saveEdit();
                }
              }}
              style={{
                flex: 1,
                resize: 'none',
                border: 'none', outline: 'none',
                background: 'var(--bg)',
                color: 'var(--t1)',
                fontFamily: 'var(--mono)',
                fontSize: 13,
                lineHeight: 1.55,
                padding: 20,
                borderBottomLeftRadius: 14,
                borderBottomRightRadius: 14,
              }}/>
          </div>
        </div>
      )}
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   PROFILE PAGE
══════════════════════════════════════════════════════════ */

// Form-field → config-key mapping for saveProfile. Each tuple is
// (formKey, cfgKey, predicate). Predicate decides whether the form
// value is set enough to push to /api/config — undefined / NaN / wrong
// type are skipped so a partial form save can't blow away values the
// user never touched.
const SEARCH_PREF_FIELDS = [
  ['search_location',           'location',           v => typeof v === 'string'],
  ['search_experience_levels',  'experience_levels',  Array.isArray],
  ['search_education_filter',   'education_filter',   Array.isArray],
  ['search_citizenship_filter', 'citizenship_filter', v => typeof v === 'string' && v.length > 0],
  ['search_max_scrape_jobs',    'max_scrape_jobs',    Number.isFinite],
  ['search_days_old',           'days_old',           Number.isFinite],
  ['search_threshold',          'threshold',          Number.isFinite],
];

/* Inline status pill for the Profile page autosave loop. Replaces the
   old "Unsaved changes" + "Save profile" button pair. Five states map to
   four visual presentations: pending (debounce queued, amber dot pulse),
   saving (spinner), saved (green check, fades after 1.8s), error
   (clickable retry), and idle (rendered as a tiny mono "Auto-save"
   marker so the user knows the feature is active). */
function AutoSaveBadge({ status, dirty, error, onRetry }) {
  // 'idle' display when nothing has happened yet AND form is clean.
  // If dirty but status hasn't latched to 'pending' yet (very brief),
  // treat it as pending so there's no flash of "saved" while a save
  // is being scheduled.
  const effective = (status === 'idle' && dirty) ? 'pending' : status;
  if (effective === 'idle') {
    return (
      <span className="auto-save auto-save-idle" title="Profile changes save automatically as you type">
        <span className="auto-save-glyph" aria-hidden="true"/>
        <span>Auto-save on</span>
      </span>
    );
  }
  if (effective === 'pending') {
    return (
      <span className="auto-save auto-save-pending" title="Changes will save shortly">
        <span className="auto-save-dot" aria-hidden="true"/>
        <span>Auto-saving…</span>
      </span>
    );
  }
  if (effective === 'saving') {
    return (
      <span className="auto-save auto-save-saving" title="Persisting changes">
        <span className="spin" style={{ width:10, height:10, borderWidth:1.5 }}/>
        <span>Saving</span>
      </span>
    );
  }
  if (effective === 'saved') {
    return (
      <span className="auto-save auto-save-saved" title="All changes saved">
        <Icon name="check" size={11}/>
        <span>Saved</span>
      </span>
    );
  }
  // error
  return (
    <button
      type="button"
      className="auto-save auto-save-error"
      onClick={onRetry}
      title={error ? `Save failed: ${error} — click to retry` : 'Click to retry'}
    >
      <Icon name="alert-circle" size={11}/>
      <span>Save failed — retry</span>
    </button>
  );
}

function ProfilePage({ state, refresh, setPage }) {
  const p = state?.profile;
  const resumes = state?.resumes || [];
  const primaryResume = resumes.find(r => r.primary) || resumes[0];
  const isExtractingPrimary = !!(primaryResume && primaryResume.extracting);
  const hasPrimary = !!primaryResume;

  const [saving, setSaving]         = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [form, setForm]             = useState(() => profileToForm(p));
  const [dirty, setDirty]           = useState(false);
  const [activeTab, setActiveTab]   = useState('personal');
  const [extractError, setExtractError] = useState('');
  // Auto-save UX status: 'idle' | 'pending' | 'saving' | 'saved' | 'error'.
  // Drives the inline status pill in the page head; the user no longer
  // clicks a Save button, so they need a clear ambient signal that work
  // is being persisted.
  const [autoSaveStatus, setAutoSaveStatus] = useState('idle');
  const [autoSaveError,  setAutoSaveError]  = useState('');

  useEffect(() => {
    if (!dirty) {
      setForm(profileToForm(p));
    }
  }, [p, dirty]);

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

  // Always-current `form` reference for the autosave loop. `performSave`
  // captures a snapshot at its start and re-reads the ref AFTER awaiting
  // the network — that lets us tell "no edits arrived during save"
  // (clear dirty) from "user kept typing" (leave dirty for next pass).
  const formRef         = useRef(form);
  formRef.current       = form;
  const inFlightRef     = useRef(null);   // Promise of in-flight save, await-able
  const saveTimerRef    = useRef(null);   // setTimeout handle for debounce
  const savedTimerRef   = useRef(null);   // setTimeout handle for "Saved" pill fade

  const performSave = useCallback(async () => {
    const snapshot = formRef.current;
    setSaving(true);
    setAutoSaveStatus('saving');
    setAutoSaveError('');
    try {
      const titles = splitList(snapshot.target_titles).filter(Boolean);
      await api.post('/api/profile', formToProfile(snapshot));
      // Only fields the user actually edited this session get pushed to
      // /api/config — fields where the corresponding form key is still
      // unset (undefined) leave the previously-saved value intact.
      const cfg = { job_titles: titles.join(', ') };
      for (const [src, dst, ok] of SEARCH_PREF_FIELDS) {
        if (ok(snapshot[src])) cfg[dst] = snapshot[src];
      }
      await api.post('/api/config', cfg);
      // Only mark clean if the form is exactly what we just persisted.
      // If the user kept editing during the await, formRef.current has
      // moved on — leave dirty=true so the debounce schedules another
      // pass.
      if (formRef.current === snapshot) {
        setDirty(false);
        setAutoSaveStatus('saved');
        clearTimeout(savedTimerRef.current);
        savedTimerRef.current = setTimeout(() => {
          setAutoSaveStatus(s => s === 'saved' ? 'idle' : s);
        }, 1800);
      } else {
        // User kept editing during the await — the still-running debounce
        // will fire another save shortly. Surface that we're queued, not
        // done.
        setAutoSaveStatus('pending');
      }
      await refresh?.();
    } catch (e) {
      setAutoSaveStatus('error');
      setAutoSaveError(e.message || 'Save failed');
      // Don't alert — autosave failures are ambient. The status pill
      // shows a retry affordance.
    } finally {
      setSaving(false);
    }
  }, [refresh]);

  // Debounced autosave. Every edit pushes `dirty=true` and resets the
  // 700ms timer; the save fires once the user stops typing. The cleanup
  // cancels any pending fire if the component unmounts mid-debounce.
  useEffect(() => {
    if (!dirty) return;
    setAutoSaveStatus(s => s === 'saving' ? s : 'pending');
    clearTimeout(saveTimerRef.current);
    saveTimerRef.current = setTimeout(() => {
      inFlightRef.current = performSave();
    }, 700);
    return () => clearTimeout(saveTimerRef.current);
  }, [form, dirty, performSave]);

  // Flush pending edits on unmount — fire-and-forget. If the user
  // navigates to another page while typing, the most recent draft still
  // gets persisted (the POST completes after unmount). A ref-indirection
  // captures the latest `dirty` + `performSave` so the empty-deps effect
  // below doesn't close over stale values.
  const flushRef = useRef();
  flushRef.current = () => {
    if (dirty) {
      clearTimeout(saveTimerRef.current);
      performSave();
    }
  };
  useEffect(() => () => {
    flushRef.current?.();
    clearTimeout(saveTimerRef.current);
    clearTimeout(savedTimerRef.current);
  }, []);

  // saveProfile retained for explicit-save callers (syncSearch).
  // Cancels the debounce, awaits any in-flight save, then performs a
  // fresh save synchronously so the caller can rely on persistence.
  const saveProfile = async () => {
    clearTimeout(saveTimerRef.current);
    if (inFlightRef.current) {
      try { await inFlightRef.current; } catch (_) {/* surfaced via status pill */}
    }
    if (formRef.current && dirty) {
      inFlightRef.current = performSave();
      try { await inFlightRef.current; } catch (_) {/* surfaced */}
    }
  };

  const retryAutoSave = () => {
    inFlightRef.current = performSave();
  };

  const rerunExtraction = async () => {
    if (!hasPrimary) {
      alert('Upload a resume first.');
      return;
    }
    setExtracting(true);
    setExtractError('');
    try {
      await api.post('/api/profile/extract', {
        resume_id: primaryResume.id,
        preferred_titles: splitList(form.target_titles).filter(Boolean),
        force: true,
      });
      // Reset dirty so the useEffect refreshes the form once new data arrives.
      setDirty(false);
      await refresh?.();
    } catch (e) {
      setExtractError(e.message || 'Extraction failed');
    } finally {
      setExtracting(false);
    }
  };

  const syncSearch = async () => {
    await saveProfile();
    setPage?.('jobs');
  };

  if (!p) {
    if (isExtractingPrimary || extracting) {
      return (
        <div className="placeholder-page">
          <span className="spin" style={{ width:28, height:28, borderWidth:3, marginBottom:16 }}/>
          <div style={{ fontSize:20, fontWeight:600 }}>Analyzing resume…</div>
          <div style={{ fontSize:15.5, color:'var(--t2)', maxWidth:340, textAlign:'center', lineHeight:1.55, marginTop:6 }}>
            Extracting skills, experience, and target roles from <strong>{primaryResume?.filename}</strong>. This usually takes 5–30 seconds.
          </div>
        </div>
      );
    }
    return (
      <div className="placeholder-page">
        <div className="placeholder-icon"><Icon name="user" size={22}/></div>
        <div style={{ fontSize:20, fontWeight:600 }}>No profile found</div>
        <div style={{ fontSize:15.5, color:'var(--t2)', maxWidth:360, textAlign:'center', lineHeight:1.55, marginTop:6 }}>
          {hasPrimary
            ? 'Re-run the extractor to populate your profile from this resume.'
            : 'Upload a resume on the Resume page, or create the profile manually.'}
        </div>
        {extractError && (
          <div style={{ fontSize:14.5, color:'var(--bad)', marginTop:10 }}>Last error: {extractError}</div>
        )}
        <div style={{ display:'flex', gap:10, marginTop:16 }}>
          {hasPrimary ? (
            <button className="btn-primary" onClick={rerunExtraction} disabled={extracting}>
              <Icon name="scan-text" size={14}/> {extracting ? 'Extracting…' : 'Extract from resume'}
            </button>
          ) : (
            <button className="btn-primary" onClick={() => setPage?.('resume')}>
              <Icon name="upload-cloud" size={14}/> Upload a resume
            </button>
          )}
          <button className="btn-ghost" onClick={async () => {
            try {
              await api.post('/api/profile', { name:'', target_titles:[], top_hard_skills:[], top_soft_skills:[], education:[], experience:[], research:[], projects:[] });
              await refresh?.();
            } catch (e) {
              alert(e.message || 'Failed to create profile');
            }
          }}>
            <Icon name="pencil" size={14}/> Create manually
          </button>
        </div>
      </div>
    );
  }

  const showExtractingBanner = extracting || isExtractingPrimary;

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Profile</div>
        {showExtractingBanner && (
          <span style={{ marginLeft:14, fontSize:14.5, color:'var(--accent-h)', display:'inline-flex', alignItems:'center', gap:6 }}>
            <span className="spin" style={{ width:11, height:11, borderWidth:1.5 }}/> Re-scraping resume…
          </span>
        )}
        {!showExtractingBanner && (
          <AutoSaveBadge status={autoSaveStatus} dirty={dirty} error={autoSaveError} onRetry={retryAutoSave}/>
        )}
        <div className="head-spacer"/>
        <button className="btn-ghost" onClick={rerunExtraction} disabled={showExtractingBanner || !hasPrimary}>
          <Icon name="scan-text" size={13}/> {showExtractingBanner ? 'Extracting…' : 'Re-scrape resume'}
        </button>
        <button className="lp-btn-p" onClick={syncSearch} disabled={saving} style={{ marginLeft:8, padding:'6px 14px', fontSize:15.5 }}>
          <Icon name="search" size={13}/> Explore jobs
        </button>
      </div>
      {extractError && (
        <div className="notice-strip" style={{ background:'rgba(239,68,68,0.05)', borderColor:'rgba(239,68,68,0.2)', color:'var(--bad)', margin:'0 24px' }}>
          <Icon name="alert-circle" size={13}/> Extraction failed: {extractError}
        </div>
      )}

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
            <>
              <div className="data-card" style={{ padding:24 }}>
                <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:16 }}>
                  <Icon name="user" size={14}/> Personal Information
                </h3>
                <div className="profile-grid">
                  <ProfileInput label="Name" value={form.name} onChange={v => updateField('name', v)}/>
                  <ProfileInput label="Email" value={form.email} onChange={v => updateField('email', v)}/>
                  <ProfileInput label="Phone" value={form.phone} onChange={v => updateField('phone', v)}/>
                  <ProfileInput label="Home location" value={form.location} onChange={v => updateField('location', v)}/>
                  <ProfileSelect
                    label="Work authorization (US)"
                    value={form.work_authorization}
                    onChange={v => updateField('work_authorization', v)}
                    options={US_WORK_AUTH_OPTIONS}
                  />
                  <ProfileSalary
                    amount={form.target_salary_amount}
                    currency={form.target_salary_currency}
                    period={form.target_salary_period}
                    onAmount={v => updateField('target_salary_amount', v)}
                    onCurrency={v => updateField('target_salary_currency', v)}
                    onPeriod={v => updateField('target_salary_period', v)}
                  />
                </div>
                <ProfileInput label="Professional summary" textarea value={form.summary} onChange={v => updateField('summary', v)}/>
              </div>
              <div style={{ marginTop:18 }}>
                <SearchPrefsCard form={form} state={state} updateField={updateField}/>
              </div>
              <ProfileLinksCard form={form} updateField={updateField}/>
            </>
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
                 <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:16 }}><Icon name="target" size={14}/> Target Roles</h3>
                 <ProfileInput label="Comma-separated titles" value={form.target_titles} onChange={v => updateField('target_titles', v)}/>
                 <ProfileInput label="Critical analysis" textarea value={form.critical_analysis} onChange={v => updateField('critical_analysis', v)}/>
                 <ProfileInput label="ATS gaps, comma-separated" value={form.resume_gaps} onChange={v => updateField('resume_gaps', v)}/>
               </div>
               <div className="data-card" style={{ padding:24 }}>
                 <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:16 }}><Icon name="list-checks" size={14}/> Skills</h3>
                 <ProfileInput label="Top Hard Skills" textarea value={form.top_hard_skills} onChange={v => updateField('top_hard_skills', v)}/>
                 <ProfileInput label="Soft Skills" value={form.top_soft_skills} onChange={v => updateField('top_soft_skills', v)}/>
               </div>
               <div className="data-card" style={{ padding:24 }}>
                 <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:6, display:'flex', alignItems:'center', gap:10, flexWrap:'wrap' }}>
                   <Icon name="search" size={14}/> Discovery & Search
                   <span style={{
                     fontSize:10.5, fontWeight:600, letterSpacing:'.04em',
                     textTransform:'uppercase', padding:'3px 8px', borderRadius:6,
                     color:'var(--accent-h)', background:'var(--accent-d)',
                     border:'1px solid var(--accent-b)',
                   }}>Agent mode only</span>
                 </h3>
                 <div className="set-helper" style={{ marginTop:0, marginBottom:14 }}>
                   Runtime knobs for the legacy 7-phase agent run (the <strong>Agent</strong> tab).
                   For target roles, location, seniority, education, or citizenship filters,
                   use <strong>Job Search Preferences</strong> on the Personal tab.
                 </div>
                 <ProfileInput label="Max jobs to scrape" type="number"
                   value={form.search_max_scrape_jobs ?? state?.max_scrape_jobs ?? 50}
                   onChange={v => updateField('search_max_scrape_jobs', parseInt(v))}/>
                 <ProfileInput label="Posting age (days)" type="number"
                   value={form.search_days_old ?? state?.days_old ?? 30}
                   onChange={v => updateField('search_days_old', parseInt(v))}/>
                 <ProfileInput label="Match threshold" type="number"
                   value={form.search_threshold ?? state?.threshold ?? 75}
                   onChange={v => updateField('search_threshold', parseInt(v))}/>
                 <div className="set-helper" style={{ marginTop:6 }}>
                   Edits save automatically — these apply to your next <em>agent</em> run.
                 </div>
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
function profileToForm(p) {
  p = p || {};
  const salary = parseTargetSalary(p);
  return {
    name: p.name || '', email: p.email || '', phone: p.phone || '',
    location: p.location || '', linkedin: p.linkedin || '', github: p.github || '',
    website: p.website || '',
    summary: p.summary || '', work_authorization: p.work_authorization || '',
    target_salary: p.target_salary || '',
    target_salary_amount: salary.amount,
    target_salary_currency: salary.currency,
    target_salary_period: salary.period,
    critical_analysis: p.critical_analysis || '',
    target_titles: (p.target_titles || []).join(', '),
    top_hard_skills: (p.top_hard_skills || []).join(', '),
    top_soft_skills: (p.top_soft_skills || []).join(', '),
    resume_gaps: (p.resume_gaps || []).join(', '),
    education: p.education || [],
    experience: p.experience || [],
    research: p.research || p.research_experience || [],
    projects: p.projects || [],
    // Industry-specific profile URLs round-trip as a single nested dict so
    // the form can carry ~25 fields without polluting the top level. We
    // shallow-clone so React state stays isolated from the source profile.
    links: { ...(p.links || {}) },
  };
}

function formToProfile(form) {
  const roleList = rows => (rows || []).map(r => ({
    ...r,
    bullets: (Array.isArray(r.bullets) ? r.bullets : splitBullets(r.bullets)).filter(Boolean),
  }));
  // Strip empty/whitespace-only link entries before saving so we don't
  // re-persist placeholder strings the user never typed in.
  const cleanLinks = {};
  for (const [k, v] of Object.entries(form.links || {})) {
    if (typeof v === 'string' && v.trim()) cleanLinks[k] = v.trim();
  }
  return {
    name: form.name, email: form.email, phone: form.phone,
    location: form.location, linkedin: form.linkedin, github: form.github,
    website: form.website,
    summary: form.summary, work_authorization: form.work_authorization,
    target_salary: composeTargetSalary(form),
    target_salary_amount: String(form.target_salary_amount || '').trim(),
    target_salary_currency: form.target_salary_currency || '',
    target_salary_period: form.target_salary_period || '',
    critical_analysis: form.critical_analysis,
    target_titles: splitList(form.target_titles).filter(Boolean),
    top_hard_skills: splitList(form.top_hard_skills).filter(Boolean),
    top_soft_skills: splitList(form.top_soft_skills).filter(Boolean),
    resume_gaps: splitList(form.resume_gaps).filter(Boolean),
    experience: roleList(form.experience),
    research: roleList(form.research),
    education: form.education || [],
    projects: (form.projects || []).map(p => ({
      ...p,
      skills_used: (Array.isArray(p.skills_used) ? p.skills_used : splitList(p.skills_used)).filter(Boolean),
    })),
    links: cleanLinks,
  };
}
function splitBullets(value) {
  return String(value || '').split('\n').map(s => s.trim());
}
const _EXP_LEVEL_OPTIONS = [
  { v: 'internship',  label: 'Internship' },
  { v: 'entry-level', label: 'Entry-level' },
  { v: 'mid-level',   label: 'Mid-level' },
  { v: 'senior',      label: 'Senior' },
];
const _EDU_LEVEL_OPTIONS = [
  { v: 'high_school', label: 'High School' },
  { v: 'associates',  label: 'Associate' },
  { v: 'bachelors',   label: "Bachelor's" },
  { v: 'masters',     label: "Master's" },
  { v: 'phd',         label: 'PhD' },
];
const _CITIZENSHIP_OPTIONS = [
  { v: 'all',               label: 'All — no citizenship filter' },
  { v: 'exclude_required',  label: 'Exclude roles that require US citizenship / clearance' },
  { v: 'only_required',     label: 'Only roles that require US citizenship / clearance' },
];

function SearchPrefsCard({ form, state, updateField }) {
  const titles = form.target_titles ?? '';
  const searchLoc = form.search_location ?? state?.location ?? '';
  const exp = form.search_experience_levels ?? state?.experience_levels ?? [];
  const edu = form.search_education_filter ?? state?.education_filter ?? [];
  const cit = form.search_citizenship_filter ?? state?.citizenship_filter ?? 'all';
  const isEmpty = !titles.trim() && !searchLoc.trim() && exp.length === 0 && edu.length === 0;
  return (
    <div className="data-card search-prefs" style={{ padding:24 }}>
      <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:6, display:'flex', alignItems:'center', gap:10 }}>
        <Icon name="target" size={14}/> Job Search Preferences
        <span className="search-prefs-pill">drives matching</span>
      </h3>
      <div className="set-helper" style={{ marginTop:0, marginBottom:16 }}>
        These five fields control which roles the agent surfaces and scores. They're populated
        automatically from your resume on first extraction — change them anytime to broaden or narrow
        the search. Empty = no constraint.
      </div>
      {isEmpty && (
        <div className="notice-strip" style={{ marginBottom:14, background:'var(--accent-d)', borderColor:'var(--accent-b)', color:'var(--accent-h)' }}>
          <Icon name="info" size={13}/> No preferences set yet. Upload a resume on the Resume page to auto-fill these, or type them in below.
        </div>
      )}
      {/* Target roles — full-width textarea since these lists tend to run
          long once the resume scan + user edits land (5-15 titles is normal).
          A single-line input clipped most of them off-screen and forced
          horizontal scroll. */}
      <ProfileInput
        label="Target roles (comma-separated)"
        value={titles}
        onChange={v => updateField('target_titles', v)}
        textarea
        placeholder="e.g. Hardware Engineer, FPGA Engineer, Embedded Software Engineer, Robotics Engineer, IC Design Intern…"
      />
      <ProfileInput
        label="Job-search location"
        value={searchLoc}
        onChange={v => updateField('search_location', v)}
      />
      <ChipToggle
        label="Experience level"
        value={exp}
        onChange={v => updateField('search_experience_levels', v)}
        options={_EXP_LEVEL_OPTIONS}
        helper="Pick one or more. Leave all unselected to ignore experience level entirely."
      />
      <ChipToggle
        label="Highest education you hold"
        value={edu}
        onChange={v => updateField('search_education_filter', v)}
        options={_EDU_LEVEL_OPTIONS}
        helper="Used to drop roles whose required degree exceeds yours. Leave empty to skip the filter."
      />
      <ProfileSelect
        label="Citizenship / clearance filter"
        value={cit}
        onChange={v => updateField('search_citizenship_filter', v)}
        options={_CITIZENSHIP_OPTIONS}
      />
      <div className="set-helper" style={{ marginTop:8 }}>
        Changes save automatically — applied on your next discovery run.
      </div>
    </div>
  );
}

function ProfileInput({ label, value, onChange, textarea=false, type='text', placeholder }) {
  const Tag = textarea ? 'textarea' : 'input';
  const safeValue = (value === undefined || value === null || (typeof value === 'number' && Number.isNaN(value))) ? '' : value;
  return (
    <label className="set-field">
      <span className="set-label">{label}</span>
      <Tag
        type={textarea ? undefined : type}
        className={'profile-input' + (textarea ? ' profile-textarea' : '')}
        value={safeValue}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
      />
    </label>
  );
}

// Industry-grouped catalog of profile sites. Item.key mirrors
// PROFILE_LINK_KEYS in pipeline/profile_extractor.py — keep both in sync.
// `scalar: true` items are stored as flat keys on the profile dict
// (linkedin/github/website) for back-compat; everything else lives
// under profile.links[key].
// `slug` matches the Simple Icons CDN slug (https://simpleicons.org/) so we
// can render each row's real brand glyph via `cdn.simpleicons.org/<slug>/white`.
// `mono` stays as the deterministic fallback when the CDN errors or the slug
// isn't covered by Simple Icons (e.g. niche specialty directories).
const PROFILE_LINK_GROUPS = [
  {
    id: 'universal', name: 'Universal',
    description: 'Filled by every industry — start here.',
    icon: 'globe',
    defaultOpen: true,
    items: [
      { key: 'linkedin', label: 'LinkedIn',          slug: 'linkedin', mono: 'in', color: '#0A66C2', hint: 'linkedin.com/in/your-handle',     scalar: true },
      { key: 'website',  label: 'Personal website',                    mono: 'WW', color: 'var(--accent)', hint: 'https://your-name.dev',     scalar: true },
      { key: 'twitter',  label: 'Twitter / X',       slug: 'x',        mono: '𝕏',  color: '#0F1419', hint: 'x.com/your-handle' },
    ],
  },
  {
    id: 'tech', name: 'Software & Engineering',
    description: 'Code repos, Q&A reputation, and interview-prep proof.',
    icon: 'terminal',
    items: [
      { key: 'github',        label: 'GitHub',         slug: 'github',        mono: 'GH', color: '#1F2328', hint: 'github.com/your-handle',                scalar: true },
      { key: 'gitlab',        label: 'GitLab',         slug: 'gitlab',        mono: 'GL', color: '#FC6D26', hint: 'gitlab.com/your-handle' },
      { key: 'stackoverflow', label: 'Stack Overflow', slug: 'stackoverflow', mono: 'SO', color: '#F58025', hint: 'stackoverflow.com/users/123/your-handle' },
      { key: 'leetcode',      label: 'LeetCode',       slug: 'leetcode',      mono: 'LC', color: '#FFA116', hint: 'leetcode.com/u/your-handle' },
    ],
  },
  {
    id: 'data_ai', name: 'Data, ML & AI',
    description: 'For data scientists, ML/AI engineers, and applied researchers.',
    icon: 'cpu',
    items: [
      { key: 'kaggle',         label: 'Kaggle',           slug: 'kaggle',         mono: 'KG', color: '#20BEFF', hint: 'kaggle.com/your-handle' },
      { key: 'huggingface',    label: 'Hugging Face',     slug: 'huggingface',    mono: 'HF', color: '#FFD21E', hint: 'huggingface.co/your-handle' },
      { key: 'paperswithcode', label: 'Papers With Code', slug: 'paperswithcode', mono: 'PC', color: '#21CBCE', hint: 'paperswithcode.com/author/your-handle' },
    ],
  },
  {
    id: 'design', name: 'Design & Creative',
    description: 'UI/UX, illustration, 3D — the portfolio sites recruiters open first.',
    icon: 'palette',
    items: [
      { key: 'dribbble',   label: 'Dribbble',   slug: 'dribbble',   mono: 'DR', color: '#EA4C89', hint: 'dribbble.com/your-handle' },
      { key: 'behance',    label: 'Behance',    slug: 'behance',    mono: 'BE', color: '#1769FF', hint: 'behance.net/your-handle' },
      { key: 'artstation', label: 'ArtStation', slug: 'artstation', mono: 'AS', color: '#13AFF0', hint: 'artstation.com/your-handle' },
      { key: 'sketchfab',  label: 'Sketchfab',  slug: 'sketchfab',  mono: 'SF', color: '#1CAAD9', hint: 'sketchfab.com/your-handle' },
    ],
  },
  {
    id: 'writing', name: 'Writing & Content',
    description: 'Long-form writing, newsletters, video essays.',
    icon: 'feather',
    items: [
      { key: 'medium',   label: 'Medium',     slug: 'medium',   mono: 'MD', color: '#1A8917', hint: 'medium.com/@your-handle' },
      { key: 'substack', label: 'Substack',   slug: 'substack', mono: 'SS', color: '#FF6719', hint: 'your-handle.substack.com' },
      { key: 'youtube',  label: 'YouTube',    slug: 'youtube',  mono: 'YT', color: '#FF0000', hint: 'youtube.com/@your-handle' },
    ],
  },
  {
    id: 'academic', name: 'Academic & Research',
    description: 'Citations, identifiers, and peer-network sites for researchers.',
    icon: 'graduation-cap',
    items: [
      { key: 'google_scholar', label: 'Google Scholar', slug: 'googlescholar', mono: 'GS', color: '#4285F4', hint: 'scholar.google.com/citations?user=…' },
      { key: 'orcid',          label: 'ORCID',          slug: 'orcid',         mono: 'OR', color: '#A6CE39', hint: 'orcid.org/0000-0000-0000-0000' },
      { key: 'researchgate',   label: 'ResearchGate',   slug: 'researchgate',  mono: 'RG', color: '#00CCBB', hint: 'researchgate.net/profile/Your-Name' },
      { key: 'academia',       label: 'Academia.edu',   slug: 'academia',      mono: 'AE', color: '#41637E', hint: 'your-university.academia.edu/YourName' },
    ],
  },
  {
    id: 'visual', name: 'Visual & Media',
    description: 'Photography, film, and short-form video.',
    icon: 'camera',
    items: [
      { key: 'instagram', label: 'Instagram',  slug: 'instagram', mono: 'IG', color: '#E4405F', hint: 'instagram.com/your-handle' },
      { key: '500px',     label: '500px',      slug: '500px',     mono: '5P', color: '#0099E5', hint: '500px.com/p/your-handle' },
      { key: 'flickr',    label: 'Flickr',     slug: 'flickr',    mono: 'FL', color: '#FF0084', hint: 'flickr.com/photos/your-handle' },
      { key: 'vimeo',     label: 'Vimeo',      slug: 'vimeo',     mono: 'VM', color: '#1AB7EA', hint: 'vimeo.com/your-handle' },
    ],
  },
  {
    id: 'audio', name: 'Audio & Music',
    description: 'Catalog, tracks, and producer credits.',
    icon: 'music',
    items: [
      { key: 'soundcloud', label: 'SoundCloud', slug: 'soundcloud', mono: 'SC', color: '#FF5500', hint: 'soundcloud.com/your-handle' },
      { key: 'bandcamp',   label: 'Bandcamp',   slug: 'bandcamp',   mono: 'BC', color: '#629AA9', hint: 'your-handle.bandcamp.com' },
    ],
  },
  {
    id: 'business', name: 'Business & Startups',
    description: 'Founders, investors, BD — where deal flow happens.',
    icon: 'briefcase',
    items: [
      { key: 'wellfound',   label: 'Wellfound (AngelList)', slug: 'wellfound',   mono: 'WF', color: '#000000', hint: 'wellfound.com/u/your-handle' },
      { key: 'crunchbase',  label: 'Crunchbase',            slug: 'crunchbase',  mono: 'CB', color: '#146AFF', hint: 'crunchbase.com/person/your-handle' },
      { key: 'producthunt', label: 'Product Hunt',          slug: 'producthunt', mono: 'PH', color: '#DA552F', hint: 'producthunt.com/@your-handle' },
    ],
  },
  {
    id: 'specialty', name: 'Industry Specialties',
    description: 'Field-specific directories — fill what applies.',
    icon: 'badge-check',
    items: [
      // No `slug` for Doximity / Muck Rack / Martindale — Simple Icons
      // doesn't carry them, so the rendered chip falls back to the
      // letter monogram, which is still visually distinct because the
      // brand color stays.
      { key: 'doximity',   label: 'Doximity (healthcare)',                    mono: 'DX', color: '#00A0DF', hint: 'doximity.com/pub/your-handle' },
      { key: 'muckrack',   label: 'Muck Rack (journalism)',                   mono: 'MR', color: '#2E3A89', hint: 'muckrack.com/your-handle' },
      { key: 'imdb',       label: 'IMDb (film/TV)',          slug: 'imdb',    mono: 'IM', color: '#F5C518', hint: 'imdb.com/name/nm0000000' },
      { key: 'martindale', label: 'Martindale (legal)',                       mono: 'ML', color: '#BF1E2E', hint: 'martindale.com/attorney/your-name' },
    ],
  },
];

const PROFILE_LINK_ITEMS = PROFILE_LINK_GROUPS.flatMap(g => g.items);

function ProfileLinksCard({ form, updateField }) {
  // Open-state set is keyed by group.id (stable) rather than group.name
  // (which is user-facing copy and could be renamed without breaking
  // anyone's open/closed memory).
  const [openGroups, setOpenGroups] = useState(() => {
    const open = new Set();
    PROFILE_LINK_GROUPS.forEach(g => { if (g.defaultOpen) open.add(g.id); });
    return open;
  });
  const toggleGroup = (id) => {
    setOpenGroups(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  // For "scalar" entries (linkedin/github/website live as flat keys on
  // the profile rather than under .links), read & write the top-level form
  // field instead of going through .links. Keeps backward compatibility
  // with profiles persisted before the .links dict existed.
  const readValue = (item) => item.scalar
    ? (form[item.key] ?? '')
    : (form.links?.[item.key] ?? '');
  const writeValue = (item, v) => {
    if (item.scalar) {
      updateField(item.key, v);
    } else {
      updateField('links', { ...(form.links || {}), [item.key]: v });
    }
  };

  // Quick "you've filled X of Y" stat shown in the header.
  const filled = PROFILE_LINK_ITEMS.filter(it => String(readValue(it) || '').trim()).length;
  const total = PROFILE_LINK_ITEMS.length;

  return (
    <div className="data-card profile-links-card" style={{ padding:24, marginTop:18 }}>
      <h3 className="prof-h" style={{ fontSize:16.5, marginBottom:6, display:'flex', alignItems:'center', gap:10, flexWrap:'wrap' }}>
        <Icon name="link-2" size={14}/> Profiles & Online Presence
        <span className="profile-links-count">{filled} / {total} filled</span>
      </h3>
      <div className="set-helper" style={{ marginTop:0, marginBottom:16 }}>
        Industry-curated list — every site below is one a hiring manager in
        that field actually checks. Fill what's relevant; leave the rest blank.
        Detected URLs from your resume show up here automatically.
      </div>

      {PROFILE_LINK_GROUPS.map(group => {
        const isOpen = openGroups.has(group.id);
        const groupFilled = group.items.filter(it => String(readValue(it) || '').trim()).length;
        return (
          <div key={group.id} className={'profile-link-group' + (isOpen ? ' open' : '')}>
            <button
              type="button"
              className="profile-link-group-head"
              onClick={() => toggleGroup(group.id)}
              aria-expanded={isOpen}
            >
              <span className="profile-link-group-icon"><Icon name={group.icon} size={14}/></span>
              <span className="profile-link-group-name">{group.name}</span>
              <span className="profile-link-group-count">
                {groupFilled > 0 ? `${groupFilled} / ${group.items.length}` : `${group.items.length}`}
              </span>
              <span className="profile-link-group-chevron"><Icon name="chevron-down" size={14}/></span>
            </button>
            {isOpen && (
              <div className="profile-link-group-body">
                <div className="profile-link-group-desc">{group.description}</div>
                <div className="profile-link-rows">
                  {group.items.map(item => (
                    <ProfileLinkRow
                      key={item.key}
                      item={item}
                      value={readValue(item)}
                      onChange={(v) => writeValue(item, v)}
                    />
                  ))}
                </div>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ProfileLinkRow({ item, value, onChange }) {
  // Try the real brand glyph from Simple Icons (free, CDN-hosted, ~3000
  // brands). On 404 / network failure we fall back to the letter monogram
  // so a missing icon never leaves an empty chip. Personal-website rows
  // have no slug — they always render the monogram.
  const [logoFailed, setLogoFailed] = useState(false);
  const showLogo = !!item.slug && !logoFailed;
  return (
    <label className="profile-link-row">
      <span
        className={'profile-link-mono' + (showLogo ? ' has-logo' : '')}
        style={{ background: item.color, color: _readableMonoText(item.color) }}
        aria-hidden="true"
      >
        {showLogo ? (
          <img
            src={`https://cdn.simpleicons.org/${item.slug}/white`}
            alt=""
            loading="lazy"
            draggable="false"
            onError={() => setLogoFailed(true)}
          />
        ) : item.mono}
      </span>
      <span className="profile-link-row-body">
        <span className="profile-link-row-label">{item.label}</span>
        <input
          className="profile-input profile-link-input"
          type="url"
          value={value}
          placeholder={item.hint}
          onChange={e => onChange(e.target.value)}
        />
      </span>
    </label>
  );
}

/* Pick a contrasting text color for the monogram chip — yellows / pale
   greens get black text, everything else white. Kept inline so the
   color list stays portable. */
function _readableMonoText(bg) {
  if (!bg) return '#fff';
  if (bg.startsWith('var(')) return '#fff';
  // Convert #RRGGBB to luminance.
  const m = /^#?([a-f0-9]{6})$/i.exec(bg);
  if (!m) return '#fff';
  const n = parseInt(m[1], 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.62 ? '#0a0b1d' : '#ffffff';
}

/* US work-authorization choices used by the Profile page dropdown.
   Mirrors the categories most US job-application forms ask for. The
   value sent to the backend is the human-readable string itself, so
   downstream tools (resume tailor, cover letter, ATS) can use it
   verbatim without a translation table. */
const US_WORK_AUTH_OPTIONS = [
  { v: '',                                          label: '— Select work authorization —' },
  { v: 'US Citizen',                                label: 'US Citizen' },
  { v: 'Permanent Resident (Green Card)',           label: 'Permanent Resident (Green Card)' },
  { v: 'Authorized to work — no sponsorship needed',label: 'Authorized to work — no sponsorship needed' },
  { v: 'F-1 OPT',                                   label: 'F-1 OPT (Optional Practical Training)' },
  { v: 'F-1 STEM OPT Extension',                    label: 'F-1 STEM OPT Extension' },
  { v: 'F-1 CPT',                                   label: 'F-1 CPT (Curricular Practical Training)' },
  { v: 'H-1B Visa',                                 label: 'H-1B Visa' },
  { v: 'H-4 with EAD',                              label: 'H-4 with EAD' },
  { v: 'L-1 / L-2 with EAD',                        label: 'L-1 / L-2 with EAD' },
  { v: 'TN Visa (USMCA / NAFTA)',                   label: 'TN Visa (USMCA / NAFTA)' },
  { v: 'E-3 Visa',                                  label: 'E-3 Visa (Australian)' },
  { v: 'O-1 Visa',                                  label: 'O-1 Visa' },
  { v: 'DACA',                                      label: 'DACA' },
  { v: 'Asylum / Refugee',                          label: 'Asylum / Refugee' },
  { v: 'Will require sponsorship now',              label: 'Will require sponsorship now' },
  { v: 'Will require sponsorship in the future',    label: 'Will require sponsorship in the future' },
  { v: 'Prefer not to disclose',                    label: 'Prefer not to disclose' },
];

function ProfileSelect({ label, value, onChange, options }) {
  const safe = (value === undefined || value === null) ? '' : String(value);
  // Preserve any legacy free-text value that predates the dropdown so the
  // user doesn't lose data. Render it as an extra option so it stays
  // selected; user can pick a canonical option to overwrite it.
  const known = new Set(options.map(o => o.v));
  const isLegacy = safe !== '' && !known.has(safe);
  return (
    <label className="set-field">
      <span className="set-label">{label}</span>
      <select
        className="profile-input profile-select"
        value={safe}
        onChange={e => onChange(e.target.value)}
      >
        {isLegacy && <option value={safe}>{safe} (existing — pick a standard option to replace)</option>}
        {options.map(o => (
          <option key={o.v || '__empty'} value={o.v}>{o.label}</option>
        ))}
      </select>
    </label>
  );
}

const TARGET_SALARY_CURRENCIES = [
  'USD', 'EUR', 'GBP', 'CAD', 'AUD', 'JPY', 'INR', 'CNY', 'CHF', 'MXN', 'SGD', 'HKD',
];
const TARGET_SALARY_PERIODS = [
  { v: 'year',  label: '/ year'  },
  { v: 'month', label: '/ month' },
  { v: 'week',  label: '/ week'  },
  { v: 'day',   label: '/ day'   },
  { v: 'hour',  label: '/ hour'  },
];

function ProfileSalary({ amount, currency, period, onAmount, onCurrency, onPeriod }) {
  const safeAmount = (amount === undefined || amount === null) ? '' : String(amount);
  const safeCurrency = currency || 'USD';
  const safePeriod = period || 'year';
  return (
    <label className="set-field">
      <span className="set-label">Target salary</span>
      <div style={{ display:'flex', gap:8, alignItems:'stretch' }}>
        <input
          type="number"
          min="0"
          inputMode="numeric"
          placeholder="80000"
          className="profile-input"
          value={safeAmount}
          onChange={e => onAmount(e.target.value)}
          style={{ flex:'1 1 auto', minWidth:0 }}
        />
        <select
          className="profile-input profile-select"
          value={safeCurrency}
          onChange={e => onCurrency(e.target.value)}
          style={{ flex:'0 0 auto', width:88 }}
        >
          {TARGET_SALARY_CURRENCIES.map(c => (
            <option key={c} value={c}>{c}</option>
          ))}
        </select>
        <select
          className="profile-input profile-select"
          value={safePeriod}
          onChange={e => onPeriod(e.target.value)}
          style={{ flex:'0 0 auto', width:108 }}
        >
          {TARGET_SALARY_PERIODS.map(p => (
            <option key={p.v} value={p.v}>{p.label}</option>
          ))}
        </select>
      </div>
    </label>
  );
}

const _PERIOD_TOKEN_TO_VALUE = {
  hour: 'hour', hr: 'hour', hourly: 'hour',
  day: 'day', daily: 'day',
  week: 'week', wk: 'week', weekly: 'week',
  month: 'month', mo: 'month', monthly: 'month',
  year: 'year', yr: 'year', yearly: 'year', annum: 'year', annual: 'year',
};

// Round-trip helpers for the 3-part salary input. Existing profiles store a
// single combined string (e.g. "80000 USD / year") that the LLM extractor
// produced — parse that on load so users don't lose what's there. New saves
// always include the structured fields plus a regenerated combined string so
// downstream readers (resume tailor, ATS) keep working.
function parseTargetSalary(p) {
  const out = { amount: '', currency: 'USD', period: 'year' };
  if (p && (p.target_salary_amount || p.target_salary_currency || p.target_salary_period)) {
    out.amount = p.target_salary_amount ? String(p.target_salary_amount) : '';
    if (p.target_salary_currency) out.currency = String(p.target_salary_currency);
    if (p.target_salary_period) out.period = String(p.target_salary_period);
    return out;
  }
  const raw = String(p?.target_salary || '').trim();
  if (!raw) return out;
  const amountMatch = raw.match(/(\d[\d,]*(?:\.\d+)?)/);
  if (amountMatch) out.amount = amountMatch[1].replace(/,/g, '');
  const currencyMatch = raw.match(/\b([A-Z]{3})\b/);
  if (currencyMatch && TARGET_SALARY_CURRENCIES.includes(currencyMatch[1])) {
    out.currency = currencyMatch[1];
  }
  const periodMatch = raw.toLowerCase().match(/\b(hour|hr|hourly|day|daily|week|wk|weekly|month|mo|monthly|year|yr|yearly|annum|annual)\b/);
  if (periodMatch) out.period = _PERIOD_TOKEN_TO_VALUE[periodMatch[1]] || out.period;
  return out;
}

function composeTargetSalary(form) {
  const amount = String(form.target_salary_amount || '').trim();
  if (!amount) return '';
  const currency = (form.target_salary_currency || 'USD').trim();
  const period = (form.target_salary_period || 'year').trim();
  return `${amount} ${currency} / ${period}`;
}

/* Multi-select chip toggle. Each chip flips one value in/out of the
   selected array. Empty selection = "no constraint" (the convention
   downstream filters expect). */
function ChipToggle({ label, value, onChange, options, helper }) {
  const selected = new Set(Array.isArray(value) ? value : []);
  const toggle = (v) => {
    const next = new Set(selected);
    if (next.has(v)) next.delete(v); else next.add(v);
    onChange(Array.from(next));
  };
  return (
    <label className="set-field" style={{ display:'block' }}>
      <span className="set-label">{label}</span>
      <div className="chip-toggle-row">
        {options.map(o => {
          const on = selected.has(o.v);
          return (
            <button
              key={o.v}
              type="button"
              className={'chip-toggle' + (on ? ' on' : '')}
              onClick={() => toggle(o.v)}
            >
              {o.label}
            </button>
          );
        })}
      </div>
      {helper && <div className="set-helper" style={{ marginTop:6 }}>{helper}</div>}
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
  const [searchQuery, setSearchQuery] = useState('');
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
            <input
              placeholder="Search 311+ companies…"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
            />
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

function useAutoScroll(deps = []) {
  const ref = useRef(null);
  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, deps);
  return ref;
}

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
  const logRef = useAutoScroll([logs.length, running]);
  if (!lines.length && !running) return null;
  return (
    <div className="agent-log" ref={logRef}>
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

/* ── Phase 4 — Tailored resume preview card ──────────────────────────────
   jobright.ai-style breakdown: header (company / role / match score / ATS
   delta) + tailored sections (skills comparison, reordered skills, per-role
   bullets, missing keywords) + download buttons. */
function TailoredResumeCard({ item }) {
  const [open, setOpen] = useState(true);

  const ats_before = Number(item.ats_before) || 0;
  const ats_after  = Number(item.ats_after) || 0;
  const ats_delta  = Number(item.ats_delta != null ? item.ats_delta : ats_after - ats_before);
  const score      = Number(item.score) || 0;

  // Download links — ALWAYS prefer the *_final* (clean / no-green) variants
  // produced by _save_tailored_resume. The diff-colored versions are only
  // useful for the in-page preview iframe (item.html_preview_url); the file
  // a user actually attaches to a job application must be all-black body
  // text. The server exposes:
  //   item.final_pdf_url  → clean PDF  (template-lib + in-place renderers)
  //   item.final_docx_url → clean DOCX (in-place .docx path only)
  //   item.final_tex_url  → clean TeX  (in-place .tex path only)
  // Fall back to resume_file (which the endpoint now also prefers _final
  // over _diff for) when the explicit URL fields aren't set — covers
  // legacy session_state rows that pre-date the schema.
  const fileBase = item.resume_file || '';
  const isPdf = /\.pdf$/i.test(fileBase);
  const isTex = /\.tex$/i.test(fileBase);
  const isDocx = /\.docx$/i.test(fileBase);
  const pdfHref = item.final_pdf_url
    || (isPdf ? `/output/${fileBase}` : null);
  const texHref = item.final_tex_url
    || (isTex ? `/output/${fileBase}` : null);
  const docxHref = item.final_docx_url
    || (isDocx ? `/output/${fileBase}` : null);

  const skills = Array.isArray(item.skills) ? item.skills : [];
  const gaps   = Array.isArray(item.ats_gaps) ? item.ats_gaps : [];
  const cmp    = Array.isArray(item.keyword_comparison) ? item.keyword_comparison : [];
  const expBlocks = Array.isArray(item.experience_bullets) ? item.experience_bullets : [];

  const errored = (item.status || '').toLowerCase() === 'error';

  return (
    <div className={'tr-card' + (errored ? ' tr-card-err' : '')}>
      <button type="button" className="tr-head" onClick={() => setOpen(o => !o)} aria-expanded={open}>
        <div className="tr-head-l">
          <CompanyLogo company={item.co} size={36}/>
          <div className="tr-head-titles">
            <div className="tr-head-co">{item.co || 'Unknown company'}</div>
            <div className="tr-head-role">{item.role || 'Untitled role'}</div>
          </div>
        </div>
        <div className="tr-head-r">
          <div className="tr-stat">
            <i>MATCH</i>
            <b>{score}</b>
          </div>
          <div className="tr-stat tr-stat-ats">
            <i>ATS</i>
            <b>
              <span className="tr-ats-num">{ats_before}</span>
              <span className="tr-ats-arrow">→</span>
              <span className="tr-ats-num tr-ats-after">{ats_after}</span>
              <span className={'tr-ats-delta' + (ats_delta >= 0 ? ' good' : ' bad')}>
                {ats_delta >= 0 ? '+' : ''}{ats_delta}
              </span>
            </b>
          </div>
          <span className="tr-chev" aria-hidden="true">
            <Icon name={open ? 'chevron-up' : 'chevron-down'} size={14}/>
          </span>
        </div>
      </button>

      {errored && item.notes && (
        <div className="tr-err-note">
          <Icon name="alert-triangle" size={12}/>
          <span>{item.notes}</span>
        </div>
      )}

      {open && !errored && (
        <div className="tr-body">
          {cmp.length > 0 && (
            <section className="tr-sec">
              <div className="tr-sec-h">
                <Icon name="table" size={12}/>
                <span>Skills comparison</span>
                <em>{cmp.filter(c => c.on_resume).length}/{cmp.length} matched</em>
              </div>
              <div className="tr-cmp">
                <div className="tr-cmp-row tr-cmp-head">
                  <span>Keyword from JD</span>
                  <span>On your resume</span>
                  <span>Action</span>
                </div>
                {cmp.map((c, i) => (
                  <div key={i} className={'tr-cmp-row' + (c.on_resume ? ' hit' : ' miss')}>
                    <span className="tr-cmp-kw">{c.keyword}</span>
                    <span className="tr-cmp-mark">
                      {c.on_resume
                        ? <span className="tr-pill tr-pill-good"><Icon name="check" size={10}/> yes</span>
                        : <span className="tr-pill tr-pill-bad"><Icon name="x" size={10}/> no</span>}
                    </span>
                    <span className={'tr-cmp-act ' + (c.on_resume ? 'keep' : 'add')}>
                      {c.action === 'add' ? '+ add to resume' : '✓ keep'}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}

          {skills.length > 0 && (
            <section className="tr-sec">
              <div className="tr-sec-h">
                <Icon name="sparkles" size={12}/>
                <span>Reordered skills (front-loaded for this JD)</span>
              </div>
              <div className="tr-chips">
                {skills.map((s, i) => (
                  <span key={i} className={'tr-chip' + (i < 5 ? ' tr-chip-top' : '')}>{s}</span>
                ))}
              </div>
            </section>
          )}

          {expBlocks.length > 0 && (
            <section className="tr-sec">
              <div className="tr-sec-h">
                <Icon name="briefcase" size={12}/>
                <span>Tailored experience bullets</span>
              </div>
              <div className="tr-roles">
                {expBlocks.map((blk, i) => (
                  <div key={i} className="tr-role">
                    {blk.role && <div className="tr-role-h">{blk.role}</div>}
                    <ul className="tr-role-bullets">
                      {(blk.bullets || []).map((b, j) => <li key={j}>{b}</li>)}
                    </ul>
                  </div>
                ))}
              </div>
            </section>
          )}

          {gaps.length > 0 && (
            <section className="tr-sec">
              <div className="tr-sec-h">
                <Icon name="alert-circle" size={12}/>
                <span>Keywords still missing — consider adding</span>
              </div>
              <div className="tr-chips">
                {gaps.map((g, i) => <span key={i} className="tr-chip tr-chip-gap">{g}</span>)}
              </div>
            </section>
          )}

          {item.has_cl && item.cover_letter && (
            <section className="tr-sec">
              <div className="tr-sec-h">
                <Icon name="mail" size={12}/>
                <span>Cover letter</span>
              </div>
              <pre className="tr-cl">{item.cover_letter}</pre>
            </section>
          )}

          {(pdfHref || docxHref || texHref) && (
            <div className="tr-actions">
              {pdfHref && (
                <a className="tr-dl tr-dl-primary" href={pdfHref} download>
                  <Icon name="download" size={12} color="#fff"/> Download PDF
                </a>
              )}
              {docxHref && (
                <a className="tr-dl" href={docxHref} download>
                  <Icon name="file-text" size={12}/> .docx source
                </a>
              )}
              {texHref && pdfHref !== texHref && (
                <a className="tr-dl" href={texHref} download>
                  <Icon name="file-code-2" size={12}/> .tex source
                </a>
              )}
              {pdfHref && (
                <a className="tr-dl tr-dl-ghost" href={pdfHref} target="_blank" rel="noreferrer">
                  <Icon name="external-link" size={12}/> Open in new tab
                </a>
              )}
              <small style={{ display: 'block', flexBasis: '100%', marginTop: 6, color: 'var(--t4)', fontSize: 11 }}>
                Downloads are the all-black, employer-ready version. Green
                highlights only appear in the preview above so you can see
                what changed.
              </small>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/* ── Phase 6: in-page application tracker spreadsheet ─────────────────────
   Renders the structured payload from phase6_update_tracker as a sortable
   HTML table. Color-codes status, summary tile bar above. Replaces the
   old "download .xlsx" link — everything stays on the page. */
function TrackerSpreadsheet({ tracker }) {
  const t = tracker || {};
  const cols = t.columns || [];
  const rows = t.rows || [];
  const summary = t.summary || {};
  const [sort, setSort] = useState({ key: 'n', dir: 'asc' });

  const sorted = useMemo(() => {
    const arr = rows.slice();
    const k = sort.key;
    arr.sort((a, b) => {
      const av = a[k], bv = b[k];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'number' && typeof bv === 'number') return av - bv;
      return String(av).localeCompare(String(bv), undefined, { numeric: true });
    });
    if (sort.dir === 'desc') arr.reverse();
    return arr;
  }, [rows, sort]);

  if (!cols.length) {
    return <div className="phase-detail"><div className="wait-state">Tracker not generated yet — run Phase 6.</div></div>;
  }

  const STATUS_TONE = {
    'Applied':         { bg: 'var(--good-d)',    br: 'var(--good-b)',    fg: 'var(--good)'    },
    'Manual Required': { bg: 'var(--warn-d)',    br: 'var(--warn-b)',    fg: 'var(--warn)'    },
    'Skipped':         { bg: 'var(--bad-d)',     br: 'var(--bad-b)',     fg: 'var(--bad)'     },
    'Error':           { bg: 'var(--accent-d)',  br: 'var(--accent-b)',  fg: 'var(--accent-h)' },
    'Tailored':        { bg: 'var(--accent2-d)', br: 'var(--accent2-b)', fg: 'var(--accent2)' },
  };

  const renderCell = (col, row) => {
    const v = row[col.key];
    if (col.type === 'url') {
      if (!v) return <span style={{ color: 'var(--t4)' }}>—</span>;
      return <a href={v} target="_blank" rel="noreferrer" style={{ color: 'var(--accent-h)' }}>
        Open <Icon name="external-link" size={10}/>
      </a>;
    }
    if (col.type === 'status') {
      const tone = STATUS_TONE[v] || STATUS_TONE['Applied'];
      return <span style={{
        display: 'inline-block', padding: '3px 9px', borderRadius: 999,
        fontSize: 11, fontWeight: 600, letterSpacing: '.02em',
        background: tone.bg, border: `1px solid ${tone.br}`, color: tone.fg,
      }}>{v || '—'}</span>;
    }
    if (col.type === 'score') {
      const s = Number(v) || 0;
      const c = s >= 85 ? 'var(--good)' : s >= 70 ? 'var(--accent-h)'
              : s >= 50 ? 'var(--warn)' : 'var(--bad)';
      return <span style={{ color: c, fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>
        {s}<i style={{ color: 'var(--t4)', fontStyle: 'normal' }}>/100</i>
      </span>;
    }
    if (col.type === 'yesno') {
      return v
        ? <span style={{ color: 'var(--good)' }}>Yes</span>
        : <span style={{ color: 'var(--t4)' }}>No</span>;
    }
    if (col.type === 'int') {
      return <span style={{ fontVariantNumeric: 'tabular-nums', color: 'var(--t3)' }}>{v ?? ''}</span>;
    }
    if (v == null || v === '') return <span style={{ color: 'var(--t4)' }}>—</span>;
    return <span>{String(v)}</span>;
  };

  const headerArrow = (key) => {
    if (sort.key !== key) return <span style={{ opacity: .25, marginLeft: 4 }}>↕</span>;
    return <span style={{ marginLeft: 4 }}>{sort.dir === 'asc' ? '↑' : '↓'}</span>;
  };

  const onHeaderClick = (key) => {
    setSort(prev => prev.key === key
      ? { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
      : { key, dir: 'asc' });
  };

  return (
    <div className="phase-detail">
      <div className="metrics" style={{ marginBottom: 14 }}>
        <div className="met"><b>{summary.total ?? rows.length}</b><span>Tracked</span></div>
        <div className="met"><b style={{ color: 'var(--good)' }}>{summary.applied ?? 0}</b><span>Applied</span></div>
        <div className="met"><b style={{ color: 'var(--warn)' }}>{summary.manual ?? 0}</b><span>Manual</span></div>
        <div className="met"><b style={{ color: 'var(--bad)' }}>{summary.skipped ?? 0}</b><span>Skipped</span></div>
        <div className="met"><b>{summary.avg_score ?? 0}</b><span>Avg score</span></div>
        <div className="met"><b style={{ fontSize: 14 }}>{summary.run_date || ''}</b><span>Run date</span></div>
      </div>
      <div style={{
        border: '1px solid var(--bdr)', borderRadius: 10, overflow: 'auto',
        background: 'var(--surface)', maxHeight: '60vh',
      }}>
        <table style={{
          borderCollapse: 'separate', borderSpacing: 0, width: '100%',
          fontSize: 12.5, fontVariantLigatures: 'none',
        }}>
          <thead>
            <tr>
              {cols.map(col => (
                <th key={col.key}
                  onClick={() => onHeaderClick(col.key)}
                  style={{
                    position: 'sticky', top: 0, zIndex: 2,
                    background: 'var(--bg-2)', color: 'var(--t2)',
                    textAlign: 'left', padding: '10px 12px',
                    fontWeight: 600, fontSize: 11.5, letterSpacing: '.02em',
                    textTransform: 'uppercase', cursor: 'pointer',
                    minWidth: col.width || 100, whiteSpace: 'nowrap',
                    borderBottom: '1px solid var(--bdr2)', userSelect: 'none',
                  }}>
                  {col.label}{headerArrow(col.key)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && (
              <tr><td colSpan={cols.length} style={{ padding: 24, color: 'var(--t3)', textAlign: 'center' }}>
                No applications tracked yet.
              </td></tr>
            )}
            {sorted.map((row, i) => (
              <tr key={`${row.n}-${i}`} style={{
                background: i % 2 ? 'var(--surface)' : 'var(--sur2)',
              }}>
                {cols.map(col => (
                  <td key={col.key} style={{
                    padding: '8px 12px', borderBottom: '1px solid var(--bdr)',
                    color: 'var(--t1)', verticalAlign: 'top',
                    maxWidth: (col.width || 200) + 80, overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    whiteSpace: col.type === 'text' ? 'normal' : 'nowrap',
                  }} title={typeof row[col.key] === 'string' ? row[col.key] : ''}>
                    {renderCell(col, row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--t4)' }}>
        Click any column header to sort. Tracker is saved to your account — no file download required.
      </div>
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
    if (!items.length) {
      return <div className="phase-detail"><div className="wait-state">Run or re-run Phase 4 to see tailored resume details.</div></div>;
    }
    const totalDelta = items.reduce((acc, it) => acc + (Number(it.ats_delta) || 0), 0);
    const avgScore = Math.round(items.reduce((a, it) => a + (it.score || 0), 0) / items.length);
    return (
      <div className="phase-detail">
        <div className="metrics">
          <div className="met"><b>{data.count ?? items.length}</b><span>Resume variants</span></div>
          <div className="met"><b>{avgScore}</b><span>Avg match</span></div>
          <div className="met">
            <b style={{ color: totalDelta >= 0 ? 'var(--good)' : 'var(--bad)' }}>
              {totalDelta >= 0 ? '+' : ''}{totalDelta}
            </b>
            <span>Total ATS gain</span>
          </div>
        </div>
        <div className="tr-list">
          {items.map((it, i) => <TailoredResumeCard key={(it.resume_file || it.co) + i} item={it}/>)}
        </div>
      </div>
    );
  }
  if (n === 5) {
    const apps = data.apps || state.applications || [];
    return <div className="phase-detail"><div className="metrics"><div className="met"><b>{data.applied ?? apps.filter(a=>a.app_status==='Applied'||a.status==='Applied').length}</b><span>Applied</span></div><div className="met"><b>{data.manual ?? apps.filter(a=>a.app_status==='Manual Required'||a.status==='Manual Required').length}</b><span>Manual</span></div></div><DetailTable columns={[{key:'co',label:'Company',strong:true},{key:'role',label:'Role'},{key:'score',label:'Score'},{key:'status',label:'Status',render:x=>x.status || x.app_status},{key:'confirmation',label:'Confirmation'},{key:'resume',label:'Resume',render:x=>x.resume || x.resume_version || '-'},{key:'url',label:'URL',render:x=>x.url?<a href={x.url} target="_blank" rel="noreferrer">Open</a>:'-'}]} rows={apps}/></div>;
  }
  if (n === 6) {
    // Prefer the SSE payload (arrives on `done`), fall back to the persisted
    // ``state.tracker_data`` so reloads / late mounts still show the
    // spreadsheet without re-running Phase 6.
    const tracker = (data && (data.rows || data.columns)) ? data : state.tracker_data;
    return <TrackerSpreadsheet tracker={tracker}/>;
  }
  if (n === 7) {
    const reportText = data.report || state.report || '';
    if (!reportText.trim()) {
      return <div className="phase-detail"><div className="wait-state">Run report not generated yet.</div></div>;
    }
    return (
      <div className="phase-detail">
        <div style={{
          padding: 22, borderRadius: 12,
          background: 'var(--surface)', border: '1px solid var(--bdr)',
          color: 'var(--t1)', lineHeight: 1.6,
        }}>
          <Markdown text={reportText}/>
        </div>
        <div style={{ marginTop: 8, fontSize: 11.5, color: 'var(--t4)' }}>
          Run report saved to your account — no file download required.
        </div>
      </div>
    );
  }
  return null;
}

/* ── Atlas chat — streaming over fetch (SSE-style) ─────────────────────── */
function streamAtlasChat({ message, history, onStart, onDelta, onDone, onError }) {
  const ctrl = new AbortController();
  fetch('/api/atlas/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history }),
    signal: ctrl.signal,
  }).then(async resp => {
    if (!resp.ok) {
      const txt = await resp.text().catch(() => '');
      // Pretty-print FastAPI's {"detail":"..."} envelope so the user sees a
      // sentence rather than a JSON blob.
      let detail = txt;
      try { detail = JSON.parse(txt).detail || txt; } catch (_) {}
      // 404 on this route almost always means the server is running an older
      // app.py that predates the Ask Atlas endpoint — give the user a hint.
      if (resp.status === 404) {
        detail = 'Ask Atlas endpoint not found (HTTP 404). Restart the backend (uvicorn app:app) so it picks up the /api/atlas/chat/stream route.';
      }
      onError?.(new Error(detail || `HTTP ${resp.status}`));
      return;
    }
    const reader = resp.body?.getReader();
    if (!reader) { onError?.(new Error('No stream body')); return; }
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const events = buf.split('\n\n');
      buf = events.pop() || '';
      for (const ev of events) {
        const dataLine = ev.split('\n').find(l => l.startsWith('data: '));
        if (!dataLine) continue;
        try {
          const m = JSON.parse(dataLine.slice(6));
          if      (m.type === 'start') onStart?.(m);
          else if (m.type === 'delta') onDelta?.(m.text || '');
          else if (m.type === 'done')  { onDone?.(m); return; }
          else if (m.type === 'error') { onError?.(new Error(m.message || 'chat error')); return; }
        } catch (e) { /* malformed event — skip */ }
      }
    }
    onDone?.({});
  }).catch(err => {
    if (err.name !== 'AbortError') onError?.(err);
  });
  return () => ctrl.abort();
}

function AtlasChat({ state, dataPing }) {
  const [messages, setMessages] = useState([]); // [{role, content, streaming?, error?}]
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);
  const cancelRef = useRef(null);
  const transcriptRef = useAutoScroll([messages]);

  const mode = state?.mode || 'ollama';
  // Neutral label — the user shouldn't have to know which provider is wired.
  const modeLabel = 'AI';

  const send = (text) => {
    const trimmed = (text ?? draft).trim();
    if (!trimmed || busy) return;
    setDraft('');
    const history = messages
      .filter(m => !m.streaming && m.content && !m.error)
      .map(({ role, content }) => ({ role, content }));
    setMessages(prev => [
      ...prev,
      { role: 'user', content: trimmed },
      { role: 'assistant', content: '', streaming: true },
    ]);
    setBusy(true);
    cancelRef.current = streamAtlasChat({
      message: trimmed,
      history,
      onDelta: (chunk) => {
        setMessages(prev => {
          const copy = prev.slice();
          const last = copy[copy.length - 1];
          if (last && last.role === 'assistant') {
            copy[copy.length - 1] = { ...last, content: (last.content || '') + chunk };
          }
          return copy;
        });
      },
      onDone: () => {
        setMessages(prev => {
          const copy = prev.slice();
          const last = copy[copy.length - 1];
          if (last && last.streaming) {
            copy[copy.length - 1] = { ...last, streaming: false, content: last.content || '(no reply)' };
          }
          return copy;
        });
        setBusy(false);
      },
      onError: (err) => {
        setMessages(prev => {
          const copy = prev.slice();
          const last = copy[copy.length - 1];
          if (last && last.streaming) {
            copy[copy.length - 1] = { ...last, streaming: false, content: '', error: err.message || 'chat failed' };
          }
          return copy;
        });
        setBusy(false);
      },
    });
  };

  const stop = () => {
    cancelRef.current?.();
    cancelRef.current = null;
    setMessages(prev => {
      const copy = prev.slice();
      const last = copy[copy.length - 1];
      if (last && last.streaming) {
        copy[copy.length - 1] = { ...last, streaming: false, content: last.content || '(stopped)' };
      }
      return copy;
    });
    setBusy(false);
  };

  const reset = () => {
    cancelRef.current?.();
    setMessages([]);
    setBusy(false);
  };

  const suggestions = useMemo(() => {
    const out = [];
    if (state?.scored_summary?.total) {
      out.push("Which of my top-scored jobs should I prioritise this week?");
      out.push("What's the pattern in the gaps the scorer flagged?");
    } else if (state?.has_resume) {
      out.push("What's the strongest narrative my resume can tell?");
      out.push("Which titles am I closest to landing right now?");
    } else {
      out.push("How does Jobs AI work?");
      out.push("What should I upload first?");
    }
    if ((state?.applications || []).length) {
      out.push("How am I trending across the applications I've sent?");
    } else {
      out.push("How aggressive should my apply threshold be?");
    }
    return out.slice(0, 3);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state?.scored_summary?.total, state?.has_resume, state?.applications, dataPing]);

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <aside className="atlas-panel">
      <header className="atlas-head">
        <div className="atlas-head-l">
          <span className="atlas-mark" aria-hidden="true">✦</span>
          <div className="atlas-head-text">
            <div className="atlas-name">Ask <em>Atlas</em></div>
            <div className="atlas-sub">Career-wide strategist · <code>{modeLabel}</code></div>
          </div>
        </div>
        <div className="atlas-head-r">
          {busy
            ? <button className="atlas-iconbtn" onClick={stop} title="Stop generating"><Icon name="square" size={11}/></button>
            : <button className="atlas-iconbtn" onClick={reset} disabled={!messages.length} title="Clear chat"><Icon name="rotate-ccw" size={11}/></button>}
        </div>
      </header>

      <div className="atlas-transcript" ref={transcriptRef}>
        {messages.length === 0 ? (
          <div className="atlas-empty">
            <div className="atlas-empty-mark">✦</div>
            <div className="atlas-empty-h">I've read your resume, your queue, and your applications.</div>
            <div className="atlas-empty-sub">Ask anything — strategy, gaps, what to fix next.</div>
            <div className="atlas-suggestions">
              {suggestions.map((s, i) => (
                <button key={i} className="atlas-sug" onClick={() => send(s)}>
                  <span className="atlas-sug-arrow">↳</span> {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((m, i) => (
            <div key={i} className={'atlas-msg atlas-msg-' + m.role + (m.streaming ? ' streaming' : '') + (m.error ? ' error' : '')}>
              {m.role === 'assistant' && <span className="atlas-msg-mark" aria-hidden="true">✦</span>}
              <div className="atlas-msg-bubble">
                {m.error
                  ? <span className="atlas-msg-err"><Icon name="circle-alert" size={12}/> {m.error}</span>
                  : (m.content
                      ? (
                          <>
                            {m.role === 'assistant'
                              ? <Markdown text={m.content}/>
                              : m.content}
                            {m.streaming ? <span className="atlas-cursor">▍</span> : null}
                          </>
                        )
                      : (m.streaming ? <span className="atlas-thinking"><span/><span/><span/></span> : null))}
              </div>
            </div>
          ))
        )}
      </div>

      <div className="atlas-composer">
        <textarea
          className="atlas-input"
          placeholder={busy ? 'Atlas is replying…' : 'Ask Atlas about your search…'}
          value={draft}
          onChange={e => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
        />
        <button className="atlas-send" onClick={() => send()} disabled={busy || !draft.trim()} aria-label="Send message">
          {busy ? <span className="spin" style={{ width:13, height:13, borderWidth:2 }}/> : <Icon name="arrow-up" size={14}/>}
        </button>
      </div>
    </aside>
  );
}

/* ── Past runs & outputs ─────────────────────────────────────────────────
   Lists every artefact this user's pipeline has produced — tailored
   resumes, monthly tracker spreadsheets, run reports — so they don't have
   to dig into the filesystem. Reads `state.output_files` (already shaped
   by /api/state with name, phase, size_kb, url, mtime-sorted desc). */
const _RUN_BUCKETS = [
  { key: 'tracker',  match: f => /\.xlsx$/i.test(f.name) || f.phase === 6,
    title: 'Trackers',         icon: 'file-spreadsheet', desc: 'Monthly application tracker spreadsheets.' },
  { key: 'resumes',  match: f => f.phase === 4 || /_Resume_/i.test(f.name),
    title: 'Tailored résumés', icon: 'file-text',        desc: 'Per-job tailored résumé PDFs and LaTeX sources.' },
  { key: 'reports',  match: f => f.phase === 7 || /\.md$/i.test(f.name),
    title: 'Run reports',      icon: 'file-line-chart',  desc: 'Plain-language summaries of each pipeline run.' },
  { key: 'other',    match: () => true,
    title: 'Other',            icon: 'file',             desc: 'Other artefacts produced by the pipeline.' },
];

function _humanSize(kb) {
  const n = Number(kb) || 0;
  if (n < 1) return `${Math.max(1, Math.round(n * 1024))} B`;
  if (n < 1024) return `${n.toFixed(1)} KB`;
  return `${(n / 1024).toFixed(2)} MB`;
}

function RunOutputsPanel({ state, refresh }) {
  const allFiles = state?.output_files || [];
  const [openBuckets, setOpenBuckets] = useState({});
  const [expandedBuckets, setExpandedBuckets] = useState({});
  const [collapsed, setCollapsed] = useState(false);

  // Bucket the files exactly once per render. Each file lands in its
  // first-matching bucket so no row is duplicated and "Other" is a true
  // catch-all rather than a merge of leftovers.
  const grouped = useMemo(() => {
    const claimed = new Set();
    const out = {};
    for (const b of _RUN_BUCKETS) out[b.key] = [];
    for (const f of allFiles) {
      for (const b of _RUN_BUCKETS) {
        if (claimed.has(f.url)) break;
        if (b.match(f)) {
          out[b.key].push(f);
          claimed.add(f.url);
          break;
        }
      }
    }
    return out;
  }, [allFiles]);

  const totalCount = allFiles.length;
  const trackerLatest = grouped.tracker?.[0];

  // Empty state — nothing to show yet.
  if (totalCount === 0) {
    return (
      <section className="run-outputs run-outputs-empty">
        <div className="run-outputs-head">
          <div className="run-outputs-head-l">
            <Icon name="archive" size={14}/>
            <span className="run-outputs-h">Past runs &amp; outputs</span>
          </div>
        </div>
        <div className="run-outputs-empty-body">
          <Icon name="folder-open" size={20} color="var(--t4)"/>
          <div>
            <div className="run-outputs-empty-h">No artefacts yet</div>
            <div className="run-outputs-empty-sub">
              Run phases 4 (résumé tailoring), 6 (tracker), or 7 (run report)
              to generate downloadable files. They'll all surface here once produced.
            </div>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className={'run-outputs' + (collapsed ? ' run-outputs-collapsed' : '')}>
      <div className="run-outputs-head" onClick={() => setCollapsed(c => !c)}
           role="button" tabIndex={0}
           aria-expanded={!collapsed}>
        <div className="run-outputs-head-l">
          <Icon name="archive" size={14}/>
          <span className="run-outputs-h">Past runs &amp; outputs</span>
          <span className="run-outputs-count">{totalCount} file{totalCount === 1 ? '' : 's'}</span>
        </div>
        <div className="run-outputs-head-r">
          {trackerLatest && (
            <a className="run-outputs-quick"
               href={trackerLatest.url}
               download={trackerLatest.name}
               onClick={e => e.stopPropagation()}
               title={`Download ${trackerLatest.name}`}>
              <Icon name="download" size={11}/> Latest tracker
            </a>
          )}
          <button className="run-outputs-refresh"
                  onClick={e => { e.stopPropagation(); refresh?.(); }}
                  title="Refresh outputs list">
            <Icon name="refresh-cw" size={11}/>
          </button>
          <span className="run-outputs-chev" aria-hidden="true">
            <Icon name={collapsed ? 'chevron-down' : 'chevron-up'} size={14}/>
          </span>
        </div>
      </div>

      {!collapsed && (
        <div className="run-outputs-body">
          {_RUN_BUCKETS.map(bucket => {
            const files = grouped[bucket.key] || [];
            if (!files.length) return null;
            const isOpen = openBuckets[bucket.key] !== false;   // default open
            const expanded = !!expandedBuckets[bucket.key];
            const visible = expanded ? files : files.slice(0, 6);
            return (
              <div key={bucket.key} className={'run-bucket' + (isOpen ? '' : ' run-bucket-closed')}>
                <button type="button" className="run-bucket-h"
                        onClick={() => setOpenBuckets(b => ({ ...b, [bucket.key]: !isOpen }))}
                        aria-expanded={isOpen}>
                  <Icon name={bucket.icon} size={12}/>
                  <span className="run-bucket-t">{bucket.title}</span>
                  <span className="run-bucket-n">{files.length}</span>
                  <span className="run-bucket-d">{bucket.desc}</span>
                  <Icon name={isOpen ? 'chevron-up' : 'chevron-down'}
                        size={12} className="run-bucket-chev"/>
                </button>

                {isOpen && (
                  <ul className="run-files">
                    {visible.map(f => (
                      <li key={f.url} className="run-file">
                        <span className="run-file-icon">
                          <Icon name={
                            /\.xlsx$/i.test(f.name) ? 'file-spreadsheet' :
                            /\.pdf$/i.test(f.name)  ? 'file-text'        :
                            /\.tex$/i.test(f.name)  ? 'file-code-2'      :
                            /\.md$/i.test(f.name)   ? 'file-line-chart'  : 'file'
                          } size={13}/>
                        </span>
                        <span className="run-file-name" title={f.name}>{f.name}</span>
                        <span className="run-file-size">{_humanSize(f.size_kb)}</span>
                        <span className="run-file-actions">
                          <a className="run-file-btn"
                             href={f.url} target="_blank" rel="noopener noreferrer"
                             title="Open in new tab">
                            <Icon name="external-link" size={11}/>
                          </a>
                          <a className="run-file-btn run-file-btn-primary"
                             href={f.url} download={f.name}
                             title={`Download ${f.name}`}>
                            <Icon name="download" size={11}/>
                          </a>
                        </span>
                      </li>
                    ))}
                    {files.length > visible.length && (
                      <li className="run-files-more">
                        <button type="button" className="run-files-more-btn"
                                onClick={() => setExpandedBuckets(e => ({ ...e, [bucket.key]: true }))}>
                          Show {files.length - visible.length} more
                        </button>
                      </li>
                    )}
                    {expanded && files.length > 6 && (
                      <li className="run-files-more">
                        <button type="button" className="run-files-more-btn"
                                onClick={() => setExpandedBuckets(e => ({ ...e, [bucket.key]: false }))}>
                          Collapse
                        </button>
                      </li>
                    )}
                  </ul>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}


function AgentPage({ state, refresh }) {
  const [open,    setOpen]    = useState({});
  const [running, setRunning] = useState(null);
  const [errors,  setErrors]  = useState({});
  const [phaseResults, setPhaseResults] = useState({});
  const [phaseLogs, setPhaseLogs] = useState({});
  const [resetBusy, setResetBusy] = useState(false);
  const [resetError, setResetError] = useState(null);
  // True iff this AgentPage instance owns the active SSE EventSource.
  // Distinguishes "we're driving the phase" from "the server is running it
  // because we kicked it off in a previous mount" — see the running_phases
  // sync below.
  const drivingRef = useRef(false);

  const done = useMemo(() => new Set(state?.done || []), [state?.done]);
  const pct  = Math.round((done.size / 7) * 100);
  const C = 56, circ = 2 * Math.PI * C;
  const off = circ - (circ * pct / 100);
  const ringTone = pct === 100 ? 'var(--good)' : pct > 0 ? 'var(--accent-h)' : 'var(--t4)';

  // ── Backend running-phase sync ──────────────────────────────────────────
  // /api/state.running_phases is the server's source of truth for "what's
  // executing right now". When the user starts a phase, navigates away,
  // then comes back, the local `running` state is gone but the worker
  // thread is still alive on the server. Without this effect, the UI would
  // wrongly report "idle" while the backend kept churning. We hydrate the
  // recent-log tail so the user sees continuity, and clear `running` when
  // the server reports the phase finished.
  const serverRunningPhases = state?.running_phases || [];
  const serverRunning = serverRunningPhases[0]?.phase ?? null;
  useEffect(() => {
    if (serverRunning != null && running == null && !drivingRef.current) {
      // Server says a phase is in flight, but we're not driving it.
      // This is the "navigate away → come back" case.
      setRunning(serverRunning);
      setOpen(o => ({ ...o, [serverRunning]: true }));
      const rec = serverRunningPhases.find(r => r.phase === serverRunning);
      if (rec?.recent_logs?.length) {
        setPhaseLogs(p => {
          // Only hydrate when we don't already have richer local logs —
          // a concurrent SSE feed will deliver more lines than the
          // capped server buffer ever does.
          if ((p[serverRunning] || []).length >= rec.recent_logs.length) return p;
          return { ...p, [serverRunning]: rec.recent_logs };
        });
      }
    } else if (serverRunning == null && running != null && !drivingRef.current) {
      // Server reports idle but we still think a phase is running.
      // Either it finished while we were on another page, or an upstream
      // error closed the worker. Clear the local indicator — the result
      // (if any) will surface via state.done / state.error on the next poll.
      setRunning(null);
    }
    // We deliberately depend only on the server-side signal so a local
    // setRunning(null) inside onDone/onError doesn't immediately re-trigger.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverRunning, serverRunningPhases.length]);

  const startPhase = (n, rerun=false) => {
    if (running) return;
    setRunning(n);
    drivingRef.current = true;
    setErrors(p => ({ ...p, [n]:null }));
    setOpen(o => ({ ...o, [n]:true }));
    setPhaseLogs(p => ({ ...p, [n]:[] }));
    runPhaseSSE(n, {
      rerun,
      onLog:   m  => setPhaseLogs(p => ({ ...p, [n]:[...(p[n] || []), m.text || m.line || ''] })),
      onDone:  m  => {
        setPhaseResults(p => ({ ...p, [n]:m.data || {} }));
        setRunning(null); drivingRef.current = false; refresh();
      },
      onError: e  => {
        setRunning(null); drivingRef.current = false;
        setErrors(p => ({ ...p, [n]:e.message || 'failed' }));
        refresh();
      },
    });
  };

  // Pipeline-only reset. Wipes jobs / scoring / applications / tracker /
  // report. Preserves resume, profile, settings, and every generated
  // document. The destructive "reset everything" flow lives on the
  // Settings page; the Agent page button must never wipe profile data
  // or delete files the user has already produced.
  const handleResetRun = async () => {
    if (resetBusy || running) return;
    const ok = confirm(
      'Reset the pipeline run?\n\n'
      + '✓ Resume, profile, and settings stay intact\n'
      + '✓ Documents you\'ve generated stay in the Documents page\n'
      + '✗ Discovered jobs, scores, tailored applications, tracker, and the run report are cleared'
    );
    if (!ok) return;
    setResetBusy(true);
    setResetError(null);
    try {
      await api.post('/api/pipeline/reset', {});
      // Drop local phase results so the per-phase cards collapse to "idle".
      setPhaseResults({});
      setPhaseLogs({});
      setErrors({});
      setOpen({});
      await refresh();
    } catch (e) {
      setResetError(e?.message || 'Reset failed');
      // Auto-clear the inline error so it doesn't linger after the user
      // resolves whatever blocked the reset (typically: phase still running).
      setTimeout(() => setResetError(null), 4500);
    } finally {
      setResetBusy(false);
    }
  };

  // Run-all uses a local "completed" set so phases finishing mid-loop are
  // tracked correctly (the captured `done` Set otherwise goes stale).
  const runAll = async () => {
    if (running) return;
    drivingRef.current = true;
    const completed = new Set(done);
    try {
      for (let n = 1; n <= 7; n++) {
        if (completed.has(n)) continue;
        const ok = await new Promise(resolve => {
          setRunning(n);
          setOpen(o => ({ ...o, [n]:true }));
          setPhaseLogs(p => ({ ...p, [n]:[] }));
          runPhaseSSE(n, {
            onLog:   m => setPhaseLogs(p => ({ ...p, [n]:[...(p[n] || []), m.text || m.line || ''] })),
            onDone:  m => {
              setPhaseResults(p => ({ ...p, [n]:m.data || {} }));
              setRunning(null); refresh();
              completed.add(n); resolve(true);
            },
            onError: e => {
              setRunning(null);
              setErrors(p => ({ ...p, [n]:e.message || 'failed' }));
              refresh(); resolve(false);
            },
          });
        });
        if (!ok) break;
      }
    } finally {
      drivingRef.current = false;
    }
  };

  const totalElapsed = useMemo(() => {
    const e = state?.elapsed || {};
    return Object.values(e).reduce((acc, v) => acc + (Number(v) || 0), 0);
  }, [state?.elapsed]);

  const appliedCount = (state?.applications || []).filter(a => a.app_status === 'Applied' || a.status === 'Applied').length;

  // Inline persistence for the Phase 2 cap slider — POSTs /api/config on
  // commit so the value is available to the next phase run. Optimistic
  // refresh so the displayed number tracks the slider in real time.
  const tuneMaxJobs = async (n) => {
    try { await api.post('/api/config', { max_scrape_jobs: n }); }
    catch (_) { /* swallow — slider stays local until next refresh */ }
    refresh();
  };

  return (
    <div className="agent-shell">
      {/* Slim header with mono cadence — no decorative hero */}
      <header className="agent-head">
        <div className="agent-head-l">
          <div className="agent-eyebrow">Pipeline · 7 phases</div>
          <h1 className="agent-h"><em>Atlas</em> runs your entire search.</h1>
        </div>
        <div className="agent-head-r">
          <button
            className="btn-ghost"
            onClick={handleResetRun}
            disabled={!!running || resetBusy}
            title="Clear pipeline data only — preserves your resume, profile, settings, and generated documents.">
            {resetBusy
              ? <><span className="spin"/> Resetting…</>
              : <><Icon name="rotate-ccw" size={12}/> Reset run</>}
          </button>
          <button className="head-cta agent-runall" onClick={runAll} disabled={!!running}>
            {running
              ? <>
                  <span className="spin"/>
                  {drivingRef.current
                    ? `Running phase ${running}…`
                    : `Phase ${running} running on server…`}
                </>
              : <><Icon name="play" size={13} color="#fff"/> Run all phases</>}
          </button>
        </div>
        {resetError && (
          <div
            className="agent-reset-error"
            role="alert"
            style={{
              gridColumn: '1 / -1',
              marginTop: 10,
              padding: '8px 12px',
              borderRadius: 8,
              background: 'var(--bad-d)',
              border: '1px solid var(--bad-b)',
              color: 'var(--bad)',
              fontSize: 13.5,
              display: 'flex',
              alignItems: 'center',
              gap: 8,
            }}>
            <Icon name="alert-triangle" size={13}/>
            <span>{resetError}</span>
          </div>
        )}
      </header>

      <div className="agent-grid">
        {/* ── Left: pipeline ──────────────────────────────────── */}
        <section className="agent-pipeline">
          <div className="agent-meter">
            <div className="agent-meter-ring">
              <svg width="120" height="120" viewBox="0 0 120 120">
                <circle cx="60" cy="60" r={C} fill="none" strokeWidth="6" stroke="var(--bdr)"/>
                <circle cx="60" cy="60" r={C} fill="none" strokeWidth="6" stroke={ringTone} strokeLinecap="round"
                  strokeDasharray={circ} strokeDashoffset={off}
                  transform="rotate(-90 60 60)"
                  style={{ transition:'stroke-dashoffset .9s cubic-bezier(.16,1,.3,1), stroke .25s' }}/>
              </svg>
              <div className="agent-meter-pct" style={{ color: ringTone }}>
                <b>{pct}<i>%</i></b>
                <span>{done.size}/7</span>
              </div>
            </div>
            <div className="agent-meter-stats">
              <div className="agent-meter-stat">
                <i>RUNTIME</i>
                <b>{totalElapsed > 0 ? `${totalElapsed.toFixed(1)}s` : '—'}</b>
              </div>
              <div className="agent-meter-stat">
                <i>JOBS</i>
                <b>{state?.scored_summary?.total ?? state?.job_count ?? 0}</b>
              </div>
              <div className="agent-meter-stat">
                <i>APPLIED</i>
                <b>{appliedCount}</b>
              </div>
              <div className="agent-meter-stat">
                <i>STATUS</i>
                <b style={{ color: running ? 'var(--warn)' : pct === 100 ? 'var(--good)' : 'var(--t2)' }}>
                  {running ? 'RUNNING' : pct === 100 ? 'COMPLETE' : pct > 0 ? 'IDLE' : 'READY'}
                </b>
              </div>
            </div>
          </div>

          {/* Inline Phase-2 cap dial. Lets the user trade scoring breadth
              vs. run-time before clicking Run. The DB query underneath is
              already BM25 + skill overlap + freshness + title match ranked
              — a higher cap surfaces the next-most-relevant rows, not
              random extras. */}
          <div className="agent-tune">
            <div className="agent-tune-row">
              <div className="agent-tune-label">
                <Icon name="briefcase" size={12}/>
                <span>Phase 2 cap — top ranked jobs from the live index</span>
              </div>
              <div className="agent-tune-val"><b>{state?.max_scrape_jobs ?? 50}</b><i>jobs</i></div>
            </div>
            <input
              type="range"
              className="set-range agent-tune-range"
              min="10" max="200" step="10"
              value={state?.max_scrape_jobs ?? 50}
              disabled={!!running}
              onChange={e => tuneMaxJobs(parseInt(e.target.value))}
            />
            <div className="agent-tune-helper">
              Phase 2 reads from the local jobs DB (~{state?.job_count || 0} indexed) and ranks by BM25 +
              skill overlap + freshness + title match. Higher cap = more candidates for Phase 3 to score.
            </div>
          </div>

          <RunOutputsPanel state={state} refresh={refresh}/>

          <ol className="agent-phases">
            {[1,2,3,4,5,6,7].map((n, idx) => {
              const isDone = done.has(n);
              const isRun  = running === n;
              const err    = errors[n] || state?.error?.[n];
              const elapsed = state?.elapsed?.[n];
              const cls = isRun ? 'run' : err ? 'err' : isDone ? 'done' : 'idle';
              const isOpen = !!open[n];

              return (
                <li key={n} className={'aph aph-' + cls + (isOpen ? ' aph-open' : '')}
                    style={{ animationDelay: `${idx * 60}ms` }}>
                  <div className="aph-hd" onClick={() => setOpen(o => ({ ...o, [n]: !o[n] }))} role="button" tabIndex={0}>
                    <span className="aph-num">{String(n).padStart(2, '0')}</span>
                    <span className="aph-info">
                      <span className="aph-name">{PHASE_INFO[n].n}</span>
                      <span className="aph-sub">{PHASE_INFO[n].s}</span>
                    </span>
                    <span className="aph-status">
                      {isRun  && <span className="aph-pulse"><span className="aph-dot"/> Running</span>}
                      {!isRun && isDone && <span className="aph-pill aph-pill-good"><Icon name="check" size={10}/> Done</span>}
                      {!isRun && !isDone && !err && <span className="aph-pill aph-pill-mid">Idle</span>}
                      {!isRun && err && <span className="aph-pill aph-pill-bad"><Icon name="alert-triangle" size={10}/> Error</span>}
                      {elapsed != null && <span className="aph-elapsed">{elapsed.toFixed(1)}s</span>}
                    </span>
                    <span className="aph-actions">
                      <button type="button" className="aph-runbtn"
                        onClick={e => { e.stopPropagation(); startPhase(n, isDone); }}
                        disabled={!!running}
                        title={isDone ? 'Re-run phase' : 'Run phase'}>
                        <Icon name={isDone ? 'rotate-ccw' : 'play'} size={11}/>
                      </button>
                      <span className="aph-chev" aria-hidden="true">
                        <Icon name="chevron-down" size={14}/>
                      </span>
                    </span>
                  </div>

                  {isRun && <div className="aph-bar"><div className="aph-bar-fill"/></div>}

                  {isOpen && (
                    <div className="aph-body">
                      <PhaseLog n={n} logs={phaseLogs[n]} running={isRun}/>
                      {err && <div className="err-block"><Icon name="alert-triangle" size={12}/> {err}</div>}
                      {!err && isDone && (
                        <PhaseDetails
                          n={n}
                          data={phaseResults[n] || state?.phase_results?.[n] || state?.phase_results?.[String(n)] || {}}
                          state={state}
                          threshold={state?.threshold || 75}/>
                      )}
                      {!err && !isDone && !isRun && (
                        <div className="wait-state">
                          {n === 1 ? 'Click Run to extract your profile.' : `Waiting for phase ${n - 1} to complete first.`}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </section>

        {/* ── Right: Atlas chat sidebar ──────────────────────── */}
        <AtlasChat state={state} dataPing={(state?.done || []).join(',') + '|' + (state?.applications || []).length}/>
      </div>
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   SETTINGS PAGE
══════════════════════════════════════════════════════════ */
function SettingsPage({ state, refresh, setPage }) {
  const [cfg, setCfg] = useState(state || {});
  const [saving, setSaving] = useState(false);
  const [ollamaModels, setOllamaModels] = useState([]);
  const [ollamaOk, setOllamaOk] = useState(null);
  const [planError, setPlanError] = useState(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState(null);

  const isPro = !!state?.is_pro;
  const isDev = !!state?.is_dev;
  const isCloudModel = name => /cloud$/i.test(name || '');

  const handleReset = async () => {
    if (resetting) return;
    if (!confirm('Delete all data? This will permanently remove your resume, profile, jobs, and applications. Your account stays signed in.')) return;
    setResetting(true);
    setResetError(null);
    try {
      const res = await api.post('/api/reset', {});
      if (res?.ok === false) throw new Error(res.error || 'Reset failed');
      // Pull the now-blank server state and route to home so the cleared
      // state is unmistakable. The Onboarding gate will fire because
      // has_resume is false, which is exactly the post-reset experience.
      await refresh();
      setPage?.('home');
    } catch (err) {
      setResetError(err?.message || 'Reset failed');
    } finally {
      setResetting(false);
    }
  };

  const update = async (newCfg) => {
    const prev = { ...cfg };
    setCfg(p => ({ ...p, ...newCfg }));
    setSaving(true);
    try {
      await api.post('/api/config', newCfg);
      setPlanError(null);
      refresh();
    } catch (e) {
      // 402 Pro plan required OR 503 coming-soon: roll back optimistic state,
      // surface inline. The /api/config endpoint emits both — `Pro plan` for
      // cloud Ollama gating and `coming soon` for Anthropic Claude.
      if (/Pro plan/i.test(e.message || '') || /coming soon/i.test(e.message || '')) {
        setPlanError(e.message);
        setCfg(prev);
      } else {
        setPlanError(e.message || 'Settings save failed');
      }
    } finally {
      setTimeout(() => setSaving(false), 600);
    }
  };

  // Ollama runs SERVER-SIDE (the RPi in production), not on the visiting
  // user's laptop. Fetch on mount unconditionally so the dropdown is populated
  // by the time the user enters Ollama mode (fixes the "models only show on
  // hover" bug — the previous mode-gated effect never fired until mode flipped).
  const [ollamaStatus, setOllamaStatus] = useState(null);
  const [ensuringPull, setEnsuringPull] = useState(false);

  const refreshOllama = useCallback(async () => {
    try {
      const s = await api.get('/api/ollama/status');
      setOllamaStatus(s);
      setOllamaOk(s.running);
      setOllamaModels(s.models || []);
    } catch (e) {
      setOllamaOk(false);
      setOllamaStatus({ running: false, error: e.message });
    }
  }, []);

  useEffect(() => {
    refreshOllama();
    const id = setInterval(refreshOllama, 8000);
    return () => clearInterval(id);
  }, [refreshOllama]);

  // When the configured model isn't pulled, ask the server to background-pull it.
  useEffect(() => {
    if (cfg.mode !== 'ollama') return;
    if (!ollamaStatus?.running) return;
    if (ollamaStatus?.pulled) return;
    if (ensuringPull) return;
    const ps = ollamaStatus?.pull?.status;
    if (ps === 'pulling' || ps === 'starting') return;
    setEnsuringPull(true);
    api.post('/api/ollama/ensure', {})
      .catch(() => {})
      .finally(() => { setEnsuringPull(false); refreshOllama(); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cfg.mode, ollamaStatus?.running, ollamaStatus?.pulled, ollamaStatus?.pull?.status]);

  // If the configured model isn't in the available list, snap to the first one.
  // Free users get snapped to the first local model rather than potentially a cloud model.
  useEffect(() => {
    if (cfg.mode !== 'ollama') return;
    const models = ollamaStatus?.models || [];
    if (!models.length) return;
    const inList = cfg.ollama_model && models.find(m => m.name === cfg.ollama_model);
    // Free users must not stay on a cloud model — snap away even if it's in the list.
    if (inList && (isPro || !isCloudModel(cfg.ollama_model))) return;
    const firstModel = isPro
      ? models[0]
      : (models.find(m => !isCloudModel(m.name)) || models[0]);
    update({ ollama_model: firstModel.name });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ollamaStatus?.models, cfg.mode, isPro]);

  const Toggle = ({ field, label, sub }) => (
    <div className="set-row">
      <div style={{ flex:1 }}>
        <div className="set-label" style={{ marginBottom:2 }}>{label}</div>
        {sub && <div className="set-helper">{sub}</div>}
      </div>
      <button className={'set-toggle' + (cfg[field] ? ' on' : '')}
        onClick={() => update({ [field]: !(cfg[field] ?? false) })}/>
    </div>
  );

  return (
    <>
      <div className="page-head">
        <div className="page-title-big">Settings</div>
        <div className="head-spacer"/>
        {saving && <div style={{ fontSize:14.5, color:'var(--accent-h)', marginRight:12, display:'flex', alignItems:'center', gap:6 }}><span className="spin"/> Saving…</div>}
      </div>

      <div className="page-body solo" style={{ paddingTop:14 }}>
        <div className="settings-grid">
          {/* LLM Backend */}
          <div className="set-sec">
            <div className="set-sec-h">
              <Icon name="cpu" size={14}/> LLM Provider
              {isPro && <span className="plan-chip plan-chip-pro">Pro</span>}
            </div>
            <div className="set-field">
              <div className="set-label">
                Model mode
                <span className="set-label-hint">Claude — coming soon</span>
              </div>
              <select className="set-select" value={cfg.mode} onChange={e => update({ mode: e.target.value })}>
                {/* Anthropic stays in the schema for developer testing; non-devs
                    see it as disabled "Coming soon". The backend rejects the
                    selection regardless, so this is just UX clarity. */}
                <option value="anthropic" disabled={!isDev}>
                  Anthropic Claude{isDev ? ' (developer build)' : ' — Coming soon'}
                </option>
                <option value="ollama">Ollama (local + cloud models)</option>
              </select>
            </div>
            {planError && (
              <div className="plan-banner">
                <Icon name="lock" size={14}/>
                <div className="plan-banner-body">
                  <b>{planError}</b>
                  <span>
                    {/coming soon/i.test(planError)
                      ? "Anthropic Claude isn't live yet — Ollama is selected for now."
                      : 'Switch to a local model, or upgrade to unlock the high-quality cloud models.'}
                  </span>
                </div>
                {!/coming soon/i.test(planError) && (
                  <button className="plan-banner-cta" onClick={() => setPage && setPage('plans')}>
                    View plans <Icon name="arrow-right" size={11}/>
                  </button>
                )}
              </div>
            )}
            {cfg.mode === 'anthropic' && isDev && (
              <div className="set-field">
                <div className="set-label">Anthropic API Key <span className="set-label-hint">developer-only</span></div>
                <input className="set-input" type="password" placeholder="sk-ant-…" value={cfg.api_key || ''}
                  onChange={e => update({ api_key: e.target.value })}/>
              </div>
            )}
            {cfg.mode === 'ollama' && (
              <div className="set-field">
                <div className="set-label">
                  Ollama Model
                  {!isPro && <span className="set-label-hint">cloud models require Pro</span>}
                </div>

                {/* Server-side status banner — makes it clear this is on the
                    deployment host (the RPi), not the user's machine. */}
                <div className={'ollama-banner' + (
                  ollamaStatus == null      ? ' ob-loading'
                  : !ollamaStatus.running   ? ' ob-down'
                  : ollamaStatus.pull?.status === 'pulling' || ollamaStatus.pull?.status === 'starting' ? ' ob-pulling'
                  : !ollamaStatus.pulled    ? ' ob-missing'
                  : ' ob-ok'
                )}>
                  <div className="ob-row">
                    <span className="ob-tag">SERVER</span>
                    <code className="ob-host">{ollamaStatus?.host || 'http://localhost:11434'}</code>
                    <span className="ob-state">
                      {ollamaStatus == null
                        ? <><span className="spin" style={{ width:9, height:9, borderWidth:1.5 }}/> CHECKING</>
                        : !ollamaStatus.running
                          ? <>● OFFLINE</>
                          : ollamaStatus.pull?.status === 'pulling' || ollamaStatus.pull?.status === 'starting'
                            ? <>● PULLING {typeof ollamaStatus.pull?.percent === 'number' ? `${ollamaStatus.pull.percent}%` : ''}</>
                            : ollamaStatus.pulled
                              ? <>● READY</>
                              : <>● MODEL MISSING</>}
                    </span>
                  </div>
                  {ollamaStatus?.pull?.status === 'pulling' || ollamaStatus?.pull?.status === 'starting' ? (
                    <div className="ob-progress">
                      <div className="ob-progress-bar" style={{ width: `${Math.max(2, ollamaStatus?.pull?.percent || 2)}%` }}/>
                      <div className="ob-progress-stage">
                        Pulling <code>{ollamaStatus?.pull?.model || cfg.ollama_model}</code> · {ollamaStatus?.pull?.stage || 'starting'}
                      </div>
                    </div>
                  ) : null}
                  {!ollamaStatus?.running && (
                    <div className="ob-help">
                      The deployment server can't reach Ollama. On the RPi, run <code>ollama serve</code>
                      {' '}— or set <code>OLLAMA_URL</code> to point at the host that's running it.
                    </div>
                  )}
                  {ollamaStatus?.running && !ollamaStatus?.pulled && ollamaStatus?.pull?.status !== 'pulling' && ollamaStatus?.pull?.status !== 'starting' && (
                    <div className="ob-help">
                      Model <code>{cfg.ollama_model}</code> isn't pulled on the server yet — auto-pull starting…
                    </div>
                  )}
                </div>

                <select className="set-select" value={cfg.ollama_model || ''}
                  disabled={!ollamaModels.length}
                  onChange={e => {
                    const name = e.target.value;
                    if (isCloudModel(name) && !isPro) {
                      setPlanError('Cloud models require the Pro plan');
                      return;
                    }
                    update({ ollama_model: name });
                  }}>
                  {ollamaModels.length > 0 ? (() => {
                    const local = ollamaModels.filter(m => !isCloudModel(m.name));
                    const cloud = ollamaModels.filter(m => isCloudModel(m.name));
                    return <>
                      {local.length > 0 && <optgroup label="Local">
                        {local.map(m => (
                          <option key={m.name} value={m.name}>
                            {m.name}{m.size_gb ? `  ·  ${m.size_gb} GB` : ''}{m.params ? `  ·  ${m.params}` : ''}
                          </option>
                        ))}
                      </optgroup>}
                      {cloud.length > 0 && <optgroup label={`Cloud${isPro ? '' : ' — Pro'}`}>
                        {cloud.map(m => (
                          <option key={m.name} value={m.name} disabled={!isPro}>
                            {m.name}{!isPro ? ' — Pro' : ''}
                          </option>
                        ))}
                      </optgroup>}
                    </>;
                  })()
                  : <option>{ollamaStatus == null ? 'Loading models…' : ollamaStatus.running ? 'No models pulled yet' : 'Ollama offline'}</option>}
                </select>
                <div className="set-helper" style={{ marginTop:6 }}>
                  Ollama is hosted on the deployment server. Browser ↔ FastAPI traffic goes over your Tailnet Funnel; FastAPI ↔ Ollama stays internal on the host.
                </div>
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

          {/* Job Discovery — controls how many ranked rows Phase 2 pulls
              from the local jobs index. The DB stays the same size; this
              just sets the cap on how many of the highest-relevance rows
              the agent examines per run. */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="briefcase" size={14}/> Job Discovery</div>
            <div className="set-field">
              <div className="set-row">
                <div className="set-label">Max jobs per discovery run</div>
                <span className="set-range-val">{cfg.max_scrape_jobs ?? 50}</span>
              </div>
              <input type="range" className="set-range" min="10" max="200" step="10"
                value={cfg.max_scrape_jobs ?? 50}
                onChange={e => update({ max_scrape_jobs: parseInt(e.target.value) })}/>
              <div className="set-helper">
                Phase 2 reads ranked results from the live jobs index (BM25 + skill overlap + freshness + title match).
                Higher = more candidates for Phase 3 to score; lower = faster runs.
              </div>
            </div>
            <div className="set-field">
              <div className="set-row">
                <div className="set-label">Posting age window (days)</div>
                <span className="set-range-val">{cfg.days_old ?? 30}</span>
              </div>
              <input type="range" className="set-range" min="1" max="90" step="1"
                value={cfg.days_old ?? 30}
                onChange={e => update({ days_old: parseInt(e.target.value) })}/>
              <div className="set-helper">Drops postings older than N days.</div>
            </div>
          </div>

          {/* General User Settings */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="user" size={14}/> General Settings</div>
            <Toggle field="light_mode" label="Light mode" sub="Switch to a light color theme."/>
          </div>

          {/* Account/Data */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="database" size={14}/> Data Management</div>
            <button
              className="btn-ghost"
              style={{ width:'100%', justifyContent:'flex-start', color:'var(--bad)', opacity: resetting ? 0.6 : 1 }}
              onClick={handleReset}
              disabled={resetting}
            >
              {resetting
                ? <><span className="spin"/> Resetting…</>
                : <><Icon name="trash-2" size={14}/> Reset all data</>}
            </button>
            <div className="set-helper" style={{ marginTop:8 }}>This will clear your resume, jobs, and all application data permanently. Your account stays signed in.</div>
            {resetError && (
              <div className="set-helper" style={{ marginTop:6, color:'var(--bad)' }}>
                <Icon name="alert-circle" size={12}/> {resetError}
              </div>
            )}
          </div>
        </div>
      </div>
    </>
  );
}

/* ══════════════════════════════════════════════════════════
   PLANS PAGE — Free vs Pro. Stub until Stripe lands; the upgrade
   CTA is informational and admins flip plan_tier from Dev Ops.
══════════════════════════════════════════════════════════ */
function PlansPage({ state, setPage }) {
  const isPro = !!state?.is_pro;
  const tier = state?.plan_tier || 'free';
  const [contactSent, setContactSent] = useState(false);

  // Stripe-backed checkout/portal handlers are wired on the backend already
  // (see /api/billing/* in app.py). This page deliberately keeps using the
  // feedback-stub flow until the Stripe account is fully provisioned —
  // swap the JSX below back to the startCheckout / openPortal versions
  // once STRIPE_SECRET_KEY + STRIPE_PRICE_ID_PRO_MONTHLY are set.
  const requestUpgrade = () => {
    setContactSent(true);
    api.post('/api/feedback', {
      message: `Upgrade request from ${state?.user?.email || 'unknown'} — wants Pro plan.`,
      kind: 'upgrade_request',
    }).catch(() => {});
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Billing</div>
          <div className="page-title-big">Plans</div>
        </div>
        <div className="head-spacer"/>
        <div className="plan-current-pill">
          <span className={'plan-dot ' + (isPro ? 'pro' : 'free')}/>
          You're on <b>{isPro ? 'Pro' : 'Free'}</b>
        </div>
      </div>

      <div className="page-body solo plans-wrap">
        <div className="plans-eyebrow">
          <Icon name="zap" size={11}/> Free runs the local models on our server. Pro unlocks the high-quality cloud models.
        </div>

        <div className="plans-grid">
          {/* FREE */}
          <div className={'plan-card' + (tier === 'free' ? ' current' : '')}>
            {tier === 'free' && <div className="plan-current-badge">Current plan</div>}
            <div className="plan-card-h">
              <div className="plan-name">Free</div>
              <div className="plan-price"><b>$0</b><span>/forever</span></div>
            </div>
            <div className="plan-tag">Local Ollama models on our server</div>
            <ul className="plan-features">
              <li><Icon name="check" size={13}/> Local LLMs — small open-weight models hosted on the Pi</li>
              <li><Icon name="check" size={13}/> Full 7-phase pipeline</li>
              <li><Icon name="check" size={13}/> Excel tracker + run reports</li>
              <li><Icon name="check" size={13}/> Job discovery across 22+ sources</li>
              <li><Icon name="check" size={13}/> Cover letter generation (template)</li>
              <li className="plan-feature-muted"><Icon name="x" size={13}/> High-quality cloud models</li>
              <li className="plan-feature-muted"><Icon name="clock" size={13}/> Anthropic Claude — coming soon</li>
            </ul>
            <button className="plan-cta plan-cta-ghost" disabled>
              {tier === 'free' ? 'Active' : 'Downgrade'}
            </button>
          </div>

          {/* PRO */}
          <div className={'plan-card plan-card-pro' + (tier === 'pro' ? ' current' : '')}>
            {tier === 'pro' && <div className="plan-current-badge plan-current-badge-pro">Current plan</div>}
            <div className="plan-glow"/>
            <div className="plan-card-h">
              <div className="plan-name">Pro</div>
              <div className="plan-price"><b>$4</b><span>/month</span></div>
            </div>
            <div className="plan-tag">High-quality cloud models — sharper scoring &amp; tailoring</div>
            <ul className="plan-features">
              <li><Icon name="check" size={13}/> Everything in Free</li>
              <li className="plan-feature-hi"><Icon name="sparkles" size={13}/> Cloud Ollama models unlocked (frontier-class quality)</li>
              <li><Icon name="check" size={13}/> Higher-fidelity scoring &amp; tailoring</li>
              <li><Icon name="check" size={13}/> Sharper résumé critique &amp; ATS gap analysis</li>
              <li><Icon name="check" size={13}/> Faster, more reliable runs (no Pi-hardware ceiling)</li>
              <li><Icon name="check" size={13}/> Priority support</li>
              <li className="plan-feature-muted"><Icon name="clock" size={13}/> Anthropic Claude — coming soon, included when it launches</li>
            </ul>
            {tier === 'pro' ? (
              <button className="plan-cta plan-cta-ghost" disabled>Active</button>
            ) : contactSent ? (
              <div className="plan-cta-sent">
                <Icon name="check-circle-2" size={14}/>
                Got it — we'll be in touch.
              </div>
            ) : (
              <button className="plan-cta plan-cta-pro" onClick={requestUpgrade}>
                <Icon name="zap" size={13}/> Request upgrade
              </button>
            )}
            <div className="plan-helper">
              Stripe checkout coming soon. For now, request upgrade and an admin flips you live.
            </div>
          </div>
        </div>

        <div className="plans-faq">
          <div className="plans-faq-h">FAQ</div>
          <details className="plans-faq-item" open>
            <summary>What's the difference between local and cloud models?</summary>
            <div>
              The Free tier uses small open-weight models hosted directly on our Pi server —
              fast, private, and free, but the reasoning depth is limited. Pro upgrades you to
              the cloud-hosted Ollama Turbo models (proxied through the same daemon), which
              are dramatically larger and produce noticeably better scoring rubrics, tailoring,
              and résumé critique. Same pipeline, much sharper output.
            </div>
          </details>
          <details className="plans-faq-item">
            <summary>When is Claude available?</summary>
            <div>
              Anthropic Claude is under active development and will launch at a future date.
              Pro subscribers will get access automatically when it ships — no separate upgrade
              or API key needed on your end.
            </div>
          </details>
          <details className="plans-faq-item">
            <summary>Can I cancel anytime?</summary>
            <div>Yes — once Stripe billing is wired in, you'll have a self-serve customer portal. Today, contact the admin.</div>
          </details>
          <details className="plans-faq-item">
            <summary>Where do the models actually run?</summary>
            <div>
              Both tiers go through the same Ollama daemon on our Pi server. Free uses local
              models pulled to disk; Pro routes through Ollama Turbo, where the daemon
              transparently proxies your request to Ollama's hosted servers and streams the
              answer back. Either way, you don't need to install or run anything yourself.
            </div>
          </details>
        </div>

        <div className="plans-back">
          <button className="btn-ghost" onClick={() => setPage && setPage('settings')}>
            <Icon name="arrow-left" size={13}/> Back to Settings
          </button>
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
        <div style={{ fontSize:20, fontWeight:600 }}>Thank You</div>
        <div style={{ fontSize:15.5, color:'var(--t2)', maxWidth:400, textAlign:'center', lineHeight:1.55, marginTop:8 }}>
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
              <h2 style={{ fontSize:22, fontWeight:700, color:'var(--t1)' }}>Tell us what you think</h2>
              <p style={{ fontSize:16.5, color:'var(--t2)', marginTop:8, lineHeight:1.6 }}>
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

/* Dev console — operator surface */
const DEV_TABS = [
  { id:'overview', label:'Overview',  icon:'gauge',              hint:'health, metrics & recent users' },
  { id:'sessions', label:'Users',     icon:'users',              hint:'inspect, impersonate, manage plans' },
  { id:'server',   label:'Server',    icon:'sliders-horizontal', hint:'runtime flags, LLM, pipeline knobs' },
  { id:'console',  label:'Console',   icon:'terminal',           hint:'sandboxed CLI + event log' },
  { id:'tweaks',   label:'Tweaks',    icon:'wand-sparkles',      hint:'per-session UI experiments' },
];

/* ── Dev Ops helpers ─────────────────────────────────────────────────────
   Uptime formatter + htop-style per-core CPU panel. Pulled out of DevPage
   to keep that component readable; both are pure, so they re-render only
   when their props change. */
function _formatUptime(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `${d}d ${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${sec.toString().padStart(2, '0')}`;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${sec.toString().padStart(2, '0')}`;
}

/* Live mirror of the running app.py terminal. SSE-driven; stdout / stderr
   / Python `logging` records are tagged separately so the panel can color
   them (white for stdout, red for stderr, dim cyan for logger lines). */
function _appendLogs(prev, fresh, cap) {
  if (!fresh.length) return prev;
  const seen = new Set(prev.map(r => r.seq));
  const add  = fresh.filter(r => !seen.has(r.seq));
  if (!add.length) return prev;
  const merged = prev.concat(add);
  return merged.length > cap ? merged.slice(merged.length - cap) : merged;
}

function _logTimestamp(ts) {
  if (!ts) return '--:--:--';
  const d = new Date(ts * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function _logStreamLabel(stream) {
  if (!stream) return 'out';
  if (stream === 'stdout') return 'out';
  if (stream === 'stderr') return 'err';
  if (stream.startsWith('logger:')) return stream.slice(7);
  return stream;
}

function DevServerLogPanel({ active }) {
  const [logs, setLogs] = useState([]);
  const [paused, setPaused] = useState(false);
  const [autoScroll, setAutoScroll] = useState(true);
  const [filter, setFilter] = useState('');
  const [streamErr, setStreamErr] = useState(null);
  const scrollRef = useRef(null);
  const seqRef = useRef(0);

  // SSE connection lifecycle: open whenever the panel's parent is the
  // active sub-page AND the user hasn't paused.  Closing on tab-switch
  // saves a long-lived TCP connection per session.
  useEffect(() => {
    if (!active || paused) return undefined;

    api.get(`/api/dev/logs?since=${seqRef.current}&limit=2000`)
      .then(res => {
        const fresh = res?.logs || [];
        if (!fresh.length) return;
        setLogs(prev => _appendLogs(prev, fresh, 4000));
        seqRef.current = res.latest_seq || seqRef.current;
        setStreamErr(null);
      })
      .catch(() => {/* SSE will catch up if backfill fails */});

    let es;
    try {
      es = new EventSource(`/api/dev/logs/stream?since=${seqRef.current}`);
    } catch (e) {
      setStreamErr(`Stream init failed: ${e.message}`);
      return undefined;
    }
    es.addEventListener('log', ev => {
      try {
        const rec = JSON.parse(ev.data || '{}');
        if (!rec || !rec.line) return;
        seqRef.current = Math.max(seqRef.current, rec.seq || 0);
        setLogs(prev => _appendLogs(prev, [rec], 4000));
      } catch (_) {/* drop malformed records */}
    });
    es.addEventListener('ping', () => {/* keepalive */});
    es.onerror = () => {
      setStreamErr('Reconnecting…');
      setTimeout(() => setStreamErr(null), 4000);
    };

    return () => { try { es.close(); } catch (_) {} };
  }, [active, paused]);

  // Pin to bottom on every new line unless the user scrolled up.
  useEffect(() => {
    if (!autoScroll) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [logs, autoScroll]);

  const onScroll = () => {
    const el = scrollRef.current; if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 12;
    setAutoScroll(atBottom);
  };

  const filtered = useMemo(() => {
    if (!filter.trim()) return logs;
    const needle = filter.toLowerCase();
    return logs.filter(r => (r.line || '').toLowerCase().includes(needle));
  }, [logs, filter]);

  const copyAll = () => {
    const text = (filtered.length ? filtered : logs)
      .map(r => `${_logTimestamp(r.ts)} ${r.stream} ${r.line}`)
      .join('\n');
    navigator.clipboard?.writeText(text);
  };

  return (
    <div className="dop-panel dev-log-panel">
      <DevSecHead
        title="Server log"
        hint={paused
          ? 'paused — live tail off'
          : (streamErr || `live · ${logs.length} line${logs.length === 1 ? '' : 's'}`)}
        meta={(filter ? `${filtered.length} match${filtered.length === 1 ? '' : 'es'} · ` : '')
              + 'echoes app.py terminal output'}
      />
      <div className="dev-log-toolbar">
        <input
          type="text" className="dev-log-filter" placeholder="filter…"
          value={filter} onChange={e => setFilter(e.target.value)}/>
        <button className={'dev-log-btn' + (paused ? ' active' : '')}
                onClick={() => setPaused(p => !p)}
                title={paused ? 'Resume live tail' : 'Pause live tail'}>
          <Icon name={paused ? 'play' : 'pause'} size={11}/>
          {paused ? 'Resume' : 'Pause'}
        </button>
        <button className={'dev-log-btn' + (autoScroll ? ' active' : '')}
                onClick={() => setAutoScroll(s => !s)}
                title="Pin to bottom on new lines">
          <Icon name="arrow-down" size={11}/> Follow
        </button>
        <button className="dev-log-btn" onClick={() => setLogs([])}
                title="Clear local view (server ring keeps the lines)">
          <Icon name="trash-2" size={11}/> Clear
        </button>
        <button className="dev-log-btn" onClick={copyAll} title="Copy visible lines">
          <Icon name="copy" size={11}/> Copy
        </button>
      </div>
      <div className="dev-log-body" ref={scrollRef} onScroll={onScroll}>
        {filtered.length === 0 && (
          <div className="dev-log-empty">
            {filter ? 'No lines match the filter.' : 'Waiting for terminal output…'}
          </div>
        )}
        {filtered.map(r => {
          const stream = r.stream || 'stdout';
          const cls = stream === 'stderr' ? 'dev-log-line stream-stderr'
                    : stream.startsWith('logger:') ? 'dev-log-line stream-logger'
                    : 'dev-log-line stream-stdout';
          return (
            <div key={r.seq} className={cls}>
              <span className="dev-log-ts">{_logTimestamp(r.ts)}</span>
              <span className="dev-log-stream">{_logStreamLabel(stream)}</span>
              <span className="dev-log-text">{r.line}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}


function HtopCpuPanel({ metrics }) {
  const cpu  = metrics?.cpu  || null;
  const mem  = metrics?.memory || null;
  const temp = metrics?.cpu_temp || null;
  const cores = cpu?.cores || [];
  const loadAvg = cpu?.load_avg || null;
  const error = cpu?.error;

  if (error && !cores.length) {
    return (
      <div className="dop-panel">
        <DevSecHead title="System resources" hint="psutil unavailable"/>
        <div className="dop-empty" style={{ padding:'16px 14px', color:'var(--t3)', fontSize:13 }}>
          {error.includes('not installed')
            ? 'psutil not installed — pip install psutil to see live CPU + memory usage.'
            : `psutil error: ${error}`}
        </div>
      </div>
    );
  }

  return (
    <div className="dop-panel">
      <DevSecHead
        title={`System resources · ${cores.length} core${cores.length === 1 ? '' : 's'}`}
        hint="user · system · iowait · idle (htop-style)"/>
      <div className="htop-cpu">
        {cores.map((c, i) => {
          const total = Math.max(0, Math.min(100, Number(c.total) || 0));
          const u = Math.max(0, Math.min(100, Number(c.user)   || 0));
          const s = Math.max(0, Math.min(100, Number(c.system) || 0));
          const w = Math.max(0, Math.min(100, Number(c.iowait) || 0));
          const tone = total >= 85 ? 'crit' : total >= 60 ? 'warn' : 'ok';
          return (
            <div key={i} className="htop-row">
              <span className="htop-lbl">{String(i).padStart(2, '0')}</span>
              <div className="htop-track" title={`Core ${i}: user ${u}% · sys ${s}% · iowait ${w}% · idle ${c.idle}%`}>
                <div className="htop-seg htop-user"   style={{ width:`${u}%` }}/>
                <div className="htop-seg htop-system" style={{ width:`${s}%` }}/>
                <div className="htop-seg htop-iowait" style={{ width:`${w}%` }}/>
              </div>
              <span className={'htop-pct htop-pct-' + tone}>{Math.round(total)}<i>%</i></span>
            </div>
          );
        })}
      </div>

      {/* Memory bar — single track using the same paint vocabulary so the
          eye recognizes it as part of the same panel. */}
      {mem && mem.total_mb != null && (
        <div className="htop-mem">
          <span className="htop-mem-lbl">Mem</span>
          <div className="htop-track">
            <div className="htop-seg htop-user" style={{ width:`${Math.min(100, mem.percent)}%` }}/>
          </div>
          <span className="htop-mem-num">
            {Math.round((mem.used_mb || 0) / 1024 * 10) / 10}<i>G</i>
            <em>/</em>
            {Math.round((mem.total_mb || 0) / 1024 * 10) / 10}<i>G</i>
          </span>
        </div>
      )}

      {loadAvg && loadAvg.length === 3 && (
        <div className="htop-load">
          <span>load</span>
          <b>{loadAvg[0].toFixed(2)}</b>
          <i>·</i>
          <b>{loadAvg[1].toFixed(2)}</b>
          <i>·</i>
          <b>{loadAvg[2].toFixed(2)}</b>
          <span className="htop-load-hint">1 / 5 / 15 min</span>
        </div>
      )}

      {/* CPU temperature row — psutil's sensors_temperatures(). Hidden
          on platforms (macOS, locked-down Windows) where the OS doesn't
          expose thermal zones to userspace. Color tone follows the same
          ok/warn/crit ladder used by the per-core total. */}
      {temp && Number.isFinite(temp.current) && (() => {
        const t = temp.current;
        const high = temp.high || 80;
        const crit = temp.critical || 100;
        const tone = t >= crit - 5 ? 'crit' : t >= high - 8 ? 'warn' : 'ok';
        return (
          <div className={'htop-temp htop-temp-' + tone}>
            <Icon name="thermometer" size={11}/>
            <span>cpu temp</span>
            <b>{t.toFixed(1)}<i>°C</i></b>
            <span className="htop-temp-meta" title={`${temp.label} (${temp.source})`}>
              {temp.high ? `high ${temp.high}° / crit ${temp.critical || '?'}°` : temp.label}
            </span>
          </div>
        );
      })()}

      {/* Legend. Mirrors the htop convention so anyone who's used a
          terminal knows immediately what each color means. */}
      <div className="htop-legend">
        <span><i className="htop-dot htop-user"/> user</span>
        <span><i className="htop-dot htop-system"/> system</span>
        <span><i className="htop-dot htop-iowait"/> iowait</span>
      </div>
    </div>
  );
}


/* Top-5 processes by CPU + memory, side-by-side. Each row is a single
   "fuel-gauge" line: the colored bar lives in the row's BACKGROUND and
   the process name + pid + value read on top of it. Compact and dense
   without the cramped 4-column grid the previous design used. */
function TopProcessesPanel({ processes }) {
  const TOP = 5;
  const byCpu = (processes?.by_cpu || []).slice(0, TOP);
  const byMem = (processes?.by_mem || []).slice(0, TOP);
  const error = processes?.error;
  const total = processes?.total || 0;

  if (error) {
    return (
      <div className="dop-panel">
        <DevSecHead title="Top processes" hint="psutil unavailable"/>
        <div className="dop-empty" style={{ padding:'16px 14px', color:'var(--t3)', fontSize:13 }}>
          {error.includes('not installed')
            ? 'psutil not installed — pip install psutil to see the process table.'
            : `psutil error: ${error}`}
        </div>
      </div>
    );
  }
  if (!processes || (!byCpu.length && !byMem.length)) {
    return (
      <div className="dop-panel">
        <DevSecHead title="Top processes" hint="sampling…"/>
        <div className="proc-skel">
          {[0,1,2,3,4].map(i => <div key={i} className="proc-skel-row" style={{animationDelay: `${i*120}ms`}}/>)}
        </div>
      </div>
    );
  }

  const renderRow = (p, kind, idx) => {
    const v = kind === 'cpu' ? p.cpu : p.mem_pct;
    const tone = v >= 60 ? 'crit' : v >= 25 ? 'warn' : 'ok';
    const display = kind === 'cpu'
      ? `${p.cpu.toFixed(1)}%`
      : (p.mem_mb >= 1024
          ? `${(p.mem_mb / 1024).toFixed(2)} GB`
          : `${p.mem_mb.toFixed(0)} MB`);
    return (
      <li
        key={p.pid}
        className={'proc-row proc-tone-' + tone}
        style={{ animationDelay: `${idx * 35}ms`, '--fill': `${Math.min(100, Math.max(0, v))}%` }}
        title={`${p.name || '(unnamed)'} · pid ${p.pid} · ${p.user || 'unknown user'}`}>
        <span className="proc-rank">{String(idx + 1).padStart(2, '0')}</span>
        <span className="proc-name">{p.name || '(unnamed)'}</span>
        <span className="proc-pid">{p.pid}</span>
        <span className="proc-num">{display}</span>
      </li>
    );
  };

  return (
    <div className="dop-panel">
      <DevSecHead
        title="Top processes"
        hint={`top 5 of ${total} · sampled ${processes.sampled_at ? new Date(processes.sampled_at).toLocaleTimeString() : 'just now'}`}
      />
      <div className="proc-grid">
        <div className="proc-col">
          <div className="proc-col-h"><Icon name="cpu" size={11}/> CPU</div>
          <ul className="proc-list">
            {byCpu.map((p, i) => renderRow(p, 'cpu', i))}
          </ul>
        </div>
        <div className="proc-col">
          <div className="proc-col-h"><Icon name="memory-stick" size={11}/> Memory</div>
          <ul className="proc-list">
            {byMem.map((p, i) => renderRow(p, 'mem', i))}
          </ul>
        </div>
      </div>
    </div>
  );
}


function DevPage({ state: globalState, refresh: globalRefresh }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [devTab, setDevTab] = useState('overview');
  const [selected, setSelected] = useState(null);
  const [fullState, setFullState] = useState(null);
  const [cli, setCli] = useState({ command:'git_status', output:'', running:false });
  const [tweaks, setTweaks] = useState(null);
  const [loadingFull, setLoadingFull] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [runtime, setRuntime] = useState(null);
  const [apiKeyDraft, setApiKeyDraft] = useState('');
  const [savingKey, setSavingKey] = useState(false);
  const [reloadFlash, setReloadFlash] = useState(null);
  const [now, setNow] = useState(() => new Date());
  const [planFlash, setPlanFlash] = useState(null);
  // Live system metrics — driven by the dedicated /api/dev/metrics
  // fast-path endpoint so we can render htop-style CPU bars at a
  // 2-second cadence without re-fetching the heavier /api/dev/overview
  // payload (which iterates every session row).
  const [metrics, setMetrics] = useState(null);

  const setPlanTier = async (userId, tier) => {
    if (!userId) return;
    try {
      await api.post(`/api/dev/users/${userId}/plan`, { tier });
      setPlanFlash({ userId, tier, kind: 'ok' });
      refresh();
    } catch (e) {
      setPlanFlash({ userId, tier, kind: 'err', message: e.message });
    }
    setTimeout(() => setPlanFlash(null), 2400);
  };

  // Live tick for the ops-bar clock
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

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

  // Live psutil tick — every 2s while the Overview or Server tab is
  // active. The Server tab opts into the per-process snapshot (heavier:
  // ~250ms server-side) so the top-N tables can populate; Overview no
  // longer renders htop bars so it just needs the cheap cpu/memory.
  useEffect(() => {
    if (devTab !== 'overview' && devTab !== 'server') return undefined;
    const wantsProcesses = devTab === 'server';
    let cancelled = false;
    const tick = () => {
      const url = '/api/dev/metrics' + (wantsProcesses ? '?with_processes=1' : '');
      api.get(url)
        .then(m => { if (!cancelled) setMetrics(m); })
        .catch(() => {/* silent — last cached value remains visible */});
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [devTab]);

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
    await api.post('/api/config', { force_customer_mode: true });
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

  const loadRuntime = useCallback(async () => {
    try { setRuntime(await api.get('/api/dev/runtime')); }
    catch (e) { /* dev permission failed — silent */ }
  }, []);
  useEffect(() => { loadRuntime(); }, [loadRuntime]);

  const setRuntimeFlag = async (key, value) => {
    setRuntime(r => r ? { ...r, runtime: { ...r.runtime, [key]: value } } : r);
    try {
      const res = await api.post('/api/dev/runtime', { [key]: value });
      setRuntime(r => r ? { ...r, runtime: res.runtime } : r);
    } catch (e) { loadRuntime(); }
  };

  const saveSessionConfig = async patch => {
    await api.post('/api/config', patch);
    globalRefresh?.();
    loadRuntime();
  };

  const saveApiKey = async () => {
    if (!apiKeyDraft.trim()) return;
    setSavingKey(true);
    try {
      await api.post('/api/config', { api_key: apiKeyDraft.trim(), mode: 'anthropic' });
      setApiKeyDraft('');
      globalRefresh?.();
    } finally { setSavingKey(false); }
  };

  const reloadEnv = async () => {
    setReloadFlash({ kind: 'pending', text: 'Reloading…' });
    try {
      const res = await api.post('/api/dev/reload-env', {});
      setReloadFlash({
        kind: res.anthropic_key_present ? 'ok' : 'warn',
        text: res.anthropic_key_present
          ? `Loaded · ANTHROPIC_API_KEY ${res.anthropic_key_present ? 'present' : 'missing'}`
          : 'Reloaded but ANTHROPIC_API_KEY still missing',
      });
      loadRuntime();
    } catch (e) {
      setReloadFlash({ kind: 'err', text: e.message || 'Reload failed' });
    }
    setTimeout(() => setReloadFlash(null), 4500);
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
      <div style={{ fontSize:20, fontWeight:600 }}>Developer Access Required</div>
      <div style={{ fontSize:15.5, color:'var(--t2)', maxWidth:400, textAlign:'center', lineHeight:1.55, marginTop:8 }}>
        This page is restricted to accounts marked as developers. Ask an administrator to set <code>users.is_developer = 1</code> on your account.
      </div>
    </div>
  );

  if (!data) return (
    <div className="placeholder-page">
      <span className="spin"/>
      <div style={{ color:'var(--t2)' }}>Loading dev console...</div>
    </div>
  );

  const isImpersonating = typeof document !== 'undefined' && document.cookie.includes('dev_impersonate_id');
  const errorCount = sessions.reduce((acc, s) => acc + Object.values(s.errors || {}).filter(Boolean).length, 0);
  const opsHealth = errorCount === 0 ? 'ok' : (errorCount < 3 ? 'warn' : 'bad');
  const clock = now.toTimeString().slice(0, 8);
  const dateStamp = now.toISOString().slice(0, 10);

  return (
    <div className="dop-shell">
      <span className="dop-grain" aria-hidden="true"/>

      {/* ── Ops bar ───────────────────────────────────────────────── */}
      <div className="dop-opsbar">
        <div className="dop-opsbar-left">
          <span className="dop-brand">Dev Console</span>
          <span className={'dop-pulse dop-pulse-' + opsHealth} title={errorCount + ' open error(s) across active sessions'}>
            <span className="dop-dot"/>
            {opsHealth === 'ok' ? 'All systems normal' : opsHealth === 'warn' ? `${errorCount} issue${errorCount === 1 ? '' : 's'}` : `${errorCount} alerts`}
          </span>
          {isImpersonating && (
            <span className="dop-pulse dop-pulse-warn">
              <Icon name="eye" size={10}/> Viewing as user
            </span>
          )}
        </div>
        <div className="dop-opsbar-meta">
          <span title="Local time"><i>Time</i><b>{clock}</b></span>
          <span title="Today's date (UTC)"><i>Date</i><b>{dateStamp}</b></span>
          <span title="Python version"><i>Python</i><b>{status.python || '—'}</b></span>
          <span title="Generated artifacts in output/"><i>Outputs</i><b>{status.output_files ?? 0}</b></span>
          <span title="Session DB file size"><i>DB</i><b>{status.session_db_mb ?? 0} MB</b></span>
          <span title="Free disk space"><i>Disk</i><b>{status.disk_free_gb ?? 0} GB</b></span>
        </div>
        <div className="dop-opsbar-right">
          {isImpersonating && (
            <button className="dop-btn dop-btn-warn" onClick={stopImpersonating} title="Stop viewing as that user">
              <Icon name="user-minus" size={11}/> Stop
            </button>
          )}
          <button className="dop-btn" onClick={testAsCustomer} title="See the app the way a free-tier customer does">
            <Icon name="user" size={11}/> As customer
          </button>
          <button className="dop-btn" onClick={refresh} disabled={refreshing} title="Reload metrics + sessions (auto-refreshes every 10s)">
            {refreshing ? <span className="spin" style={{ width:11, height:11, borderWidth:2 }}/> : <Icon name="refresh-cw" size={11}/>}
            Refresh
          </button>
        </div>
      </div>

      {/* ── Sub-nav ───────────────────────────────────────────────── */}
      <nav className="dop-tabs" role="tablist" aria-label="Dev sub-pages">
        {DEV_TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={devTab === t.id}
            className={'dop-tab' + (devTab === t.id ? ' on' : '')}
            onClick={() => setDevTab(t.id)}
            title={t.hint}>
            <Icon name={t.icon} size={13}/>
            <span className="dop-tab-label">{t.label}</span>
          </button>
        ))}
        <span className="dop-tabs-hint">{DEV_TABS.find(t => t.id === devTab)?.hint}</span>
      </nav>

      {/* ── Sub-page body ────────────────────────────────────────── */}
      <div className="dop-body">

        {/* OVERVIEW — at-a-glance health, system snapshot, recent users */}
        {devTab === 'overview' && (
          <div className="dop-page fade-in">
            <DevSecHead
              title="Key metrics"
              hint="counts roll up across every authenticated user"
              meta={refreshing ? 'updating now…' : 'auto-refreshing every 10s'}
            />
            <div className="dop-kpis">
              <DevKpi label="Users"        value={summary.users || 0}        icon="users"/>
              <DevKpi label="Resumes"      value={summary.with_resume || 0}  icon="file-check-2"/>
              <DevKpi label="Applications" value={summary.applications || 0} icon="send"/>
              <DevKpi label="Applied"      value={summary.applied || 0}      icon="check-circle-2"/>
              <DevKpi label="Manual"       value={summary.manual || 0}       icon="hand"/>
              <DevKpi label="Errors"       value={summary.errors || 0}       icon="alert-triangle" warn={summary.errors > 0}/>
            </div>

            <div className="dop-overview-grid">
              <div className="dop-panel">
                <DevSecHead title="System" hint="server process snapshot"/>
                <div className="dop-keyval">
                  <div><span>App status</span><b className={'tag tag-' + (status.app === 'running' ? 'ok' : 'bad')}>{status.app || '—'}</b></div>
                  <div title={(metrics?.server_started_at || status.server_started_at || '') + ' (uvicorn process boot — resets on systemctl restart)'}>
                    <span>Process uptime</span>
                    <b>{_formatUptime(metrics?.server_uptime_s ?? status.server_uptime_s ?? 0)}</b>
                  </div>
                  {(() => {
                    const osUp = metrics?.os_uptime_s ?? status.os_uptime_s;
                    if (osUp == null) return null;
                    const osBoot = metrics?.os_boot_at || status.os_boot_at || '';
                    return (
                      <div title={osBoot ? `host booted ${osBoot}` : 'host kernel uptime'}>
                        <span>Host uptime</span>
                        <b>{_formatUptime(osUp)}</b>
                      </div>
                    );
                  })()}
                  <div><span>Disk free</span><b>{status.disk_free_gb ?? 0} GB</b></div>
                </div>
                <div className="dop-sys-foot">
                  <span><i>py</i><b>{status.python || '—'}</b></span>
                  <span><i>db</i><b>{status.session_db_mb ?? 0} MB</b></span>
                  <span><i>files</i><b>{status.output_files ?? 0}</b></span>
                  <button className="dop-btn dop-btn-link dop-sys-link" onClick={() => setDevTab('server')}>
                    <Icon name="cpu" size={11}/> Live resources →
                  </button>
                </div>
              </div>

              <div className="dop-panel">
                <DevSecHead title="Environment" hint="secrets & runtime flags"/>
                <div className="dop-keyval">
                  <div><span>Anthropic API key</span><b className={'tag tag-' + (runtime?.env?.anthropic_key_present ? 'ok' : 'bad')}>{runtime?.env?.anthropic_key_present ? 'Present' : 'Missing'}</b></div>
                  <div><span>SMTP</span><b className={'tag tag-' + (runtime?.env?.smtp_configured ? 'ok' : 'mid')}>{runtime?.env?.smtp_configured ? 'Configured' : 'Unset'}</b></div>
                  <div><span>Ollama URL</span><b className="tag tag-mid" title={runtime?.env?.ollama_url || ''}>{runtime?.env?.ollama_url || '—'}</b></div>
                  <div><span>Local dev bypass</span><b className={'tag tag-' + (runtime?.env?.local_dev_bypass ? 'warn' : 'mid')}>{runtime?.env?.local_dev_bypass ? 'On (insecure)' : 'Off'}</b></div>
                  <div><span>Maintenance mode</span><b className={'tag tag-' + (runtime?.runtime?.maintenance ? 'warn' : 'mid')}>{runtime?.runtime?.maintenance ? 'On' : 'Off'}</b></div>
                  <div><span>Verbose logs</span><b className={'tag tag-' + (runtime?.runtime?.verbose_logs ? 'ok' : 'mid')}>{runtime?.runtime?.verbose_logs ? 'On' : 'Off'}</b></div>
                </div>
              </div>

              <div className="dop-panel">
                <DevSecHead title="Recent users" hint="click to jump to inspector"/>
                <div className="dop-recent-users">
                  {sessions.slice(0, 6).map(s => (
                    <button key={s.id} className="dop-recent-row" onClick={() => { setSelected(s); setDevTab('sessions'); }}>
                      <span className="dop-recent-id" title={s.id}>{s.id.slice(0, 8)}</span>
                      <span className="dop-recent-name">{s.name || 'Anonymous'}</span>
                      <span className="dop-recent-phase" title={`${s.done.length} of 7 phases complete`}>{s.done.length}/7</span>
                      {s.unread_feedback_count > 0 && (
                        <span className="dop-recent-fb" title={`${s.unread_feedback_count} unread feedback`}><Icon name="message-square" size={9}/>{s.unread_feedback_count}</span>
                      )}
                    </button>
                  ))}
                  {sessions.length === 0 && <div className="dop-empty">No sessions yet — they'll appear here as users sign in.</div>}
                </div>
              </div>
            </div>

            <DevSecHead
              title="Activity"
              hint="server-side events from the last few minutes"
              extra={<button className="dop-btn dop-btn-link" onClick={() => setDevTab('console')}>View full log →</button>}
            />
            <div className="dop-events">
              {(data.events || []).slice(0, 8).map((e, i) => (
                <div key={i} className="dop-event">
                  <span>{new Date(e.ts).toLocaleTimeString()}</span>
                  <b>{e.kind}</b>
                  <p>{e.message}</p>
                </div>
              ))}
              {(data.events || []).length === 0 && <div className="dop-empty">Nothing recent. The server is quiet.</div>}
            </div>

            {/* Live mirror of the running app.py terminal. Sits at the
                bottom of the overview (full-width) since the lines are
                wide and the user typically scrolls to it intentionally. */}
            <DevServerLogPanel active={devTab === 'overview'}/>
          </div>
        )}

        {/* SESSIONS — user list + inspector */}
        {devTab === 'sessions' && (
          <div className="dop-page dop-sessions fade-in">
            <aside className="dop-userlist">
              <div className="dop-userlist-head">
                <span className="dop-userlist-title">All users</span>
                <span className="dop-userlist-count">{sessions.length}</span>
              </div>
              <div className="dop-userlist-scroll">
                {sessions.map(s => (
                  <button key={s.id}
                    className={'dop-user' + (active?.id === s.id ? ' on' : '')}
                    onClick={() => setSelected(s)}
                    title={s.email || s.id}>
                    <span className="dop-user-av">{(s.name || 'U')[0]}</span>
                    <span className="dop-user-meta">
                      <b>{s.name || 'Anonymous'}</b>
                      <small>{s.email || s.resume_filename || s.id.slice(0, 10)}</small>
                    </span>
                    <span className="dop-user-tail">
                      {s.user_id && (
                        <span className={'plan-pill-mini plan-pill-' + (s.plan_tier || 'free')}
                              title={`Plan tier: ${s.plan_tier || 'free'}`}>
                          {((s.plan_tier || 'free') === 'pro') ? 'Pro' : 'Free'}
                        </span>
                      )}
                      <em title={`${s.done.length} of 7 phases complete`}>{s.done.length}/7</em>
                      {s.unread_feedback_count > 0 && (
                        <span className="dop-user-fb" title={`${s.unread_feedback_count} unread feedback`}><Icon name="message-square" size={9}/>{s.unread_feedback_count}</span>
                      )}
                    </span>
                  </button>
                ))}
                {sessions.length === 0 && <div className="dop-empty">No sessions yet.</div>}
              </div>
            </aside>

            <section className="dop-inspect">
              {!active ? (
                <div className="dop-empty-card">
                  <Icon name="user-cog" size={22} color="var(--accent-h)"/>
                  <div>Select a user from the left to inspect.</div>
                </div>
              ) : (
                <>
                  <div className="dop-inspect-head">
                    <div className="dop-inspect-id">
                      <div className="dop-inspect-name">{active.name || 'Anonymous'}</div>
                      <div className="dop-inspect-meta-row">
                        <code className="dop-inspect-sid" title="Session ID">{active.id}</code>
                        {active.email && <span className="dop-inspect-email">{active.email}</span>}
                      </div>
                    </div>
                    <div className="dop-inspect-actions">
                      <button className="dop-btn dop-btn-warn"
                        onClick={async () => { if (confirm('Reset this session state? Files will be deleted.')) { await api.post(`/api/dev/session/${active.id}/reset`, {}); refresh(); setSelected(null); } }}
                        title="Wipe session state but keep the user account">
                        <Icon name="rotate-ccw" size={11}/> Reset session
                      </button>
                      <button className="dop-btn dop-btn-bad"
                        onClick={async () => { if (confirm('Delete this user entirely? This cannot be undone.')) { await fetch(`/api/dev/session/${active.id}`, { method:'DELETE' }); refresh(); setSelected(null); } }}
                        title="Permanently delete this user and all their data">
                        <Icon name="trash-2" size={11}/> Delete user
                      </button>
                      <button className="dop-btn dop-btn-accent" onClick={() => impersonate(active.id)}
                        title="Open the app from this user's perspective">
                        <Icon name="user-plus" size={11}/> View as user
                      </button>
                    </div>
                  </div>

                  <div className="dop-inspect-grid">
                    <div className="dop-panel dop-panel-plan">
                      <DevSecHead
                        title="Plan & billing"
                        hint="manual flip mirrors the Stripe webhook"
                        extra={active.is_developer && <span className="dop-pill-mini dop-pill-dev">Developer</span>}
                      />
                      {!active.user_id ? (
                        <div className="dop-empty">Anonymous session — no user account to bill.</div>
                      ) : (
                        <>
                          <div className="dop-keyval">
                            <div><span>Tier</span>
                              <b className={'plan-pill plan-pill-' + (active.plan_tier || 'free')}>
                                {(active.plan_tier || 'free') === 'pro' ? 'Pro' : 'Free'}
                              </b>
                            </div>
                            <div><span>Email</span><b style={{fontFamily:'var(--mono)',fontSize:11.5}}>{active.email || '—'}</b></div>
                          </div>
                          <div className="dop-plan-actions">
                            {(active.plan_tier || 'free') === 'free' ? (
                              <button className="dop-btn dop-btn-accent" onClick={() => setPlanTier(active.user_id, 'pro')}>
                                <Icon name="zap" size={11}/> Grant Pro
                              </button>
                            ) : (
                              <button className="dop-btn dop-btn-warn" onClick={() => setPlanTier(active.user_id, 'free')}>
                                <Icon name="arrow-down" size={11}/> Revoke Pro
                              </button>
                            )}
                            {planFlash?.userId === active.user_id && (
                              <span className={'dop-plan-flash dop-plan-flash-' + planFlash.kind}>
                                <Icon name={planFlash.kind === 'ok' ? 'check' : 'x'} size={10}/>
                                {planFlash.kind === 'ok'
                                  ? `Set to ${planFlash.tier}`
                                  : (planFlash.message || 'Failed')}
                              </span>
                            )}
                          </div>
                        </>
                      )}
                    </div>

                    <div className="dop-panel">
                      <DevSecHead title="Pipeline progress" hint="phase counts and discovery state"/>
                      <div className="dop-keyval">
                        <div><span>Resume</span><b>{active.has_resume ? 'Uploaded' : 'None'}</b></div>
                        <div><span>Target roles</span><b style={{fontFamily:'var(--mono)',fontSize:11.5}}>{active.target || '—'}</b></div>
                        <div><span>Jobs discovered</span><b>{active.job_count}</b></div>
                        <div><span>Jobs scored</span><b>{active.scored_count}</b></div>
                        <div><span>Applications built</span><b>{active.application_count}</b></div>
                        <div><span>Applications submitted</span><b>{active.applied_count}</b></div>
                      </div>
                      <div className="dop-phases" role="group" aria-label="Pipeline phases completed">
                        {PHASE_LABELS.map((label, i) => {
                          const n = i + 1;
                          const done = active.done.includes(n);
                          return (
                            <span key={n} className={done ? 'on' : ''} title={`Phase ${n}: ${label}${done ? ' (complete)' : ' (not yet run)'}`}>
                              {n}
                            </span>
                          );
                        })}
                      </div>
                    </div>

                    <div className="dop-panel dop-panel-feedback">
                      <DevSecHead
                        title="User feedback"
                        hint="messages this user sent in-app"
                        extra={(fullState?.feedback || []).length > 0 && (
                          <span className="dop-pill-mini">{(fullState?.feedback || []).length}</span>
                        )}
                      />
                      {loadingFull ? <div className="dop-empty">Loading…</div> :
                        ((fullState?.feedback || []).length > 0 ? (
                          <div className="dop-fb-list">
                            {fullState.feedback.map(f => (
                              <div key={f.id} className="dop-fb-item">
                                <div className="dop-fb-meta">
                                  <span>{new Date(f.created_at).toLocaleString()}</span>
                                  {!f.read && <span className="dop-fb-new">New</span>}
                                </div>
                                <div className="dop-fb-msg">{f.message}</div>
                              </div>
                            ))}
                            {fullState.feedback.some(f => !f.read) && (
                              <button className="dop-btn dop-btn-link" style={{ alignSelf:'flex-start' }} onClick={async () => {
                                await api.post(`/api/dev/session/${active.id}/feedback/read`, {});
                                api.get(`/api/dev/session/${active.id}`).then(setFullState);
                                refresh();
                              }}>
                                <Icon name="check-check" size={11}/> Mark all read
                              </button>
                            )}
                          </div>
                        ) : <div className="dop-empty">No feedback from this user.</div>)
                      }
                    </div>

                    <div className="dop-panel">
                      <DevSecHead title="Resume text" hint="first 2000 chars of the parsed plain text"/>
                      <pre className="dop-pre dop-pre-fixed">
                        {loadingFull ? 'Loading…' : (fullState?.resume_text || 'No resume uploaded.')}
                      </pre>
                    </div>

                    <DevJsonPanel data={fullState} loading={loadingFull}/>
                  </div>
                </>
              )}
            </section>
          </div>
        )}

        {/* SERVER — runtime + LLM + pipeline + live system resources */}
        {devTab === 'server' && (
          <div className="dop-page fade-in">
            {/* Live psutil-driven resource panels — htop CPU breakdown +
                memory + load avg + temperature, then the top-N processes
                by CPU and memory.  Both poll /api/dev/metrics every 2s
                (with the per-process snapshot opt-in via with_processes=1). */}
            <DevSecHead
              title="Live system resources"
              hint="2-second refresh · psutil-driven"
              meta={metrics?.cpu_temp ? `CPU ${metrics.cpu_temp.current}°C` : null}
            />
            <div className="dop-resources-grid">
              <HtopCpuPanel metrics={metrics || { cpu: status.cpu, memory: status.memory, server_uptime_s: status.server_uptime_s, cpu_temp: status.cpu_temp }}/>
              <TopProcessesPanel processes={metrics?.processes}/>
            </div>

            <DevSecHead
              title="Server controls"
              hint="changes apply live — no process restart needed"
            />

            <div className="sc-grid">
              <div className="sc-col sc-runtime">
                <div className="sc-col-h"><Icon name="server" size={11}/> Runtime — applies to every session</div>

                <div className="sc-row">
                  <div className="sc-row-l">
                    <div className="sc-row-h">Maintenance mode</div>
                    <div className="sc-row-d">Block new phase runs across every session.</div>
                  </div>
                  <button
                    className={'set-toggle' + (runtime?.runtime?.maintenance ? ' on warn' : '')}
                    onClick={() => setRuntimeFlag('maintenance', !runtime?.runtime?.maintenance)}/>
                </div>

                <div className="sc-row">
                  <div className="sc-row-l">
                    <div className="sc-row-h">Verbose phase logs</div>
                    <div className="sc-row-d">Mirror SSE log lines to the server stderr.</div>
                  </div>
                  <button
                    className={'set-toggle' + (runtime?.runtime?.verbose_logs ? ' on' : '')}
                    onClick={() => setRuntimeFlag('verbose_logs', !runtime?.runtime?.verbose_logs)}/>
                </div>

                <div className="sc-row">
                  <div className="sc-row-l">
                    <div className="sc-row-h">.env reload</div>
                    <div className="sc-row-d">
                      Re-read .env to pick up <code>ANTHROPIC_API_KEY</code> changes without restarting.
                    </div>
                  </div>
                  <button className="btn-ghost sc-action" onClick={reloadEnv} disabled={reloadFlash?.kind === 'pending'}>
                    {reloadFlash?.kind === 'pending'
                      ? <><span className="spin"/> Reload</>
                      : <><Icon name="refresh-cw" size={12}/> Reload</>}
                  </button>
                </div>

                {reloadFlash && (
                  <div className={'sc-flash sc-flash-' + reloadFlash.kind}>
                    <Icon name={reloadFlash.kind === 'ok' ? 'check-circle-2' : reloadFlash.kind === 'err' ? 'x-circle' : 'loader'} size={12}/>
                    {reloadFlash.text}
                  </div>
                )}

                <div className="sc-env">
                  <div className="sc-env-row">
                    <span>ANTHROPIC_API_KEY</span>
                    <b className={runtime?.env?.anthropic_key_present ? 'ok' : 'bad'}>
                      {runtime?.env?.anthropic_key_present ? 'present' : 'missing'}
                    </b>
                  </div>
                  <div className="sc-env-row">
                    <span>SMTP</span>
                    <b className={runtime?.env?.smtp_configured ? 'ok' : 'mid'}>
                      {runtime?.env?.smtp_configured ? 'configured' : 'not configured'}
                    </b>
                  </div>
                  <div className="sc-env-row">
                    <span>OLLAMA_URL</span>
                    <b className="mid">{runtime?.env?.ollama_url || '—'}</b>
                  </div>
                  <div className="sc-env-row">
                    <span>LOCAL_DEV_BYPASS</span>
                    <b className={runtime?.env?.local_dev_bypass ? 'warn' : 'mid'}>
                      {runtime?.env?.local_dev_bypass ? 'on (insecure)' : 'off'}
                    </b>
                  </div>
                </div>
              </div>

              <div className="sc-col">
                <div className="sc-col-h"><Icon name="cpu" size={11}/> LLM provider — this session only</div>
                <div className="sc-radio-row">
                  {['anthropic', 'ollama'].map(m => (
                    <button
                      key={m}
                      className={'sc-radio' + (globalState?.mode === m ? ' on' : '')}
                      onClick={() => saveSessionConfig({ mode: m })}>
                      {m === 'anthropic' ? 'Claude' : 'Ollama'}
                    </button>
                  ))}
                </div>
                <div className="sc-field">
                  <label>Anthropic API key</label>
                  <div className="sc-key-row">
                    <input
                      type="password"
                      className="set-input"
                      placeholder="sk-ant-…"
                      value={apiKeyDraft}
                      onChange={e => setApiKeyDraft(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}/>
                    <button className="btn-primary sc-action" onClick={saveApiKey} disabled={savingKey || !apiKeyDraft.trim()}>
                      {savingKey ? <span className="spin"/> : <Icon name="key" size={12}/>}
                      Save
                    </button>
                  </div>
                  <div className="sc-helper">Held in volatile session memory only. Never written to disk.</div>
                </div>
                <div className="sc-field">
                  <label>Ollama model</label>
                  <input
                    type="text"
                    className="set-input"
                    value={globalState?.ollama_model || ''}
                    onChange={e => saveSessionConfig({ ollama_model: e.target.value })}
                    placeholder="llama3.2"/>
                </div>
              </div>

              <div className="sc-col">
                <div className="sc-col-h"><Icon name="gauge" size={11}/> Pipeline — this session only</div>
                <div className="sc-field">
                  <label>Score threshold <i>{globalState?.threshold ?? 75}</i></label>
                  <input type="range" min="50" max="95" step="1" className="set-range"
                    value={globalState?.threshold ?? 75}
                    onChange={e => saveSessionConfig({ threshold: Number(e.target.value) })}/>
                </div>
                <div className="sc-field">
                  <label>Max scrape jobs <i>{globalState?.max_scrape_jobs ?? 50}</i></label>
                  <input type="range" min="10" max="200" step="10" className="set-range"
                    value={globalState?.max_scrape_jobs ?? 50}
                    onChange={e => saveSessionConfig({ max_scrape_jobs: Number(e.target.value) })}/>
                </div>
                <div className="sc-field">
                  <label>Days old <i>{globalState?.days_old ?? 30}</i></label>
                  <input type="range" min="1" max="90" step="1" className="set-range"
                    value={globalState?.days_old ?? 30}
                    onChange={e => saveSessionConfig({ days_old: Number(e.target.value) })}/>
                </div>
                <div className="sc-row">
                  <div className="sc-row-l"><div className="sc-row-h">Generate cover letters</div></div>
                  <button className={'set-toggle' + (globalState?.cover_letter ? ' on' : '')}
                    onClick={() => saveSessionConfig({ cover_letter: !globalState?.cover_letter })}/>
                </div>
                <div className="sc-row">
                  <div className="sc-row-l"><div className="sc-row-h">SimplifyJobs scraper</div></div>
                  <button className={'set-toggle' + (globalState?.use_simplify !== false ? ' on' : '')}
                    onClick={() => saveSessionConfig({ use_simplify: globalState?.use_simplify === false })}/>
                </div>
                <div className="sc-row">
                  <div className="sc-row-l"><div className="sc-row-h">Light theme</div></div>
                  <button className={'set-toggle' + (globalState?.light_mode ? ' on' : '')}
                    onClick={() => saveSessionConfig({ light_mode: !globalState?.light_mode })}/>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* CONSOLE — sandboxed CLI + event log */}
        {devTab === 'console' && (
          <div className="dop-page dop-console fade-in">
            <div className="dop-panel dop-panel-cli">
              <DevSecHead
                title="Sandboxed CLI"
                hint="whitelisted read-only inspections — never execs arbitrary code"
              />
              <div className="dop-cli-actions">
                {commands.map(([id, label]) => (
                  <button key={id}
                    className={'dop-cli-cmd' + (cli.command === id && cli.output ? ' on' : '')}
                    disabled={cli.running}
                    onClick={() => runCli(id)}>
                    <span className="dop-cli-prompt">$</span> {label.toLowerCase()}
                  </button>
                ))}
              </div>
              <pre className="dop-pre dop-pre-cli">
                {cli.output
                  ? <>
                      <span className="dop-pre-prompt">$ {cli.command}</span>{'\n'}
                      {cli.output}
                    </>
                  : <span className="dop-pre-hint">Pick a command above to run a sandboxed inspection.</span>}
              </pre>
            </div>

            <div className="dop-panel">
              <DevSecHead
                title="Recent events"
                hint="server-emitted events for this process"
                meta={`${(data.events || []).length} entr${(data.events || []).length === 1 ? 'y' : 'ies'}`}
              />
              <div className="dop-events dop-events-tall">
                {(data.events || []).slice(0, 80).map((e, i) => (
                  <div key={i} className="dop-event">
                    <span>{new Date(e.ts).toLocaleTimeString()}</span>
                    <b>{e.kind}</b>
                    <p>{e.message}</p>
                  </div>
                ))}
                {(data.events || []).length === 0 && <div className="dop-empty">No events recorded yet.</div>}
              </div>
            </div>
          </div>
        )}

        {/* TWEAKS — per-session UI experiments */}
        {devTab === 'tweaks' && (
          <div className="dop-page fade-in" style={{ maxWidth: 720 }}>
            <DevSecHead
              title="Site tweaks"
              hint="visual experiments — applied to this session only, not persisted globally"
            />


            <div className="dop-panel">
              <div className="sc-col-h"><Icon name="palette" size={11}/> Accent</div>
              <div className="dev-tweak-row">
                {accents.map(color => (
                  <button key={color}
                    className={'dev-swatch' + (tweaks?.accent === color ? ' on' : '')}
                    style={{ background: color }}
                    onClick={() => saveTweaks({ accent: color })}
                    title={color}/>
                ))}
              </div>
            </div>

            <div className="dop-panel">
              <div className="sc-col-h"><Icon name="layout" size={11}/> Layout</div>
              <div className="set-field">
                <div className="set-label">Density</div>
                <select className="set-select" value={tweaks?.density || 'comfortable'} onChange={e => saveTweaks({ density: e.target.value })}>
                  <option value="compact">Compact</option>
                  <option value="comfortable">Comfortable</option>
                  <option value="spacious">Spacious</option>
                </select>
              </div>
              <div className="set-field">
                <div className="set-label">Experiment mode</div>
                <select className="set-select" value={tweaks?.experiment || 'standard'} onChange={e => saveTweaks({ experiment: e.target.value })}>
                  <option value="standard">Standard</option>
                  <option value="focus">Focus</option>
                  <option value="command">Command</option>
                  <option value="launch">Launch</option>
                </select>
              </div>
            </div>

            <div className="dop-panel">
              <div className="sc-col-h"><Icon name="megaphone" size={11}/> Site banner</div>
              <div className="sc-row">
                <div className="sc-row-l">
                  <div className="sc-row-h">Show banner</div>
                  <div className="sc-row-d">Use the dev banner as the site-wide promo strip.</div>
                </div>
                <button className={'set-toggle' + (tweaks?.show_promo !== false ? ' on' : '')}
                  onClick={() => saveTweaks({ show_promo: tweaks?.show_promo === false })}/>
              </div>
              <input
                className="set-input"
                value={tweaks?.dev_banner || ''}
                onChange={e => setTweaks({ ...(tweaks || {}), dev_banner: e.target.value })}
                onBlur={e => saveTweaks({ dev_banner: e.target.value })}
                placeholder="Dev banner text"/>
            </div>
          </div>
        )}
      </div>
    </div>
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

/* Section header for the Dev Console. `meta` is a small right-aligned
   label (e.g. "last refresh < 10s") and `extra` slots an action button. */
function DevSecHead({ title, hint, meta, extra }) {
  return (
    <header className="dop-secrow">
      <div className="dop-sec-h">
        <span className="dop-sec-dot" aria-hidden="true"/>
        <span className="dop-sec-title">{title}</span>
        {hint && <span className="dop-sec-hint">— {hint}</span>}
      </div>
      <div className="dop-sec-tail">
        {meta && <span className="dop-sec-meta">{meta}</span>}
        {extra}
      </div>
    </header>
  );
}

/* Hover-tooltip labels for the seven pipeline phase chips. */
const PHASE_LABELS = [
  'Resume ingestion',
  'Job discovery',
  'Relevance scoring',
  'Resume tailoring',
  'Application submission',
  'Excel tracker',
  'Run report',
];

/* Collapsible session_state dump — closed by default since it can run
   into hundreds of kB and would otherwise dominate the inspector grid. */
function DevJsonPanel({ data, loading }) {
  const [open, setOpen] = useState(false);
  const text = loading ? 'Loading…' : JSON.stringify(data, null, 2);
  const sizeKb = !loading && data ? Math.round(text.length / 1024) : 0;
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); }
    catch (_) { /* clipboard blocked — silent */ }
  };
  return (
    <div className="dop-panel">
      <DevSecHead
        title="Full session JSON"
        hint="raw state — useful for replaying bugs"
        extra={(
          <span className="dop-json-actions">
            {!loading && <span className="dop-sec-meta">{sizeKb} KB</span>}
            <button className="dop-btn dop-btn-link" onClick={copy} disabled={loading}>
              <Icon name="copy" size={11}/> Copy
            </button>
            <button className="dop-btn dop-btn-link" onClick={() => setOpen(o => !o)}>
              <Icon name={open ? 'chevron-up' : 'chevron-down'} size={11}/>
              {open ? 'Hide' : 'Show'}
            </button>
          </span>
        )}
      />
      {open && (
        <pre className="dop-pre dop-pre-fixed dop-pre-json">
          {text}
        </pre>
      )}
    </div>
  );
}

/* ══════════════════════════════════════════════════════════
   ROOT
══════════════════════════════════════════════════════════ */
function GoogleG() {
  return (
    <svg viewBox="0 0 18 18" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.49h4.84a4.14 4.14 0 0 1-1.79 2.72v2.26h2.9c1.7-1.56 2.69-3.87 2.69-6.63z" fill="#4285F4"/>
      <path d="M9 18c2.43 0 4.46-.81 5.95-2.18l-2.9-2.26c-.8.54-1.83.86-3.05.86-2.34 0-4.32-1.58-5.03-3.71H.96v2.33A9 9 0 0 0 9 18z" fill="#34A853"/>
      <path d="M3.97 10.71a5.41 5.41 0 0 1 0-3.42V4.96H.96a9 9 0 0 0 0 8.08l3.01-2.33z" fill="#FBBC05"/>
      <path d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58A8.99 8.99 0 0 0 9 0 9 9 0 0 0 .96 4.96l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58z" fill="#EA4335"/>
    </svg>
  );
}

function AuthPage({ onAuth }) {
  const [mode, setMode]         = useState('login');
  const [email, setEmail]       = useState('');
  const [password, setPassword] = useState('');
  const [showPw, setShowPw]     = useState(false);
  const [error, setError]       = useState(null);
  const [loading, setLoading]   = useState(false);

  const isLogin = mode === 'login';

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const res = await api.post(`/api/auth/${mode}`, { email, password });
      if (res.ok) await onAuth(res.user);
      else setError(res.error || 'Authentication failed');
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
      if (res.url) window.location.href = res.url;
      else throw new Error('No redirect URL received');
    } catch (err) {
      setError(err.message || 'Could not initialize Google login');
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-grain" aria-hidden="true"/>
      <div className="auth-card">
        <div className="auth-brand"><BrandMark/></div>

        <header className="auth-head">
          <div className="auth-eyebrow">{isLogin ? 'Welcome back' : 'New account'}</div>
          <h1 className="auth-h">
            {isLogin ? <>Sign in to <em>jobsai</em></> : <>Start your <em>automated</em> search</>}
          </h1>
          <p className="auth-sub">
            {isLogin
              ? 'Pick up where you left off — your tailored applications and tracker are waiting.'
              : 'Upload a resume once. We discover, score, tailor, and apply on your behalf.'}
          </p>
        </header>

        <button type="button" className="auth-google" onClick={handleGoogle}>
          <GoogleG/>
          <span>Continue with Google</span>
        </button>
        <div style={{
          marginTop: 8,
          padding: '8px 12px',
          borderRadius: 8,
          background: 'var(--warn-d)',
          border: '1px solid var(--warn-b)',
          color: 'var(--warn)',
          fontSize:15,
          lineHeight: 1.45,
          textAlign: 'center',
        }}>
          Google sign-in is currently under development — please sign in with email below.
        </div>

        <div className="auth-divider"><span>or with email</span></div>

        <form className="auth-form" onSubmit={handleSubmit} noValidate>
          <div className="auth-field">
            <label className="auth-label" htmlFor="auth-email">Email address</label>
            <div className="auth-input-wrap">
              <Icon name="mail" size={15}/>
              <input
                id="auth-email"
                className="auth-input"
                type="email"
                inputMode="email"
                autoComplete="email"
                autoCapitalize="off"
                autoCorrect="off"
                spellCheck={false}
                value={email}
                onChange={e => setEmail(e.target.value)}
                placeholder="name@company.com"
                required
              />
            </div>
          </div>

          <div className="auth-field">
            <label className="auth-label" htmlFor="auth-password">Password</label>
            <div className="auth-input-wrap">
              <Icon name="lock" size={15}/>
              <input
                id="auth-password"
                className="auth-input"
                type={showPw ? 'text' : 'password'}
                autoComplete={isLogin ? 'current-password' : 'new-password'}
                value={password}
                onChange={e => setPassword(e.target.value)}
                placeholder={isLogin ? 'Enter your password' : 'At least 6 characters'}
                minLength={6}
                required
              />
              <button
                type="button"
                className="auth-eye"
                onClick={() => setShowPw(v => !v)}
                aria-label={showPw ? 'Hide password' : 'Show password'}
                tabIndex={-1}
              >
                <Icon name={showPw ? 'eye-off' : 'eye'} size={15}/>
              </button>
            </div>
          </div>

          {error && (
            <div className="auth-error" role="alert">
              <Icon name="circle-alert" size={14}/>
              <span>{error}</span>
            </div>
          )}

          <button className="auth-submit" type="submit" disabled={loading}>
            {loading
              ? <span className="spin"/>
              : <>
                  <span>{isLogin ? 'Sign in' : 'Create account'}</span>
                  <Icon name="arrow-right" size={14}/>
                </>}
          </button>
        </form>

        <div className="auth-switch">
          {isLogin ? 'New to Jobs AI?' : 'Already have an account?'}
          <button type="button" onClick={() => { setMode(isLogin ? 'signup' : 'login'); setError(null); }}>
            {isLogin ? 'Create an account' : 'Sign in'}
          </button>
        </div>
      </div>
    </div>
  );
}

function App() {
  const [state,     setState]     = useState(null);
  const [page,      _setPage]     = useState(pageFromHash);
  const [showPromo, setShowPromo] = useState(true);
  const [booted,    setBooted]    = useState(false);
  const [navOpen,   setNavOpen]   = useState(false);

  // Wrap setPage so navigation always updates the URL hash. Children call
  // this exactly like before — they don't need to know about routing.
  const setPage = useCallback((next) => {
    if (!VALID_PAGES.has(next)) next = 'home';
    const targetHash = hashFromPage(next);
    if (location.hash !== targetHash) {
      // pushState lets the back button return to the previous page; using a
      // bare hash assignment would also work but doesn't let us suppress
      // history entries for redundant transitions.
      const url = location.pathname + location.search + targetHash;
      try { history.pushState(null, '', url); }
      catch (e) { location.hash = targetHash; }  // fallback for older browsers
    }
    _setPage(next);
  }, []);

  // React to back/forward navigation and direct hash edits in the URL bar.
  useEffect(() => {
    const sync = () => _setPage(pageFromHash());
    window.addEventListener('popstate', sync);
    window.addEventListener('hashchange', sync);
    return () => {
      window.removeEventListener('popstate', sync);
      window.removeEventListener('hashchange', sync);
    };
  }, []);

  // Always close the mobile drawer when the active page changes — keeps the
  // drawer from lingering after navigating from the rail.
  useEffect(() => { setNavOpen(false); }, [page]);

  // Lock body scroll while the drawer is open so the underlying page doesn't
  // bounce on iOS Safari.
  useEffect(() => {
    if (!navOpen) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = prev; };
  }, [navOpen]);

  // Close the drawer on Escape.
  useEffect(() => {
    if (!navOpen) return;
    const onKey = (e) => { if (e.key === 'Escape') setNavOpen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [navOpen]);

  const refresh = useCallback(async () => {
    try {
      const next = await api.get('/api/state');
      // Skip setState when the response is byte-for-byte identical to the
      // current state. Every 8 s poll was previously creating a fresh
      // `state` reference, which made the whole component tree re-render
      // (App → JobsPage → 30 JobCards) even when nothing changed — users
      // perceived this as "the job listing page keeps refreshing every
      // few seconds." JSON.stringify is cheap on a ~tens-of-KB state
      // object compared to the React reconciliation pass we're avoiding.
      setState(prev => {
        try {
          if (prev && JSON.stringify(prev) === JSON.stringify(next)) return prev;
        } catch (_) { /* fall through to fresh setState */ }
        return next;
      });
      applyDevTweaks(next.dev_tweaks);
      return next;
    }
    catch (err) { return null; }
    finally { setBooted(true); }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // Apply light/dark theme to <html> whenever the setting changes.
  // Skip the DOM write when the value already matches so polling doesn't
  // touch the layout-affecting attribute on every state refresh.
  useEffect(() => {
    const want = state?.light_mode ? 'light' : '';
    const have = document.documentElement.dataset.theme || '';
    if (want === have) return;
    if (want) document.documentElement.dataset.theme = 'light';
    else delete document.documentElement.dataset.theme;
  }, [state?.light_mode]);

  // Adaptive polling: 2 s while any resume is extracting, 8 s otherwise.
  // Only poll once the user is authenticated. Polling on the AuthPage races
  // with login: a poll started just before the user clicks Sign-in resolves
  // *after* login with the OLD anonymous cookies, then setState clobbers
  // state.user back to null and the AuthPage re-renders — which is why login
  // used to require two clicks.
  const anyExtracting = (state?.resumes || []).some(r => r.extracting);
  useEffect(() => {
    if (!state?.user) return;
    const id = setInterval(refresh, anyExtracting ? 2000 : 8000);
    return () => clearInterval(id);
  }, [refresh, anyExtracting, state?.user]);

  // Note: discovery (phases 1/2/3) is owned by JobsPage when the user opens it.
  // We deliberately do NOT prefetch from App.jsx — running both led to two
  // parallel SSE chains hitting the same session and clobbering scored results.

  if (!booted) return (
    <div style={{ display:'flex', alignItems:'center', justifyContent:'center', height:'100vh', color:'var(--t3)', fontSize:15.5 }}>
      <span className="spin" style={{ marginRight:8 }}/> Loading workspace…
    </div>
  );

  /* Auth gate — the entire SPA requires a real authenticated user.
     Anonymous visitors only ever see AuthPage. This prevents ghost/unprofiled
     users from being created in the Dev Ops user list. */
  if (!state?.user) {
    return <AuthPage onAuth={refresh} />;
  }

  /* Onboarding gate — once authenticated, route resume-less users into upload. */
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
      case 'home':      return <Dashboard state={state} setPage={setPage} refresh={refresh}/>;
      case 'jobs':      return <JobsPage state={state} refresh={refresh} setPage={setPage}/>;
      case 'resume':    return <ResumePage state={state} refresh={refresh} setPage={setPage}/>;
      case 'documents': return <DocumentsPage state={state} refresh={refresh} setPage={setPage}/>;
      case 'profile':   return <ProfilePage state={state} refresh={refresh} setPage={setPage}/>;
      case 'agent':     return <AgentPage state={state} refresh={refresh}/>;
      case 'dev':       return <DevPage state={state} refresh={refresh}/>;
      case 'feedback':  return <FeedbackPage refresh={refresh}/>;
      case 'settings':  return <SettingsPage state={state} refresh={refresh} setPage={setPage}/>;
      case 'plans':     return <PlansPage state={state} setPage={setPage}/>;
      case 'auth':      return <AuthPage onAuth={async () => { await refresh(); setPage('home'); }} />;
      default:          return <Dashboard state={state} setPage={setPage}/>;
    }
  })();

  const handleLogout = async () => {
    // Always navigate, even if the network call fails — the user's intent
    // is unambiguous and we never want a stale 5xx to leave them stuck
    // on a "logged-in" view.
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
        credentials: 'same-origin',
        cache: 'no-store',
      });
    } catch (err) {
      console.warn('logout request failed (continuing anyway):', err);
    }
    // Best-effort: drop any non-HttpOnly state cookies the SPA can see.
    // The auth cookie itself is HttpOnly so the browser must drop it via
    // the Set-Cookie response (server-side path/samesite/secure now mirror
    // the original set_cookie so the delete is honored).
    try {
      document.cookie = 'jobs_ai_session=; Max-Age=0; Path=/; SameSite=Lax';
    } catch (_) { /* ignore */ }
    // Force a FULL document load (not a hash change) so the SPA re-mounts
    // and re-fetches /api/state with the cleared cookie jar. We use
    // `replace` so the browser back button doesn't bounce the user back
    // into the authenticated view.
    window.location.replace('/?signed_out=1');
  };

  const exitCustomerMode = async () => {
    await api.post('/api/config', { force_customer_mode: false });
    window.location.href = '/app#dev';
    window.location.reload();
  };

  return (
    <div className="shell">
      {/* Brand cell — clicking logo goes home. The hamburger sits before
         the wordmark and is hidden on desktop via .nav-toggle media rule. */}
      <div className="brand-cell">
        <button
          className="nav-toggle"
          aria-label={navOpen ? 'Close navigation menu' : 'Open navigation menu'}
          aria-expanded={navOpen}
          onClick={() => setNavOpen(o => !o)}
        >
          <Icon name={navOpen ? 'x' : 'menu'} size={18}/>
        </button>
        <BrandMark onClick={() => window.location.href = '/'}/>
      </div>

      {/* Promo strip (dismissable) — only shown when a dev_banner is set */}
      {showPromo && state?.dev_tweaks?.show_promo !== false && state?.dev_tweaks?.dev_banner ? (
        <PromoStrip onClose={() => setShowPromo(false)} text={state.dev_tweaks.dev_banner}/>
      ) : (
        <div style={{ gridArea:'promo', background:'var(--bg-1)', borderBottom:'1px solid var(--bdr)' }}/>
      )}

      {/* Backdrop dims the main content while the mobile drawer is open.
         Desktop hides this via .rail-backdrop CSS. */}
      <div
        className={'rail-backdrop' + (navOpen ? ' is-open' : '')}
        onClick={() => setNavOpen(false)}
        aria-hidden="true"
      />

      <Rail
        page={page}
        setPage={setPage}
        counts={counts}
        isDev={state?.is_dev}
        onLogout={handleLogout}
        navOpen={navOpen}
        closeNav={() => setNavOpen(false)}
      />

      <main className="main">{pageEl}</main>

      {/* Escape hatch: when a dev has flipped "Test as Customer", is_dev is false
         everywhere (Rail dev item hidden, dev-float gone). Without this pill the
         dev would be trapped without a way back to /api/dev/*. Always shown when
         the underlying user is a developer in simulation mode. */}
      {state?.dev_simulating && (
        <button className="sim-pill" onClick={exitCustomerMode} title="You're viewing as a customer. Click to return to Dev Ops.">
          <span className="sim-dot"/>
          <span className="sim-text"><b>Customer mode</b><i>Click to exit</i></span>
          <Icon name="log-out" size={13}/>
        </button>
      )}
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
