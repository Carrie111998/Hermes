/* Boot: wire auth, initialize tenant-safe state, build routes, start the router. */

import { startRouter, navigate } from './router.js';
import { resetReal } from './state.js';
import { clearSession, getSession, homeRoute, isAuthed, updateSession } from './session.js';
import { config } from './api.js';
import { mountShell } from './shell.js';
import { el, emptyState, button } from './ui.js';

import * as login from './pages/login.js';
import * as accessPending from './pages/access-pending.js';
import * as today from './pages/today.js';
import * as approvals from './pages/approvals.js';
import * as researchResults from './pages/research-results.js';
import * as setup from './pages/setup.js';
import * as analytics from './pages/analytics.js';
import * as admin from './pages/admin.js';
import * as agentRuns from './pages/agent-runs.js';
import * as adminDocuments from './pages/admin-documents.js';
import * as research from './pages/research.js';
import * as researchEditor from './pages/research-editor.js';
import * as researchBrief from './pages/research-brief.js';
import * as researchDetail from './pages/research-detail.js';

// Bearer auth for every real-backend request (api.js reads this per call).
config.authHeader = () => {
  const token = getSession()?.token;
  return token ? `Bearer ${token}` : null;
};
let refreshPromise = null;
config.refreshAuth = async () => {
  const session = getSession();
  if (!session?.refresh_token) return null;
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch('/api/v1/auth/refresh', {
          method: 'POST',
          headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: session.refresh_token }),
        });
        if (!response.ok) return null;
        const auth = await response.json();
        const next = updateSession({
          token: auth.access_token,
          refresh_token: auth.refresh_token,
          expires_in: auth.expires_in,
          expires_at: Date.now() + Number(auth.expires_in || 3600) * 1000,
        });
        return `Bearer ${next.token}`;
      } catch {
        return null;
      }
    })().finally(() => { refreshPromise = null; });
  }
  return refreshPromise;
};
config.onAuthFailure = () => {
  clearSession();
  resetReal();
  navigate('/login', { replace: true });
};
config.beforeRequest = req => {
  const session = getSession();
  const companyId = session?.company?.id;
  if (!companyId || req.headers?.Authorization !== `Bearer ${session.token}`) return req;
  return {
    ...req,
    headers: { ...(req.headers || {}), 'X-Company-ID': companyId },
  };
};

resetReal();

const appRoot = document.getElementById('app');

function appPage(title, mountFn) {
  return async (ctx) => {
    const shell = mountShell(appRoot);
    shell.setTitle(typeof title === 'function' ? title(ctx) : title);
    shell.setActiveNav('/' + ctx.route.path.split('/').slice(1, 3).join('/'));
    shell.closeNav?.();
    shell.pageRoot.className = 'ifz-page';
    shell.pageRoot.replaceChildren();
    shell.pageRoot.scrollTop = 0;
    // Move focus to main for screen readers after route change
    requestAnimationFrame(() => shell.pageRoot.focus({ preventScroll: true }));
    return mountFn(shell.pageRoot, ctx);
  };
}

/* Build a hash target, dropping empty values so redirects never emit "?a=&b=". */
function withQuery(path, params = {}) {
  const pairs = Object.entries(params)
    .filter(([, value]) => value != null && value !== '')
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  return pairs.length ? `${path}?${pairs.join('&')}` : path;
}

/* Phase 5 cutover. Every pre-collapse customer bookmark still resolves, and it
   carries whatever context the new destination can actually use — a lead id
   becomes an expanded company, a contact id becomes a highlighted person, a
   message id opens that email in the review queue. `to` receives the matched
   route ({ params, query, path }) and returns the replacement hash path. */
const LEGACY_REDIRECTS = [
  { path: '/app/dashboard',       to: () => '/app/today' },
  { path: '/app/onboarding',      to: () => '/app/setup' },

  // The evidence-first research workspace supersedes the old buyer ledger.
  { path: '/app/buyers',          to: () => '/app/research' },
  { path: '/app/leads',           to: () => '/app/research' },
  { path: '/app/leads/:leadId',   to: () => '/app/research' },
  { path: '/app/contacts',        to: () => '/app/research' },
  { path: '/app/contacts/:contactId', to: () => '/app/research' },
  { path: '/app/custom-outreach', to: () => '/app/research' },
  { path: '/app/lead-map',        to: () => '/app/research' },

  // Campaigns became a filter over the approval queue, not a destination.
  { path: '/app/outreach',        to: r => withQuery('/app/approvals', { message: r.query.message }) },
  { path: '/app/outreach/campaigns/:campaignId', to: () => '/app/approvals' },

  // Company Brain, Integrations, Email Templates and Settings became Setup sections.
  { path: '/app/company-brain',   to: () => withQuery('/app/setup', { section: 'brain' }) },
  { path: '/app/integrations',    to: () => withQuery('/app/setup', { section: 'mailbox' }) },
  { path: '/app/email-templates', to: () => withQuery('/app/setup', { section: 'email-style' }) },
  { path: '/app/settings',        to: () => withQuery('/app/setup', { section: 'sending' }) },

  // Agent Runs is a log viewer: admin-only now. Research configuration moved too.
  { path: '/app/agent-runs',      to: () => '/app/today' },
  { path: '/app/agent-runs/:runId', to: () => '/app/today' },
  // /app/research/new is a real customer page now: source access stays
  // admin-owned, but the brief — markets, sector, what a good lead weighs —
  // belongs to whoever has to act on the results.
  { path: '/app/research/:campaignId', to: () => '/app/research' },
  { path: '/app/research/:campaignId/edit', to: () => '/app/research' },
];

