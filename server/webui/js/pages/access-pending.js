/* Access pending - public account state page. */

import { el, button } from '../ui.js';
import { logoNode, destroyShell } from '../shell.js';

export function mount(root, ctx) {
  destroyShell();
  root.replaceChildren(el('div', { class: 'ifz-login' },
    el('div', { class: 'ifz-login-brandside' },
      logoNode(),
      el('div', { class: 'ifz-login-headline' },
        'Your workspace is being prepared.', el('br'), el('em', {}, 'Admin access pending.')),
      el('div', { class: 'ifz-login-points' },
        el('div', { class: 'ifz-login-point' }, 'Customer users are created by an interfaze administrator'),
        el('div', { class: 'ifz-login-point' }, 'Company Brain setup begins after activation'),
        el('div', { class: 'ifz-login-point' }, 'No open signup is exposed in the MVP'))),
    el('div', { class: 'ifz-login-formside' },
      el('div', { class: 'ifz-login-box' },
        logoNode({ compactTag: true, variant: 'light' }),
        el('div', { class: 'ifz-login-title' }, 'Access pending'),
        el('div', { class: 'ifz-login-sub' }, 'Your company account exists, but an admin still needs to activate it.'),
        el('div', { class: 'ifz-login-note' }, 'Contact your interfaze administrator if access should already be active.'),
        el('div', { class: 'ifz-row ifz-mt-4' },
          button('Back to sign in', { kind: 'primary', onClick: () => ctx.navigate('/login') }))))));
  document.title = 'Access pending · interfaze-agent';
}
