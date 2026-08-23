() => {
    let token = null;
    let error = null;
    __DOM_READ__
    if (!token && window.__omcToken) {
        token = window.__omcToken;
    }
    if (!token) {
        const input = document.querySelector('[name="cf-turnstile-response"]')
            || document.querySelector('input[name*="turnstile"]');
        if (input && input.value && input.value.length > 20) {
            token = input.value;
        } else if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
            try {
                const resp = window.turnstile.getResponse();
                if (resp && resp.length > 20) token = resp;
            } catch (e) {}
        }
    }
    return {token: token, error: error || window.__omcError || null};
}
