/* Prototype-only client-side cart + checkout for the General Store — a
   separate retail line (books, activity books) intended for a future retail payment flow,
   kept apart from the ESA-funded store/cart.js on purpose so a retail
   sale never gets mixed into a TEFA/ESA invoice.

   No payment is ever collected here. "Preview confirmation" on checkout.html only
   ever shows a client-side confirmation panel — there is no backend, and
   this file must not be extended with a real card-number field without
   first wiring up an actual PCI-compliant payment processor. */
(function () {
  const KEY = 'mmm_gs_cart_v1';

  const PRODUCTS = {
    'GEN-BK-001': { name: 'The Vulturian', price: null, format: 'Book' },
    'GEN-CB-101': { name: 'Future Founders — Coloring Book', price: 8.95, format: 'Coloring book' },
    'GEN-CB-102': { name: 'Tools & Trades — Coloring Book', price: 8.95, format: 'Coloring book' },
    'GEN-CB-103': { name: 'Big Feelings, Big Wins — Coloring Book', price: 8.95, format: 'Coloring book' },
  };
  const TAX_RATE = 0.0825; // placeholder — see general-store/README note
  const SHIPPING = { standard: 4.95, expedited: 12.95 };

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
      return sum + (p && p.price ? p.price * qty : 0);
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
        toast((p ? p.name : sku) + ' added to preview — ' + cartCount() + ' in preview cart');
      });
    });

    if (document.getElementById('checkout-list')) renderCheckout();
  });

  function moneyOrTBD(v) {
    return v == null ? 'TBD' : '$' + v.toFixed(2);
  }

  function renderCheckout() {
    const cart = readCart();
    const list = document.getElementById('checkout-list');
    const empty = document.getElementById('checkout-empty');
    const skus = Object.keys(cart);

    if (skus.length === 0) {
      list.innerHTML = '';
      if (empty) empty.hidden = false;
      updateTotals(0, true);
      return;
    }
    if (empty) empty.hidden = true;

    let anyTBD = false;
    list.innerHTML = skus.map(function (sku) {
      const p = PRODUCTS[sku] || { name: sku, price: 0, format: '' };
      const qty = cart[sku];
      if (p.price == null) anyTBD = true;
      const lineTotal = p.price == null ? null : (p.price * qty);
      return (
        '<tr>' +
          '<td><span class="oi-name">' + p.name + '</span><span class="oi-sku">' + sku + ' · ' + p.format + '</span></td>' +
          '<td class="oi-num">' + moneyOrTBD(p.price) + '</td>' +
          '<td style="text-align:center"><span class="qty">' +
            '<button type="button" data-step="-1" data-sku="' + sku + '" aria-label="Decrease quantity">−</button>' +
            '<input type="text" inputmode="numeric" value="' + qty + '" data-qty-for="' + sku + '" aria-label="Quantity for ' + p.name + '">' +
            '<button type="button" data-step="1" data-sku="' + sku + '" aria-label="Increase quantity">+</button>' +
          '</span></td>' +
          '<td class="oi-num">' + moneyOrTBD(lineTotal) + '</td>' +
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
        renderCheckout();
      });
    });
    list.querySelectorAll('[data-qty-for]').forEach(function (input) {
      input.addEventListener('change', function () {
        setQty(input.getAttribute('data-qty-for'), input.value);
        renderCheckout();
      });
    });
    list.querySelectorAll('[data-remove]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        removeFromCart(btn.getAttribute('data-remove'));
        renderCheckout();
      });
    });

    updateTotals(cartSubtotal(), anyTBD);
  }

  function currentShipping() {
    const checked = document.querySelector('input[name="ship"]:checked');
    return checked ? SHIPPING[checked.value] : SHIPPING.standard;
  }

  function updateTotals(subtotal, anyTBD) {
    const shipping = currentShipping();
    const tax = subtotal * TAX_RATE;
    const total = subtotal + shipping + tax;
    const set = function (id, val) { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('co-subtotal', anyTBD ? '$' + subtotal.toFixed(2) + ' + TBD' : '$' + subtotal.toFixed(2));
    set('co-shipping', '$' + shipping.toFixed(2));
    set('co-tax', '$' + tax.toFixed(2) + ' (est.)');
    set('co-total', (anyTBD ? '$' + total.toFixed(2) + ' + TBD' : '$' + total.toFixed(2)));
  }

  document.addEventListener('change', function (e) {
    if (e.target.name === 'ship') renderCheckout();
  });

  const placeOrderBtn = document.getElementById('place-order');
  if (placeOrderBtn) {
    placeOrderBtn.addEventListener('click', function (e) {
      e.preventDefault();
      const form = document.getElementById('checkout-form');
      if (form && !form.reportValidity()) return;
      writeCart({}); // the preview confirmation resets this device-only cart
      document.getElementById('checkout-body').hidden = true;
      document.getElementById('checkout-confirm').hidden = false;
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  window.mmmGsCart = { addToCart, removeFromCart, setQty, cartCount, cartSubtotal, PRODUCTS };
})();
