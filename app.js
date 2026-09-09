// Accessibility only. Layout and slide-menu behavior come from Gallery03.
const button=document.querySelector('#js-hamburger');
const navigation=document.querySelector('#js-globalnav');
function syncMenu(){const open=button.classList.contains('_active');button.setAttribute('aria-expanded',String(open));button.setAttribute('aria-label',open?'關閉選單':'開啟選單');}
function closeMenu(){button.classList.remove('_active');navigation.classList.remove('_active');document.querySelector('#js-main').classList.remove('_darker');syncMenu();}
button.addEventListener('click',syncMenu);
let activeView='credits';
const viewScroll={credits:0,lookup:0};
navigation.addEventListener('click',e=>{
 const link=e.target.closest('a');if(!link)return;
 e.preventDefault();closeMenu();
 const hash=link.getAttribute('href'),next=hash==='#course-lookup'?'lookup':'credits';
 const scroller=document.querySelector('.cloud-scroll')||document.scrollingElement;
 const previous=activeView;viewScroll[previous]=scroller.scrollTop;
 document.querySelectorAll('.fv_text > section').forEach(section=>section.hidden=(section.id==='course-lookup')!==(next==='lookup'));
 activeView=next;
 navigation.querySelectorAll('a').forEach(a=>a.removeAttribute('aria-current'));
 link.setAttribute('aria-current','page');
 if(next==='lookup')window.loadCourseLookup?.();
 if(previous!==next){
  scroller.scrollTo({top:viewScroll[next],behavior:'instant'});
  const heading=document.querySelector(next==='lookup'?'#lookup-heading':'#about .headingL');
  heading.setAttribute('tabindex','-1');heading.focus({preventScroll:true});
  if(!matchMedia('(prefers-reduced-motion: reduce)').matches)document.querySelector('.fv_text').animate([{opacity:.3},{opacity:1}],{duration:220,easing:'ease-out'});
 }else document.querySelector(hash)?.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});
 if(hash==='#information')document.querySelector(hash).scrollIntoView({behavior:'instant'});
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&button.classList.contains('_active')){closeMenu();button.focus();}});
