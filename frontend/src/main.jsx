import React, { useEffect, useState, useRef } from 'react';
import { BrowserRouter, Routes, Route, Navigate, useNavigate, useParams, Link } from 'react-router-dom';
import { createRoot } from "react-dom/client";
import "./styles.css";

const navItems = [
  { label: "Origin", href: "#origin" },
  { label: "Product", href: "#product" },
  { label: "Workflow", href: "#workflow" },
  { label: "Issue Scout", href: "#scout" },
  { label: "Infrastructure", href: "#infrastructure" },
];

function Mark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <span />
      <span />
    </span>
  );
}

function GithubWindow() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', maxWidth: '780px', margin: '0 auto' }}>
      <img src="/bot-accepted.png" alt="Bot ACCEPTED response" style={{ width: '100%', borderRadius: '10px', border: '1px solid #30363d' }} />
      <img src="/bot-revision.png" alt="Bot REVISION_REQUIRED response" style={{ width: '100%', borderRadius: '10px', border: '1px solid #30363d' }} />
    </div>
  )
}

function GitHubIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path
        fill="currentColor"
        d="M12 .8a11.2 11.2 0 0 0-3.54 21.83c.56.1.77-.24.77-.54v-2.03c-3.13.68-3.79-1.32-3.79-1.32-.51-1.3-1.25-1.64-1.25-1.64-1.02-.7.08-.68.08-.68 1.13.08 1.72 1.16 1.72 1.16 1 .1.99 1.99 2.93 1.41.1-.7.39-1.18.7-1.45-2.5-.29-5.13-1.25-5.13-5.58 0-1.23.44-2.24 1.16-3.03-.12-.29-.5-1.45.11-3.02 0 0 .95-.3 3.1 1.15a10.7 10.7 0 0 1 5.64 0c2.15-1.46 3.1-1.15 3.1-1.15.61 1.57.23 2.73.11 3.02.72.79 1.16 1.8 1.16 3.03 0 4.34-2.64 5.29-5.16 5.57.4.35.75 1.04.75 2.1v3.11c0 .3.2.65.78.54A11.2 11.2 0 0 0 12 .8Z"
      />
    </svg>
  );
}

function Arrow() {
  return <span className="arrow">↗</span>;
}

function Navbar({ onOpenPanel }) {
  const [scrolled, setScrolled] = useState(false);

  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 20);
    fn();
    window.addEventListener("scroll", fn, { passive: true });
    return () => window.removeEventListener("scroll", fn);
  }, []);

  return (
    <div className={`nav-shell ${scrolled ? "scrolled" : ""}`}>
      <nav className="nav">
        <a className="brand" href="#top" aria-label="issueMatch home">
          <Mark />
          <span className="brand-name">issueMatch</span>
          <span className="brand-tag">For a more open tomorrow.</span>
        </a>

        <div className="nav-links">
          {navItems.map((item) => (
            <a key={item.href} href={item.href}>
              {item.label}
            </a>
          ))}
        </div>

        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <a className="nav-cta" href="/login">
            <GitHubIcon />
            Sign in <Arrow />
          </a>
          {onOpenPanel && (
            <button className="nav-cta" onClick={onOpenPanel} style={{ cursor: 'pointer', border: 'none', color: 'var(--text)' }}>
              Story
            </button>
          )}
        </div>
      </nav>
    </div>
  );
}

function Reveal({ children, className = "" }) {
  const [visible, setVisible] = useState(false);
  const ref = React.useRef(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setVisible(true);
          observer.disconnect();
        }
      },
      { threshold: 0.14 }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={ref} className={`reveal ${visible ? "is-visible" : ""} ${className}`}>
      {children}
    </div>
  );
}

function Hero() {
  return (
    <section className="hero" id="top">
      <div className="starfield" aria-hidden="true" />
      <div className="wrap hero-grid">
        <Reveal className="hero-copy-block">
          <div className="eyebrow mono">PEOPLE / CODE / A MORE OPEN TOMORROW</div>
          <h1>
            Open source
            <span>moves when people do.</span>
          </h1>
          <p className="hero-copy">
            issueMatch helps maintainers find technically grounded contributors
            and brings real issues to the surface — so good ideas can turn into
            meaningful impact.
          </p>
          <div className="hero-actions">
            <a className="btn btn-primary" href="#product">
              Explore issueMatch <Arrow />
            </a>
            <a className="btn btn-secondary" href="#workflow">
              See how it works <span className="down">↓</span>
            </a>
          </div>
          <div className="hero-micro">
            <span className="micro-line" />
            <span>Built for maintainers. Open to contributors.</span>
          </div>
        </Reveal>

        <Reveal className="earth-stage">
          <img
            className="earth"
            id="earth"
            src="/earth.png"
            alt="Nighttime Earth with global contributor connections"
          />
        </Reveal>
      </div>

      <div className="hero-footer wrap">
        <span className="mono">SHARED CODE / SHARED FUTURE</span>
        <span className="mono">SCROLL TO EXPLORE ↓</span>
      </div>
    </section>
  );
}

