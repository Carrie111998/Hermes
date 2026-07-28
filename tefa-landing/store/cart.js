/* Prototype-only client-side "order" cart. No backend: everything lives in
   localStorage, and the only thing it ever produces is a pre-filled email —
   consistent with the real ordering model (invoice first, no checkout/payment).
   This is what makes the mockup feel like a real store to click through;
   it is not meant to ship as-is to a live site without a review. */
(function () {
  const KEY = 'mmm_cart_v1';

  const PRODUCTS = {
    'MMM-PR-101': { name: 'Home & Repair Tool Roll', price: 83.95, dept: 'Practical & Trade' },
    'MMM-PR-102': { name: 'Money & First Job Kit', price: 48.95, dept: 'Practical & Trade' },
    'MMM-PR-103': { name: 'Kitchen & Provision Kit', price: 59.95, dept: 'Practical & Trade' },
    'MMM-SC-201': { name: 'Situation Handling Deck', price: 30.95, dept: 'Situation Handling & Self-Command' },
    'MMM-SC-202': { name: 'Focus & Energy System', price: 52.95, dept: 'Situation Handling & Self-Command' },
    'MMM-SC-203': { name: 'Self-Advocacy Workbook', price: 24.95, dept: 'Situation Handling & Self-Command' },
    'MMM-CS-301': { name: 'Graphic Design Bench', price: 318.95, dept: 'Design & Motion Studio' },
    'MMM-CS-302': { name: 'Motion & Video Kit', price: 363.95, dept: 'Design & Motion Studio' },
    'MMM-CS-303': { name: 'Skill-to-Income Pack', price: 35.95, dept: 'Design & Motion Studio' },
    'MMM-AT-401': { name: 'AI Literacy Bench Kit', price: 92.95, dept: 'AI & Emerging Tech Bench' },
    'MMM-AT-402': { name: 'Electronics & Robotics Starter', price: 127.95, dept: 'AI & Emerging Tech Bench' },
    'MMM-AT-403': { name: '3D Design & Fabrication Intro', price: 462.95, dept: 'AI & Emerging Tech Bench' },
    'MMM-HS-501': { name: 'Core Subjects Workbook Set', price: 70.95, dept: 'Homeschool Essentials' },
    'MMM-HS-502': { name: 'Homeschool Assessment & Portfolio Kit', price: 57.95, dept: 'Homeschool Essentials' },
    'MMM-HS-503': { name: 'Daily Supply Restock Box', price: 41.95, dept: 'Homeschool Essentials' },
    'MMM-HS-504': { name: 'Art & Craft Foundations Kit', price: 46.95, dept: 'Homeschool Essentials' },
  };

  function readCart() {
    try { return JSON.parse(localStorage.getItem(KEY)) || {}; }
    catch (e) { return {}; }
  }
  function writeCart(cart) {
    localStorage.setItem(KEY, JSON.stringify(cart));
    updateBadges();
  }
  function addToCart(sku, qty) {
    qty = qty || 1;
    const cart = readCart();
    cart[sku] = (cart[sku] || 0) + qty;
    writeCart(cart);
  }
  function setQty(sku, qty) {
    const cart = readCart();
    qty = Math.max(0, parseInt(qty, 10) || 0);
    if (qty === 0) delete cart[sku];
    else cart[sku] = qty;
    writeCart(cart);
  }
  function removeFromCart(sku) {
    const cart = readCart();
    delete cart[sku];
    writeCart(cart);
  }
  function cartCount() {
    return Object.values(readCart()).reduce((a, b) => a + b, 0);
  }
  function cartSubtotal() {
    const cart = readCart();
    return Object.entries(cart).reduce((sum, [sku, qty]) => {
      const p = PRODUCTS[sku];
      return sum + (p ? p.price * qty : 0);
    }, 0);
  }
  function updateBadges() {
    const n = cartCount();
    document.querySelectorAll('[data-cart-badge]').forEach(function (el) {
      el.textContent = n;
      el.hidden = n === 0;
    });
  }

  function toast(msg) {
    let t = document.getElementById('mmm-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'mmm-toast';
      t.style.cssText = 'position:fixed;bottom:1.25rem;left:50%;transform:translateX(-50%) translateY(8px);' +
        'background:var(--ink);color:var(--paper);font-family:var(--mono);font-size:.72rem;letter-spacing:.06em;' +
        'padding:.65rem 1rem;border-radius:3px;z-index:200;opacity:0;transition:opacity .2s ease, transform .2s ease;pointer-events:none';
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.style.opacity = '1';
    t.style.transform = 'translateX(-50%) translateY(0)';
    clearTimeout(t._hideTimer);
    t._hideTimer = setTimeout(function () {
      t.style.opacity = '0';
      t.style.transform = 'translateX(-50%) translateY(8px)';
    }, 1600);
  }

  document.addEventListener('DOMContentLoaded', function () {
    updateBadges();
    document.querySelectorAll('[data-add-to-cart]').forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.preventDefault();
        const sku = btn.getAttribute('data-add-to-cart');
        const qtyInput = document.querySelector('[data-qty-for="' + sku + '"]');
        const qty = qtyInput ? parseInt(qtyInput.value, 10) || 1 : 1;
        addToCart(sku, qty);
        const p = PRODUCTS[sku];
        toast((p ? p.name : sku) + ' added — ' + cartCount() + ' in order');
      });
    });

    // Order page render
    const list = document.getElementById('order-list');
    if (list) renderOrder();
  });

  function renderOrder() {
    const cart = readCart();
    const list = document.getElementById('order-list');
    const empty = document.getElementById('order-empty');
    const skus = Object.keys(cart);

    if (skus.length === 0) {
      list.innerHTML = '';
      if (empty) empty.hidden = false;
      updateSummary(0);
      return;
    }
    if (empty) empty.hidden = true;

    list.innerHTML = skus.map(function (sku) {
      const p = PRODUCTS[sku] || { name: sku, price: 0, dept: '' };
      const qty = cart[sku];
      const lineTotal = (p.price * qty).toFixed(2);
      return (
        '<tr data-row="' + sku + '">' +
          '<td><span class="oi-name">' + p.name + '</span><span class="oi-sku">' + sku + ' · ' + p.dept + '</span></td>' +
          '<td class="oi-num">$' + p.price.toFixed(2) + '</td>' +
          '<td style="text-align:center"><span class="qty">' +
            '<button type="button" data-step="-1" data-sku="' + sku + '" aria-label="Decrease quantity">−</button>' +
            '<input type="text" inputmode="numeric" value="' + qty + '" data-qty-for="' + sku + '" aria-label="Quantity for ' + p.name + '">' +
            '<button type="button" data-step="1" data-sku="' + sku + '" aria-label="Increase quantity">+</button>' +
          '</span></td>' +
          '<td class="oi-num">$' + lineTotal + '</td>' +
          '<td><button type="button" class="oi-remove" data-remove="' + sku + '">Remove</button></td>' +
        '</tr>'
      );
    }).join('');

    list.querySelectorAll('[data-step]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const sku = btn.getAttribute('data-sku');
        const step = parseInt(btn.getAttribute('data-step'), 10);
        const current = readCart()[sku] || 0;
        setQty(sku, current + step);
        renderOrder();
      });
    });
    list.querySelectorAll('[data-qty-for]').forEach(function (input) {
      input.addEventListener('change', function () {
        setQty(input.getAttribute('data-qty-for'), input.value);
        renderOrder();
      });
    });
    list.querySelectorAll('[data-remove]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        removeFromCart(btn.getAttribute('data-remove'));
        renderOrder();
      });
    });

    updateSummary(cartSubtotal());
    buildQuoteLink();
  }

  function updateSummary(subtotal) {
    const el = document.getElementById('order-subtotal');
    const total = document.getElementById('order-total');
    if (el) el.textContent = '$' + subtotal.toFixed(2);
    if (total) total.textContent = '$' + subtotal.toFixed(2);
  }

  function buildQuoteLink() {
    const cart = readCart();
    const link = document.getElementById('quote-link');
    if (!link) return;
    const lines = Object.entries(cart).map(function ([sku, qty]) {
      const p = PRODUCTS[sku] || { name: sku, price: 0 };
      return '- ' + p.name + ' (' + sku + ')  x' + qty + '  @ $' + p.price.toFixed(2);
    });
    const body = 'Hi MMM Investment,%0D%0A%0D%0AI would like an itemized quote for:%0D%0A%0D%0A' +
      encodeURIComponent(lines.join('\n')).replace(/%0A/g, '%0D%0A') +
      '%0D%0A%0D%0AEstimated subtotal: $' + cartSubtotal().toFixed(2) +
      '%0D%0A%0D%0AState: [your state]%0D%0AFunding program: [TEFA / other]%0D%0A';
    link.href = 'mailto:Mmminvestment25@gmail.com?subject=' +
      encodeURIComponent('Quote request — ' + Object.keys(cart).length + ' item(s)') +
      '&body=' + body;
  }

  window.mmmCart = { addToCart, removeFromCart, setQty, cartCount, cartSubtotal, PRODUCTS };
})();
