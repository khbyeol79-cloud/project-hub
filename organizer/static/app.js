'use strict';
const $ = id => document.getElementById(id);
let selected = null, offset = 0, generation = 0, searching = 0, busy = false, combinedFileOffset=0, combinedMessageOffset=0, combinedFilesMore=true, combinedMessagesMore=true;
const statusNames = {indexed:'본문 준비됨',partial:'일부 본문',pending:'본문 준비 중',unsupported:'원본으로 보기',no_text:'추출된 본문 없음'};
function node(tag, text, cls) { const e=document.createElement(tag); if(text!==undefined)e.textContent=text; if(cls)e.className=cls; return e; }
async function api(path, options={}) { const r=await fetch('/library/api/'+path,options); if(r.redirected||!r.headers.get('content-type')?.includes('application/json'))throw Error('자료실 연결을 확인해 주세요.');const data=await r.json();if(!r.ok)throw Error(data.error||'요청에 실패했습니다.');return data; }
const size = n => n<1024*1024 ? Math.max(1,Math.round(n/1024))+' KB' : (n/1024/1024).toFixed(1)+' MB';
async function loadFiles(append=false){if($('search-kind').value==='all')return loadCombined(append);if($('search-kind').value==='messages')return loadMessages(append);const seq=++searching; if(!append)offset=0; $('list-message').textContent='자료를 찾고 있어요…';$('more').hidden=true;
 try {const params=new URLSearchParams({q:$('query').value,channel:$('channel').value,extension:$('extension').value,sort:$('sort').value,view:$('file-view').value,days:$('message-days').value,offset}); const d=await api('files?'+params); if(seq!==searching)return;
 if(!append)$('files').replaceChildren(); const channel=$('channel').value;$('channel').replaceChildren(new Option('모든 채널','')); d.channels.forEach(c=>$('channel').add(new Option(c.name,c.id)));if(channel&&![...$('channel').options].some(o=>o.value===channel))$('channel').add(new Option(channel,channel));$('channel').value=channel;const ext=$('extension').value;$('extension').replaceChildren(new Option('모든 파일',''));d.extensions.forEach(e=>$('extension').add(new Option(e==='(none)'?'확장자 없음':e,e)));$('extension').value=ext;if(!append)$('file-scroll').scrollTop=0;
 d.files.forEach(f=>{const b=node('button',undefined,'file');b.dataset.id=f.id;b.classList.toggle('selected',selected?.id===f.id);b.setAttribute('aria-label',f.original_filename+' 열기');b.append(node('span',f.original_filename.split('.').pop().slice(0,5).toUpperCase(),'file-icon'));const info=node('span',undefined,'file-info');info.append(node('span',f.original_filename,'filename'));info.append(node('span',f.discord_channel+' · '+size(f.file_size_bytes),'file-meta'));const meta=node('span',(statusNames[f.content_status]||'원본으로 보기')+' · '+String(f.uploaded_at).slice(0,10),'file-meta');if(f.identical_count)meta.append(node('span','동일 내용 '+(f.identical_count+1)+'건','badge'));if(f.content_versions>1)meta.append(node('span','내용 '+f.content_versions+'종','badge'));if(f.upload_count>1)meta.append(node('span',f.latest_id===f.id?'최신 업로드':'이전 업로드','badge'));info.append(meta);if(f.excerpt)info.append(node('span',f.excerpt,'search-excerpt'));b.append(info);b.onclick=()=>openFile(f.id);$('files').append(b);});
 $('count').textContent=$('files').children.length+' / '+d.total+'개';$('list-message').textContent=$('files').children.length?'':'검색된 자료가 없어요. 다른 단어로 찾아보세요.';offset+=d.files.length;$('more').hidden=!d.more;
 }catch(e){if(seq===searching)$('list-message').textContent=e.message;}}
function tab(ai){scopeLabel();$('weekly-panel').hidden=true;$('document').hidden=ai||!selected;$('welcome').hidden=ai||Boolean(selected);$('ai-panel').hidden=!ai;document.querySelector('.reader').classList.add('open');if(ai){setSidebar(false);usage();document.querySelector('.reader').scrollIntoView({behavior:'smooth',block:'start'});}}