const originMoments = [
  {
    year: "1983",
    title: "GNU Project",
    body: "The GNU Project began building a Unix-like operating system around the idea that people should be free to study, modify, and share software.",
    token: "$ share(source)",
  },
  {
    year: "1991",
    title: "Linux kernel",
    body: "Linus Torvalds announced the Linux kernel, opening a project that would become one of the world's largest collaborative software efforts.",
    token: "git / linux",
  },
  {
    year: "1997",
    title: "The Bazaar",
    body: "Eric Raymond's essay The Cathedral and the Bazaar described an open, decentralized model for evolving software through many contributors.",
    token: "PR #open",
  },
  {
    year: "1998",
    title: "Open Source",
    body: "The Open Source Initiative was founded as the term “open source” took hold, giving the movement a shared vocabulary and a formal definition.",
    token: "open(source)",
  },
  {
    year: "2005 →",
    title: "Git",
    body: "Git was created to support the distributed development of the Linux kernel. Today, Git underpins much of the world's open-source collaboration.",
    token: "commit →",
  },
];

function OpenSourceScroll() {
  const sectionRef = React.useRef(null);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    const update = () => {
      const el = sectionRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const total = Math.max(1, rect.height - window.innerHeight);
      const next = Math.min(1, Math.max(0, -rect.top / total));
      setProgress(next);
    };

    update();
    window.addEventListener("scroll", update, { passive: true });
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update);
      window.removeEventListener("resize", update);
    };
  }, []);

  const logoTop = 5 + progress * 88;

  return (
    <section ref={sectionRef} className="section origin-scroll" id="origin">
      <div className="wrap origin-scroll-inner">
        <div className="origin-scroll-head">
          <Reveal>
            <div className="section-label mono">THE ORIGIN OF OPEN SOURCE</div>
            <h2 className="section-title">
              Code became a commons.
              <span>Then the world showed up.</span>
            </h2>
            <p className="section-copy">
              A short history of the ideas and infrastructure that turned
              software from something you consume into something you can shape.
            </p>
          </Reveal>
        </div>

        <div className="origin-journey">
          <div className="journey-rail">
            <div className="journey-rail-line" />
            <img
              className="osi-runner"
              src="/osi-mark.png"
              alt="Open Source Initiative mark"
              style={{ top: `${logoTop}%` }}
            />
            <span className="runner-glow" style={{ top: `${logoTop}%` }} />
          </div>

          <div className="journey-canvas">
            {originMoments.map((item, index) => {
              const threshold = index / (originMoments.length - 1);
              const active = progress >= threshold - 0.06;
              const eaten = progress > threshold + 0.09;

              return (
                <Reveal
                  className={`origin-moment ${active ? "active" : ""} ${eaten ? "eaten" : ""}`}
                  key={item.year}
                >
                  <div className="moment-marker">
                    <span>{String(index + 1).padStart(2, "0")}</span>
                  </div>
                  <div className="moment-copy">
                    <div className="moment-top">
                      <span className="year mono">{item.year}</span>
                      <span className="code-token mono">{item.token}</span>
                    </div>
                    <h3>{item.title}</h3>
                    <p>{item.body}</p>
                  </div>
                  <div className="moment-snack" aria-hidden="true">
                    {index === 0 ? "source" : index === 1 ? "commit" : index === 2 ? "pull" : index === 3 ? "issue" : "merge"}
                  </div>
                </Reveal>
              );
            })}
          </div>
        </div>

        <div className="origin-source-note mono">
          Historical milestones: GNU Project · Linux · The Cathedral and the Bazaar · Open Source Initiative · Git
        </div>
      </div>
    </section>
  );
}

function Product() {
  return (
    <section className="section product" id="product">
      <div className="wrap">
        <Reveal>
          <div className="section-label mono">THE PRODUCT</div>
          <div className="section-head">
            <h2 className="section-title">
              Don't reward the <em>loudest</em> claim.
            </h2>
            <p className="section-side-copy">
              Reward the approach that survives contact with the repository.
            </p>
          </div>
        </Reveal>

        <Reveal className="product-stage">
          <GithubWindow />
        </Reveal>
      </div>
    </section>
  );
}

const steps = [
  ["01", "Claim with context", "A contributor explains how they intend to solve the issue."],
  ["02", "Understand the repository", "issueMatch retrieves the issue, relevant code and tests."],
  ["03", "Verify the approach", "AI extracts claims; deterministic checks validate repository evidence."],
  ["04", "Recommend to the maintainer", "A GitHub-native report explains why the approach is qualified."],
];

