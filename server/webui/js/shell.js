/* App chrome: sidebar + topbar. Mounted once; pages render into shell.pageRoot. */

import { el, icon } from './ui.js';
import { navigate } from './router.js';
import { call, config } from './api.js';
import { db, resetReal, subscribe } from './mocks/db.js';
import { getSession, clearSession } from './session.js';

const NAV_GROUPS = [
  { label: 'Overview', items: [
    { path: '/app/dashboard', label: 'Dashboard', icon: 'dashboard' },
  ]},
  { label: 'Market', items: [
    { path: '/app/lead-map', label: 'Lead Map', icon: 'map' },
    { path: '/app/research', label: 'Research', icon: 'search' },
    { path: '/app/leads', label: 'Leads', icon: 'leads' },
    { path: '/app/contacts', label: 'Contacts', icon: 'contact' },
  ]},
  { label: 'Outreach', items: [
    { path: '/app/outreach', label: 'Campaigns', icon: 'mail' },
    { path: '/app/custom-outreach', label: 'Custom Outreach', icon: 'send' },
    { path: '/app/email-templates', label: 'Email Templates', icon: 'mail' },
  ]},
  { label: 'Intelligence', items: [
    { path: '/app/company-brain', label: 'Company Brain', icon: 'brain' },
    { path: '/app/analytics', label: 'Analytics', icon: 'chart' },
  ]},
  { label: 'System', items: [
    { path: '/app/agent-runs', label: 'Agent Runs', icon: 'bolt', badgeTopic: 'runs' },
    { path: '/app/integrations', label: 'Integrations', icon: 'plug' },
    { path: '/app/settings', label: 'Settings', icon: 'gear' },
  ]},
  { label: 'Admin', items: [
    { path: '/admin/dashboard', label: 'Admin Home', icon: 'building' },
    { path: '/admin/companies', label: 'Companies', icon: 'building' },
    { path: '/admin/users', label: 'Users', icon: 'contact' },
  ]},
];

const LOGO_MARKS = {
  sidebar: `
    <rect width="32" height="32" rx="4" fill="rgba(255,255,255,0.22)"/>
    <path d="M16 7l7 9-7 9-7-9z" fill="#FFFFFF"/>
    <path d="M16 12l3.5 4.5L16 21l-3.5-4.5z" fill="rgba(255,255,255,0.72)"/>`,
  light: `
    <rect width="32" height="32" rx="4" fill="#1C1B18"/>
    <path d="M16 7l7 9-7 9-7-9z" fill="#FAF9F5"/>
    <path d="M16 12l3.5 4.5L16 21l-3.5-4.5z" fill="#F3F1EB"/>`,
};

export function logoNode({ compactTag = false, variant = 'sidebar' } = {}) {
  const mark = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  mark.setAttribute('viewBox', '0 0 32 32');
  mark.setAttribute('width', '26');
  mark.setAttribute('height', '26');
  mark.setAttribute('aria-hidden', 'true');
  mark.innerHTML = LOGO_MARKS[variant] || LOGO_MARKS.sidebar;
  return el('div', { class: `ifz-logo${variant === 'light' ? ' light' : ''}` },
    mark,
    el('span', { class: 'ifz-logo-word' }, 'inter', el('em', {}, 'faze')),
    compactTag ? null : el('span', { class: 'ifz-logo-tag' }, 'agent'));
}

let _shell = null;

