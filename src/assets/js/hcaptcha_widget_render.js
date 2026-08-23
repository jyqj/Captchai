window.__omcWidgetId = window.hcaptcha.render('omc-hcaptcha', opts);
window.__omcExecute = function () {
    if (window.__omcExecuted) return;
    window.__omcExecuted = true;
    try { window.hcaptcha.execute(window.__omcWidgetId); }
    catch (e) { window.__omcError = String(e); __omcSet('error', e && e.message ? e.message : String(e)); }
};
__omcInstallExecBridge();
if (opts.size === 'invisible' && !window.__omcDeferExecute) {
    window.__omcExecute();
}