function Workflow() {
  return (
    <section className="section workflow" id="workflow">
      <div className="wrap workflow-grid">
        <Reveal className="workflow-copy">
          <div className="section-label mono">THE FLOW</div>
          <h2 className="section-title">
            AI reasons.
            <span>Evidence decides.</span>
          </h2>
          <p className="section-copy">
            The model never gets to assign ownership. It turns intent into
            claims; the repository and policy layer decide whether those claims hold up.
          </p>

          <div className="process-list">
            {steps.map(([number, title, body]) => (
              <div className="process-item" key={number}>
                <span className="process-number mono">{number}</span>
                <div>
                  <h3>{title}</h3>
                  <p>{body}</p>
                </div>
              </div>
            ))}
          </div>
        </Reveal>

        <Reveal className="workflow-visual">
          <div className="signal-grid" />
          <div className="signal-orbit one" />
          <div className="signal-orbit two" />
          <div className="signal-orbit three" />
          <div className="signal-core">
            <span>REPOSITORY<br />EVIDENCE</span>
          </div>
          <div className="signal-card c1">comment → claims</div>
          <div className="signal-card c2">claims → evidence</div>
          <div className="signal-card c3">evidence → report</div>
          <div className="signal-card c4">maintainer → decision</div>
        </Reveal>
      </div>
    </section>
  );
}

function Scout() {
  return (
    <section className="section scout" id="scout">
      <div className="wrap">
        <Reveal>
          <div className="section-label mono">A SECOND LOOP</div>
          <div className="section-head">
            <h2 className="section-title">
              Let the repository
              <span>surface what matters.</span>
            </h2>
            <p className="section-side-copy">
              Issue Scout looks for actionable problems. An independent verifier
              tries to disprove them before anything reaches GitHub.
            </p>
          </div>
        </Reveal>

        <div className="scout-grid">
          <Reveal className="dark-card">
            <div className="card-index mono">AGENT / 01</div>
            <h3>Issue Scout</h3>
            <p>Finds suspicious behavior and produces a structured evidence packet.</p>
            <div className="terminal">
              <div className="terminal-bar">repository scan / latest push</div>
              <pre>{`Potential bug detected

src/auth.py:82
validate_token()

Expired JWT reaches
generic error handler.

confidence: 0.91`}</pre>
            </div>
          </Reveal>

          <Reveal className="dark-card verifier-card">
            <div className="card-index mono">AGENT / 02</div>
            <h3>Independent Verifier</h3>
            <p>Its job is not to agree. Its job is to find the evidence that proves the Scout wrong.</p>
            <div className="verify-list">
              <div><span>✓</span> Code path is reachable</div>
              <div><span>✓</span> Expected behavior documented</div>
              <div><span>✓</span> No existing issue matches</div>
              <div><span>✓</span> Existing tests miss the case</div>
            </div>
            <div className="verified-badge">VERIFIED → CREATE ISSUE</div>
          </Reveal>
        </div>
      </div>
    </section>
  );
}

