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
    kebabBtn.closest('.kebab').classList.toggle('open');
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
        var field = modal.querySelector('[name="' + m[1] + '"]');
        if (!field) return;
        if (field.type === 'checkbox') {
          var values = (attr.value || '').split(',').filter(Boolean);
          field.checked = values.indexOf(field.value) !== -1;
        } else {
          field.value = attr.value;
        }
      });
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
