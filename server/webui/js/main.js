/* Boot: wire auth, initialize tenant-safe state, build routes, start the router. */

import { startRouter, navigate } from './router.js';
import { reset, resetReal } from './mocks/db.js';
import { clearSession, getSession, homeRoute, isAuthed, updateSession } from './session.js';
import { config } from './api.js';
import { mountShell } from './shell.js';
import { el, emptyState, button } from './ui.js';

import * as login from './pages/login.js';
import * as accessPending from './pages/access-pending.js';
import * as dashboard from './pages/dashboard.js';
import * as onboarding from './pages/onboarding.js';
import * as companyBrain from './pages/company-brain.js';
import * as leadMap from './pages/lead-map.js';
import * as leads from './pages/leads.js';
import * as contacts from './pages/contacts.js';
import * as outreach from './pages/outreach.js';
import * as customOutreach from './pages/custom-outreach.js';
import * as emailTemplates from './pages/email-templates.js';
import * as analytics from './pages/analytics.js';
import * as agentRuns from './pages/agent-runs.js';
import * as integrations from './pages/integrations.js';
import * as settings from './pages/settings.js';
import * as admin from './pages/admin.js';
import * as research from './pages/research.js';
import * as researchEditor from './pages/research-editor.js';
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

if (config.mode === 'mock') await reset();
else resetReal();

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

const routes = [
  { path: '/login', title: 'Sign in', public: true, mount: (ctx) => login.mount(appRoot, ctx) },
  { path: '/access-pending', title: 'Access pending', public: true, mount: (ctx) => accessPending.mount(appRoot, ctx) },
  { path: '/app/dashboard',       mount: appPage('Dashboard', dashboard.mount) },
  { path: '/app/onboarding',      mount: appPage('Onboarding', onboarding.mount) },
  { path: '/app/company-brain',   mount: appPage('Company Brain', companyBrain.mount) },
  { path: '/app/lead-map',        mount: appPage('Lead Map', leadMap.mount) },
  { path: '/app/research',        mount: appPage('Research', research.mount) },
  { path: '/app/research/new',    mount: appPage('New research campaign', researchEditor.mount) },
  { path: '/app/research/:campaignId/edit', mount: appPage('Edit research campaign', researchEditor.mount) },
  { path: '/app/research/:campaignId', mount: appPage('Research campaign', researchDetail.mount) },
  { path: '/app/leads',           mount: appPage('Leads', leads.mountList) },
  { path: '/app/leads/:leadId',   mount: appPage('Lead', leads.mountDetail) },
  { path: '/app/contacts',        mount: appPage('Contacts', contacts.mountList) },
  { path: '/app/contacts/:contactId', mount: appPage('Contact', contacts.mountDetail) },
  { path: '/app/outreach',        mount: appPage('Outreach', outreach.mountList) },
  { path: '/app/outreach/campaigns/:campaignId', mount: appPage('Campaign', outreach.mountDetail) },
  { path: '/app/custom-outreach', mount: appPage('Custom Outreach', customOutreach.mount) },
  { path: '/app/email-templates', mount: appPage('Email Templates', emailTemplates.mount) },
  { path: '/app/analytics',       mount: appPage('Analytics', analytics.mount) },
  { path: '/app/agent-runs',      mount: appPage('Agent Runs', agentRuns.mountList) },
  { path: '/app/agent-runs/:runId', mount: appPage('Agent Run', agentRuns.mountDetail) },
  { path: '/app/integrations',    mount: appPage('Integrations', integrations.mount) },
  { path: '/app/settings',        mount: appPage('Settings', settings.mount) },
  { path: '/admin/dashboard',      mount: appPage('Admin Dashboard', admin.mountDashboard) },
  { path: '/admin/companies',      mount: appPage('Companies', admin.mountCompanies) },
  { path: '/admin/companies/:companyId', mount: appPage('Company', admin.mountCompanyDetail) },
  { path: '/admin/users',          mount: appPage('Users', admin.mountUsers) },
  { path: '/admin/agent-runs',     mount: appPage('Admin Agent Runs', admin.mountAgentRuns) },
  { path: '/admin/analytics',      mount: appPage('Admin Analytics', admin.mountAnalytics) },
  { path: '/admin/integrations',   mount: appPage('Integration Health', admin.mountIntegrations) },
  { path: '/admin/errors',         mount: appPage('Errors', admin.mountErrors) },
  { path: '/admin/logs',           mount: appPage('Logs', admin.mountLogs) },
  { path: '/admin/data-sources',   mount: appPage('Data Sources', admin.mountDataSources) },
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
        action: button('Back to dashboard', { kind: 'primary', onClick: () => navigate(homeRoute()) }),
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
      action: button('Back to dashboard', { kind: 'primary', onClick: () => navigate(homeRoute()) }),
    }));
  },
});
