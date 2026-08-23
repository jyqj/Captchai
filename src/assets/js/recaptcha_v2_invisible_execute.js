([key]) => new Promise((resolve, reject) => {
    const gr = window.grecaptcha?.enterprise || window.grecaptcha;
    if (!gr) { reject(new Error('grecaptcha not found')); return; }
    gr.ready(() => {
        gr.execute(key).then(resolve).catch(reject);
    });
})