function Infrastructure() {
  const nodes = [
    ["01", "GitHub", "events"],
    ["02", "API Gateway", "ingress"],
    ["03", "Lambda", "normalization"],
    ["04", "SQS", "backpressure"],
    ["05", "Step Functions", "orchestration"],
    ["06", "Amazon Bedrock", "reasoning"],
    ["07", "Verifier", "authority"],
    ["08", "DynamoDB", "state"],
  ];

  return (
    <section className="section infrastructure" id="infrastructure">
      <div className="wrap">
        <Reveal>
          <div className="section-label mono">BUILT FOR REAL SYSTEMS</div>
          <div className="section-head">
            <h2 className="section-title">
              Event-driven by design.
              <span>Human-controlled by default.</span>
            </h2>
          </div>
        </Reveal>

        <Reveal className="invariant-panel">
          <div className="invariant-copy">
            <div className="section-label mono">ONE RULE</div>
            <h3>AI can recommend. It can never grant ownership.</h3>
          </div>
          <div className="invariant-flow">
            <span>AI reasoning</span>
            <b>→</b>
            <span>Repository evidence</span>
            <b>→</b>
            <span>Maintainer decision</span>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

function FinalCTA({ onOpenPanel }) {
  return (
    <section className="final">
      <div className="final-constellation" aria-hidden="true">
        {Array.from({ length: 22 }).map((_, i) => (
          <i key={i} style={{ "--i": i }} />
        ))}
      </div>

      <div className="wrap">
        <Reveal>
          <div className="section-label mono">FOR THE PEOPLE WHO BUILD</div>
          <h2>
            A more open
            <span>tomorrow, together.</span>
          </h2>
          <p>
            Connect a repository. Let the code provide the context. Let people
            bring the ideas.
          </p>
          <div className="hero-actions center">
            <a className="btn btn-primary" href="#top">
              Install on GitHub <GitHubIcon /> <Arrow />
            </a>
            <button className="btn btn-secondary" onClick={onOpenPanel} style={{ cursor: 'pointer' }}>
              Our story <Arrow />
            </button>
          </div>
        </Reveal>

        <footer className="footer">
          <span>issueMatch © 2026</span>
          <span>PEOPLE / CODE / COMMUNITY</span>
          <span>BUILT FOR OPEN SOURCE</span>
        </footer>
      </div>
    </section>
  );
}


function Badge({ status }) {
  let styleClass = '';
  switch (status?.toLowerCase()) {
    case 'verified':
    case 'accepted':
      styleClass = 'badge-success';
      break;
    case 'rejected':
    case 'declined':
    case 'error':
      styleClass = 'badge-danger';
      break;
    case 'verifying':
    case 'monitoring':
    case 'waiting':
      styleClass = 'badge-info';
      break;
    case 'proposed':
    case 'revision required':
      styleClass = 'badge-warning';
      break;
    default:
      styleClass = 'badge-muted';
  }
  return <span className={`status-badge ${styleClass}`}>{status.toUpperCase()}</span>;
}

function Panel({ open, onClose }) {
  const closeRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  return (
    <>
      <div className={`panel-backdrop${open ? ' open' : ''}`} onClick={onClose} aria-hidden="true" />
      <aside className={`panel${open ? ' open' : ''}`} role="dialog" aria-modal="true" aria-label="Story dashboard">
        <div className="panel-header">
          <span className="panel-title">Story</span>
          <button ref={closeRef} className="panel-close" onClick={onClose} aria-label="Close panel">x</button>
        </div>
        <div className="panel-body" role="tabpanel">
          <div style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '32px' }}>

            {/* What we built */}
            <div>
              <h3 style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#8b949e', marginBottom: '12px' }}>What we built</h3>
              <p style={{ fontSize: '15px', lineHeight: '1.6', color: '#e6edf3', margin: 0 }}>
                IssueScout is an AI agent that analyzes your codebase to find potential bugs. 
                IssueVerify is an independent agent that validates those findings before a GitHub 
                issue is ever created. A third bot evaluates contributor comments — checking 
                whether a proposed fix actually matches the issue and the codebase.
              </p>
            </div>

            {/* Why we built it */}
            <div>
              <h3 style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#8b949e', marginBottom: '12px' }}>Why we built it</h3>
              <p style={{ fontSize: '15px', lineHeight: '1.6', color: '#e6edf3', margin: 0 }}>
                Open source contributors spend hours hunting for genuine issues. Maintainers 
                manually evaluate every contributor claim. We automated the discovery and 
                qualification layer — keeping the final decision with the maintainer.
              </p>
            </div>

            {/* Team photo */}
            <div>
              <h3 style={{ fontSize: '11px', letterSpacing: '0.1em', textTransform: 'uppercase', color: '#8b949e', marginBottom: '16px' }}>The team</h3>
              <img 
                src="/photo.jpeg" 
                alt="Team" 
                style={{ width: '100%', borderRadius: '10px', border: '1px solid #30363d', marginBottom: '20px' }} 
              />
              <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div style={{ padding: '12px 16px', background: '#161b22', borderRadius: '8px', border: '1px solid #21262d' }}>
                  <div style={{ fontSize: '14px', fontWeight: '600', color: '#e6edf3' }}>Atul Kumar &amp; Abhinav Bharadwaj</div>
                  <div style={{ fontSize: '12px', color: '#8b949e', marginTop: '4px' }}>Claim assignment · AWS integration</div>
                </div>
                <div style={{ padding: '12px 16px', background: '#161b22', borderRadius: '8px', border: '1px solid #21262d' }}>
                  <div style={{ fontSize: '14px', fontWeight: '600', color: '#e6edf3' }}>Yashas K N &amp; Koushik Suresh</div>
                  <div style={{ fontSize: '12px', color: '#8b949e', marginTop: '4px' }}>Issue creation · Frontend</div>
                </div>
              </div>
            </div>

          </div>
        </div>
      </aside>
    </>
  );
}

function Products() {
  return (
    <section className="section products-section" id="product">
      <div className="wrap">
        <Reveal>
          <div className="product-cards">
            <div className="product-card">
              <div className="product-label mono">PRODUCT A · ISSUE SCOUT</div>
              <h3 className="product-heading">Find issues before users do.</h3>
              <p className="product-desc">Every push to your default branch is analysed. Scout proposes findings. An independent verifier challenges each one. Only verified findings become GitHub issues.</p>
              <a href={import.meta.env.VITE_SCOUT_APP_INSTALL_URL} target="_blank" rel="noopener noreferrer" className="btn btn-secondary product-btn">Install Issue Scout →</a>
            </div>
            <div className="product-card">
              <div className="product-label mono">PRODUCT B · ASSIGNMENT BOT</div>
              <h3 className="product-heading">Right contributor, right issue.</h3>
              <p className="product-desc">Contributors comment with an implementation approach. The bot evaluates it against the issue and codebase. A maintainer gets a recommendation — never an automatic assignment.</p>
              <a href={import.meta.env.VITE_ASSIGNMENT_APP_INSTALL_URL} target="_blank" rel="noopener noreferrer" className="btn btn-secondary product-btn">Install Assignment Bot →</a>
              <div className="product-warning mono">Never assigns automatically. Maintainer decides.</div>
            </div>
          </div>
        </Reveal>
      </div>
    </section>
  );
}

