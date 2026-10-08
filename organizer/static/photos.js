'use strict';
let photoRequest=0, photoItems=[], photoPage='', photoIndex=0, photoReturnFocus=null;
function closePhotos(){
 document.body.classList.remove('photos-view');
 photoRequest++;$('photos-panel').hidden=true;$('photos-open').setAttribute('aria-pressed','false');
 closePhotoDialog();
}
function openPhotos(){
 document.body.classList.add('photos-view');
 ++generation;selected=null;setSidebar(false);
 $('welcome').hidden=$('document').hidden=$('ai-panel').hidden=$('weekly-panel').hidden=true;
 $('photos-panel').hidden=false;$('photos-open').setAttribute('aria-pressed','true');
 $('photos-scope').textContent=scopeName()+' · Google Drive 사진';
 const url=new URL(scopedUrl(location.pathname),location.origin);url.searchParams.set('view','photos');history.replaceState(null,'',url.pathname+url.search);
 document.querySelector('.reader').classList.add('open');loadPhotos();
 if(mobile.matches)document.querySelector('.reader').scrollIntoView({behavior:'smooth',block:'start'});
}
function photoFolder(url){
 const a=$('photos-drive');a.hidden=!/^https:\/\/drive\.google\.com\/drive\/folders\/[A-Za-z0-9_-]+$/.test(url||'');
 if(a.hidden)a.removeAttribute('href');else a.href=url;
}
async function loadPhotos(append=false){
 const seq=++photoRequest, version=scopeVersion;
 if(!append){photoItems=[];photoPage='';$('photos-grid').replaceChildren();photoFolder(null);}
 $('photos-status').textContent='사진을 불러오고 있어요…';$('photos-more').hidden=true;
 try{
  const params=new URLSearchParams({sort:$('photos-sort').value,days:$('photos-days').value,page_token:photoPage});
  const response=await fetch(scopedUrl('/library/api/photos?'+params));
  if(response.redirected||!response.headers.get('content-type')?.includes('application/json'))throw Error('자료실 연결을 확인해 주세요.');
  const d=await response.json();
  if(seq!==photoRequest||version!==scopeVersion||$('photos-panel').hidden)return;
  photoFolder(d.folder_url);if(!response.ok)throw Error(d.error||'사진을 불러오지 못했습니다.');
  d.photos.forEach(p=>{
   const index=photoItems.length;photoItems.push(p);
   const card=node('button',undefined,'photo-card');card.type='button';card.setAttribute('aria-label',p.name+' 크게 보기');
   const frame=node('span',undefined,'photo-thumb'),img=node('img');img.src=scopedUrl('/library/photos/'+encodeURIComponent(p.id)+'/thumbnail');img.alt='';img.loading='lazy';img.decoding='async';
   img.onerror=()=>{frame.replaceChildren(node('span','미리보기 없음 · 눌러서 원본 보기','photo-missing'));};frame.append(img);
   card.append(frame,node('span',p.name,'photo-name'),node('span',p.created_at?new Date(p.created_at).toLocaleDateString('ko-KR',{timeZone:'Asia/Seoul'}):'등록일 미상','photo-date'));
   card.onclick=()=>openPhoto(index,card);$('photos-grid').append(card);
  });
  photoPage=d.next_page_token||'';$('photos-more').hidden=!photoPage;
  $('photos-status').textContent=d.notice||(photoItems.length?photoItems.length+'장'+(photoPage?' · 더 불러올 수 있어요':''):'등록된 사진이 없어요.');
 }catch(e){if(seq===photoRequest&&version===scopeVersion&&!$('photos-panel').hidden){$('photos-status').textContent=e.message;$('photos-more').hidden=!photoPage;}}
}
function openPhoto(index,button){
 photoIndex=index;if(button)photoReturnFocus=button;
 const p=photoItems[index];if(!p)return;
 $('photo-title').textContent=p.name;$('photo-frame').src=p.preview_url;$('photo-original').href=p.url;
 $('photo-position').textContent=(index+1)+' / '+photoItems.length;
 $('photo-prev').disabled=index===0;$('photo-next').disabled=index===photoItems.length-1;
 if(!$('photo-dialog').open){$('photo-dialog').showModal();$('photo-close').focus();}
}
function closePhotoDialog(){if($('photo-dialog').open)$('photo-dialog').close();$('photo-frame').removeAttribute('src');}
$('photo-dialog').addEventListener('close',()=>{$('photo-frame').removeAttribute('src');if(photoReturnFocus?.isConnected)photoReturnFocus.focus();});
$('photo-close').onclick=closePhotoDialog;
$('photo-prev').onclick=()=>openPhoto(photoIndex-1);
$('photo-next').onclick=()=>openPhoto(photoIndex+1);
$('photo-dialog').addEventListener('keydown',e=>{if(e.key==='ArrowLeft'&&photoIndex>0)openPhoto(photoIndex-1);if(e.key==='ArrowRight'&&photoIndex<photoItems.length-1)openPhoto(photoIndex+1);});
$('photos-open').onclick=openPhotos;
$('photos-refresh').onclick=()=>loadPhotos();
$('photos-sort').onchange=$('photos-days').onchange=()=>loadPhotos();
$('photos-more').onclick=()=>loadPhotos(true);
