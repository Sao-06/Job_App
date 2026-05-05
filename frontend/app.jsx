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
  'home', 'jobs', 'resume', 'profile', 'agent', 'dev',
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
const NAV_UTIL = [
  { id:'plans',    label:'Plans',    icon:'gem' },
  { id:'feedback', label:'Feedback', icon:'circle-help' },
  { id:'settings', label:'Settings', icon:'settings' },
  { id:'logout',   label:'Sign out', icon:'log-out' },
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
               className={'rail-item' + (page === it.id ? ' active' : '')}
               onClick={() => utilSelect(it)}>
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
    { label: '80–89', range: [80, 90],  color: '#34d399',         n: 0 },
    { label: '70–79', range: [70, 80],  color: 'var(--accent-h)', n: 0 },
    { label: '60–69', range: [60, 70],  color: '#7c83a8',         n: 0 },
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
          <circle cx="60" cy="60" r={C} fill="none" stroke="rgba(255,255,255,.06)" strokeWidth="10"/>
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

function MarketNews() {
  const [items, setItems] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    let cancelled = false;
    const since = Math.floor(Date.now() / 1000) - 7 * 24 * 3600;
    const url = `https://hn.algolia.com/api/v1/search_by_date?query=hiring%20OR%20layoffs%20OR%20internship%20OR%20%22job%20market%22&tags=story&numericFilters=created_at_i%3E${since}&hitsPerPage=12`;

    fetch(url)
      .then(r => r.json())
      .then(d => {
        if (cancelled) return;
        const hits = (d.hits || [])
          .filter(h => h.title && h.url)
          .slice(0, 6)
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
        setItems(hits);
      })
      .catch(e => { if (!cancelled) setErr(e.message || 'Network'); });

    return () => { cancelled = true; };
  }, []);

  const fmt = iso => {
    const ms = Date.now() - new Date(iso).getTime();
    const h = ms / 3.6e6;
    if (h < 1) return Math.max(1, Math.round(h * 60)) + 'm ago';
    if (h < 24) return Math.round(h) + 'h ago';
    return Math.round(h / 24) + 'd ago';
  };

  if (err) {
    return (
      <div className="news-empty">
        <Icon name="wifi-off" size={18}/>
        <span>News feed offline — {err}</span>
      </div>
    );
  }
  if (!items) {
    return (
      <div className="news-skel">
        {[0, 1, 2, 3].map(i => <div key={i} className="news-skel-row" style={{ animationDelay: `${i * 120}ms` }}/>)}
      </div>
    );
  }
  if (!items.length) {
    return (
      <div className="news-empty">
        <Icon name="search" size={18}/>
        <span>No matching headlines this week.</span>
      </div>
    );
  }

  return (
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
            <circle cx="80" cy="80" r={R} fill="none" stroke="rgba(255,255,255,.06)" strokeWidth="9"/>
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
            {primary && (
              <button className="intel-btn ghost" onClick={async () => {
                try {
                  await api.post('/api/profile/extract', { resume_id: primary.id, force: true });
                  refresh?.();
                } catch (e) { /* swallow — UI shows extracting state */ }
              }}>
                <Icon name="refresh-cw" size={11}/> Re-scan
              </button>
            )}
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
      n: '03', icon: 'scan-text', label: 'Re-scan resume',
      sub: primary ? primary.filename.replace(/\.[^.]+$/, '').slice(0, 22) : 'analyze resume',
      tone: 'pink',
      onClick: async () => {
        if (!primary) { setPage('resume'); return; }
        try { await api.post('/api/profile/extract', { resume_id: primary.id, force: true }); refresh?.(); }
        catch (e) {}
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
      sub: state?.mode === 'demo' ? 'demo mode' : state?.mode === 'ollama' ? 'local AI' : 'cloud AI',
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
            className={'mission-tile tone-' + a.tone}
            onClick={a.onClick}
            disabled={!has_resume && (a.label === 'Discover roles' || a.label === 'Run agent')}
            style={{ animationDelay: `${i * 60}ms` }}
          >
            <span className="mission-num">{a.n}</span>
            <span className="mission-icon"><Icon name={a.icon} size={20}/></span>
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

  const streak = Math.max(1, done.size + (apps.length > 0 ? 2 : 0));

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

  const profileSkills = (state?.profile?.skills || state?.profile?.hard_skills || []).map(s =>
    typeof s === 'string' ? s : (s.name || s.skill || ''));

  const phaseLabels = ['Ingest','Discover','Score','Tailor','Submit','Track','Report'];

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
              <span className="hero-eyebrow-sep">/</span>
              <span className="hero-streak"><Icon name="flame" size={11}/> {streak}-day streak</span>
            </div>
            <h1 className="hero-h">
              {greet}, <em>{firstName}</em>.
            </h1>
            <p className="hero-p">
              {matches > 0
                ? <>You have <strong>{matches}</strong> high-confidence roles open in the queue. Atlas finished phase&nbsp;<strong>{done.size}/7</strong> — <em>your move</em>.</>
                : <>Atlas is warming up. Run discovery to surface the freshest roles tuned to your profile.</>}
            </p>
            <div className="hero-cta-row">
              <button className="hero-cta-p" onClick={() => setPage('jobs')}>
                <Icon name="zap" size={14}/> {matches > 0 ? 'Review matches' : 'Find matches'}
              </button>
              <button className="hero-cta-g" onClick={() => setPage('agent')}>
                <Icon name="sparkles" size={14}/> Open agent
              </button>
              <button className="hero-cta-g" onClick={() => setPage('resume')}>
                <Icon name="file-text" size={14}/> Tune résumé
              </button>
            </div>
            <div className="hero-pipeline">
              {phaseLabels.map((lbl, i) => {
                const n = i + 1;
                const isDone = done.has(n);
                return (
                  <div key={n} className={'pp-step' + (isDone ? ' done' : '')}>
                    <span className="pp-dot">{isDone ? <Icon name="check" size={10}/> : n}</span>
                    <span className="pp-lbl">{lbl}</span>
                  </div>
                );
              })}
            </div>
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
                <circle cx="90" cy="90" r="74" fill="none" stroke="rgba(255,255,255,.05)" strokeWidth="2"/>
                <circle cx="90" cy="90" r="60" fill="none" stroke="rgba(255,255,255,.08)" strokeWidth="1" strokeDasharray="2 6"/>
                <circle cx="90" cy="90" r="74" fill="none" stroke="url(#ringGrad)" strokeWidth="6"
                  strokeLinecap="round"
                  strokeDasharray={2 * Math.PI * 74}
                  strokeDashoffset={2 * Math.PI * 74 - (2 * Math.PI * 74 * phasePct / 100)}
                  transform="rotate(-90 90 90)"
                  style={{ transition:'stroke-dashoffset 1.4s cubic-bezier(.16,1,.3,1)', filter:'drop-shadow(0 0 12px rgba(123,132,232,.45))' }}/>
              </svg>
              <div className="hero-ring-c">
                <div className="hrc-pct"><CountUp to={phasePct}/><i>%</i></div>
                <div className="hrc-lbl">pipeline complete</div>
                <div className="hrc-sub">{done.size}/7 phases · {jobs.length} jobs scanned</div>
              </div>
            </div>
          </div>
        </div>
      </section>

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
            <Sparkline values={spark(seed + 5, 6)} color="#a855f7"/>
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
              <div className="viz-eyebrow"><span className="news-rss"/> Hacker News · last 7 days</div>
              <div className="viz-h">Hiring, layoffs &amp; the labor market</div>
            </div>
            <a className="viz-link" href="https://hn.algolia.com/?dateRange=pastWeek&query=hiring%20OR%20layoffs" target="_blank" rel="noopener noreferrer">
              All stories <Icon name="external-link" size={11}/>
            </a>
          </div>
          <MarketNews/>
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
            <div style={{ marginTop:4, fontSize:14.5, color:'var(--t3)' }}>PDF · DOCX · TXT</div>
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

function JobCard({ job, idx, isLiked, onLike, onHide, onAsk, onTailor }) {
  // Prefer stable per-job values (set by JobsPage); fall back to idx-based for callers that don't enrich.
  const logo    = job._logo   ?? LOGO_VARIANTS[idx % LOGO_VARIANTS.length];
  const posted  = job._posted ?? POSTED_LABELS[idx % POSTED_LABELS.length];
  const model   = job._model  ?? WORK_MODELS[idx % WORK_MODELS.length];
  const exp     = job._exp    ?? EXP_LEVELS[idx % EXP_LEVELS.length];
  const pct     = Math.round(job.score || 0);
  const stripe  = pct >= 85 ? 'score-high' : pct >= 65 ? 'score-mid' : 'score-low';
  const tags    = (job.skills || '').split(',').map(s => s.trim()).filter(Boolean).slice(0,3);

  return (
    <div className={'job-card ' + stripe}>
      <div className="job-card-inner">
        <div className="job-body">
          <div className="job-header">
            <CompanyLogo company={job.co} fallbackVariant={logo} size={38}/>
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
              <button className="btn-ghost" onClick={() => onAsk?.(job)}>
                <Icon name="sparkles" size={12}/> Ask Atlas
              </button>
              <button className="btn-ghost btn-tailor" onClick={() => onTailor?.(job)} title="Generate a resume tailored to this job">
                <Icon name="wand-2" size={12}/> Tailor
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

function JobsPage({ state, refresh, setPage }) {
  const [tab, setTab]           = useState('recommended');
  const [searchQuery, setQuery] = useState('');
  const [running, setRun]       = useState(false);
  const [searchingMore, setSearchingMore] = useState(false);
  const [runLabel, setRunLabel] = useState('');
  const [askJob,  setAskJob]    = useState(null);  // active "Ask Atlas" target
  const [tailorJob, setTailorJob] = useState(null); // active "Tailor for this job" target

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
      const q = searchQuery.toLowerCase();
      list = list.filter(j => (j.co || '').toLowerCase().includes(q) || (j.role || '').toLowerCase().includes(q) || (j.skills || '').toLowerCase().includes(q));
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
  // popover. Pinned to common US/global hubs + Remote/Anywhere because those
  // are the search intents we want to land in one click. The DB-facet list
  // below it surfaces everything else (London, Bangalore, hybrid clusters,
  // single-city listings, …) sorted by inventory count.
  const locationDefaults = useMemo(() => [
    { value: 'Remote',         label: 'Remote',         icon: 'globe' },
    { value: 'United States',  label: 'United States',  icon: 'flag' },
    { value: 'San Francisco',  label: 'San Francisco',  icon: 'map-pin' },
    { value: 'New York',       label: 'New York',       icon: 'map-pin' },
    { value: 'Seattle',        label: 'Seattle',        icon: 'map-pin' },
    { value: 'Austin',         label: 'Austin',         icon: 'map-pin' },
    { value: 'Boston',         label: 'Boston',         icon: 'map-pin' },
    { value: 'Los Angeles',    label: 'Los Angeles',    icon: 'map-pin' },
    { value: 'Chicago',        label: 'Chicago',        icon: 'map-pin' },
    { value: 'London',         label: 'London',         icon: 'map-pin' },
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
      </div>

      <div className="page-body" onScroll={onScroll} style={{ overflowY: 'auto' }}>
        <div className="col-main">
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
                  isLiked={liked.has(j.id)}
                  onLike={() => handleAction(liked.has(j.id) ? 'unlike' : 'like', j)}
                  onHide={() => handleAction('hide', j)}
                  onAsk={() => setAskJob(j)}
                  onTailor={() => setTailorJob(j)}/>
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

      {askJob && (
        <AskAtlas job={askJob} mode={state?.mode} isPro={!!state?.is_pro} onClose={() => setAskJob(null)}/>
      )}
      {tailorJob && (
        <TailorDrawer job={tailorJob} mode={state?.mode} isPro={!!state?.is_pro}
                      hasResume={!!state?.profile} onClose={() => setTailorJob(null)}/>
      )}
    </>
  );
}

/* ──────────────────────────────────────────────────────────
   Ask Atlas — per-job chat advisor. Slides in from the right;
   thread is owned locally and reset when the drawer closes.
   ────────────────────────────────────────────────────────── */
function AskAtlas({ job, mode, isPro, onClose }) {
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
  const score = Math.round(job.score || 0);
  // Neutral label — the chat advisor uses whichever provider is configured;
  // surfacing the brand here is just noise.
  const providerLabel = mode === 'demo' ? 'Demo' : 'AI';

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
                {!isPro && mode === 'anthropic' && <span className="ask-pro-pill">Pro</span>}
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
function TailorDrawer({ job, mode, isPro, hasResume, onClose }) {
  const jobId = job.id || `${job.co || job.company || ''}|${job.role || job.title || ''}`;
  const [item, setItem]       = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState(null);

  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose?.(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Trigger the tailoring run as soon as the drawer opens. Anthropic mode
  // can take 15–25 s; Ollama varies with the local model. We surface a
  // progress checklist while it streams so the wait doesn't feel dead.
  useEffect(() => {
    let cancelled = false;
    if (!hasResume) {
      setLoading(false);
      setError('Upload a resume first — Atlas needs your profile to tailor against this posting.');
      return () => { cancelled = true; };
    }
    setLoading(true);
    setError(null);
    setItem(null);
    // 90s timeout — Claude tool-calling for tailoring runs 15-25s; Ollama on
    // a slow box can exceed the 30s default and 504 prematurely.
    api.post('/api/resume/tailor', { job_id: jobId }, { timeoutMs: 90000 })
      .then(res => {
        if (cancelled) return;
        setItem(res?.item || null);
        setLoading(false);
      })
      .catch(e => {
        if (cancelled) return;
        let msg = e?.message || 'Tailoring failed.';
        // The server returns "Claude tailoring requires the Pro plan…" via
        // JSONResponse — _handle surfaces it as e.message verbatim. We only
        // rewrite genuinely-confusing messages.
        if (/Job not found|API 404/i.test(msg)) {
          msg = 'This job is no longer in the index. Refresh the feed and try again.';
        }
        setError(msg);
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, [jobId, hasResume]);

  const co       = job.co || job.company || '—';
  const role     = job.role || job.title || 'Untitled role';
  const score    = Math.round(job.score || 0);
  const provLbl  = mode === 'demo' ? 'Demo' : (mode === 'anthropic' ? 'Claude' : 'Ollama');

  const STEPS = [
    'Reading the full posting and extracting requirements',
    'Comparing your resume keywords against the JD',
    'Reordering skills to front-load the strongest matches',
    'Rewriting experience bullets for this role',
    'Computing the before/after ATS score',
  ];

  return (
    <div className="ask-overlay" onClick={onClose}>
      <aside className="ask-drawer tailor-drawer" onClick={e => e.stopPropagation()}>
        <header className="ask-head">
          <div className="ask-head-l">
            <CompanyLogo company={co} fallbackVariant="v2" size={36}/>
            <div className="ask-head-meta">
              <div className="ask-head-eyebrow">
                <Icon name="wand-2" size={10}/> Tailor · {provLbl}
                {!isPro && mode === 'anthropic' && <span className="ask-pro-pill">Pro</span>}
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
          {loading && (
            <div className="tailor-loading">
              <div className="tailor-loading-eyebrow">
                <span className="spin"/> Generating tailored resume…
              </div>
              <div className="tailor-loading-hint">
                {mode === 'anthropic'
                  ? 'Claude is doing a careful pass over the JD and your profile. Usually 15–25 seconds.'
                  : mode === 'ollama'
                    ? 'Your local model is running — speed depends on the model size and your hardware.'
                    : 'Demo mode: keyword reorder + ATS score, no LLM. Should finish almost instantly.'}
              </div>
              <ol className="tailor-steps">
                {STEPS.map((s, i) => (
                  <li key={i} style={{ animationDelay: `${i * 200}ms` }}>
                    <Icon name="check" size={11}/> {s}
                  </li>
                ))}
              </ol>
            </div>
          )}

          {error && (
            <div className="tailor-error">
              <Icon name="alert-triangle" size={14}/>
              <div>
                <div className="tailor-error-h">Couldn't tailor this job</div>
                <div className="tailor-error-msg">{error}</div>
              </div>
            </div>
          )}

          {item && !loading && !error && (
            <div className="tailor-result">
              {/* Index-fed jobs (job_repo) don't carry a profile-relative
                  score in their raw row — the score lives on the *feed*
                  payload that JobsPage already has. Overlay it so the
                  embedded card's MATCH stat reads correctly without an
                  extra round-trip. */}
              <TailoredResumeCard item={{
                ...item,
                co:    item.co    || co,
                role:  item.role  || role,
                loc:   item.loc   || job.loc || job.location || '',
                score: item.score || score || 0,
              }}/>
            </div>
          )}
        </div>
      </aside>
    </div>
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
          <circle cx="70" cy="70" r={C} fill="none" strokeWidth="8" stroke="rgba(255,255,255,.06)"/>
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
  return (
    <div className="rs-deep fade-in">
      <article className="rs-narrative">
        <div className="rs-narrative-sub">Critical analysis</div>
        <h3 className="rs-narrative-h">A reading of <em>your resume</em></h3>
        {text
          ? <div className="rs-narrative-text">{text}</div>
          : <div className="set-helper">No narrative was generated. Re-scan to produce a detailed write-up.</div>}
      </article>

      {titles.length > 0 && (
        <div className="rs-deep-pillsec">
          <div className="rs-metric-group-h"><span style={{ display:'inline-flex', alignItems:'center', gap:8 }}><Icon name="briefcase" size={12}/>Target roles inferred</span></div>
          <div className="rs-deep-pills">
            {titles.map((t, i) => <span key={i} className="skill-pill hard">{t}</span>)}
          </div>
        </div>
      )}

      <div className="rs-rerun">
        <button className="btn-ghost" onClick={onRescan} disabled={rescanning}>
          {rescanning ? <span className="spin" style={{ width:11, height:11, borderWidth:1.5 }}/> : <Icon name="refresh-cw" size={11}/>}
          Re-scan & re-verify
        </button>
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
              style={{ position:'absolute', top:'calc(100% + 6px)', right:0, minWidth:200, zIndex:50 }}
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

              {tab === 'preview' && (
                <div className="data-card fade-in" style={{ padding:0, overflow:'hidden' }}>
                  <div style={{ padding:'10px 16px', background:'var(--bg-2)', borderBottom:'1px solid var(--bdr)', display:'flex', alignItems:'center', justifyContent:'space-between', gap:12, flexWrap:'wrap' }}>
                    <div style={{ display:'flex', alignItems:'center', gap:12, minWidth:0 }}>
                      <div style={{ fontSize:14.5, color:'var(--t3)', fontFamily:'var(--mono)', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{selected.filename}</div>
                      {/* Document / Text toggle — only shown when the user
                          uploaded an actual PDF we can embed. .docx / .tex /
                          .txt fall back to text-only because there's nothing
                          else to render. */}
                      {selected.original_url && selected.original_kind === 'pdf' && !isEditing && (
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
                          {selected.original_url ? (
                            <a className="icon-btn" title="Download original" href={selected.original_url}
                               download={selected.filename || true} target="_blank" rel="noopener noreferrer">
                              <Icon name="download" size={12}/>
                            </a>
                          ) : (
                            <button className="icon-btn" title="No original file" disabled style={{ opacity:.4, cursor:'not-allowed' }}><Icon name="download" size={12}/></button>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                  {(!isEditing && selected.original_url && selected.original_kind === 'pdf' && previewMode === 'document') ? (
                    <div style={{ padding:0, height:720, background:'#0f0f13' }}>
                      {/* Browsers render PDFs natively in an iframe. The
                          #toolbar=0&navpanes=0 hash hides the Chrome/Edge
                          built-in toolbar so the embed sits flush; Firefox
                          ignores the params, harmless. The key forces a
                          remount when the user switches resumes. */}
                      <iframe
                        key={selected.original_url}
                        src={selected.original_url + '#toolbar=0&navpanes=0'}
                        title="Resume preview"
                        style={{ width:'100%', height:'100%', border:'none', background:'#0f0f13' }}/>
                    </div>
                  ) : (
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
                      <pre style={{ margin:0, whiteSpace:'pre-wrap', fontSize:15.5, lineHeight:1.6, color:'#d1d1d6', fontFamily:'"JetBrains Mono", Menlo, monospace' }}>
                        {resumeText || 'No text content available.'}
                      </pre>
                    )}
                  </div>
                  )}
                </div>
              )}

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
   PROFILE PAGE
══════════════════════════════════════════════════════════ */
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

  const saveProfile = async () => {
    setSaving(true);
    try {
      const titles = splitList(form.target_titles).filter(Boolean);
      await api.post('/api/profile', formToProfile(form));
      // Keep the search config in sync so Phase 2 + the live job feed pick up
      // the new preferences. Only send fields the user actually edited this
      // session — `?? undefined` guards leave the previously-saved value intact.
      const cfg = { job_titles: titles.join(', ') };
      if (form.search_location !== undefined) cfg.location = form.search_location;
      if (Array.isArray(form.search_experience_levels)) cfg.experience_levels = form.search_experience_levels;
      if (Array.isArray(form.search_education_filter)) cfg.education_filter = form.search_education_filter;
      if (form.search_citizenship_filter !== undefined) cfg.citizenship_filter = form.search_citizenship_filter;
      if (Number.isFinite(form.search_max_scrape_jobs)) cfg.max_scrape_jobs = form.search_max_scrape_jobs;
      if (Number.isFinite(form.search_days_old)) cfg.days_old = form.search_days_old;
      if (Number.isFinite(form.search_threshold)) cfg.threshold = form.search_threshold;
      await api.post('/api/config', cfg);
      setDirty(false);
      await refresh?.();
    } catch (e) {
      alert(e.message || 'Failed to save profile');
    } finally {
      setSaving(false);
    }
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
        {dirty && !showExtractingBanner && (
          <span style={{ marginLeft:14, fontSize:14.5, color:'var(--warn)' }}>● Unsaved changes</span>
        )}
        <div className="head-spacer"/>
        <button className="btn-ghost" onClick={rerunExtraction} disabled={showExtractingBanner || !hasPrimary}>
          <Icon name="scan-text" size={13}/> {showExtractingBanner ? 'Extracting…' : 'Re-scrape resume'}
        </button>
        <button className="btn-ghost" onClick={saveProfile} disabled={saving} style={{ marginLeft:8 }}>
          <Icon name="save" size={13}/> {saving ? 'Saving...' : 'Save profile'}
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
                   Click <strong>Save profile</strong> to apply these to your next <em>agent</em> run.
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
/* The "Search Preferences" card lives at the top of the Personal tab so the
   five fields that drive job matching (target roles, search location, seniority,
   degree level, citizenship gate) are the FIRST thing a new user sees after
   their resume is parsed — and they can be edited at any time without hunting
   through Settings. Each field has a fallback chain:
     form.search_X  (user has typed something this session)
     ?? state.X     (persisted value from a previous save / resume extraction)
     ?? sane default
   so the inputs always have a controlled value and the user's edits stick. */
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
      <div className="profile-grid">
        <ProfileInput
          label="Target roles (comma-separated)"
          value={titles}
          onChange={v => updateField('target_titles', v)}
        />
        <ProfileInput
          label="Job-search location"
          value={searchLoc}
          onChange={v => updateField('search_location', v)}
        />
      </div>
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
        Click <strong>Save profile</strong> at the top right to apply.
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

/* ────────────────────────────────────────────────────────────────
   Profile links — industry-specific online presence

   The catalog below is the result of mapping which profile sites each
   industry's hiring pipeline actually values. It's grouped so the
   five sites a software engineer cares about don't drown out the
   three a journalist needs (and vice versa). Each entry has:

     key      : stable id, mirrors PROFILE_LINK_KEYS in profile_extractor.py
     label    : what we show in the input label
     mono     : 2-letter monogram for the colored chip
     color    : brand color used by the chip (semi-transparent so it
                works in both dark + light themes)
     hint     : URL example shown as the input placeholder so users see
                the canonical shape they should paste
   ──────────────────────────────────────────────────────────────── */
const PROFILE_LINK_GROUPS = [
  {
    name: 'Universal',
    description: 'Filled by every industry — start here.',
    icon: 'globe',
    defaultOpen: true,
    items: [
      { key: 'linkedin', label: 'LinkedIn',          mono: 'in', color: '#0A66C2', hint: 'linkedin.com/in/your-handle',     scalar: true },
      { key: 'website',  label: 'Personal website',  mono: 'WW', color: 'var(--accent)', hint: 'https://your-name.dev',     scalar: true },
      { key: 'twitter',  label: 'Twitter / X',       mono: '𝕏',  color: '#0F1419', hint: 'x.com/your-handle' },
    ],
  },
  {
    name: 'Software & Engineering',
    description: 'Code repos, Q&A reputation, and interview-prep proof.',
    icon: 'terminal',
    items: [
      { key: 'github',        label: 'GitHub',         mono: 'GH', color: '#1F2328', hint: 'github.com/your-handle',                scalar: true },
      { key: 'gitlab',        label: 'GitLab',         mono: 'GL', color: '#FC6D26', hint: 'gitlab.com/your-handle' },
      { key: 'stackoverflow', label: 'Stack Overflow', mono: 'SO', color: '#F58025', hint: 'stackoverflow.com/users/123/your-handle' },
      { key: 'leetcode',      label: 'LeetCode',       mono: 'LC', color: '#FFA116', hint: 'leetcode.com/u/your-handle' },
    ],
  },
  {
    name: 'Data, ML & AI',
    description: 'For data scientists, ML/AI engineers, and applied researchers.',
    icon: 'cpu',
    items: [
      { key: 'kaggle',         label: 'Kaggle',           mono: 'KG', color: '#20BEFF', hint: 'kaggle.com/your-handle' },
      { key: 'huggingface',    label: 'Hugging Face',     mono: 'HF', color: '#FFD21E', hint: 'huggingface.co/your-handle' },
      { key: 'paperswithcode', label: 'Papers With Code', mono: 'PC', color: '#21CBCE', hint: 'paperswithcode.com/author/your-handle' },
    ],
  },
  {
    name: 'Design & Creative',
    description: 'UI/UX, illustration, 3D — the portfolio sites recruiters open first.',
    icon: 'palette',
    items: [
      { key: 'dribbble',   label: 'Dribbble',   mono: 'DR', color: '#EA4C89', hint: 'dribbble.com/your-handle' },
      { key: 'behance',    label: 'Behance',    mono: 'BE', color: '#1769FF', hint: 'behance.net/your-handle' },
      { key: 'artstation', label: 'ArtStation', mono: 'AS', color: '#13AFF0', hint: 'artstation.com/your-handle' },
      { key: 'sketchfab',  label: 'Sketchfab',  mono: 'SF', color: '#1CAAD9', hint: 'sketchfab.com/your-handle' },
    ],
  },
  {
    name: 'Writing & Content',
    description: 'Long-form writing, newsletters, video essays.',
    icon: 'feather',
    items: [
      { key: 'medium',   label: 'Medium',     mono: 'MD', color: '#1A8917', hint: 'medium.com/@your-handle' },
      { key: 'substack', label: 'Substack',   mono: 'SS', color: '#FF6719', hint: 'your-handle.substack.com' },
      { key: 'youtube',  label: 'YouTube',    mono: 'YT', color: '#FF0000', hint: 'youtube.com/@your-handle' },
    ],
  },
  {
    name: 'Academic & Research',
    description: 'Citations, identifiers, and peer-network sites for researchers.',
    icon: 'graduation-cap',
    items: [
      { key: 'google_scholar', label: 'Google Scholar', mono: 'GS', color: '#4285F4', hint: 'scholar.google.com/citations?user=…' },
      { key: 'orcid',          label: 'ORCID',          mono: 'OR', color: '#A6CE39', hint: 'orcid.org/0000-0000-0000-0000' },
      { key: 'researchgate',   label: 'ResearchGate',   mono: 'RG', color: '#00CCBB', hint: 'researchgate.net/profile/Your-Name' },
      { key: 'academia',       label: 'Academia.edu',   mono: 'AE', color: '#41637E', hint: 'your-university.academia.edu/YourName' },
    ],
  },
  {
    name: 'Visual & Media',
    description: 'Photography, film, and short-form video.',
    icon: 'camera',
    items: [
      { key: 'instagram', label: 'Instagram',  mono: 'IG', color: '#E4405F', hint: 'instagram.com/your-handle' },
      { key: '500px',     label: '500px',      mono: '5P', color: '#0099E5', hint: '500px.com/p/your-handle' },
      { key: 'flickr',    label: 'Flickr',     mono: 'FL', color: '#FF0084', hint: 'flickr.com/photos/your-handle' },
      { key: 'vimeo',     label: 'Vimeo',      mono: 'VM', color: '#1AB7EA', hint: 'vimeo.com/your-handle' },
    ],
  },
  {
    name: 'Audio & Music',
    description: 'Catalog, tracks, and producer credits.',
    icon: 'music',
    items: [
      { key: 'soundcloud', label: 'SoundCloud', mono: 'SC', color: '#FF5500', hint: 'soundcloud.com/your-handle' },
      { key: 'bandcamp',   label: 'Bandcamp',   mono: 'BC', color: '#629AA9', hint: 'your-handle.bandcamp.com' },
    ],
  },
  {
    name: 'Business & Startups',
    description: 'Founders, investors, BD — where deal flow happens.',
    icon: 'briefcase',
    items: [
      { key: 'wellfound',   label: 'Wellfound (AngelList)', mono: 'WF', color: '#000000', hint: 'wellfound.com/u/your-handle' },
      { key: 'crunchbase',  label: 'Crunchbase',            mono: 'CB', color: '#146AFF', hint: 'crunchbase.com/person/your-handle' },
      { key: 'producthunt', label: 'Product Hunt',          mono: 'PH', color: '#DA552F', hint: 'producthunt.com/@your-handle' },
    ],
  },
  {
    name: 'Industry Specialties',
    description: 'Field-specific directories — fill what applies.',
    icon: 'badge-check',
    items: [
      { key: 'doximity',   label: 'Doximity (healthcare)',   mono: 'DX', color: '#00A0DF', hint: 'doximity.com/pub/your-handle' },
      { key: 'muckrack',   label: 'Muck Rack (journalism)',  mono: 'MR', color: '#2E3A89', hint: 'muckrack.com/your-handle' },
      { key: 'imdb',       label: 'IMDb (film/TV)',          mono: 'IM', color: '#F5C518', hint: 'imdb.com/name/nm0000000' },
      { key: 'martindale', label: 'Martindale (legal)',      mono: 'ML', color: '#BF1E2E', hint: 'martindale.com/attorney/your-name' },
    ],
  },
];

function ProfileLinksCard({ form, updateField }) {
  // Three groups stay open by default (Universal + the user's most likely
  // primary profession by inference), the rest collapse so the card doesn't
  // dominate the page. The user can click any header to toggle a group.
  const [openGroups, setOpenGroups] = useState(() => {
    const open = new Set();
    PROFILE_LINK_GROUPS.forEach(g => { if (g.defaultOpen) open.add(g.name); });
    return open;
  });
  const toggleGroup = (name) => {
    setOpenGroups(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
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
  const allItems = PROFILE_LINK_GROUPS.flatMap(g => g.items);
  const filled = allItems.filter(it => String(readValue(it) || '').trim()).length;
  const total = allItems.length;

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
        const isOpen = openGroups.has(group.name);
        const groupFilled = group.items.filter(it => String(readValue(it) || '').trim()).length;
        return (
          <div key={group.name} className={'profile-link-group' + (isOpen ? ' open' : '')}>
            <button
              type="button"
              className="profile-link-group-head"
              onClick={() => toggleGroup(group.name)}
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
  return (
    <label className="profile-link-row">
      <span
        className="profile-link-mono"
        style={{ background: item.color, color: _readableMonoText(item.color) }}
        aria-hidden="true"
      >
        {item.mono}
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

  // resume_file is "sessions/<sid>/<base>.pdf" when pdflatex / reportlab
  // produced a PDF, OR "sessions/<sid>/<base>.tex" when neither was
  // available. Build whichever links make sense, label them accurately.
  const fileBase = item.resume_file || '';
  const isPdf   = /\.pdf$/i.test(fileBase);
  const isTex   = /\.tex$/i.test(fileBase);
  const pdfHref = isPdf ? `/output/${fileBase}` : null;
  const texHref = isTex
    ? `/output/${fileBase}`
    : (fileBase ? `/output/${fileBase.replace(/\.pdf$/i, '.tex')}` : null);

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

          {(pdfHref || texHref) && (
            <div className="tr-actions">
              {pdfHref && (
                <a className="tr-dl tr-dl-primary" href={pdfHref} download>
                  <Icon name="download" size={12} color="#fff"/> Download PDF
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

  const mode = state?.mode || 'anthropic';
  // Neutral label — the user shouldn't have to know which provider is wired.
  const modeLabel = mode === 'demo' ? 'DEMO' : 'AI';

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

function AgentPage({ state, refresh }) {
  const [open,    setOpen]    = useState({});
  const [running, setRunning] = useState(null);
  const [errors,  setErrors]  = useState({});
  const [phaseResults, setPhaseResults] = useState({});
  const [phaseLogs, setPhaseLogs] = useState({});

  const done = useMemo(() => new Set(state?.done || []), [state?.done]);
  const pct  = Math.round((done.size / 7) * 100);
  const C = 56, circ = 2 * Math.PI * C;
  const off = circ - (circ * pct / 100);
  const ringTone = pct === 100 ? 'var(--good)' : pct > 0 ? 'var(--accent-h)' : 'var(--t4)';

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

  // Run-all uses a local "completed" set so phases finishing mid-loop are
  // tracked correctly (the captured `done` Set otherwise goes stale).
  const runAll = async () => {
    if (running) return;
    const completed = new Set(done);
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
          <button className="btn-ghost" onClick={() => api.post('/api/reset', {}).then(refresh)} disabled={!!running}>
            <Icon name="rotate-ccw" size={12}/> Reset
          </button>
          <button className="head-cta agent-runall" onClick={runAll} disabled={!!running}>
            {running
              ? <><span className="spin"/> Running phase {running}…</>
              : <><Icon name="play" size={13} color="#fff"/> Run all phases</>}
          </button>
        </div>
      </header>

      <div className="agent-grid">
        {/* ── Left: pipeline ──────────────────────────────────── */}
        <section className="agent-pipeline">
          <div className="agent-meter">
            <div className="agent-meter-ring">
              <svg width="120" height="120" viewBox="0 0 120 120">
                <circle cx="60" cy="60" r={C} fill="none" strokeWidth="6" stroke="rgba(255,255,255,.06)"/>
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
      // 402 Pro plan required: roll back optimistic state, surface inline.
      if (/Pro plan/i.test(e.message || '')) {
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
                {!isPro && <span className="set-label-hint">Claude requires Pro</span>}
              </div>
              <select className="set-select" value={cfg.mode} onChange={e => update({ mode: e.target.value })}>
                <option value="anthropic">Anthropic Claude (High quality){isPro ? '' : ' — Pro'}</option>
                <option value="ollama">Local Ollama (Free/Private)</option>
                <option value="demo">Demo mode (Offline/Template)</option>
              </select>
            </div>
            {planError && (
              <div className="plan-banner">
                <Icon name="lock" size={14}/>
                <div className="plan-banner-body">
                  <b>{planError}</b>
                  <span>{/cloud/i.test(planError) ? 'Switch to a local model, or upgrade to unlock cloud models.' : 'Switch your provider, or upgrade to unlock Claude.'}</span>
                </div>
                <button className="plan-banner-cta" onClick={() => setPage && setPage('plans')}>
                  View plans <Icon name="arrow-right" size={11}/>
                </button>
              </div>
            )}
            {cfg.mode === 'anthropic' && (
              <div className="set-field">
                <div className="set-label">Anthropic API Key</div>
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
          
          {/* Advanced */}
          <div className="set-sec">
            <div className="set-sec-h"><Icon name="cpu" size={14}/> Advanced</div>
            <Toggle field="quick_score_only" label="Quick score only" sub="Skip LLM rubric scoring (faster, less accurate)."/>
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
          <Icon name="zap" size={11}/> One simple split — local LLMs are free, Claude is Pro.
        </div>

        <div className="plans-grid">
          {/* FREE */}
          <div className={'plan-card' + (tier === 'free' ? ' current' : '')}>
            {tier === 'free' && <div className="plan-current-badge">Current plan</div>}
            <div className="plan-card-h">
              <div className="plan-name">Free</div>
              <div className="plan-price"><b>$0</b><span>/forever</span></div>
            </div>
            <div className="plan-tag">Bring your own local LLM</div>
            <ul className="plan-features">
              <li><Icon name="check" size={13}/> Demo mode (offline, template-based)</li>
              <li><Icon name="check" size={13}/> Local Ollama — private, free, your hardware</li>
              <li><Icon name="check" size={13}/> Full 7-phase pipeline</li>
              <li><Icon name="check" size={13}/> Excel tracker + run reports</li>
              <li><Icon name="check" size={13}/> Job discovery across all scrapers</li>
              <li><Icon name="check" size={13}/> Cover letter generation (template)</li>
              <li className="plan-feature-muted"><Icon name="x" size={13}/> Cloud Ollama models</li>
              <li className="plan-feature-muted"><Icon name="x" size={13}/> Anthropic Claude provider</li>
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
            <div className="plan-tag">Unlock Claude — bring your own API key</div>
            <ul className="plan-features">
              <li><Icon name="check" size={13}/> Everything in Free</li>
              <li className="plan-feature-hi"><Icon name="sparkles" size={13}/> Anthropic Claude provider unlocked</li>
              <li className="plan-feature-hi"><Icon name="sparkles" size={13}/> Cloud Ollama models unlocked</li>
              <li><Icon name="check" size={13}/> Higher-fidelity scoring &amp; tailoring</li>
              <li><Icon name="check" size={13}/> Better résumé critique &amp; ATS gap analysis</li>
              <li><Icon name="check" size={13}/> Priority support</li>
              <li className="plan-feature-muted"><Icon name="key" size={13}/> Bring your own ANTHROPIC_API_KEY</li>
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
            <summary>Why does Claude cost more if I bring my own key?</summary>
            <div>
              You pay Anthropic directly for tokens — we don't mark up the LLM. Pro covers the
              tooling around Claude: scoring rubrics, tailoring prompts, ATS gap analysis, and the
              orchestration layer that turns a résumé into 50+ tailored applications.
            </div>
          </details>
          <details className="plans-faq-item">
            <summary>Can I cancel anytime?</summary>
            <div>Yes — once Stripe billing is wired in, you'll have a self-serve customer portal. Today, contact the admin.</div>
          </details>
          <details className="plans-faq-item">
            <summary>What if I run Ollama locally?</summary>
            <div>
              Free plan covers Ollama fully. The whole pipeline works against your local model with
              zero API costs — just run <code>ollama serve</code> and pick a model in Settings.
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
  { id:'overview', label:'OVERVIEW', icon:'gauge'              },
  { id:'sessions', label:'SESSIONS', icon:'users'              },
  { id:'server',   label:'SERVER',   icon:'sliders-horizontal' },
  { id:'console',  label:'CONSOLE',  icon:'terminal'           },
  { id:'tweaks',   label:'TWEAKS',   icon:'wand-sparkles'      },
];

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
          <span className="dop-brand">JOBSAI <span>·</span> DEV</span>
          <span className={'dop-pulse dop-pulse-' + opsHealth}>
            <span className="dop-dot"/>
            {opsHealth === 'ok' ? 'LIVE' : opsHealth === 'warn' ? 'DEGRADED' : 'ALERT'}
          </span>
          {isImpersonating && (
            <span className="dop-pulse dop-pulse-warn">
              <Icon name="eye" size={10}/> IMPERSONATING
            </span>
          )}
        </div>
        <div className="dop-opsbar-meta">
          <span><i>UTC</i><b>{clock}</b></span>
          <span><i>DATE</i><b>{dateStamp}</b></span>
          <span><i>PY</i><b>{status.python || '—'}</b></span>
          <span><i>OUT</i><b>{status.output_files ?? 0}</b></span>
          <span><i>DB</i><b>{status.session_db_mb ?? 0}MB</b></span>
          <span><i>DISK</i><b>{status.disk_free_gb ?? 0}G</b></span>
        </div>
        <div className="dop-opsbar-right">
          {isImpersonating && (
            <button className="dop-btn dop-btn-warn" onClick={stopImpersonating}>
              <Icon name="user-minus" size={11}/> STOP
            </button>
          )}
          <button className="dop-btn" onClick={testAsCustomer}>
            <Icon name="user" size={11}/> AS CUSTOMER
          </button>
          <button className="dop-btn" onClick={refresh} disabled={refreshing}>
            {refreshing ? <span className="spin" style={{ width:11, height:11, borderWidth:2 }}/> : <Icon name="refresh-cw" size={11}/>}
            REFRESH
          </button>
        </div>
      </div>

      {/* ── Sub-nav ───────────────────────────────────────────────── */}
      <nav className="dop-tabs" role="tablist" aria-label="Dev sub-pages">
        {DEV_TABS.map((t, idx) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={devTab === t.id}
            className={'dop-tab' + (devTab === t.id ? ' on' : '')}
            onClick={() => setDevTab(t.id)}>
            <span className="dop-tab-num">[{String(idx + 1).padStart(2, '0')}]</span>
            <Icon name={t.icon} size={12}/>
            <span className="dop-tab-label">{t.label}</span>
          </button>
        ))}
        <span className="dop-tabs-trail">
          <span className="dop-tab-cursor">▸</span> {DEV_TABS.find(t => t.id === devTab)?.label.toLowerCase()}
        </span>
      </nav>

      {/* ── Sub-page body ────────────────────────────────────────── */}
      <div className="dop-body">

        {/* [01] OVERVIEW ── KPIs · system status · activity */}
        {devTab === 'overview' && (
          <div className="dop-page fade-in">
            <div className="dop-secrow">
              <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> KPIs</div>
              <div className="dop-sec-meta">last refresh {refreshing ? '… now' : '< 10s'}</div>
            </div>
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
                <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> SYSTEM</div>
                <div className="dop-keyval">
                  <div><span>app</span><b className={'tag tag-' + (status.app === 'running' ? 'ok' : 'bad')}>{status.app || '—'}</b></div>
                  <div><span>python</span><b>{status.python || '—'}</b></div>
                  <div><span>output_files</span><b>{status.output_files ?? 0}</b></div>
                  <div><span>session_files</span><b>{status.session_files ?? 0}</b></div>
                  <div><span>session_db_mb</span><b>{status.session_db_mb ?? 0}</b></div>
                  <div><span>disk_free_gb</span><b>{status.disk_free_gb ?? 0}</b></div>
                </div>
              </div>

              <div className="dop-panel">
                <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> ENV</div>
                <div className="dop-keyval">
                  <div><span>ANTHROPIC_API_KEY</span><b className={'tag tag-' + (runtime?.env?.anthropic_key_present ? 'ok' : 'bad')}>{runtime?.env?.anthropic_key_present ? 'present' : 'missing'}</b></div>
                  <div><span>SMTP</span><b className={'tag tag-' + (runtime?.env?.smtp_configured ? 'ok' : 'mid')}>{runtime?.env?.smtp_configured ? 'configured' : 'unset'}</b></div>
                  <div><span>OLLAMA_URL</span><b className="tag tag-mid">{runtime?.env?.ollama_url || '—'}</b></div>
                  <div><span>LOCAL_DEV_BYPASS</span><b className={'tag tag-' + (runtime?.env?.local_dev_bypass ? 'warn' : 'mid')}>{runtime?.env?.local_dev_bypass ? 'on' : 'off'}</b></div>
                  <div><span>maintenance</span><b className={'tag tag-' + (runtime?.runtime?.maintenance ? 'warn' : 'mid')}>{runtime?.runtime?.maintenance ? 'on' : 'off'}</b></div>
                  <div><span>verbose_logs</span><b className={'tag tag-' + (runtime?.runtime?.verbose_logs ? 'ok' : 'mid')}>{runtime?.runtime?.verbose_logs ? 'on' : 'off'}</b></div>
                </div>
              </div>

              <div className="dop-panel">
                <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> RECENT USERS</div>
                <div className="dop-recent-users">
                  {sessions.slice(0, 6).map(s => (
                    <button key={s.id} className="dop-recent-row" onClick={() => { setSelected(s); setDevTab('sessions'); }}>
                      <span className="dop-recent-id">{s.id.slice(0, 8)}</span>
                      <span className="dop-recent-name">{s.name || 'Anonymous'}</span>
                      <span className="dop-recent-phase">{s.done.length}/7</span>
                      {s.unread_feedback_count > 0 && (
                        <span className="dop-recent-fb"><Icon name="message-square" size={9}/>{s.unread_feedback_count}</span>
                      )}
                    </button>
                  ))}
                  {sessions.length === 0 && <div className="dop-empty">No sessions yet.</div>}
                </div>
              </div>
            </div>

            <div className="dop-secrow" style={{ marginTop: 4 }}>
              <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> ACTIVITY</div>
              <button className="dop-btn dop-btn-link" onClick={() => setDevTab('console')}>view full log →</button>
            </div>
            <div className="dop-events">
              {(data.events || []).slice(0, 8).map((e, i) => (
                <div key={i} className="dop-event">
                  <span>{new Date(e.ts).toLocaleTimeString()}</span>
                  <b>{e.kind}</b>
                  <p>{e.message}</p>
                </div>
              ))}
              {(data.events || []).length === 0 && <div className="dop-empty">No recent events.</div>}
            </div>
          </div>
        )}

        {/* [02] SESSIONS ── user list + inspector */}
        {devTab === 'sessions' && (
          <div className="dop-page dop-sessions fade-in">
            <aside className="dop-userlist">
              <div className="dop-sec-h" style={{ paddingLeft: 4 }}>
                <span className="dop-sec-prefix">{'>'}</span> USERS
                <span className="dop-pill-mini">{sessions.length}</span>
              </div>
              <div className="dop-userlist-scroll">
                {sessions.map(s => (
                  <button key={s.id}
                    className={'dop-user' + (active?.id === s.id ? ' on' : '')}
                    onClick={() => setSelected(s)}>
                    <span className="dop-user-av">{(s.name || 'U')[0]}</span>
                    <span className="dop-user-meta">
                      <b>{s.name || 'Anonymous'}</b>
                      <small>{s.email || s.resume_filename || s.id.slice(0, 10)}</small>
                    </span>
                    <span className="dop-user-tail">
                      {s.user_id && (
                        <span className={'plan-pill-mini plan-pill-' + (s.plan_tier || 'free')}>
                          {(s.plan_tier || 'free').slice(0, 4).toUpperCase()}
                        </span>
                      )}
                      <em>{s.done.length}/7</em>
                      {s.unread_feedback_count > 0 && (
                        <span className="dop-user-fb"><Icon name="message-square" size={9}/>{s.unread_feedback_count}</span>
                      )}
                    </span>
                  </button>
                ))}
                {sessions.length === 0 && <div className="dop-empty">No sessions.</div>}
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
                      <code className="dop-inspect-sid">{active.id}</code>
                    </div>
                    <div className="dop-inspect-actions">
                      <button className="dop-btn dop-btn-warn"
                        onClick={async () => { if (confirm('Reset this session state? Files will be deleted.')) { await api.post(`/api/dev/session/${active.id}/reset`, {}); refresh(); setSelected(null); } }}>
                        <Icon name="rotate-ccw" size={11}/> RESET
                      </button>
                      <button className="dop-btn dop-btn-bad"
                        onClick={async () => { if (confirm('Delete this user entirely? This cannot be undone.')) { await fetch(`/api/dev/session/${active.id}`, { method:'DELETE' }); refresh(); setSelected(null); } }}>
                        <Icon name="trash-2" size={11}/> DELETE
                      </button>
                      <button className="dop-btn dop-btn-accent" onClick={() => impersonate(active.id)}>
                        <Icon name="user-plus" size={11}/> VIEW AS USER
                      </button>
                    </div>
                  </div>

                  <div className="dop-inspect-grid">
                    <div className="dop-panel dop-panel-plan">
                      <div className="dop-sec-h">
                        <span className="dop-sec-prefix">{'>'}</span> PLAN
                        {active.is_developer && <span className="dop-pill-mini">DEV</span>}
                      </div>
                      {!active.user_id ? (
                        <div className="dop-empty">Anonymous session — no user account to bill.</div>
                      ) : (
                        <>
                          <div className="dop-keyval">
                            <div><span>tier</span>
                              <b className={'plan-pill plan-pill-' + (active.plan_tier || 'free')}>
                                {(active.plan_tier || 'free').toUpperCase()}
                              </b>
                            </div>
                            <div><span>email</span><b style={{fontFamily:'var(--mono)',fontSize:11}}>{active.email || '—'}</b></div>
                          </div>
                          <div className="dop-plan-actions">
                            {(active.plan_tier || 'free') === 'free' ? (
                              <button className="dop-btn dop-btn-accent" onClick={() => setPlanTier(active.user_id, 'pro')}>
                                <Icon name="zap" size={11}/> GRANT PRO
                              </button>
                            ) : (
                              <button className="dop-btn dop-btn-warn" onClick={() => setPlanTier(active.user_id, 'free')}>
                                <Icon name="arrow-down" size={11}/> REVOKE PRO
                              </button>
                            )}
                            {planFlash?.userId === active.user_id && (
                              <span className={'dop-plan-flash dop-plan-flash-' + planFlash.kind}>
                                <Icon name={planFlash.kind === 'ok' ? 'check' : 'x'} size={10}/>
                                {planFlash.kind === 'ok'
                                  ? `set to ${planFlash.tier}`
                                  : (planFlash.message || 'failed')}
                              </span>
                            )}
                          </div>
                        </>
                      )}
                    </div>

                    <div className="dop-panel">
                      <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> STATS</div>
                      <div className="dop-keyval">
                        <div><span>resume</span><b>{active.has_resume ? 'yes' : 'no'}</b></div>
                        <div><span>target</span><b>{active.target || '—'}</b></div>
                        <div><span>jobs</span><b>{active.job_count}</b></div>
                        <div><span>scored</span><b>{active.scored_count}</b></div>
                        <div><span>apps</span><b>{active.application_count}</b></div>
                        <div><span>applied</span><b>{active.applied_count}</b></div>
                      </div>
                      <div className="dop-phases">
                        {[1,2,3,4,5,6,7].map(n => (
                          <span key={n} className={active.done.includes(n) ? 'on' : ''}>{n}</span>
                        ))}
                      </div>
                    </div>

                    <div className="dop-panel dop-panel-feedback">
                      <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> FEEDBACK <span className="dop-pill-mini">{(fullState?.feedback || []).length}</span></div>
                      {loadingFull ? <div className="dop-empty">Loading…</div> :
                        ((fullState?.feedback || []).length > 0 ? (
                          <div className="dop-fb-list">
                            {fullState.feedback.map(f => (
                              <div key={f.id} className="dop-fb-item">
                                <div className="dop-fb-meta">
                                  <span>{new Date(f.created_at).toLocaleString()}</span>
                                  {!f.read && <span className="dop-fb-new">NEW</span>}
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
                                <Icon name="check-check" size={11}/> mark all read
                              </button>
                            )}
                          </div>
                        ) : <div className="dop-empty">No feedback from this user.</div>)
                      }
                    </div>

                    <div className="dop-panel">
                      <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> RESUME TEXT</div>
                      <pre className="dop-pre dop-pre-fixed">
                        {loadingFull ? 'Loading…' : (fullState?.resume_text || '∅  No resume uploaded.')}
                      </pre>
                    </div>

                    <div className="dop-panel">
                      <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> FULL STATE JSON</div>
                      <pre className="dop-pre dop-pre-fixed dop-pre-json">
                        {loadingFull ? 'Loading…' : JSON.stringify(fullState, null, 2)}
                      </pre>
                    </div>
                  </div>
                </>
              )}
            </section>
          </div>
        )}

        {/* [03] SERVER ── runtime + LLM + pipeline */}
        {devTab === 'server' && (
          <div className="dop-page fade-in">
            <div className="dop-secrow">
              <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> SERVER CONTROLS</div>
              <div className="dop-sec-meta">live · no restart</div>
            </div>

            <div className="sc-grid">
              <div className="sc-col sc-runtime">
                <div className="sc-col-h"><Icon name="server" size={11}/> Runtime · all sessions</div>

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
                <div className="sc-col-h"><Icon name="cpu" size={11}/> LLM Provider · this session</div>
                <div className="sc-radio-row">
                  {['anthropic', 'ollama', 'demo'].map(m => (
                    <button
                      key={m}
                      className={'sc-radio' + (globalState?.mode === m ? ' on' : '')}
                      onClick={() => saveSessionConfig({ mode: m })}>
                      {m === 'anthropic' ? 'Claude' : m === 'ollama' ? 'Ollama' : 'Demo'}
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
                <div className="sc-col-h"><Icon name="gauge" size={11}/> Pipeline · this session</div>
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

        {/* [04] CONSOLE ── CLI + events */}
        {devTab === 'console' && (
          <div className="dop-page dop-console fade-in">
            <div className="dop-panel dop-panel-cli">
              <div className="dop-secrow">
                <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> CLI</div>
                <div className="dop-sec-meta">whitelist · sandboxed</div>
              </div>
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
                  : <span className="dop-pre-hint">$ — pick a command above to run a sandboxed inspection.</span>}
              </pre>
            </div>

            <div className="dop-panel">
              <div className="dop-secrow">
                <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> RECENT EVENTS</div>
                <div className="dop-sec-meta">{(data.events || []).length} entries</div>
              </div>
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

        {/* [05] TWEAKS ── UI customizations */}
        {devTab === 'tweaks' && (
          <div className="dop-page fade-in" style={{ maxWidth: 720 }}>
            <div className="dop-secrow">
              <div className="dop-sec-h"><span className="dop-sec-prefix">{'>'}</span> SITE TWEAKS</div>
              <div className="dop-sec-meta">applied to this session only</div>
            </div>

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
      setState(next);
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