const AuthContext = React.createContext(null)

function useAuth() {
  return React.useContext(AuthContext)
}

function AuthProvider({ children }) {
  const [user, setUser] = React.useState(null)
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    // Check if backend session exists
    fetch('/api/auth/me', { credentials: 'include' })
      .then(r => r.ok ? r.json() : null)
      .then(data => { setUser(data); setLoading(false) })
      .catch(() => setLoading(false))
  }, [])

  return (
    <AuthContext.Provider value={{ user, setUser, loading }}>
      {children}
    </AuthContext.Provider>
  )
}

function AuthGuard({ children }) {
  const { user, loading } = useAuth()
  if (loading) return <FullPageSpinner />
  if (!user) return <Navigate to="/login" replace />
  return children
}

function FullPageSpinner() {
  return (
    <div className="fullpage-center">
      <div className="spinner" aria-label="Loading" />
    </div>
  )
}

function LoginPage() {
  const { user } = useAuth()

  // If already logged in, go to dashboard
  if (user) return <Navigate to="/dashboard" replace />

  const handleLogin = () => {
    // Redirect to backend GitHub OAuth start
    window.location.href = '/api/auth/github'
  }

  return (
    <div className="login-page">
      <div className="login-card glass-card">
        <a className="brand login-brand" href="/">
          <Mark />
          <span className="brand-name">issueMatch</span>
        </a>

        <h1 className="login-heading">Welcome back.</h1>
        <p className="login-sub">
          Sign in with GitHub to manage your repositories and view bot activity.
        </p>

        <button className="btn btn-primary login-btn" onClick={handleLogin}>
          <GitHubIcon />
          Continue with GitHub
        </button>

        <p className="login-footer-note">
          By signing in you authorise issueMatch to read your repository list.
          No code is accessed without installing the GitHub App on a specific repository.
        </p>
      </div>
    </div>
  )
}

const MOCK_REPOS = [
  { owner: 'acme-org', repo: 'api-service',   scout: true,  lastPush: '2 hours ago',   findings: 3 },
  { owner: 'acme-org', repo: 'frontend',       scout: false, lastPush: '1 day ago',     findings: 0 },
  { owner: 'acme-org', repo: 'auth-service',   scout: true,  lastPush: '3 hours ago',   findings: 1 },
  { owner: 'acme-org', repo: 'workers',        scout: false, lastPush: '5 days ago',    findings: 0 },
]

function DashNav({ user }) {
  const { setUser } = useAuth()
  const navigate = useNavigate()

  const handleLogout = () => {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' })
      .finally(() => { setUser(null); navigate('/') })
  }

  return (
    <div className="dashnav">
      <a className="brand dashnav-brand" href="/">
        <Mark />
        <span className="brand-name">issueMatch</span>
      </a>
      <div className="dashnav-right">
        {user && (
          <span className="dashnav-user">
            {user.avatar && <img src={user.avatar} alt="" className="dashnav-avatar" />}
            {user.login}
          </span>
        )}
        <button className="btn btn-secondary dashnav-logout" onClick={handleLogout}
          style={{ fontSize: '12px', padding: '8px 14px' }}>
          Sign out
        </button>
      </div>
    </div>
  )
}

const INSTALL_SCOUT_URL = import.meta.env.VITE_SCOUT_APP_INSTALL_URL || 'https://github.com/apps/verifier-bot-dev/installations/new'
const INSTALL_ASSIGN_URL = import.meta.env.VITE_ASSIGNMENT_APP_INSTALL_URL || 'https://github.com/apps/PLACEHOLDER/installations/new'

