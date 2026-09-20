import re

with open("frontend/src/main.jsx", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Merge the import for react-router-dom at the top
if "import { BrowserRouter" not in content:
    content = content.replace(
        'import React, { useEffect, useState, useRef } from "react";',
        "import React, { useEffect, useState, useRef } from 'react';\nimport { BrowserRouter, Routes, Route, Navigate, useNavigate, useParams, Link } from 'react-router-dom';"
    )

# 2. Update Navbar
content = content.replace("function Navbar({ setPanel }) {", "function Navbar({ onOpenPanel }) {")

old_nav_cta = """        <div style={{ display: 'flex', gap: '8px' }}>
          <a className="nav-cta" href={import.meta.env.VITE_SCOUT_APP_INSTALL_URL} target="_blank" rel="noopener noreferrer">
            <GitHubIcon />
            Install Scout <Arrow />
          </a>
          <button className="nav-cta" onClick={() => setPanel(true)} style={{ color: 'var(--text)', cursor: 'pointer' }}>
            Activity
          </button>
        </div>"""

new_nav_cta = """        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <a className="nav-cta" href="/login">
            <GitHubIcon />
            Sign in <Arrow />
          </a>
          {onOpenPanel && (
            <button className="nav-cta" onClick={onOpenPanel} style={{ cursor: 'pointer', border: 'none', color: 'var(--text)' }}>
              Activity
            </button>
          )}
        </div>"""

content = content.replace(old_nav_cta, new_nav_cta)

# 3. Replace function App() downwards
app_pattern = re.compile(r'function App\(\) \{.*', re.DOTALL)

new_components = """const AuthContext = React.createContext(null)

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
      .catch(() => {
        // Fall back to demo data
        setRepos(MOCK_REPOS)
        setLoading(false)
      })
  }, [])

  return (
    <div className="dash-shell">
      <DashNav user={user} />
      <div className="dash-body">
        <div className="repopicker-wrap">
          <div className="repopicker-header">
            <h1 className="dash-heading">Your repositories</h1>
            <p className="dash-sub">Select a repository to view bot activity and manage settings.</p>
            <div className="demo-pill">Demo data — connect your backend to see real repos</div>
          </div>

          {loading && <FullPageSpinner />}

          {error && (
            <div className="dash-error">{error}</div>
          )}

          {!loading && (
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
          )}

          <div className="install-nudge glass-card">
            <p className="install-nudge-text">Don't see a repository?</p>
            <a
              href={import.meta.env.VITE_SCOUT_APP_INSTALL_URL || 'https://github.com/apps/verifier-bot-dev/installations/new'}
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
              style={{ fontSize: '13px' }}
            >
              Install GitHub App on more repos <Arrow />
            </a>
          </div>
        </div>
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
  const [findings, setFindings] = React.useState(MOCK_FINDINGS)
  const [assignments, setAssignments] = React.useState(MOCK_ASSIGNMENTS)
  const [togglingScout, setTogglingScout] = React.useState(false)

  const handleToggleScout = () => {
    setTogglingScout(true)
    fetch(`/api/repos/${owner}/${repo}/scout/enabled`, {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !scoutEnabled })
    })
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(() => setScoutEnabled(e => !e))
      .catch(() => setScoutEnabled(e => !e)) // optimistic in demo
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

          <div className="demo-pill" style={{ marginBottom: '32px' }}>
            Demo data — connect <code>/api/repos/{owner}/{repo}/scout/findings</code> and <code>/api/repos/{owner}/{repo}/assignments</code>
          </div>

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
        <FinalCTA />
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
"""

content = app_pattern.sub(new_components, content)

with open("frontend/src/main.jsx", "w", encoding="utf-8") as f:
    f.write(content)
print("Updated main.jsx")


css_to_append = """
/* SPINNER */
.fullpage-center {
  min-height: 100svh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--bg);
}

.spinner {
  width: 28px;
  height: 28px;
  border: 2px solid rgba(255, 255, 255, 0.08);
  border-top-color: rgba(255, 255, 255, 0.55);
  border-radius: 50%;
  animation: spin 0.75s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* LOGIN */
.login-page {
  min-height: 100svh;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.glass-card {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(24px);
  -webkit-backdrop-filter: blur(24px);
  border-radius: 20px;
}

.login-card {
  width: 100%;
  max-width: 420px;
  padding: 48px 44px 44px;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0;
}

.login-brand {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 40px;
  text-decoration: none;
}

.login-heading {
  margin: 0 0 10px;
  font-size: 32px;
  font-weight: 600;
  letter-spacing: -0.05em;
  line-height: 1.1;
  color: var(--text);
}

.login-sub {
  margin: 0 0 32px;
  font-size: 14px;
  color: var(--muted);
  line-height: 1.6;
}

.login-btn {
  width: 100%;
  justify-content: center;
  padding: 14px;
  font-size: 14px;
  margin-bottom: 20px;
}

.login-btn svg {
  width: 18px;
  height: 18px;
}

.login-footer-note {
  font-size: 11px;
  color: var(--dim);
  line-height: 1.65;
  margin: 0;
}

/* DASHBOARD */
.dash-shell {
  min-height: 100svh;
  background: var(--bg);
  display: flex;
  flex-direction: column;
}

.dashnav {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 32px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.07);
  background: rgba(7, 9, 10, 0.8);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  position: sticky;
  top: 0;
  z-index: 50;
  flex-shrink: 0;
}

.dashnav-brand { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; }
.dashnav-right { display: flex; align-items: center; gap: 14px; }

.dashnav-user {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--muted);
}

.dashnav-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid rgba(255,255,255,0.1);
}

.dash-body {
  flex: 1;
  overflow-y: auto;
}

.repopicker-wrap,
.repodash-wrap {
  width: min(1100px, calc(100% - 48px));
  margin-inline: auto;
  padding: 56px 0 80px;
}

.repopicker-header { margin-bottom: 36px; }

.dash-heading {
  margin: 0 0 8px;
  font-size: clamp(26px, 3.5vw, 40px);
  font-weight: 600;
  letter-spacing: -0.05em;
}

.dash-sub {
  margin: 0 0 16px;
  font-size: 15px;
  color: var(--muted);
  line-height: 1.6;
}

.demo-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 12px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.08);
  font-size: 11px;
  font-family: var(--mono);
  color: var(--dim);
  letter-spacing: 0.04em;
}

.repo-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-bottom: 24px;
  border: 1px solid rgba(255,255,255,0.07);
  border-radius: 16px;
  overflow: hidden;
}

.repo-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 20px 24px;
  background: rgba(255,255,255,0.018);
  border: none;
  border-bottom: 1px solid rgba(255,255,255,0.05);
  cursor: pointer;
  text-align: left;
  color: var(--text);
  font-family: var(--brand);
  transition: background 0.15s ease;
  width: 100%;
}

.repo-card:last-child { border-bottom: none; }
.repo-card:hover { background: rgba(255,255,255,0.035); }

.repo-card-left { display: flex; flex-direction: column; gap: 4px; }

.repo-card-name {
  font-size: 14px;
  color: var(--muted);
}

.repo-card-name strong {
  color: var(--text);
  font-weight: 500;
}

.repo-card-meta {
  font-size: 12px;
  color: var(--dim);
  font-family: var(--mono);
}

.repo-card-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-shrink: 0;
}

.repo-findings {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--muted);
}

.repo-arrow {
  color: var(--dim);
  font-size: 16px;
  transition: transform 0.15s;
}
.repo-card:hover .repo-arrow { transform: translateX(3px); }

.install-nudge {
  padding: 20px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
}

.install-nudge-text {
  margin: 0;
  font-size: 14px;
  color: var(--muted);
}

.dash-breadcrumb {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 24px;
  font-size: 13px;
  color: var(--muted);
}

.dash-back {
  background: none;
  border: none;
  color: var(--muted);
  font-family: var(--brand);
  font-size: 13px;
  cursor: pointer;
  padding: 0;
  transition: color 0.12s;
}
.dash-back:hover { color: var(--text); }

.dash-breadcrumb-sep { color: var(--dim); }

.dash-breadcrumb-repo {
  color: var(--text);
  font-size: 13px;
}
.dash-breadcrumb-repo strong { font-weight: 600; }

.repodash-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.dash-panel {
  background: rgba(255,255,255,0.02);
  border: 1px solid rgba(255,255,255,0.07);
  border-radius: 20px;
  padding: 28px;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.dash-panel-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.dash-panel-tag {
  font-family: var(--mono);
  font-size: 9px;
  letter-spacing: 0.1em;
  color: var(--dim);
  margin-bottom: 6px;
  text-transform: uppercase;
}

.dash-panel-title {
  margin: 0 0 6px;
  font-size: 18px;
  font-weight: 600;
  letter-spacing: -0.04em;
}

.dash-panel-desc {
  margin: 0;
  font-size: 13px;
  color: var(--muted);
  line-height: 1.55;
}

.dash-panel-notice {
  font-size: 12px;
  color: var(--muted);
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.06);
  border-radius: 10px;
  padding: 10px 14px;
  margin-bottom: 20px;
  line-height: 1.6;
}

.dash-panel-notice.warn {
  border-color: rgba(216,170,99,0.2);
  background: rgba(216,170,99,0.04);
  color: var(--gold);
}

.dash-panel-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.dash-item {
  padding: 14px 0;
  border-bottom: 1px solid rgba(255,255,255,0.05);
}
.dash-item:last-child { border-bottom: none; }

.dash-item-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 4px;
}

.dash-item-title {
  font-size: 13px;
  font-weight: 400;
  color: var(--text);
  line-height: 1.45;
  flex: 1;
}

.dash-item-meta {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--dim);
  margin-bottom: 5px;
}

.dash-item-feedback {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.55;
}

.dash-item-link {
  display: inline-block;
  margin-top: 6px;
  font-size: 11px;
  font-family: var(--mono);
  color: var(--dim);
  text-decoration: none;
  transition: color 0.12s;
}
.dash-item-link:hover { color: var(--text); }

.dash-error {
  padding: 16px 20px;
  border-radius: 12px;
  border: 1px solid rgba(255,129,117,0.2);
  background: rgba(255,129,117,0.04);
  color: var(--red);
  font-size: 14px;
}

/* Scout toggle */
.scout-toggle {
  width: 44px;
  height: 26px;
  border-radius: 999px;
  background: rgba(255,255,255,0.08);
  border: 1px solid rgba(255,255,255,0.1);
  cursor: pointer;
  position: relative;
  transition: background 0.2s, border-color 0.2s;
  flex-shrink: 0;
  padding: 0;
}

.scout-toggle.on {
  background: rgba(130,224,166,0.18);
  border-color: rgba(130,224,166,0.3);
}

.scout-toggle.loading { opacity: 0.5; cursor: wait; }

.scout-toggle-knob {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: rgba(255,255,255,0.35);
  transition: transform 0.2s cubic-bezier(.2,.7,.2,1), background 0.2s;
}

.scout-toggle.on .scout-toggle-knob {
  transform: translateX(18px);
  background: var(--green);
}

/* Dashboard responsive */
@media (max-width: 800px) {
  .repodash-grid { grid-template-columns: 1fr; }
  .repopicker-wrap, .repodash-wrap { padding: 32px 0 60px; }
  .dashnav { padding: 0 16px; }
  .install-nudge { flex-direction: column; align-items: flex-start; }
}
"""

with open("frontend/src/styles.css", "a", encoding="utf-8") as f:
    f.write(css_to_append)
print("Updated styles.css")
