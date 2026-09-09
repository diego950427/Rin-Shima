// Accessibility only. Layout and slide-menu behavior come from Gallery03.
const button=document.querySelector('#js-hamburger');
const navigation=document.querySelector('#js-globalnav');
function syncMenu(){const open=button.classList.contains('_active');button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'關閉選單':'開啟選單');}
function closeMenu(){button.classList.remove('_active');navigation.classList.remove('_active');document.querySelector('#js-main').classList.remove('_darker');syncMenu();}
button.addEventListener('click',syncMenu);
navigation.addEventListener('click',e=>{if(e.target.closest('a'))closeMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&button.classList.contains('_active')){closeMenu();button.focus();}});