function InstallGate({ onContinue }) {
  const navigate = useNavigate()

  return (
    <div style={{ maxWidth: '520px', margin: '64px auto 0', padding: '0 24px' }}>
      <h1 className="dash-heading" style={{ marginBottom: '12px' }}>Get started</h1>
      <p className="dash-sub" style={{ marginBottom: '40px' }}>
        Install at least one GitHub App to start monitoring your repositories.
        You choose exactly which repos get access during installation.
      </p>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', marginBottom: '32px' }}>
        <div className="glass-card" style={{ padding: '24px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '11px', fontFamily: 'var(--mono)', letterSpacing: '0.08em', color: 'var(--text-secondary)', marginBottom: '6px' }}>OPTIONAL</div>
              <div style={{ fontWeight: 500, marginBottom: '6px' }}>Issue Scout</div>
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                Analyses every push and opens GitHub issues for verified findings.
              </div>
            </div>
            <a
              href={INSTALL_SCOUT_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
              style={{ fontSize: '13px', whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              Install <Arrow />
            </a>
          </div>
        </div>

        <div className="glass-card" style={{ padding: '24px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
            <div>
              <div style={{ fontSize: '11px', fontFamily: 'var(--mono)', letterSpacing: '0.08em', color: 'var(--text-secondary)', marginBottom: '6px' }}>OPTIONAL</div>
              <div style={{ fontWeight: 500, marginBottom: '6px' }}>Assignment Bot</div>
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                Evaluates contributor approaches and recommends who to assign.
              </div>
            </div>
            <a
              href={INSTALL_ASSIGN_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
              style={{ fontSize: '13px', whiteSpace: 'nowrap', flexShrink: 0 }}
            >
              Install <Arrow />
            </a>
          </div>
        </div>
      </div>

      <button
        className="btn btn-primary"
        style={{ width: '100%', justifyContent: 'center', fontSize: '15px', padding: '14px' }}
        onClick={onContinue}
      >
        I've installed — take me to my dashboard →
      </button>

      <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '16px', textAlign: 'center' }}>
        Install at least one app first, then click the button above.
      </p>
    </div>
  )
}

function SetupPage() {
  const { user } = useAuth()
  const navigate = useNavigate()

  return (
    <div className="dash-shell">
      <DashNav user={user} />
      <div className="dash-body">
        <div style={{ maxWidth: '520px', margin: '64px auto 0', padding: '0 24px' }}>
          <h1 className="dash-heading" style={{ marginBottom: '12px' }}>Install GitHub Apps</h1>
          <p className="dash-sub" style={{ marginBottom: '40px' }}>
            Install one or both apps on your repositories. You choose exactly
            which repos get access during installation.
          </p>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px', marginBottom: '32px' }}>
            <div className="glass-card" style={{ padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
                <div>
                  <div style={{ fontSize: '11px', fontFamily: 'var(--mono)', letterSpacing: '0.08em', color: 'var(--text-secondary)', marginBottom: '6px' }}>PRODUCT A</div>
                  <div style={{ fontWeight: 500, marginBottom: '6px' }}>Issue Scout</div>
                  <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                    Analyses every push and opens GitHub issues for verified findings.
                  </div>
                </div>
                <a
                  href={INSTALL_SCOUT_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn btn-secondary"
                  style={{ fontSize: '13px', whiteSpace: 'nowrap', flexShrink: 0 }}
                >
                  Install <Arrow />
                </a>
              </div>
            </div>

            <div className="glass-card" style={{ padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
                <div>
                  <div style={{ fontSize: '11px', fontFamily: 'var(--mono)', letterSpacing: '0.08em', color: 'var(--text-secondary)', marginBottom: '6px' }}>PRODUCT B</div>
                  <div style={{ fontWeight: 500, marginBottom: '6px' }}>Assignment Bot</div>
                  <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                    Evaluates contributor approaches and recommends who to assign.
                  </div>
                </div>
                <a
                  href={INSTALL_ASSIGN_URL}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn btn-secondary"
                  style={{ fontSize: '13px', whiteSpace: 'nowrap', flexShrink: 0 }}
                >
                  Install <Arrow />
                </a>
              </div>
            </div>
          </div>

          <button
            className="btn btn-primary"
            style={{ width: '100%', justifyContent: 'center', fontSize: '15px', padding: '14px' }}
            onClick={() => navigate('/dashboard')}
          >
            Go to dashboard →
          </button>
        </div>
      </div>
    </div>
  )
}

function RepoPicker() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [repos, setRepos] = React.useState([])
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState(null)

  React.useEffect(() => {
    fetch('/api/repos', { credentials: 'include' })
      .then(r => r.ok ? r.json() : Promise.reject('Failed to load repos'))
      .then(data => { setRepos(data); setLoading(false) })
      .catch(err => {
        setError(typeof err === 'string' ? err : 'Could not load repositories.')
        setLoading(false)
      })
  }, [])

  const hasRepos = repos.length > 0

  return (
    <div className="dash-shell">
      <DashNav user={user} />
      <div className="dash-body">
        {loading && <FullPageSpinner />}

        {!loading && error && (
          <div className="repopicker-wrap">
            <div className="dash-error">{error}</div>
          </div>
        )}

        {!loading && !error && !hasRepos && <Navigate to="/setup" replace />}

        {!loading && !error && hasRepos && (
          <div className="repopicker-wrap">
            <div className="repopicker-header">
              <h1 className="dash-heading">Your repositories</h1>
              <p className="dash-sub">Select a repository to view bot activity and manage settings.</p>
            </div>

            <div className="install-nudge glass-card" style={{ marginBottom: '24px' }}>
              <p className="install-nudge-text">Don't see a repository?</p>
              <Link
                to="/setup"
                className="btn btn-secondary"
                style={{ fontSize: '13px' }}
              >
                Install GitHub Apps <Arrow />
              </Link>
            </div>

            <div className="repo-list">
              {repos.map(r => (
                <button
                  key={`${r.owner}/${r.repo}`}
                  className="repo-card"
                  onClick={() => navigate(`/dashboard/${r.owner}/${r.repo}`)}
                >
                  <div className="repo-card-left">
                    <div className="repo-card-name">{r.owner} / <strong>{r.repo}</strong></div>
                    <div className="repo-card-meta">Last push {r.lastPush}</div>
                  </div>
                  <div className="repo-card-right">
                    {r.scout && <span className="badge badge-success">Scout on</span>}
                    {r.findings > 0 && (
                      <span className="repo-findings">{r.findings} findings</span>
                    )}
                    <span className="repo-arrow">→</span>
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

const MOCK_FINDINGS = [
  { id: 'f1', commit: 'a3f9c12', title: 'Unhandled promise rejection in payment webhook', status: 'verified',  issueUrl: 'https://github.com/acme-org/api-service/issues/142', time: '2h ago' },
  { id: 'f2', commit: 'b7d2e88', title: 'Rate limiter bypass via header spoofing',          status: 'verifying', issueUrl: null, time: '18m ago' },
  { id: 'f3', commit: 'c1a4f55', title: 'Memory leak in useEffect — missing cleanup',       status: 'proposed',  issueUrl: null, time: '5m ago' },
  { id: 'f4', commit: 'd9b3c71', title: 'Database pool config flagged incorrectly',         status: 'rejected',  issueUrl: null, time: '1d ago' },
  { id: 'f5', commit: 'e2f8a30', title: 'Monitoring push on default branch',                status: 'monitoring',issueUrl: null, time: 'just now' },
]

const MOCK_ASSIGNMENTS = [
  { id: 'a1', issue: '#142 — Unhandled promise rejection', candidate: '@sara-dev',        outcome: 'accepted',  feedback: 'Approach identifies the correct async boundary. Aligns with existing patterns.',        time: '1h ago' },
  { id: 'a2', issue: '#88 — Memory leak in Dashboard',     candidate: '@ben-writes-code', outcome: 'revision required',  feedback: 'Correct direction but proposes class lifecycle — this is a hooks component.',            time: '3h ago' },
  { id: 'a3', issue: '#31 — Token refresh race condition', candidate: '@miko-sec',        outcome: 'declined',  feedback: 'No code-relevant detail about the specific race condition in the token refresh flow.',    time: '5h ago' },
  { id: 'a4', issue: '#88 — Memory leak in Dashboard',     candidate: '@priya-frontend',  outcome: 'waiting',   feedback: 'Earlier candidate accepted. Evaluation paused until maintainer unassigns.',               time: '2h ago' },
]

function RepoDashboard() {
  const { owner, repo } = useParams()
  const { user } = useAuth()
  const navigate = useNavigate()
  const [scoutEnabled, setScoutEnabled] = React.useState(true)
  const [findings, setFindings] = React.useState([])
  const [assignments, setAssignments] = React.useState([])
  const [togglingScout, setTogglingScout] = React.useState(false)
  const [loading, setLoading] = React.useState(true)

  React.useEffect(() => {
    const headers = { credentials: 'include' }
    Promise.all([
      fetch(`/api/repos/${owner}/${repo}/scout/enabled`, headers).then(r => r.json()),
      fetch(`/api/repos/${owner}/${repo}/scout/findings`, headers).then(r => r.json()),
      fetch(`/api/repos/${owner}/${repo}/assignments`, headers).then(r => r.json()),
    ])
      .then(([toggle, finds, assigns]) => {
        setScoutEnabled(toggle.enabled ?? true)
        setFindings(Array.isArray(finds) ? finds : [])
        setAssignments(Array.isArray(assigns) ? assigns : [])
      })
      .catch(err => console.error('Dashboard load error:', err))
      .finally(() => setLoading(false))
  }, [owner, repo])

  const handleToggleScout = () => {
    setTogglingScout(true)
    fetch(`/api/repos/${owner}/${repo}/scout/enabled`, {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !scoutEnabled })
    })
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(data => setScoutEnabled(data.enabled))
      .catch(() => {})
      .finally(() => setTogglingScout(false))
  }

  return (
    <div className="dash-shell">
      <DashNav user={user} />
      <div className="dash-body">
        <div className="repodash-wrap">

          {/* Breadcrumb */}
          <div className="dash-breadcrumb">
            <button className="dash-back" onClick={() => navigate('/dashboard')}>← Repositories</button>
            <span className="dash-breadcrumb-sep">/</span>
            <span className="dash-breadcrumb-repo">{owner} / <strong>{repo}</strong></span>
          </div>

          {loading && <div style={{ color: 'var(--text-secondary)', marginBottom: '32px', fontSize: '14px' }}>Loading…</div>}

          {/* Two panels side by side */}
          <div className="repodash-grid">

            {/* Left: Assignment Bot */}
            <div className="dash-panel">
              <div className="dash-panel-header">
                <div>
                  <div className="dash-panel-tag">Product B</div>
                  <h2 className="dash-panel-title">Assignment Bot</h2>
                  <p className="dash-panel-desc">Always active. Evaluates contributor approaches on issue comments.</p>
                </div>
                <span className="badge badge-success" style={{ flexShrink: 0, display: 'inline-block', borderRadius: '999px', padding: '2px 9px', fontSize: '10px', fontFamily: 'var(--mono)', letterSpacing: '0.05em', whiteSpace: 'nowrap', background: 'rgba(52, 211, 153, 0.15)', color: 'var(--green)' }}>Always on</span>
              </div>

              <div className="dash-panel-notice">
                The bot recommends. <strong>You assign.</strong> Every evaluation posts a comment directly on the GitHub issue.
              </div>

              <div className="dash-panel-list">
                {assignments.length === 0 && !loading && (
                  <div style={{ color: 'var(--text-secondary)', fontSize: '13px', padding: '16px 0' }}>
                    No open issues found in this repository.
                  </div>
                )}
                {assignments.map(a => (
                  <div key={a.id} className="dash-item">
                    <div className="dash-item-header">
                      <span className="dash-item-title">{a.issue}</span>
                      <Badge status={a.outcome} />
                    </div>
                    <div className="dash-item-meta">{a.candidate} · {a.time}</div>
                    <div className="dash-item-feedback">{a.feedback}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* Right: Issue Scout */}
            <div className="dash-panel">
              <div className="dash-panel-header">
                <div>
                  <div className="dash-panel-tag">Product A</div>
                  <h2 className="dash-panel-title">Issue Scout</h2>
                  <p className="dash-panel-desc">Analyses every push to the default branch for code-grounded problems.</p>
                </div>
                <button
                  className={`scout-toggle ${scoutEnabled ? 'on' : 'off'} ${togglingScout ? 'loading' : ''}`}
                  onClick={handleToggleScout}
                  aria-label={scoutEnabled ? 'Disable Issue Scout' : 'Enable Issue Scout'}
                  disabled={togglingScout}
                >
                  <span className="scout-toggle-knob" />
                </button>
              </div>

              {!scoutEnabled && (
                <div className="dash-panel-notice warn">
                  Scout is paused. Push events are not being analysed.
                </div>
              )}

              {scoutEnabled && (
                <div className="dash-panel-notice">
                  Not every push creates an issue. Only verified findings become GitHub issues.
                </div>
              )}

              <div className="dash-panel-list">
                {findings.length === 0 && !loading && (
                  <div style={{ color: 'var(--text-secondary)', fontSize: '13px', padding: '16px 0' }}>
                    No findings yet — push to the default branch to trigger a scan.
                  </div>
                )}
                {findings.map(f => (
                  <div key={f.id} className="dash-item">
                    <div className="dash-item-header">
                      <span className="dash-item-title">{f.title}</span>
                      <Badge status={f.status} />
                    </div>
                    <div className="dash-item-meta">
                      <code>{f.commit}</code> · {f.time}
                    </div>
                    {f.issueUrl && (
                      <a href={f.issueUrl} target="_blank" rel="noopener noreferrer" className="dash-item-link">
                        View GitHub issue →
                      </a>
                    )}
                  </div>
                ))}
              </div>
            </div>

          </div>
        </div>
      </div>
    </div>
  )
}

function LandingApp() {
  const [panelOpen, setPanelOpen] = React.useState(false)
  return (
    <>
      <Navbar onOpenPanel={() => setPanelOpen(true)} />
      <main>
        <Hero />
        <OpenSourceScroll />
        <Product />
        <Workflow />
        <Scout />
        <Products />
        <Infrastructure />
        <FinalCTA onOpenPanel={() => setPanelOpen(true)} />
      </main>
      <Panel open={panelOpen} onClose={() => setPanelOpen(false)} />
    </>
  )
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<LandingApp />} />
          <Route path="/login" element={<LoginPage />} />
          <Route path="/dashboard" element={<AuthGuard><RepoPicker /></AuthGuard>} />
          <Route path="/dashboard/:owner/:repo" element={<AuthGuard><RepoDashboard /></AuthGuard>} />
          <Route path="/setup" element={<AuthGuard><SetupPage /></AuthGuard>} />
<Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  )
}

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
)
