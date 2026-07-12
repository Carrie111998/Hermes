/* Login page — full-screen, outside the app shell. */

import { el, field, input, button, setBusy, toast, passwordField } from '../ui.js';
import { call, config } from '../api.js';
import { setSession } from '../session.js';
import { logoNode, destroyShell } from '../shell.js';

function setFieldError(inputNode, message) {
  const fieldEl = inputNode.closest('.ifz-field');
  if (!fieldEl) return;
  let err = fieldEl.querySelector('.ifz-field-error');
  if (!message) {
    inputNode.classList.remove('is-invalid');
    inputNode.removeAttribute('aria-invalid');
    if (err) err.remove();
    return;
  }
  inputNode.classList.add('is-invalid');
  inputNode.setAttribute('aria-invalid', 'true');
  if (!err) {
    err = el('div', { class: 'ifz-field-error', role: 'alert' });
    fieldEl.append(err);
  }
  err.textContent = message;
}

export function mount(root, ctx) {
  destroyShell(); // returning from a logged-in session — rebuild shell next time

  const emailInput = input({ type: 'email', value: config.mode === 'mock' ? 'meltem@silverine.com.tr' : '', autocomplete: 'username', required: true });
  const { wrap: passWrap, input: passInput } = passwordField({ placeholder: '••••••••', required: true });
  const submitBtn = button('Sign in', { kind: 'primary lg', onClick: null });
  submitBtn.style.width = '100%';

  emailInput.addEventListener('blur', () => {
    if (!emailInput.value.trim()) setFieldError(emailInput, 'Enter your work email');
    else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailInput.value)) setFieldError(emailInput, 'Enter a valid email address');
    else setFieldError(emailInput, null);
  });
  passInput.addEventListener('blur', () => {
    if (!passInput.value) setFieldError(passInput, 'Enter your password');
    else setFieldError(passInput, null);
  });

  const form = el('form', { class: 'ifz-login-box', novalidate: true },
    logoNode({ variant: 'light' }),
    el('div', { class: 'ifz-login-title' }, 'Welcome back'),
    el('div', { class: 'ifz-login-sub' }, 'Sign in to your sales workspace.'),
    field('Work email', emailInput, { required: true }),
    field('Password', passWrap, { required: true }),
    submitBtn,
    el('div', { class: 'ifz-mt-2', style: { textAlign: 'right' } },
      el('a', { href: '#/login', class: 'ifz-small', onclick: (e) => { e.preventDefault(); toast('Password reset link sent (demo)', 'info'); } }, 'Forgot password?')),
    config.mode === 'mock'
      ? el('div', { class: 'ifz-login-note' }, 'Demo environment — any credentials sign in to the Silverine workspace. Accounts are provisioned by your interfaze administrator.')
      : null);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    let ok = true;
    if (!emailInput.value.trim()) { setFieldError(emailInput, 'Enter your work email'); ok = false; }
    else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(emailInput.value)) { setFieldError(emailInput, 'Enter a valid email address'); ok = false; }
    else setFieldError(emailInput, null);
    if (!passInput.value) { setFieldError(passInput, 'Enter your password'); ok = false; }
    else setFieldError(passInput, null);
    if (!ok) {
      toast('Fix the highlighted fields', 'warning');
      form.querySelector('.is-invalid')?.focus();
      return;
    }
    setBusy(submitBtn, true, 'Signing in…');
    try {
      const res = await call('auth.login', { body: { email: emailInput.value, password: passInput.value } });
      setSession({ token: res.token, user: res.user, company: res.company });
      ctx.navigate('/app/dashboard');
    } catch (err) {
      setBusy(submitBtn, false);
      toast(err.message || 'Sign-in failed', 'error');
    }
  });
  submitBtn.setAttribute('type', 'submit');

  root.replaceChildren(el('div', { class: 'ifz-login' },
    el('div', { class: 'ifz-login-brandside' },
      logoNode(),
      el('div', { class: 'ifz-login-headline' },
        'Your agent finds the buyers.', el('br'), el('em', {}, 'You close the deals.')),
      el('div', { class: 'ifz-login-points' },
        el('div', { class: 'ifz-login-point' }, 'Scan world markets for qualified B2B leads'),
        el('div', { class: 'ifz-login-point' }, 'Research companies and find buyer contacts'),
        el('div', { class: 'ifz-login-point' }, 'Approve every email before it leaves your mailbox'))),
    el('div', { class: 'ifz-login-formside' }, form)));

  document.title = 'Sign in · interfaze-agent';
}
