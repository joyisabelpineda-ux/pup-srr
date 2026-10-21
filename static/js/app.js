/* PUP SRR front-end behaviour. No inline scripts are used (strict Content-Security-Policy). */
(function () {
  'use strict';
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // Mobile sidebar
  var side = $('#sidebar'), back = $('#backdrop'), menu = $('#menu-btn');
  function toggleMenu(open) {
    if (!side) return;
    side.classList.toggle('open', open);
    if (back) back.classList.toggle('open', open);
    if (menu) menu.setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  if (menu) menu.addEventListener('click', function () { toggleMenu(!side.classList.contains('open')); });
  if (back) back.addEventListener('click', function () { toggleMenu(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') toggleMenu(false); });

  // Dismissable notifications (success messages fade after a while)
  $$('.flash').forEach(function (f) {
    var b = $('button', f);
    if (b) b.addEventListener('click', function () { f.remove(); });
    if (f.classList.contains('success')) setTimeout(function () { if (f.parentNode) f.remove(); }, 8000);
  });

  // Show / hide password
  $$('[data-toggle-pw]').forEach(function (b) {
    b.addEventListener('click', function () {
      var i = document.getElementById(b.getAttribute('data-toggle-pw'));
      if (!i) return;
      var show = i.type === 'password';
      i.type = show ? 'text' : 'password';
      b.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      b.setAttribute('aria-pressed', show ? 'true' : 'false');
    });
  });

  // Confirmation dialog for important actions: <form data-confirm="..."> or <button data-confirm="...">
  var dlg = $('#confirm-dialog');
  var pending = null;
  function ask(form, submitter, text, title, label, danger) {
    if (!dlg || typeof dlg.showModal !== 'function') {            // very old browsers: native confirm
      return window.confirm(text);
    }
    pending = { form: form, submitter: submitter };
    $('#cd-title').textContent = title || 'Please confirm';
    $('#cd-text').textContent = text;
    var ok = $('#cd-ok');
    ok.textContent = label || 'Confirm';
    ok.className = 'btn ' + (danger === 'no' ? '' : 'danger');
    dlg.showModal();
    return false;
  }
  document.addEventListener('submit', function (e) {
    var form = e.target, sub = e.submitter;
    var src = (sub && sub.hasAttribute('data-confirm')) ? sub : (form.hasAttribute('data-confirm') ? form : null);
    if (src && !form.__confirmed) {
      var allowed = ask(form, sub, src.getAttribute('data-confirm'), src.getAttribute('data-confirm-title'),
                        src.getAttribute('data-confirm-label'), src.getAttribute('data-confirm-style'));
      if (allowed !== true) { e.preventDefault(); return; }
    }
    form.__confirmed = false;
    var btn = sub || $('button[type=submit],button:not([type])', form);
    if (btn && !form.hasAttribute('data-no-loading')) {
      btn.classList.add('loading');
      setTimeout(function () { btn.setAttribute('aria-busy', 'true'); }, 0);
    }
  }, true);
  if (dlg) {
    dlg.addEventListener('close', function () {
      if (dlg.returnValue === 'ok' && pending) {
        var p = pending; pending = null;
        p.form.__confirmed = true;
        if (p.form.requestSubmit) p.form.requestSubmit(p.submitter || undefined); else p.form.submit();
      } else { pending = null; }
      dlg.returnValue = '';
    });
  }
  // Restore buttons when navigating back from bfcache
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) $$('.btn.loading').forEach(function (b) { b.classList.remove('loading'); b.removeAttribute('aria-busy'); });
  });

  // Filters that apply as soon as a select changes
  $$('select[data-autosubmit]').forEach(function (s) { s.addEventListener('change', function () { s.form.submit(); }); });

  // Reservation form helpers: keep end time after start time
  var st = $('#start'), en = $('#end');
  if (st && en) {
    st.addEventListener('change', function () {
      if (!st.value) return;
      en.min = st.value;
      if (!en.value || en.value <= st.value) {
        var p = st.value.split(':'), h = (parseInt(p[0], 10) + 1) % 24;
        en.value = (h < 10 ? '0' : '') + h + ':' + p[1];
      }
    });
    var form = st.form;
    form.addEventListener('submit', function (e) {
      if (st.value && en.value && en.value <= st.value) {
        e.preventDefault(); en.setCustomValidity('End time must be after the start time.'); en.reportValidity();
      }
    });
    en.addEventListener('input', function () { en.setCustomValidity(''); });
  }
  // Print button on QR cards
  $$('[data-print]').forEach(function (b) { b.addEventListener('click', function () { window.print(); }); });
})();
