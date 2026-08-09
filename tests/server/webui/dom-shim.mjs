/* Minimal dependency-free DOM for exercising server/webui/js modules under
   `node --test`. It implements only what the WebUI primitives in ui.js touch:
   element creation, attributes/classes/dataset, tree mutation, a small
   selector engine (tag / .class / #id / [attr] / [attr="v"] / :not(...)),
   event dispatch, focus tracking, and controllable timers.

   The CI image installs no npm packages for the webui suite, so a real DOM
   implementation is not available here. */

const VOID_VALUE = Symbol('unset');

function parseCompound(selector) {
  const nots = [];
  const rest = selector.replace(/:not\(([^)]*)\)/g, (_, inner) => {
    nots.push(inner.trim());
    return '';
  });
  const tokens = rest.match(/\[[^\]]*\]|[#.]?[\w-]+/g) || [];
  return { tokens, nots };
}

function matchToken(node, token) {
  if (token.startsWith('#')) return node.getAttribute('id') === token.slice(1);
  if (token.startsWith('.')) return node.classList.contains(token.slice(1));
  if (token.startsWith('[')) {
    const parsed = /^\[([\w-]+)(?:="?([^"\]]*)"?)?\]$/.exec(token);
    if (!parsed) return false;
    const [, attr, value] = parsed;
    if (!node.hasAttribute(attr)) return false;
    return value === undefined ? true : node.getAttribute(attr) === value;
  }
  return node.tagName === token.toLowerCase();
}

function matchesCompound(node, selector) {
  const { tokens, nots } = parseCompound(selector.trim());
  if (!tokens.length && !nots.length) return false;
  if (!tokens.every(token => matchToken(node, token))) return false;
  return nots.every(inner => !matchesCompound(node, inner));
}

function matches(node, selectorList) {
  return String(selectorList)
    .split(',')
    .map(part => part.trim())
    .filter(Boolean)
    .some(part => matchesCompound(node, part));
}

class ClassList {
  constructor(node) { this.node = node; }
  get _values() {
    return String(this.node.getAttribute('class') || '').split(/\s+/).filter(Boolean);
  }
  _write(values) { this.node.setAttribute('class', values.join(' ')); }
  contains(name) { return this._values.includes(name); }
  add(...names) {
    const values = this._values;
    for (const name of names) if (!values.includes(name)) values.push(name);
    this._write(values);
  }
  remove(...names) { this._write(this._values.filter(v => !names.includes(v))); }
  toggle(name, force) {
    const has = this.contains(name);
    const next = force === undefined ? !has : !!force;
    if (next) this.add(name); else this.remove(name);
    return next;
  }
  toString() { return this._values.join(' '); }
}

export class TextNode {
  constructor(text) {
    this.nodeType = 3;
    this.parentNode = null;
    this._text = String(text);
  }
  get textContent() { return this._text; }
  set textContent(value) { this._text = String(value); }
  remove() {
    if (this.parentNode) this.parentNode.childNodes = this.parentNode.childNodes.filter(n => n !== this);
    this.parentNode = null;
  }
}

export class Element {
  constructor(tagName, ownerDocument) {
    this.nodeType = 1;
    this.tagName = String(tagName).toLowerCase();
    this.ownerDocument = ownerDocument;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.classList = new ClassList(this);
    this._value = VOID_VALUE;
  }

