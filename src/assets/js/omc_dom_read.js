const __omcEl = document.getElementById('__RESULT_ID__');
    if (__omcEl) {
        const __st = __omcEl.getAttribute('data-status');
        if (__st === 'done') token = __omcEl.textContent;
        else if (__st === 'error') error = __omcEl.textContent;
    }
