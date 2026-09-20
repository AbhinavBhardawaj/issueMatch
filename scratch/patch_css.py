import os

css_to_append = """

/* PANEL */
.panel-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  opacity: 0;
  visibility: hidden;
  transition: opacity 0.3s ease, visibility 0.3s ease;
  z-index: 1000;
}
.panel-backdrop.open {
  opacity: 1;
  visibility: visible;
}

.panel {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  width: 480px;
  background: rgba(7, 9, 10, 0.92);
  backdrop-filter: blur(40px);
  -webkit-backdrop-filter: blur(40px);
  border-left: 1px solid rgba(255, 255, 255, 0.08);
  transform: translateX(100%);
  transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1);
  z-index: 1001;
  display: flex;
  flex-direction: column;
}
@media (max-width: 768px) {
  .panel {
    width: 100vw;
  }
}
.panel.open {
  transform: translateX(0);
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 24px;
  border-bottom: 1px solid var(--line);
}
.panel-title {
  font-family: var(--mono);
  font-size: 14px;
  color: var(--text);
  letter-spacing: 0.05em;
}
.panel-close {
  background: none;
  border: none;
  color: var(--muted);
  font-size: 24px;
  cursor: pointer;
  line-height: 1;
  padding: 0;
}
.panel-close:hover {
  color: var(--text);
}

.panel-tabs {
  display: flex;
  border-bottom: 1px solid var(--line);
}
.panel-tab {
  flex: 1;
  background: none;
  border: none;
  padding: 16px;
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.2s ease;
}
.panel-tab:hover {
  color: var(--text);
}
.panel-tab.active {
  color: var(--text);
  border-bottom-color: var(--text);
}

.panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.demo-banner {
  padding: 12px;
  border-radius: 8px;
  font-size: 11px;
  margin-bottom: 24px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--muted);
  text-align: center;
}
.demo-banner.notice {
  background: rgba(251, 191, 36, 0.1);
  color: var(--gold);
  border: 1px solid rgba(251, 191, 36, 0.2);
}

.state-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 16px;
  margin-bottom: 16px;
}
.state-card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 12px;
}
.state-card-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  margin-bottom: 4px;
}
.state-card-meta {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
}
.state-card-body {
  font-size: 13px;
  color: var(--muted);
  line-height: 1.5;
}
.state-card-link {
  display: inline-block;
  margin-top: 12px;
  font-size: 12px;
  color: var(--text);
  text-decoration: underline;
  text-underline-offset: 4px;
}

/* BADGES */
.status-badge {
  display: inline-block;
  border-radius: 999px;
  padding: 2px 9px;
  font-size: 10px;
  font-family: var(--mono);
  letter-spacing: 0.05em;
  white-space: nowrap;
}
.badge-success { background: rgba(52, 211, 153, 0.15); color: var(--green); }
.badge-danger { background: rgba(248, 113, 113, 0.15); color: var(--red); }
.badge-info { background: rgba(255, 255, 255, 0.1); color: var(--text); }
.badge-warning { background: rgba(251, 191, 36, 0.15); color: var(--gold); }
.badge-muted { background: rgba(255, 255, 255, 0.05); color: var(--muted); }

/* PRODUCTS */
.products-section {
  padding-top: 0;
  padding-bottom: 80px;
}
.product-cards {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px;
  border: 1px solid var(--line);
  border-radius: 20px;
  overflow: hidden;
  background: var(--line);
}
@media (max-width: 800px) {
  .product-cards {
    grid-template-columns: 1fr;
  }
}
.product-card {
  background: var(--panel-strong);
  padding: 44px;
  display: flex;
  flex-direction: column;
}
.product-label {
  font-size: 11px;
  color: var(--muted);
  letter-spacing: 0.1em;
  margin-bottom: 24px;
}
.product-heading {
  font-size: 28px;
  font-weight: 400;
  color: var(--text);
  letter-spacing: -0.02em;
  margin-bottom: 16px;
}
.product-desc {
  font-size: 16px;
  color: var(--muted);
  line-height: 1.6;
  margin-bottom: 32px;
  flex: 1;
}
.product-btn {
  align-self: flex-start;
  margin-bottom: 12px;
}
.product-warning {
  font-size: 11px;
  color: var(--gold);
}
"""

with open("frontend/src/styles.css", "a", encoding="utf-8") as f:
    f.write(css_to_append)
print("Updated styles.css")
