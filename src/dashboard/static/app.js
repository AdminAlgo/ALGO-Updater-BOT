// Vanilla-JS row menus + modals — no framework, matches this dashboard's
// server-rendered style. One generic modal per page is reused across rows:
// a trigger's data-field-<name>="value" attributes populate the modal's
// matching form fields, and data-action-template (with an __ID__ placeholder)
// sets the form's submit URL.
document.addEventListener('click', function (e) {
  var kebabBtn = e.target.closest('.kebab-btn');
  document.querySelectorAll('.kebab.open').forEach(function (k) {
    if (!kebabBtn || k !== kebabBtn.closest('.kebab')) k.classList.remove('open');
  });
  if (kebabBtn) {
    var kebab = kebabBtn.closest('.kebab');
    kebab.classList.toggle('open');
    // Rows near the bottom of the window would otherwise drop their menu below
    // the fold, where it reads as a half-rendered menu rather than a scroll.
    kebab.classList.remove('drop-up');
    if (kebab.classList.contains('open')) {
      var menu = kebab.querySelector('.menu');
      if (menu && kebabBtn.getBoundingClientRect().bottom + menu.offsetHeight + 8
                  > window.innerHeight) {
        kebab.classList.add('drop-up');
      }
    }
    e.stopPropagation();
    return;
  }

  var opener = e.target.closest('[data-open-modal]');
  if (opener) {
    var modal = document.getElementById(opener.getAttribute('data-open-modal'));
    if (modal) {
      Array.prototype.slice.call(opener.attributes).forEach(function (attr) {
        var m = attr.name.match(/^data-field-(.+)$/);
        if (!m) return;
        // querySelectorAll, not querySelector: a group of checkboxes shares one
        // name, and filling only the first left the rest unticked — so saving
        // the dialog silently switched those alert types off.
        var fields = modal.querySelectorAll('[name="' + m[1] + '"]');
        var values = (attr.value || '').split(',').filter(Boolean);
        Array.prototype.forEach.call(fields, function (field) {
          if (field.type === 'checkbox') {
            field.checked = values.indexOf(field.value) !== -1;
          } else {
            field.value = attr.value;
          }
        });
      });
      var titleEl = modal.querySelector('[data-modal-title]');
      if (titleEl) titleEl.textContent = opener.getAttribute('data-title') || '';
      var form = modal.querySelector('form[data-action-template]');
      if (form) {
        var id = opener.getAttribute('data-id') || '';
        form.action = form.getAttribute('data-action-template').replace('__ID__', encodeURIComponent(id));
      }
      var overlay = modal.closest('.modal-overlay');
      if (overlay) overlay.classList.add('open');
    }
    return;
  }

  var closer = e.target.closest('[data-close-modal]');
  if (closer) {
    var closeOverlay = closer.closest('.modal-overlay');
    if (closeOverlay) closeOverlay.classList.remove('open');
    return;
  }

  if (e.target.classList.contains('modal-overlay')) {
    e.target.classList.remove('open');
  }
});

// Expand row (▸ Companies §4.3).
document.addEventListener('click', function (e) {
  var toggle = e.target.closest('.expand-toggle');
  if (!toggle) return;
  var row = document.getElementById(toggle.getAttribute('data-expand-target'));
  if (!row) return;
  var opening = !row.classList.contains('open');
  row.classList.toggle('open');
  toggle.textContent = opening ? '▾' : '▸';
});

// Confirm destructive actions: <form data-confirm="Are you sure?">.
document.addEventListener('submit', function (e) {
  var msg = e.target.getAttribute && e.target.getAttribute('data-confirm');
  if (msg && !window.confirm(msg)) e.preventDefault();
}, true);

// <select data-autosubmit> submits its form on change (filters, sort).
document.addEventListener('change', function (e) {
  if (e.target.matches && e.target.matches('select[data-autosubmit]')) e.target.form.submit();
});

// Instant search: <input data-filter-table="tableId"> hides non-matching rows.
document.addEventListener('input', function (e) {
  var id = e.target.getAttribute && e.target.getAttribute('data-filter-table');
  if (!id) return;
  var table = document.getElementById(id);
  if (!table) return;
  var q = e.target.value.trim().toLowerCase();
  table.querySelectorAll('tbody tr[data-search]').forEach(function (tr) {
    tr.style.display = !q || tr.getAttribute('data-search').indexOf(q) !== -1 ? '' : 'none';
  });
});

// Warnings form: show the Company / Driver picker only for that audience.
(function () {
  var sel = document.getElementById('audienceSelect');
  if (!sel) return;
  function sync() {
    document.querySelectorAll('[data-show-when]').forEach(function (el) {
      el.style.display = el.getAttribute('data-show-when') === sel.value ? '' : 'none';
    });
  }
  sel.addEventListener('change', sync);
  sync();
})();

// Live pages (<body data-autorefresh="120">) reload themselves like the ELD
// platform does — but never while you are typing, in a menu, or in a dialog.
(function () {
  var secs = parseInt(document.body.getAttribute('data-autorefresh') || '0', 10);
  if (!secs) return;
  setInterval(function () {
    var el = document.activeElement;
    var busy = document.querySelector('.modal-overlay.open, .kebab.open') ||
      (el && /INPUT|TEXTAREA|SELECT/.test(el.tagName));
    if (!busy && !document.hidden) window.location.reload();
  }, secs * 1000);
})();
