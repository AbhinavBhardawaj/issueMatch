import re

with open("frontend/src/main.jsx", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add import React, { useEffect, useState } from 'react' if not present
if "import React" in content and "useState" not in content:
    content = content.replace('import React, { useEffect, useState } from "react";', 'import React, { useEffect, useState, useRef } from "react";')
else:
    # Just replace the top import
    content = content.replace('import React, { useEffect, useState } from "react";', 'import React, { useEffect, useState, useRef } from "react";')

# 2. Replace Navbar
# Use regex to find Navbar
navbar_pattern = re.compile(r'function Navbar\(\) \{.*?\n\}', re.DOTALL)
new_navbar = """function Navbar({ setPanel }) {
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

        <div style={{ display: 'flex', gap: '8px' }}>
          <a className="nav-cta" href={import.meta.env.VITE_SCOUT_APP_INSTALL_URL} target="_blank" rel="noopener noreferrer">
            <GitHubIcon />
            Install Scout <Arrow />
          </a>
          <button className="nav-cta" onClick={() => setPanel(true)} style={{ border: 'none', fontFamily: 'inherit', cursor: 'pointer' }}>
            Activity
          </button>
        </div>
      </nav>
    </div>
  );
}"""

content = navbar_pattern.sub(new_navbar, content, count=1)

# 3. Replace App
app_pattern = re.compile(r'function App\(\) \{.*', re.DOTALL)

new_components = """
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
  const [activeTab, setActiveTab] = useState('scout');
  const closeRef = useRef(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = e => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const scoutFindings = [
    { title: 'Possible memory leak in useEffect', status: 'proposed', detail: 'Scout identified this pattern in src/components/Dashboard.jsx line 44.' },
    { title: 'Unhandled promise rejection in payment handler', status: 'verifying', detail: 'Verifier is independently challenging Scout\\'s finding.' },
    { title: 'Rate limiter bypass via header spoofing', status: 'verified', detail: 'Finding verified. Issue #142 created on GitHub.', issueUrl: 'https://github.com/example/repo/issues/142' },
    { title: 'Database pool config flagged', status: 'rejected', detail: 'Verifier determined the pool limit is intentional.' },
    { title: 'Analysis failed on commit d4b9f01', status: 'error', detail: 'The AI model returned a malformed response.' },
    { title: 'Watching commit e2f8a30', status: 'monitoring', detail: 'Scout is watching this push. No finding proposed yet.' },
  ];

  const assignmentDecisions = [
    { issue: '#142 — Unhandled promise rejection', candidate: '@sara-dev', outcome: 'ACCEPTED', feedback: 'Approach identifies the correct async boundary and proposes wrapping the handler in try/catch.' },
    { issue: '#88 — Memory leak in Dashboard component', candidate: '@ben-writes-code', outcome: 'REVISION REQUIRED', feedback: 'Approach is correct in principle but proposes removing the listener incorrectly.' },
    { issue: '#88 — Memory leak in Dashboard component', candidate: '@priya-frontend', outcome: 'WAITING', feedback: 'An earlier candidate was accepted for this issue. Evaluation paused.' },
    { issue: '#31 — Token refresh race condition', candidate: '@miko-sec', outcome: 'DECLINED', feedback: 'The comment describes a general solution but does not engage with the specific race condition.' },
  ];

  return (
    <>
      <div className={`panel-backdrop${open ? ' open' : ''}`} onClick={onClose} aria-hidden="true" />
      <aside className={`panel${open ? ' open' : ''}`} role="dialog" aria-modal="true" aria-label="Activity dashboard">
        <div className="panel-header">
          <span className="panel-title">Activity</span>
          <button ref={closeRef} className="panel-close" onClick={onClose} aria-label="Close panel">×</button>
        </div>
        <div className="panel-tabs" role="tablist">
          <button role="tab" aria-selected={activeTab === 'scout'} className={`panel-tab${activeTab === 'scout' ? ' active' : ''}`} onClick={() => setActiveTab('scout')}>Issue Scout</button>
          <button role="tab" aria-selected={activeTab === 'assignment'} className={`panel-tab${activeTab === 'assignment' ? ' active' : ''}`} onClick={() => setActiveTab('assignment')}>Assignment Bot</button>
        </div>
        <div className="panel-body" role="tabpanel">
          {activeTab === 'scout' && (
            <div>
              <div className="demo-banner mono">Demo data — replace with API</div>
              {scoutFindings.map((f, i) => (
                <div key={i} className="state-card">
                  <div className="state-card-header">
                    <div className="state-card-title">{f.title}</div>
                    <Badge status={f.status} />
                  </div>
                  <div className="state-card-body">
                    {f.detail}
                    {f.issueUrl && <div><a href={f.issueUrl} target="_blank" rel="noopener noreferrer" className="state-card-link">View on GitHub</a></div>}
                  </div>
                </div>
              ))}
            </div>
          )}
          {activeTab === 'assignment' && (
            <div>
              <div className="demo-banner notice">The bot recommends. A maintainer always assigns.</div>
              {assignmentDecisions.map((d, i) => (
                <div key={i} className="state-card">
                  <div className="state-card-header">
                    <div>
                      <div className="state-card-title">{d.issue}</div>
                      <div className="state-card-meta">{d.candidate}</div>
                    </div>
                    <Badge status={d.outcome} />
                  </div>
                  <div className="state-card-body">{d.feedback}</div>
                </div>
              ))}
            </div>
          )}
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

function App() {
  const [panelOpen, setPanelOpen] = useState(false);
  return (
    <>
      <Navbar setPanel={setPanelOpen} />
      <main>
        <Hero />
        <OpenSourceScroll />
        <Product />
        <Workflow />
        <Scout />
        <Products />
        <Infrastructure />
        <FinalCTA />
      </main>
      <Panel open={panelOpen} onClose={() => setPanelOpen(false)} />
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
"""

content = app_pattern.sub(new_components, content)

with open("frontend/src/main.jsx", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated main.jsx using regex")
