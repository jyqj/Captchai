() => { const el = document.getElementById('__RESULT_ID__'); if (el && el.getAttribute('data-status') === 'error') { return el.textContent || 'error'; } return window.__omcError || null; }