async function openFile(id,sourcePage=null){$('file-versions').replaceChildren();setSidebar(false);const seq=++generation;selected=null;$('welcome').hidden=true;$('document').hidden=false;document.querySelector('.reader').classList.add('open');document.querySelector('.workspace').classList.add('reading');$('doc-title').textContent='자료를 여는 중…';$('pages').replaceChildren();$('doc-channel').textContent='';$('doc-meta').textContent='';$('download').hidden=true;$('drive-view').hidden=true;$('drive-view').removeAttribute('href');$('partial').hidden=true;tab(false);$('document').hidden=false;$('welcome').hidden=true;document.querySelectorAll('.file').forEach(e=>e.classList.toggle('selected',e.dataset.id===String(id)));
 try{const f=await api('files/'+id+(sourcePage?'?source_page='+encodeURIComponent(sourcePage):''));if(seq!==generation)return;selected=f;$('doc-title').textContent=f.original_filename;$('doc-channel').textContent=f.discord_channel;$('doc-meta').textContent=size(f.file_size_bytes)+' · '+String(f.uploaded_at).slice(0,10)+' · 자료 #'+f.id;$('download').href='/library/files/'+id+'/download';$('download').hidden=false;if(f.drive_url){$('drive-view').href=f.drive_url;$('drive-view').hidden=false;}$('partial').hidden=!f.partial;
 const preview=f.html_preview?'/library/files/'+id+'/html-preview':f.drive_preview_url;
 renderVersions(f);renderReading(f,preview);$('partial').hidden=true;updateButtons();if(matchMedia('(max-width:800px)').matches)document.querySelector('.reader').scrollIntoView({behavior:'smooth',block:'start'});
 }catch(e){if(seq===generation){$('doc-title').textContent='자료를 열 수 없어요';$('pages').append(node('p',e.message,'empty'));}}}
