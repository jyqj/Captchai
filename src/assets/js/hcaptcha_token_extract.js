() => {
    let token = null;
    let error = null;
    __DOM_READ__
    if (!token && window.__omcToken) {
        token = window.__omcToken;
    }
    if (!token) {
        const textarea = document.querySelector('[name="h-captcha-response"]')
            || document.querySelector('[name="g-recaptcha-response"]');
        if (textarea && textarea.value && textarea.value.length > 20) {
            token = textarea.value;
        } else if (window.hcaptcha && typeof window.hcaptcha.getResponse === 'function') {
            try {
                const resp = window.hcaptcha.getResponse();
                if (resp && resp.length > 20) token = resp;
            } catch (e) {}
        }
    }
    return {token: token, error: error || window.__omcError || null};
}
