'use strict';
let educationSchedule=null, scheduleLoading=false, scheduleSelectedDate=null, scheduleScope=null, scheduleCheckedToday=null;
const scheduleWeekdays=['일','월','화','수','목','금','토'];
function scheduleDate(key){return new Date(key+'T00:00:00Z');}
function scheduleKey(date){return date.toISOString().slice(0,10);}
function scheduleAdd(key,n){const d=scheduleDate(key);d.setUTCDate(d.getUTCDate()+n);return scheduleKey(d);}
function scheduleLabel(key){const d=scheduleDate(key);return (d.getUTCMonth()+1)+'.'+d.getUTCDate();}
function scheduleKstToday(){
 const parts=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date());
 const value=type=>parts.find(p=>p.type===type).value;
 return value('year')+'-'+value('month')+'-'+value('day');
}
function scheduleHours(day){return (day?.lessons||[]).reduce((n,b)=>n+b.end_period-b.start_period+1,0);}
function scheduleRoomLabel(room){
 const number=room.match(/^[（(](\d+)[）)]/);
 return number?number[1]+'호':room;
}
function scheduleNoClass(key,day){
 if(key<educationSchedule.start)return '훈련 시작 전';
 if(key>educationSchedule.end)return '훈련 종료';
 return day?.note||([0,6].includes(scheduleDate(key).getUTCDay())?'주말':'수업 없음');
}
function scheduleLessons(key,compact=false){
 const day=educationSchedule.days[key],wrap=node('div',undefined,'schedule-lessons');
 if(!day?.lessons.length){wrap.append(node('span',scheduleNoClass(key,day),'schedule-off'));return wrap;}
 // Keep lunch as separate time ranges, while showing each consecutive subject once.
 const groups=[];
 day.lessons.forEach(block=>{
  const last=groups[groups.length-1];
  if(last&&last.subject===block.subject&&last.room===block.room&&last.end_period===block.start_period-1){last.blocks.push(block);last.end_period=block.end_period;}
  else groups.push({...block,blocks:[block]});
 });
 groups.forEach(group=>{
  const entry=node('div',undefined,'schedule-lesson');
  entry.append(node('strong',group.subject));
  if(compact)entry.append(node('span',(group.start_period===group.end_period?group.start_period:group.start_period+'–'+group.end_period)+'교시','schedule-time'));
  else group.blocks.forEach(b=>entry.append(node('span',b.start+'–'+b.end+' · '+(b.start_period===b.end_period?b.start_period:b.start_period+'–'+b.end_period)+'교시','schedule-time')));
  if(!compact&&group.room)entry.append(node('span',group.room,'schedule-room'));
  wrap.append(entry);
 });
 return wrap;
}
function renderScheduleWeeks(){
 const d=educationSchedule;
 $('schedule-range').textContent=scheduleLabel(d.week_start)+' – '+scheduleLabel(d.week_end)+' · 매주 일요일 자동 갱신 · 한국 시간';
 $('schedule-weeks').replaceChildren();
 const weekdays=node('div',undefined,'schedule-weekday-row');
 scheduleWeekdays.forEach(label=>weekdays.append(node('span',label)));
 $('schedule-weeks').append(weekdays);
 for(let week=0;week<4;week++){
  const start=scheduleAdd(d.week_start,week*7),end=scheduleAdd(start,6),card=node('section',undefined,'schedule-week');
  card.append(node('h3',(week===0?'이번 주':week===1?'다음 주':(week+1)+'주차')+' · '+scheduleLabel(start)+'–'+scheduleLabel(end)));
  for(let i=0;i<7;i++){
   const key=scheduleAdd(start,i),day=d.days[key],row=node('div',undefined,'schedule-day'+([0,6].includes(i)?' is-weekend':'')+(key===d.today?' is-today':''));
   const heading=node('div',undefined,'schedule-day-heading');
   const button=node('button',scheduleLabel(key),'schedule-day-select');
   button.type='button';button.dataset.scheduleDate=key;button.setAttribute('aria-pressed','false');
   button.setAttribute('aria-label',key+' '+scheduleWeekdays[i]+' 시간표 보기');
   button.onclick=()=>selectScheduleDate(key,true);
   heading.append(button);
   const rooms=[...new Set((day?.lessons||[]).map(b=>b.room).filter(Boolean))];
   if(rooms.length){
    const room=node('span',rooms.map(scheduleRoomLabel).join(' · '),'schedule-day-room');
    room.title=rooms.join(' · ');room.setAttribute('aria-label','교실: '+rooms.join(' · '));heading.append(room);
   }
   if(key===d.today)heading.append(node('span','오늘','schedule-today-label'));
   else if(day?.note&&day.lessons.length)heading.append(node('span',day.note,'schedule-note'));
   row.append(heading,scheduleLessons(key,true));card.append(row);
  }
  $('schedule-weeks').append(card);
 }
}
function selectScheduleDate(key,scroll=false){
 if(scroll)scheduleSelectedDate=key;
 document.querySelectorAll('[data-schedule-date]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.scheduleDate===key)));
 const d=scheduleDate(key),day=educationSchedule.days[key],title=key.replaceAll('-','.')+' ('+scheduleWeekdays[d.getUTCDay()]+')';
 $('schedule-detail-title').textContent=title+(day?.note?' · '+day.note:'');
 $('schedule-detail-body').replaceChildren(scheduleLessons(key));
 if(scroll&&matchMedia('(max-width:1200px)').matches)$('schedule-detail').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function renderScheduleCalendar(){
 const d=educationSchedule,start=scheduleDate(d.start),end=scheduleDate(d.end);
 $('schedule-months').replaceChildren($('schedule-detail'));
 for(let year=start.getUTCFullYear(),month=start.getUTCMonth();year<end.getUTCFullYear()||(year===end.getUTCFullYear()&&month<=end.getUTCMonth());month++){
  if(month===12){year++;month=0;}
  const card=node('section',undefined,'schedule-month'),first=new Date(Date.UTC(year,month,1)),last=new Date(Date.UTC(year,month+1,0));
  const prefix=scheduleKey(first).slice(0,7),days=Object.entries(d.days).filter(([key,day])=>key.startsWith(prefix)&&day.lessons.length);
  card.append(node('h4',(month+1)+'월'),node('p',days.length+'일 · '+days.reduce((n,[,day])=>n+scheduleHours(day),0)+'교시','schedule-month-total'));
  const grid=node('div',undefined,'schedule-calendar-grid');
  scheduleWeekdays.forEach(label=>grid.append(node('span',label,'schedule-weekday')));
  for(let i=0;i<first.getUTCDay();i++)grid.append(node('span'));
  for(let day=1;day<=last.getUTCDate();day++){
   const key=prefix+'-'+String(day).padStart(2,'0'),item=d.days[key],outside=key<d.start||key>d.end;
   const button=node('button',String(day),'schedule-date'+(outside?' is-outside':item?.lessons.length?' is-class':item?.note?' is-holiday':' is-rest')+(key===d.today?' is-today':''));
   button.type='button';button.disabled=outside;button.dataset.scheduleDate=key;
   const label=key+' · '+(outside?'훈련 기간 밖':item?.lessons.length?scheduleHours(item)+'교시'+(item.note?' · '+item.note:''):scheduleNoClass(key,item));
   button.title=label;button.setAttribute('aria-label',label);button.setAttribute('aria-pressed','false');
   if(!outside)button.onclick=()=>selectScheduleDate(key,true);
   grid.append(button);
  }
  card.append(grid);$('schedule-months').append(card);
 }
 const today=d.today<d.start?d.start:d.today>d.end?d.end:d.today;
 selectScheduleDate(scheduleSelectedDate||today);
}
function clearSchedule(){
 educationSchedule=null;scheduleSelectedDate=null;
 for(const id of ['schedule-course','schedule-meta','schedule-range','schedule-source','schedule-detail-title'])$(id).textContent='';
 for(const id of ['schedule-weeks','schedule-detail-body'])$(id).replaceChildren();
 $('schedule-months').replaceChildren($('schedule-detail'));
 $('schedule-layout').hidden=true;
 $('schedule-detail').hidden=true;
}
async function loadSchedule(force=false){
 const scope=JSON.stringify(scopeParams()),version=scopeVersion;
 if(!force&&scheduleScope===scope&&scheduleCheckedToday===scheduleKstToday())return;
 if(scheduleScope!==scope){clearSchedule();scheduleScope=scope;scheduleCheckedToday=null;}
 if(scheduleLoading)return;
 scheduleLoading=true;$('schedule-refresh').disabled=true;
 $('schedule-status').textContent=educationSchedule?'':'교육 일정을 불러오고 있어요…';
 try{
  const d=await api('schedule');
  if(version!==scopeVersion)return;
  scheduleCheckedToday=d.available===false?scheduleKstToday():d.today;
  if(d.available===false){clearSchedule();$('schedule-status').textContent=d.notice;return;}
  educationSchedule=d;
  $('schedule-course').textContent=d.course;
  $('schedule-meta').textContent=d.start.replaceAll('-','.')+' – '+d.end.replaceAll('-','.')+' · 총 '+d.teaching_days+'일 / '+d.teaching_periods+'교시';
  $('schedule-source').textContent='기준: '+d.source+' · 주말은 수업 없음 · 날짜를 누르면 시간표를 볼 수 있어요.';
  $('schedule-layout').hidden=false;$('schedule-detail').hidden=false;renderScheduleWeeks();renderScheduleCalendar();
  $('schedule-status').textContent=d.today<d.start?'훈련 시작 전입니다. 아래에서 전체 일정을 확인하세요.':d.today>d.end?'훈련이 종료되었습니다. 아래에서 전체 일정을 확인하세요.':'';
 }catch(e){if(version===scopeVersion)$('schedule-status').textContent='일정 불러오기 실패: '+e.message+(educationSchedule?' 기존 일정을 표시하고 있어요.':'');}
 finally{
  scheduleLoading=false;$('schedule-refresh').disabled=false;
  if(version!==scopeVersion&&!$('welcome').hidden)loadSchedule();
 }
}
function openSchedule(){
 ++generation;selected=null;closePhotos();setSidebar(false);
 $('document').hidden=$('ai-panel').hidden=$('weekly-panel').hidden=true;$('welcome').hidden=false;
 document.querySelector('.workspace').classList.remove('reading');document.querySelector('.reader').classList.add('open');
 document.querySelectorAll('.file.selected').forEach(e=>e.classList.remove('selected'));
 history.replaceState(null,'',scopedUrl(location.pathname));loadSchedule();
}
$('schedule-open').onclick=openSchedule;$('schedule-refresh').onclick=()=>loadSchedule(true);
function scheduleViewChanged(){
 const visible=!$('welcome').hidden;
 document.body.classList.toggle('schedule-view',visible);$('schedule-open').setAttribute('aria-pressed',String(visible));
 if(visible)loadSchedule();
}
new MutationObserver(scheduleViewChanged).observe($('welcome'),{attributes:true,attributeFilter:['hidden']});
// Poll the date, not the timetable, while open. Re-fetch only after a Korean date changes.
setInterval(()=>{if(!document.hidden&&!$('welcome').hidden)loadSchedule();},60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!$('welcome').hidden)loadSchedule();});
window.addEventListener('focus',()=>{if(!$('welcome').hidden)loadSchedule();});