const routes = [
  { path: '/login', title: 'Sign in', public: true, mount: (ctx) => login.mount(appRoot, ctx) },
  { path: '/access-pending', title: 'Access pending', public: true, mount: (ctx) => accessPending.mount(appRoot, ctx) },

  // The four customer destinations.
  { path: '/app/today',           mount: appPage('Today', today.mount) },
  { path: '/app/approvals',       mount: appPage('Approvals', approvals.mount) },
  { path: '/app/research',        mount: appPage('Research', researchResults.mount) },
  { path: '/app/research/new',    mount: appPage('New lead search', researchBrief.mount) },
  { path: '/app/setup',           mount: appPage('Setup', setup.mount) },
  // Kept and reachable from Today's "See the numbers", deliberately off the nav.
  { path: '/app/analytics',       mount: appPage('Analytics', analytics.mount) },

  ...LEGACY_REDIRECTS.map(({ path, to }) => ({ path, redirect: to, mount: () => {} })),

  { path: '/admin/dashboard',      mount: appPage('Admin Dashboard', admin.mountDashboard) },
  { path: '/admin/companies',      mount: appPage('Companies', admin.mountCompanies) },
  { path: '/admin/companies/:companyId', mount: appPage('Company', admin.mountCompanyDetail) },
  { path: '/admin/users',          mount: appPage('Users', admin.mountUsers) },
  { path: '/admin/agent-runs',     mount: appPage('Agent Runs', admin.mountAgentRuns) },
  { path: '/admin/agent-runs/:runId', mount: appPage('Agent Run', agentRuns.mountDetail) },
  // Exact list path before the :documentId detail path, so /admin/documents
  // never resolves as a document whose id is the empty string.
  { path: '/admin/documents',      mount: appPage('Documents', adminDocuments.mountList) },
  { path: '/admin/documents/:documentId', mount: appPage('Document', adminDocuments.mountDetail) },
  { path: '/admin/analytics',      mount: appPage('Admin Analytics', admin.mountAnalytics) },
  { path: '/admin/integrations',   mount: appPage('Integration Health', admin.mountIntegrations) },
  { path: '/admin/errors',         mount: appPage('Errors', admin.mountErrors) },
  { path: '/admin/logs',           mount: appPage('Logs', admin.mountLogs) },
  { path: '/admin/data-sources',   mount: appPage('Data Sources', admin.mountDataSources) },
  // Research configuration is operator machinery: scoring weights, enrichment and
  // model profiles. It stays available, behind the admin guard.
  { path: '/admin/research',       mount: appPage('Research', research.mount) },
  { path: '/admin/research/new',   mount: appPage('New research campaign', researchEditor.mount) },
  { path: '/admin/research/:campaignId/edit', mount: appPage('Edit research campaign', researchEditor.mount) },
  { path: '/admin/research/:campaignId', mount: appPage('Research campaign', researchDetail.mount) },
];

startRouter({
  routes,
  beforeEach(route) {
    const session = getSession();
    const home = homeRoute(session);
    if (route.path === '/' || route.path === '') return isAuthed() ? home : '/login';
    if (route.public) {
      if (route.path === '/login' && isAuthed()) return home;
      return null;
    }
    if (!route.mount) return null; // unmatched — handled by notFound
    if (!isAuthed()) return '/login';
    // Legacy bookmarks resolve only once the session exists, so a signed-out
    // visitor lands on /login instead of bouncing through the redirect first.
    if (route.redirect) return route.redirect(route);
    if (route.path.startsWith('/admin') && session?.user?.role !== 'admin') return home;
    if (route.path.startsWith('/app') && session?.user?.role === 'admin' && !session?.company?.id) {
      return '/admin/dashboard';
    }
    return null;
  },
  notFound(path) {
    if (isAuthed()) {
      const shell = mountShell(appRoot);
      shell.setTitle('Not found');
      shell.pageRoot.replaceChildren(emptyState({
        icon: 'search',
        title: 'Page not found',
        hint: `No page matches "${path}".`,
        action: button('Back to Today', { kind: 'primary', onClick: () => navigate(homeRoute()) }),
      }));
    } else {
      navigate('/login', { replace: true });
    }
  },
  onError(path, error) {
    if (error?.status === 401) {
      config.onAuthFailure?.();
      return;
    }
    const shell = mountShell(appRoot);
    shell.setTitle(error?.status === 403 ? 'Access denied' : 'Something went wrong');
    shell.pageRoot.replaceChildren(emptyState({
      icon: error?.status === 403 ? 'ban' : 'warning',
      title: error?.status === 403 ? 'You do not have access to this page' : 'This page could not be loaded',
      hint: error?.message || `The route ${path} failed to load.`,
      action: button('Back to Today', { kind: 'primary', onClick: () => navigate(homeRoute()) }),
    }));
  },
});