function updateButtons() {$('summarize').disabled=busy;$('ask-button').disabled=busy;}
async function usage(){try{const s=await api('status');$('usage').textContent=s.enabled?'오늘 '+s.daily_calls+'/'+s.daily_limit+'회 · 이번 달 '+s.monthly_calls+'/'+s.monthly_limit+'회 사용':'AI 정리 기능이 꺼져 있어요.';}catch{}}
async function runAI(task){if(busy)return;busy=true;updateButtons();$('ai-status').textContent='자료를 읽고 정리하고 있어요. 잠시 기다려 주세요…';$('answer').hidden=true;
 try{const r=await api('ai',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ProjectHub'},body:JSON.stringify({task,question:task==='ask'?$('question').value:'',days:Number($('message-days').value),channel:$('channel').value})});
 $('answer').hidden=false;$('answer-label').textContent=(task==='ask'?'선택 범위 답변':'최근 자료 정리')+(r.cached?' · 저장된 결과':'');$('answer-body').textContent=r.answer;$('answer-partial').hidden=!r.partial;$('sources').replaceChildren();r.sources.forEach(s=>{if(s.kind==='message'){const a=node('a','['+s.id+'] '+s.label);if(/^https:\/\/discord\.com\/channels\/\d+\/\d+\/\d+$/.test(s.url||'')){a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';}$('sources').append(a);return;}const b=node('button','['+s.id+'] '+s.label);b.onclick=async()=>{await openFile(s.file_id,s.page);tab(false);if($('extracted-text'))$('extracted-text').open=true;document.getElementById('page-'+s.page)?.scrollIntoView({behavior:'smooth'});};$('sources').append(b);});$('ai-status').textContent='';
 }catch(e){$('ai-status').textContent=e.message;}finally{busy=false;updateButtons();usage();}}
$('search').onsubmit=e=>{e.preventDefault();loadFiles();};$('channel').onchange=()=>{scopeLabel();loadFiles();};$('extension').onchange=()=>loadFiles();$('sort').onchange=()=>loadFiles();$('more').onclick=()=>loadFiles(true);$('tab-text').onclick=()=>tab(false);$('global-ai').onclick=()=>tab(true);$('sidebar-ai').onclick=()=>tab(true);$('summarize').onclick=()=>runAI('summary');$('ask').onsubmit=e=>{e.preventDefault();runAI('ask');};
async function start(){const token=new URLSearchParams(location.hash.slice(1)).get('key');if(token){history.replaceState(null,'',location.pathname);try{await api('access',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ProjectHub'},body:JSON.stringify({token})});}catch(e){$('list-message').textContent=e.message;return;}}loadFiles();}start();
$('share').onclick=async()=>{try{const r=await api('share-link');await navigator.clipboard.writeText(r.url);$('share').textContent='링크 복사됨 ✓';setTimeout(()=>$('share').textContent='주소 복사',2500);}catch(e){$('list-message').textContent=e.message||'전달받은 원래 공유 링크를 복사해 주세요.';}};

const mobile=matchMedia('(max-width:800px)');
function setSidebar(open){open=Boolean(open&&matchMedia('(max-width:800px)').matches);document.body.classList.toggle('sidebar-open',open);$('menu-toggle').setAttribute('aria-expanded',String(open));$('menu-toggle').setAttribute('aria-label',open?'자료 목록 닫기':'자료 목록 열기');$('sidebar-shade').hidden=!open;const side=$('library-sidebar');side.inert=matchMedia('(max-width:800px)').matches&&!open;document.querySelector('.reader').inert=open;document.querySelector('.intro').inert=open;if(open){side.setAttribute('role','dialog');side.setAttribute('aria-modal','true');$('sidebar-close').focus();}else{side.removeAttribute('role');side.removeAttribute('aria-modal');if(side.contains(document.activeElement))$('menu-toggle').focus();}}
$('menu-toggle').onclick=()=>setSidebar(!document.body.classList.contains('sidebar-open'));
$('sidebar-close').onclick=()=>setSidebar(false);$('sidebar-shade').onclick=()=>setSidebar(false);
document.addEventListener('keydown',e=>{if(!document.body.classList.contains('sidebar-open'))return;if(e.key==='Escape'){setSidebar(false);return;}if(e.key==='Tab'){const items=[...$('library-sidebar').querySelectorAll('button,input,select,[tabindex="0"]')].filter(x=>!x.hidden&&!x.disabled&&x.getClientRects().length);const first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}});
mobile.addEventListener('change',()=>setSidebar(false));setSidebar(mobile.matches);

async function loadMessages(append=false){
 const seq=++searching;if(!append)offset=0;
 $('list-message').textContent='대화를 찾고 있어요…';$('more').hidden=true;
 try{
  const d=await api('messages?'+new URLSearchParams({q:$('query').value,channel:$('channel').value,days:$('message-days').value,offset}));
  if(seq!==searching)return;
  if(!append){$('files').replaceChildren();$('file-scroll').scrollTop=0;}
  const channel=$('channel').value;$('channel').replaceChildren(new Option('모든 채널',''));
  d.channels.forEach(c=>$('channel').add(new Option(c.name,c.id)));if(channel&&![...$('channel').options].some(o=>o.value===channel))$('channel').add(new Option(channel,channel));$('channel').value=channel;
  d.messages.forEach(m=>{
   const b=node('button',undefined,'file');const info=node('span',undefined,'file-info');
   info.append(node('span',m.author_name+' · '+excerpt(m.content,$('query').value),'filename'));
   info.append(node('span',m.category+' · '+new Date(m.created_at).toLocaleString('ko-KR'),'file-meta'));
   b.append(info);b.onclick=()=>openMessage(m);$('files').append(b);
  });
  offset+=d.messages.length;$('count').textContent=$('files').children.length+'개';$('more').hidden=!d.more;
  $('list-message').textContent=!d.enabled?'대화 공개가 설정되지 않았습니다.':!$('files').children.length?'조건에 맞는 대화가 없어요.':'';
 }catch(e){if(seq===searching)$('list-message').textContent=e.message;}
}
function openMessage(m){$('file-versions').replaceChildren();
 ++generation;selected={kind:'message'};setSidebar(false);tab(false);
 $('doc-title').textContent=m.author_name+'님의 대화';$('doc-channel').textContent=m.category;
 $('doc-meta').textContent=new Date(m.created_at).toLocaleString('ko-KR')+(m.edited_at?' · 수정됨':'');
 $('download').hidden=true;$('drive-view').hidden=true;$('partial').hidden=true;$('pages').replaceChildren();
 const body=node('div',undefined,'comfortable-reading');body.append(node('p',m.content));
 if(/^https:\/\/discord\.com\/channels\/\d+\/\d+\/\d+$/.test(m.url||'')){
  const link=node('a','Discord 원문 보기 ↗','download');link.href=m.url;link.target='_blank';link.rel='noopener noreferrer';body.append(link);
 }
 if(m.files.length){body.append(node('h3','이 대화에 첨부된 파일'));m.files.forEach(f=>{const b=node('button',f.original_filename,'secondary');b.onclick=()=>openFile(f.id);body.append(b);});}
 $('pages').append(body);document.querySelector('.reader').scrollIntoView({behavior:'smooth',block:'start'});
}
$('search-kind').onchange=()=>{const kind=$('search-kind').value;$('extension').disabled=kind!=='files';$('sort').disabled=kind!=='files';$('file-view').disabled=kind!=='files';loadFiles();};
$('search-days').onchange=()=>{$('message-days').value=$('search-days').value;scopeLabel();loadFiles();};
$('message-days').onchange=()=>{$('search-days').value=$('message-days').value;scopeLabel();loadFiles();};

let weeklyBusy=false;
function weeklyResult(r){
 $('weekly-result').hidden=!r;if(!r)return;
 $('weekly-date').textContent='정리 시각: '+new Date(r.generated_at).toLocaleString('ko-KR')+' · 파일 '+r.matched_files+'개 / 대화 '+r.matched_messages+'개 참고';
 $('weekly-answer').textContent=r.answer;$('weekly-sources').replaceChildren();
 r.sources.forEach(s=>{if(s.kind==='message'){
  const a=node('a','['+s.id+'] '+s.label,'download');
  if(/^https:\/\/discord\.com\/channels\/\d+\/\d+\/\d+$/.test(s.url||'')){a.href=s.url;a.target='_blank';a.rel='noopener noreferrer';}
  $('weekly-sources').append(a);
 }else{const b=node('button','['+s.id+'] '+s.label,'secondary');b.onclick=()=>openFile(s.file_id,s.page);$('weekly-sources').append(b);}});
}
async function openWeekly(){
 ++generation;setSidebar(false);$('welcome').hidden=true;$('document').hidden=true;$('ai-panel').hidden=true;$('weekly-panel').hidden=false;
 $('weekly-status').textContent='저장된 정리와 최근 자료를 불러오고 있어요…';
 try{const r=await api('weekly');weeklyResult(r.saved);$('weekly-status').textContent=r.saved?'':'저장된 정리가 없습니다. 버튼을 눌러 만들어 보세요.';
 $('weekly-files').replaceChildren();r.files.forEach(f=>{const b=node('button',f.original_filename,'file');b.onclick=()=>openFile(f.id);$('weekly-files').append(b);});
 $('weekly-messages').replaceChildren();r.messages.forEach(m=>{const b=node('button',m.author_name+' · '+m.content.slice(0,100),'file');b.onclick=()=>openMessage(m);$('weekly-messages').append(b);});
 }catch(e){$('weekly-status').textContent=e.message;}
}
$('weekly-open').onclick=openWeekly;
$('weekly-generate').onclick=async()=>{
 if(weeklyBusy)return;weeklyBusy=true;$('weekly-generate').disabled=true;$('weekly-status').textContent='최근 7일 자료를 정리하고 있어요…';
 try{const r=await api('weekly',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ProjectHub'},body:'{}'});weeklyResult(r);$('weekly-status').textContent='정리를 저장했습니다.';}
 catch(e){$('weekly-status').textContent=e.message;}finally{weeklyBusy=false;$('weekly-generate').disabled=false;usage();}
};

$('file-view').onchange=()=>loadFiles();
function renderVersions(f){
 const box=$('file-versions');box.replaceChildren();if(!f.related.length)return;
 if(f.latest_id!==f.id){const latest=node('button','이 파일명의 최신 업로드 열기','secondary');latest.onclick=()=>openFile(f.latest_id);box.append(latest);}
 const details=node('details');details.append(node('summary','동일 파일·이전 업로드 보기 ('+f.related.length+(f.related_more?'+':'')+'건)'));
 details.append(node('p','동일 내용은 파일 해시 기준입니다. 같은 채널·파일명의 내용이 다르면 수정본 후보로 표시하며, 최신 업로드가 최종 확정본이라는 뜻은 아닙니다.','privacy'));
 f.related.forEach(r=>{const label=r.identical?'동일 내용':r.same_group?'수정본 후보':'관련 파일';const b=node('button',label+' · '+r.original_filename+' · '+r.discord_channel+' · '+new Date(r.uploaded_at).toLocaleString('ko-KR')+(r.id===f.latest_id?' · 최신 업로드':''),'file');b.onclick=()=>openFile(r.id);details.append(b);});box.append(details);
}

function scopeLabel(){const channel=$('channel').selectedOptions[0]?.textContent||'모든 채널';$('ai-scope').textContent='AI 참고 범위: '+channel+' · '+$('message-days').selectedOptions[0].textContent;}
function excerpt(text,query){const start=Math.max(0,text.toLocaleLowerCase().indexOf(query.toLocaleLowerCase())-60);return (start?'…':'')+text.slice(start,start+180);}
async function loadCombined(append=false){
 const seq=++searching;if(!append){combinedFileOffset=combinedMessageOffset=0;combinedFilesMore=combinedMessagesMore=true;}
 $('list-message').textContent='파일과 대화를 함께 찾고 있어요…';$('more').hidden=true;
 const params={q:$('query').value,channel:$('channel').value,days:$('message-days').value};
 try{
 const [f,m]=await Promise.all([combinedFilesMore?api('files?'+new URLSearchParams({...params,offset:combinedFileOffset})):Promise.resolve(null),combinedMessagesMore?api('messages?'+new URLSearchParams({...params,offset:combinedMessageOffset})):Promise.resolve(null)]);
 if(seq!==searching)return;
 if(!append){$('files').replaceChildren();$('file-scroll').scrollTop=0;const current=$('channel').value;const options=new Map([...(f?.channels||[]),...(m?.channels||[])].map(c=>[c.id,c.name]));$('channel').replaceChildren(new Option('모든 채널',''));options.forEach((name,id)=>$('channel').add(new Option(name,id)));if(current&&!options.has(current))$('channel').add(new Option(current,current));$('channel').value=current;scopeLabel();}
 const entries=[...(f?.files||[]).map(file=>({file,date:file.uploaded_at})),...(m?.messages||[]).map(message=>({message,date:message.created_at}))].sort((a,b)=>Date.parse(b.date)-Date.parse(a.date));
 entries.forEach(({file,message})=>{const b=node('button',undefined,'file');const info=node('span',undefined,'file-info');if(file){info.append(node('span','파일 · '+file.original_filename,'filename'));info.append(node('span',file.discord_channel+' · '+String(file.uploaded_at).slice(0,10),'file-meta'));if(file.excerpt)info.append(node('span',file.excerpt,'search-excerpt'));b.onclick=()=>openFile(file.id);}else{info.append(node('span','대화 · '+message.author_name,'filename'));info.append(node('span',new Date(message.created_at).toLocaleString('ko-KR'),'file-meta'));info.append(node('span',excerpt(message.content,params.q),'search-excerpt'));b.onclick=()=>openMessage(message);}b.append(info);$('files').append(b);});
 if(f){combinedFileOffset+=f.files.length;combinedFilesMore=f.more;}if(m){combinedMessageOffset+=m.messages.length;combinedMessagesMore=m.more;}
 $('count').textContent='파일 '+combinedFileOffset+' · 대화 '+combinedMessageOffset;$('more').hidden=!(combinedFilesMore||combinedMessagesMore);$('list-message').textContent=entries.length||append?'':'검색 결과가 없습니다.';
 }catch(e){if(seq===searching)$('list-message').textContent=e.message;}
}
$('extension').disabled=$('sort').disabled=$('file-view').disabled=$('search-kind').value!=='files';