  /* ---- attributes / reflected properties ---- */
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }

  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get id() { return this.getAttribute('id') || ''; }
  set id(value) { this.setAttribute('id', value); }
  get type() { return this.getAttribute('type') || (this.tagName === 'input' ? 'text' : ''); }
  set type(value) { this.setAttribute('type', value); }
  get disabled() { return this.hasAttribute('disabled'); }
  set disabled(value) { if (value) this.setAttribute('disabled', ''); else this.removeAttribute('disabled'); }
  get hidden() { return this.hasAttribute('hidden'); }
  set hidden(value) { if (value) this.setAttribute('hidden', ''); else this.removeAttribute('hidden'); }

  get value() {
    if (this._value !== VOID_VALUE) return this._value;
    if (this.tagName === 'select') {
      const options = this.querySelectorAll('option');
      const selected = options.find(option => option.hasAttribute('selected'));
      const fallback = selected || options[0];
      return fallback ? fallback.getAttribute('value') ?? '' : '';
    }
    return this.getAttribute('value') ?? '';
  }
  set value(next) { this._value = String(next); }

  /* ---- tree ---- */
  get children() { return this.childNodes.filter(node => node.nodeType === 1); }
  get isConnected() {
    let node = this;
    while (node.parentNode) node = node.parentNode;
    return node === this.ownerDocument?.body || node.tagName === 'body';
  }
  append(...nodes) {
    for (const node of nodes.flat(Infinity)) {
      if (node == null || node === false) continue;
      const child = node?.nodeType ? node : new TextNode(String(node));
      child.remove();
      child.parentNode = this;
      if (child instanceof Element) child.ownerDocument = this.ownerDocument;
      this.childNodes.push(child);
    }
  }
  appendChild(node) { this.append(node); return node; }
  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.childNodes = this.parentNode.childNodes.filter(node => node !== this);
      this.parentNode = null;
    }
  }

  get textContent() {
    return this.childNodes.map(node => node.textContent).join('');
  }
  set textContent(value) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    if (value !== '') this.append(new TextNode(value));
  }

  /* ---- queries ---- */
  matches(selectorList) { return matches(this, selectorList); }
  closest(selectorList) {
    let node = this;
    while (node && node.nodeType === 1) {
      if (matches(node, selectorList)) return node;
      node = node.parentNode;
    }
    return null;
  }
  querySelectorAll(selectorList) {
    const found = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType !== 1) continue;
        if (matches(child, selectorList)) found.push(child);
        walk(child);
      }
    };
    walk(this);
    return found;
  }
  querySelector(selectorList) { return this.querySelectorAll(selectorList)[0] || null; }

  /* ---- events / focus ---- */
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  dispatchEvent(event) {
    const evt = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...event };
    evt.target = evt.target || this;
    evt.currentTarget = this;
    for (const fn of [...(this.listeners.get(evt.type) || [])]) fn(evt);
    return !evt.defaultPrevented;
  }
  click() { return this.dispatchEvent({ type: 'click' }); }
  focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  blur() { if (this.ownerDocument?.activeElement === this) this.ownerDocument.activeElement = null; }
}

class Document {
  constructor() {
    this.listeners = new Map();
    this.body = new Element('body', this);
    this.body.ownerDocument = this;
    this.activeElement = null;
  }
  createElement(tag) { return new Element(tag, this); }
  createElementNS(_ns, tag) { return new Element(tag, this); }
  createTextNode(text) { return new TextNode(text); }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(fn);
  }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  querySelector(selector) { return this.body.querySelector(selector); }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
}

/* Timers are collected rather than scheduled so toast auto-dismiss and the
   modal's deferred focus cannot keep the test process alive or fire between
   assertions. */
function timerHarness() {
  let nextId = 1;
  const pending = new Map();
  return {
    setTimeout(fn) { const id = nextId++; pending.set(id, fn); return id; },
    clearTimeout(id) { pending.delete(id); },
    flush() {
      for (const [id, fn] of [...pending]) { pending.delete(id); fn(); }
    },
    get size() { return pending.size; },
  };
}

/** Install the shim on globalThis. Returns handles the tests drive. */
export function installDom() {
  const document = new Document();
  const timers = timerHarness();
  globalThis.document = document;
  globalThis.window = globalThis.window || {};
  globalThis.setTimeout = timers.setTimeout;
  globalThis.clearTimeout = timers.clearTimeout;
  return { document, timers };
}

/** Reset the page between tests without reloading the module graph.
    ui.js caches its toast host in a module-level variable, so that node is
    emptied and re-attached rather than dropped — otherwise every later toast
    would render into a detached tree. */
export function resetDom(handles) {
  const toastHost = handles.document.body.querySelector('.ifz-toasts');
  handles.document.body.replaceChildren();
  if (toastHost) {
    toastHost.replaceChildren();
    handles.document.body.append(toastHost);
  }
  handles.document.activeElement = null;
  handles.document.listeners.clear();
  handles.timers.flush();
}

/** Depth-first search for the first element whose text is exactly `label`. */
export function byText(root, tagSelector, label) {
  return root.querySelectorAll(tagSelector).find(node => node.textContent.trim() === label) || null;
}
