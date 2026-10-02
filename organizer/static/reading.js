'use strict';
function readingBlock(parent,b){
 if(b.type==='table'){const wrap=node('div',undefined,'reading-table');wrap.tabIndex=0;wrap.setAttribute('aria-label','표 가로 스크롤');const table=node('table');(b.rows||[]).forEach(row=>{const tr=node('tr');row.forEach(cell=>tr.append(node('td',cell)));table.append(tr);});wrap.append(table);parent.append(wrap);return;}
 const tag=['h1','h2','h3','h4','pre','blockquote'].includes(b.type)?b.type:'p';
 parent.append(node(tag,(b.type==='li'?'• ':'')+(b.text||'')));
}
function readingText(parent,text,markdown){
 const lines=text.split(/\r?\n/);let paragraph=[],code=null;
 const flush=()=>{if(paragraph.length)readingBlock(parent,{type:'p',text:paragraph.join(' ')});paragraph=[];};
 for(let i=0;i<lines.length;i++){const line=lines[i];
  if(markdown&&/^\s*```/.test(line)){flush();if(code!==null){readingBlock(parent,{type:'pre',text:code.join('\n')});code=null;}else code=[];continue;}
  if(code!==null){code.push(line);continue;}
  if(!line.trim()){flush();continue;}
  if(markdown&&line.includes('|')&&i+1<lines.length&&/^\s*\|?\s*:?-{3}/.test(lines[i+1])){flush();const cells=s=>s.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());const rows=[cells(line)];i++;while(i+1<lines.length&&lines[i+1].includes('|'))rows.push(cells(lines[++i]));readingBlock(parent,{type:'table',rows});continue;}
  const heading=markdown&&line.match(/^(#{1,4})\s+(.+)/);const list=line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.+)/);
  if(heading){flush();readingBlock(parent,{type:'h'+heading[1].length,text:heading[2]});}
  else if(list){flush();readingBlock(parent,{type:'li',text:line.trim().replace(/^[-*•]\s*/, '')});}
  else paragraph.push(line.trim());
 }
 flush();if(code!==null)readingBlock(parent,{type:'pre',text:code.join('\n')});
}
function renderReading(f,preview){
 const host=$('pages');host.replaceChildren();
 const toolbar=node('div',undefined,'reading-toolbar');const easy=node('button','편하게 읽기');const original=node('button','원본 보기');const zoom=node('button','글자 크게');zoom.setAttribute('aria-pressed','false');toolbar.append(easy,original,zoom);host.append(toolbar);
 const article=node('article',undefined,'comfortable-reading');article.id='comfortable-reading';
 if(f.reading_blocks?.length?f.reading_partial:f.partial)article.append(node('p','일부 내용만 표시됩니다. 전체 내용과 정확한 배치는 원본에서 확인해 주세요.','notice'));
 if(f.reading_blocks?.length)f.reading_blocks.forEach(b=>readingBlock(article,b));
 else f.pages.forEach(p=>{const section=node('section');if(f.pages.length>1&&!/^추출 구간/.test(p.label))section.append(node('h3',p.label));readingText(section,p.text,/\.md|\.markdown$/i.test(f.original_filename));article.append(section);});
 const hasText=Boolean(f.reading_blocks?.length||f.pages.length);if(!hasText)article.append(node('p','읽기용 텍스트가 없어 원본 보기로 표시합니다.'));
 const originalPanel=node('div');originalPanel.id='original-reading';let loaded=false;
 function show(mode){article.hidden=!mode;originalPanel.hidden=mode;easy.setAttribute('aria-pressed',String(mode));original.setAttribute('aria-pressed',String(!mode));zoom.hidden=!mode;
  if(!mode&&!loaded){loaded=true;if(preview){const frame=node('iframe');frame.className='document-preview';frame.title=f.original_filename+' 원본 미리보기';frame.referrerPolicy='no-referrer';if(f.html_preview)frame.setAttribute('sandbox','');frame.src=preview;originalPanel.append(frame);}else if(f.image_preview){const img=node('img');img.className='image-preview';img.src='/library/files/'+f.id+'/preview';img.alt=f.original_filename;originalPanel.append(img);}else originalPanel.append(node('p','위의 원본 다운로드로 확인해 주세요.'));}
 }
 easy.onclick=()=>show(true);original.onclick=()=>show(false);zoom.onclick=()=>{const big=article.classList.toggle('large-text');zoom.textContent=big?'기본 글자':'글자 크게';zoom.setAttribute('aria-pressed',String(big));};
 host.append(article,originalPanel);show(hasText&&!f.image_preview&&(matchMedia('(max-width:800px)').matches||!preview));
 const sources=node('details');sources.id='extracted-text';sources.append(node('summary','AI 출처 · 추출 텍스트'));f.pages.forEach(p=>{const section=node('section',undefined,'page');section.id='page-'+p.page;section.append(node('h3',p.label),node('p',p.text));sources.append(section);});host.append(sources);
}
