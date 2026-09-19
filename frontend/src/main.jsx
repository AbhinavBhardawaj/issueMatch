import React, { useEffect, useState } from "react";
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
    <div style={{ padding: "2rem", border: "1px solid #333", borderRadius: "8px", background: "#111", color: "#888", textAlign: "center" }}>
      GithubWindow Component Placeholder
    </div>
  );
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

function Navbar() {
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

        <a className="nav-cta" href="#product">
          <GitHubIcon />
          Install App <Arrow />
        </a>
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
  useEffect(() => {
    const stage = document.querySelector(".earth-stage");
    const earth = document.querySelector(".earth");

    if (!stage || !earth) return;

    const move = (event) => {
      const rect = stage.getBoundingClientRect();

      const x = (event.clientX - rect.left) / rect.width - 0.5;
      const y = (event.clientY - rect.top) / rect.height - 0.5;

      const rotateY = x * 7;
      const rotateX = y * -5;

      const moveX = x * 14;
      const moveY = y * 9;

      earth.style.transform = `
        translate3d(${moveX}px, ${moveY}px, 0)
        rotateX(${rotateX}deg)
        rotateY(${rotateY}deg)
        scale(1.012)
      `;
    };

    const reset = () => {
      earth.style.transform = `
        translate3d(0, 0, 0)
        rotateX(0deg)
        rotateY(0deg)
        scale(1)
      `;
    };

    stage.addEventListener("pointermove", move);
    stage.addEventListener("pointerleave", reset);

    return () => {
      stage.removeEventListener("pointermove", move);
      stage.removeEventListener("pointerleave", reset);
    };
  }, []);

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
            src="/src/assets/earth.png"
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
              src="/src/assets/osi-mark.png"
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
            <p className="section-side-copy">
              AWS handles bursty webhook traffic, durable workflows, AI reasoning
              and authoritative state — without turning every box into a microservice.
            </p>
          </div>
        </Reveal>

        <div className="infra-track">
          {nodes.map(([number, name, role], i) => (
            <Reveal className="infra-node" key={name}>
              <span className="mono">{number}</span>
              <strong>{name}</strong>
              <small>{role}</small>
              {i < nodes.length - 1 && <i className="connector" />}
            </Reveal>
          ))}
        </div>

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

function FinalCTA() {
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
            <a className="btn btn-secondary" href="#origin">
              Our story <Arrow />
            </a>
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

function App() {
  return (
    <>
      <Navbar />
      <main>
        <Hero />
        <OpenSourceScroll />
        <Product />
        <Workflow />
        <Scout />
        <Infrastructure />
        <FinalCTA />
      </main>
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