export function mountShell(root) {
  if (_shell) return _shell;

  const session = getSession();
  const isAdmin = session?.user?.role === 'admin';
  const visibleGroups = NAV_GROUPS.filter(group => group.label === 'Admin'
    ? isAdmin
    : !isAdmin || Boolean(session?.company?.id));
  const runBadge = el('span', { class: 'ifz-nav-badge', style: { display: 'none' }, 'aria-label': 'Running agents' });
  const navHost = el('nav', { class: 'ifz-nav', 'aria-label': 'Primary' }, visibleGroups.map(group =>
    [el('div', { class: 'ifz-nav-group-label', id: `nav-g-${group.label}` }, group.label),
     group.items.map(item => el('button', {
       class: 'ifz-nav-item',
       type: 'button',
       dataset: { path: item.path },
       onclick: () => { closeNav(); navigate(item.path); },
       title: item.label,
       'aria-label': item.label,
     }, icon(item.icon, 16), el('span', {}, item.label), item.badgeTopic === 'runs' ? runBadge : null))]));

  function refreshRunBadge() {
    const running = (db.agentRuns || []).filter(r => r.status === 'running').length;
    runBadge.textContent = running;
    runBadge.style.display = running ? '' : 'none';
    runBadge.setAttribute('aria-label', `${running} agent run${running === 1 ? '' : 's'} in progress`);
  }
  subscribe('runs', refreshRunBadge);
  refreshRunBadge();

  const sidebar = el('aside', { class: 'ifz-sidebar', id: 'ifz-sidebar', 'aria-label': 'App navigation' },
    logoNode(),
    navHost);

  const titleNode = el('div', { class: 'ifz-topbar-title', id: 'ifz-page-title' }, 'Dashboard');
  const themeBtn = el('button', { class: 'ifz-iconbtn', type: 'button', title: 'Toggle theme', 'aria-label': 'Toggle theme' });
  function paintThemeIcon() {
    themeBtn.replaceChildren(icon(document.documentElement.classList.contains('dark') ? 'sun' : 'moon', 15));
  }
  themeBtn.addEventListener('click', () => {
    const dark = document.documentElement.classList.toggle('dark');
    try { localStorage.setItem('ifz-theme', dark ? 'dark' : 'light'); } catch { /* ignore */ }
    paintThemeIcon();
  });
  paintThemeIcon();

  const userName = session?.user?.name || 'User';
  const initials = userName.split(' ').map(w => w[0]).slice(0, 2).join('').toUpperCase();

  const menuHost = el('div', { class: 'ifz-usermenu' });
  const avatarBtn = el('button', {
    class: 'ifz-iconbtn',
    type: 'button',
    style: { border: 'none', background: 'transparent' },
    'aria-label': 'User menu',
    'aria-haspopup': 'menu',
    'aria-expanded': 'false',
  }, el('span', { class: 'ifz-avatar', 'aria-hidden': 'true' }, initials));
  let pop = null;
  function closeMenu() {
    if (pop) {
      pop.remove();
      pop = null;
      avatarBtn.setAttribute('aria-expanded', 'false');
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onMenuKey);
    }
  }
  function onDoc(e) { if (!menuHost.contains(e.target)) closeMenu(); }
  function onMenuKey(e) {
    if (e.key === 'Escape') {
      closeMenu();
      avatarBtn.focus();
    }
  }
  avatarBtn.addEventListener('click', () => {
    if (pop) { closeMenu(); return; }
    pop = el('div', { class: 'ifz-usermenu-pop', role: 'menu' },
      el('div', { class: 'ifz-usermenu-head' },
        el('div', { class: 'ifz-usermenu-name' }, userName),
        el('div', { class: 'ifz-usermenu-email' }, session?.user?.email || '')),
      el('button', {
        class: 'ifz-usermenu-item',
        type: 'button',
        role: 'menuitem',
        onclick: () => { closeMenu(); navigate('/app/settings'); },
      }, icon('gear', 14), 'Settings'),
      el('button', {
        class: 'ifz-usermenu-item danger',
        type: 'button',
        role: 'menuitem',
        onclick: async () => {
          closeMenu();
          if (!window.confirm('Log out of interfaze-agent?')) return;
          try { await call('auth.logout'); } catch { /* local session is cleared regardless */ }
          clearSession();
          resetReal();
          navigate('/login');
        },
      }, icon('logout', 14), 'Log out'));
    menuHost.append(pop);
    avatarBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onMenuKey);
    pop.querySelector('button')?.focus();
  });
  menuHost.append(avatarBtn);

  const menuBtn = el('button', {
    class: 'ifz-iconbtn ifz-menu-btn',
    type: 'button',
    'aria-label': 'Open navigation',
    'aria-controls': 'ifz-sidebar',
    'aria-expanded': 'false',
  }, icon('menu', 18));

  let appFrame = null;
  function setNavOpen(open) {
    if (!appFrame) return;
    appFrame.classList.toggle('nav-open', open);
    menuBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    menuBtn.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    document.body.style.overflow = open ? 'hidden' : '';
  }
  function closeNav() { setNavOpen(false); }
  function toggleNav() { setNavOpen(!appFrame?.classList.contains('nav-open')); }
  menuBtn.addEventListener('click', toggleNav);

  const scrim = el('button', {
    class: 'ifz-app-scrim',
    type: 'button',
    'aria-label': 'Close navigation',
    onclick: closeNav,
  });

  const initialCompanyName = session?.company?.name || db.company?.name || '';
  const companyAvatar = el('span', { class: 'ifz-avatar', 'aria-hidden': 'true' }, initialCompanyName.slice(0, 1).toUpperCase() || '—');
  const companyNameNode = el('span', {}, initialCompanyName || (isAdmin ? 'No workspace selected' : 'Workspace'));
  subscribe('company', company => {
    const name = company?.name || getSession()?.company?.name || '';
    companyAvatar.textContent = name.slice(0, 1).toUpperCase() || '—';
    companyNameNode.textContent = name || (isAdmin ? 'No workspace selected' : 'Workspace');
  });

  const topbar = el('header', { class: 'ifz-topbar' },
    menuBtn,
    titleNode,
    el('div', { class: 'ifz-topbar-spacer' }),
    el('span', { class: 'ifz-company-chip' },
      companyAvatar,
      companyNameNode),
    themeBtn,
    menuHost);

  const pageRoot = el('div', { class: 'ifz-page', id: 'ifz-main', tabindex: '-1' });
  const main = el('main', { class: 'ifz-main' }, topbar, pageRoot);

  appFrame = el('div', { class: 'ifz-app' }, scrim, sidebar, main);
  root.replaceChildren(appFrame);

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && appFrame.classList.contains('nav-open')) closeNav();
  });

  // Close drawer when resizing to desktop
  const mq = window.matchMedia('(min-width: 901px)');
  const onMq = () => { if (mq.matches) closeNav(); };
  mq.addEventListener?.('change', onMq);

  _shell = {
    pageRoot,
    setTitle(t) {
      titleNode.textContent = t;
      document.title = `${t} · interfaze-agent`;
    },
    setActiveNav(path) {
      navHost.querySelectorAll('.ifz-nav-item').forEach(btn => {
        const p = btn.dataset.path;
        const active = p === path || (p !== '/app/dashboard' && path.startsWith(p));
        btn.classList.toggle('active', active);
        if (active) btn.setAttribute('aria-current', 'page');
        else btn.removeAttribute('aria-current');
      });
    },
    closeNav,
  };
  return _shell;
}

export function destroyShell() {
  _shell = null;
  document.body.style.overflow = '';
}
