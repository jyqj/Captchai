function __omcResultEl(){var el=document.getElementById('__RESULT_ID__');
if(!el){el=document.createElement('div');el.id='__RESULT_ID__';
el.setAttribute('data-status','');el.style.display='none';
(document.body||document.documentElement).appendChild(el);}return el;}
function __omcSet(status,value){var el=__omcResultEl();
el.textContent=(value==null?'':String(value));
el.setAttribute('data-status',status);}
function __omcMarkReady(){var el=__omcResultEl();
if(!el.getAttribute('data-status')){el.setAttribute('data-status','rendered');}}
function __omcInstallExecBridge(){var t=document.getElementById('__EXEC_ID__');
if(!t){t=document.createElement('div');t.id='__EXEC_ID__';
t.setAttribute('data-exec','0');t.style.display='none';
(document.body||document.documentElement).appendChild(t);}
try{var obs=new MutationObserver(function(){
if(t.getAttribute('data-exec')==='1'&&window.__omcExecute){
window.__omcExecute();}});
obs.observe(t,{attributes:true,attributeFilter:['data-exec']});}catch(e){}}
