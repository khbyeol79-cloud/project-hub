"""Help, unified search, scoped status, file details and grounded AI commands."""
import asyncio
from datetime import datetime, timezone
import logging
import re

import discord
from discord import app_commands

if __package__:
    from . import command_data as data, file_search as search
else:
    import command_data as data, file_search as search

logger = logging.getLogger('project-hub')
PERIODS = [app_commands.Choice(name='오늘',value='today'), app_commands.Choice(name='어제',value='yesterday'),
           app_commands.Choice(name='최근 7일',value='week')]


def message_url(guild,channel,message):
    parts = [str(value) for value in (guild,channel,message)]
    return 'https://discord.com/channels/'+'/'.join(parts) if all(p.isascii() and p.isdecimal() for p in parts) else None


def file_number(value):
    if value is None:
        return None
    if not re.fullmatch(r'[1-9][0-9]{0,17}',value):
        raise ValueError('파일명 입력 후 목록에서 파일을 선택해 주세요.')
    return int(value)


def kst(value):
    if not value:
        return '기록 없음'
    try:
        date = datetime.fromisoformat(value)
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
        return date.astimezone(search.KST).strftime('%Y-%m-%d %H:%M')
    except ValueError:
        return '날짜 확인 필요'


class CommandSuite:
    def __init__(self,owner):
        self.owner,self.client = owner,owner.client
        self.ai_lock = asyncio.Lock()
        tree = owner.tree

        @tree.command(name='도움말',description='모든 명령의 기능과 사용 예시를 확인합니다.')
        @app_commands.guild_only()
        async def help_command(interaction:discord.Interaction):
            if await self.begin(interaction):
                await self.help(interaction)

        @tree.command(name='찾기',description='파일명·문서 본문·대화를 한 번에 검색합니다.')
        @app_commands.guild_only()
        @app_commands.rename(keyword='키워드',category='분류',page='페이지',scope='자료범위')
        @app_commands.describe(keyword='파일이나 대화에서 찾을 단어',category='찾을 자료 분류',page='5개씩 표시할 페이지')
        @app_commands.choices(category=search.CHOICES,scope=search.SCOPE_CHOICES)
        @app_commands.checks.cooldown(1,3,key=lambda i:(i.guild_id,i.user.id))
        async def find(interaction:discord.Interaction,keyword:app_commands.Range[str,1,100],
                       category:str=None,page:app_commands.Range[int,1,10000]=1,scope:str=None):
            if await self.begin(interaction):
                if not data.clean(keyword):
                    await self.tell(interaction,'검색할 단어나 문구를 입력해 주세요.')
                    return
                channels = await self.allowed(interaction,category,scope)
                rows,counts = await asyncio.to_thread(data.unified,interaction.guild_id,channels,keyword,category,page,scope=search.projects.invocation_scope(interaction,scope))
                embed = discord.Embed(title='통합 검색',colour=0x386FAD,
                    description=f"검색어: {search.display_text(keyword)}\n분류: {search.CATEGORY_LABELS.get(category,'전체')}")
                for n,row in enumerate(rows,(page-1)*5+1):
                    if row['kind']=='file':
                        field = search.result_embed([row],1).fields[0]
                        match = '파일명·본문' if row['name_hit'] and row['page'] is not None else ('파일명' if row['name_hit'] else '본문')
                        value = field.value
                        if row.get('excerpt'):
                            value += '\n'+search.display_text(row['location'],100)+'\n> '+search.display_text(row['excerpt'],230)
                        name = f"{n}. [{match}] {search.display_text(row['original_filename'],145)}"
                    else:
                        name = f"{n}. [대화] {search.display_text(row['author_name'],145)}"
                        value = f"{search.CATEGORY_LABELS.get(row['category'],'기타')} · {kst(row['created_at'])}"
                        url = message_url(row['guild_id'],row['channel_id'],row['message_id'])
                        if url:
                            value += f'\n[원본 대화]({url})'
                        value += '\n> '+search.display_text(row['excerpt'],230)
                    embed.add_field(name=name,value=value,inline=False)
                total = sum(counts.values())
                if not rows:
                    embed.description += '\n이 페이지에 일치하는 자료가 없습니다.'
                embed.set_footer(text=f"파일 {counts.get('file',0)}개 · 대화 {counts.get('message',0)}개 · {page}/{max(1,(total+4)//5)}페이지 · 본인에게만 표시")
                await self.send(interaction,embed)

        @tree.command(name='상태',description='열람 가능한 자료의 저장·업로드·본문 준비 상태를 확인합니다.')
        @app_commands.guild_only()
        @app_commands.rename(scope='자료범위')
        @app_commands.choices(scope=search.SCOPE_CHOICES)
        @app_commands.checks.cooldown(1,5,key=lambda i:(i.guild_id,i.user.id))
        async def status(interaction:discord.Interaction,scope:str=None):
            if await self.begin(interaction):
                channels = await self.allowed(interaction,scope=scope)
                snapshot = await asyncio.to_thread(data.status,interaction.guild_id,channels,scope=search.projects.invocation_scope(interaction,scope))
                upload,docs = snapshot['uploads'],snapshot['documents']
                embed = discord.Embed(title='Project Hub 상태',description='봇 응답 정상 · 열람 가능한 자료 기준',colour=0x386FAD)
                embed.add_field(name='수집 자료',value=f"파일 {sum(upload.values())}개 · 대화 {snapshot['conversations']}개",inline=False)
                embed.add_field(name='Drive 업로드',value=f"완료 {upload.get('uploaded',0)} · 대기 {upload.get('pending',0)} · 재시도 {upload.get('failed',0)} · 확인 필요 {upload.get('needs_review',0)}",inline=False)
                excluded = sum(n for key,n in docs.items() if key not in ('indexed','partial','pending','ocr_pending'))
                embed.add_field(name='본문·OCR 검색 준비',value=f"완료 {docs.get('indexed',0)} · 일부 추출 {docs.get('partial',0)} · 준비/재시도 {docs.get('pending',0)+docs.get('ocr_pending',0)} · 추출 제외 {excluded}",inline=False)
                checked = max((c['last_checked_at'] or '' for c in snapshot['checks']),default='')
                failures = sum(c['failures']>0 for c in snapshot['checks'])
                embed.add_field(name='채널 수집 점검',value=f"마지막 확인: {kst(checked)}\n오류가 기록된 점검 {failures}개",inline=False)
                embed.set_footer(text='한국 시간 · 본인에게만 표시')
                await self.send(interaction,embed)

        @tree.command(name='파일정보',description='선택한 파일의 작성자·버전·중복·저장 상태를 확인합니다.')
        @app_commands.guild_only()
        @app_commands.rename(file='파일',scope='자료범위')
        @app_commands.choices(scope=search.SCOPE_CHOICES)
        @app_commands.describe(file='이름을 입력한 뒤 목록에서 파일 선택')
        @app_commands.checks.cooldown(1,3,key=lambda i:(i.guild_id,i.user.id))
        async def info(interaction:discord.Interaction,file:str,scope:str=None):
            if await self.begin(interaction):
                try:
                    number = file_number(file)
                except ValueError as error:
                    await self.tell(interaction,str(error))
                    return
                channels = await self.allowed(interaction,scope=scope)
                row = await asyncio.to_thread(data.detail,interaction.guild_id,channels,number,scope=search.projects.invocation_scope(interaction,scope))
                if row is None:
                    await self.tell(interaction,'이 파일을 확인할 수 없습니다. 파일을 다시 선택해 주세요.')
                    return
                embed = search.result_embed([row],1)
                embed.title = '파일 정보'
                embed.add_field(name='작성자·크기',value=f"{search.display_text(row['discord_author'],160)} · {row['file_size_bytes']:,}바이트",inline=False)
                state = '최신' if row['version']==row['version_total'] else '이전'
                embed.add_field(name='버전·동일 내용',value=f"{state} V{row['version']}/{row['version_total']} · 같은 내용 {row['same_content']}개 기록\n동일 내용 개수는 열람 가능한 파일 기준",inline=False)
                labels = {'indexed':'준비 완료','partial':'일부 내용 준비','pending':'준비 중','ocr_pending':'OCR 재시도 대기',
                          'unsupported':'지원하지 않는 형식','no_text':'읽을 수 있는 글자 없음','failed':'추출 실패·재시도 예정',
                          'encrypted':'암호화된 문서','too_large':'처리 크기 한도 초과'}
                embed.add_field(name='본문 검색',value=f"{labels.get(row['content_status'],'추출 제외')} · {row['parts']}개 내용 위치",inline=False)
                await self.send(interaction,embed)

        @tree.command(name='요약',description='선택 문서 또는 기간별 대화를 Google AI로 요약합니다.')
        @app_commands.guild_only()
        @app_commands.rename(file='파일',period='대화기간',category='분류',scope='자료범위')
        @app_commands.describe(file='문서 요약: 이름을 입력한 뒤 파일 선택',period='대화 요약 기간, 파일 미선택 시 기본 오늘',category='대화 요약에 사용할 분류')
        @app_commands.choices(period=PERIODS,category=search.CHOICES,scope=search.SCOPE_CHOICES)
        @app_commands.checks.cooldown(1,30,key=lambda i:(i.guild_id,i.user.id))
        async def summary(interaction:discord.Interaction,file:str=None,period:str=None,category:str=None,scope:str=None):
            if await self.begin(interaction):
                if file and (period or category):
                    await self.tell(interaction,'문서 요약은 파일만 선택하고, 대화 요약은 기간·분류를 선택해 주세요.')
                    return
                await self.ai(interaction,'summary',file=file,period=period or ('today' if not file else None),category=category,scope=scope)

        @tree.command(name='질문',description='열람 가능한 파일·대화를 근거로 Google AI가 답합니다.')
        @app_commands.guild_only()
        @app_commands.rename(question='질문',file='파일',category='분류',scope='자료범위')
        @app_commands.describe(question='저장된 자료에 관해 물어볼 내용',file='선택하면 해당 문서만 사용',category='찾을 자료 분류')
        @app_commands.choices(category=search.CHOICES,scope=search.SCOPE_CHOICES)
        @app_commands.checks.cooldown(1,30,key=lambda i:(i.guild_id,i.user.id))
        async def ask(interaction:discord.Interaction,question:app_commands.Range[str,1,1000],file:str=None,category:str=None,scope:str=None):
            if await self.begin(interaction):
                if not question.strip():
                    await self.tell(interaction,'질문을 입력해 주세요.')
                    return
                await self.ai(interaction,'ask',file=file,question=question,category=category,scope=scope)

        for command in (info,summary,ask):
            command.autocomplete('file')(self.autocomplete)

    async def begin(self,interaction):
        if interaction.guild_id is None or not isinstance(interaction.user,discord.Member):
            await interaction.response.send_message('서버 안에서 사용해 주세요.',ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True,thinking=True)
        return True

    async def send(self,interaction,embed):
        selected = search.projects.option_scope(interaction)
        embed.description = search.projects.LABELS[search.projects.invocation_scope(interaction, selected)] + '\n' + (embed.description or '')
        await interaction.edit_original_response(embed=embed,allowed_mentions=discord.AllowedMentions.none())

    async def tell(self,interaction,text):
        await interaction.edit_original_response(content=text,allowed_mentions=discord.AllowedMentions.none())

    async def allowed(self,interaction,category=None,scope=None):
        candidates = await asyncio.to_thread(data.candidates,interaction.guild_id,category)
        candidates = await asyncio.to_thread(self.owner.narrow,interaction,candidates,scope)
        return await asyncio.wait_for(search.visible_channels(self.client,interaction.user,interaction.guild_id,candidates),25)

    async def autocomplete(self,interaction,current):
        if interaction.guild_id is None or not isinstance(interaction.user,discord.Member):
            return []
        async def choices():
            selected = search.projects.option_scope(interaction)
            channels = await self.allowed(interaction,scope=selected)
            rows = await asyncio.to_thread(data.suggestions,interaction.guild_id,channels,current,scope=search.projects.invocation_scope(interaction,selected))
            return [app_commands.Choice(name=f"{row['original_filename'][:70]} · {kst(row['uploaded_at'])} · #{row['id']}"[:100],
                                       value=str(row['id'])) for row in rows]
        try:
            return await asyncio.wait_for(choices(),2.3)
        except (TimeoutError,discord.HTTPException):
            return []

    async def help(self,interaction):
        embed = discord.Embed(title='Project Hub 사용 안내',description='키워드는 원하는 단어로 바꾸세요. 예시의 ‘모터’만 검색되는 것이 아닙니다.',colour=0x386FAD)
        examples = [('찾기','/찾기 키워드:모터 — 파일명·본문·대화 통합 검색'),
            ('검색 · 최근파일 · 버전','/검색 키워드:도면\n/최근파일\n/버전 키워드:도면'),
            ('내용검색 · 대화검색','/내용검색 키워드:서보\n/대화검색 키워드:회의'),
            ('상태 · 파일정보','/상태 — 수집·업로드·본문 준비 확인\n/파일정보 파일:이름을 입력하고 목록에서 선택'),
            ('요약','/요약 파일:선택한 문서\n/요약 대화기간:오늘 분류:PLC'),
            ('질문','/질문 질문:서보 설정값이 뭐였지?\n파일을 선택하면 해당 문서만 사용'),
            ('사용 방법','자료범위를 생략하면 현재 채널의 프로젝트·팀만 조회합니다. 자료범위로 다른 팀을 선택할 수 있으며 열람 권한은 그대로 적용됩니다.\n검색에는 분류·페이지를 추가할 수 있습니다. 결과는 본인에게만 보입니다.\n요약·질문은 조회 가능한 자료를 Google AI로 처리하며 웹 자료실과 같은 사용 한도를 적용합니다.')]
        for name,value in examples:
            embed.add_field(name=name,value=value,inline=False)
        embed.set_footer(text='도움말 포함 11개 명령 · 파일 선택 목록에도 열람 권한 적용')
        await self.send(interaction,embed)

    async def ai(self,interaction,task,*,file=None,period=None,category=None,question='',scope=None):
        if self.ai_lock.locked():
            await self.tell(interaction,'다른 AI 요청을 처리하고 있습니다. 잠시 후 다시 요청해 주세요.')
            return
        try:
            number = file_number(file)
        except ValueError as error:
            await self.tell(interaction,str(error))
            return
        async with self.ai_lock:
            channels = await self.allowed(interaction,category,scope)
            items,partial = await asyncio.to_thread(data.sources,interaction.guild_id,channels,
                file_id=number,question=question,period=period,category=category,scope=search.projects.invocation_scope(interaction,scope))
            if not items:
                await self.tell(interaction,'열람 가능한 자료에서 읽을 내용을 찾지 못했습니다. 파일 선택이나 질문의 단어를 확인해 주세요. AI는 호출하지 않았습니다.')
                return
            if __package__:
                from . import discord_ai
            else:
                import discord_ai
            try:
                engine = await asyncio.to_thread(discord_ai.configured)
                result = await asyncio.to_thread(engine.run,task,items,question,partial)
            except discord_ai.AIError as error:
                await self.tell(interaction,str(error))
                return
            except (ValueError,OSError):
                await self.tell(interaction,'AI 설정을 읽지 못했습니다. 관리자에게 확인해 주세요.')
                return
            # Recheck live permissions and source revisions after the remote request too.
            allowed = await asyncio.wait_for(search.visible_channels(self.client,interaction.user,interaction.guild_id,
                                               {item['channel_id'] for item in items}),25)
            allowed = await asyncio.to_thread(self.owner.narrow,interaction,allowed,scope)
            if not await asyncio.to_thread(data.sources_current,interaction.guild_id,allowed,items,scope=search.projects.invocation_scope(interaction,scope)):
                await self.tell(interaction,'자료 내용이나 열람 권한이 바뀌어 결과를 표시하지 않았습니다. 다시 요청해 주세요.')
                return
            refs = {f'S{i}':item for i,item in enumerate(items,1)}
            citations = [r['id'] for r in result.get('sources',[]) if r['id'] in refs]
            if not citations and '자료에서 확인되지 않습니다' not in result['answer']:
                await self.tell(interaction,'답변의 근거 출처가 없어 결과를 표시하지 않았습니다. 질문이나 자료를 구체적으로 선택해 주세요.')
                return
            answer = '\n'.join(search.display_text(line,800) for line in result['answer'].splitlines())[:3000]
            embed = discord.Embed(title='자료 요약' if task=='summary' else '자료에 근거한 답변',description=answer,colour=0x386FAD)
            links = []
            for ref in citations:
                item = refs[ref]
                url = message_url(item['guild_id'],item['channel_id'],item['message_id'])
                label = search.display_text(item['label'],75)
                links.append(f'[{ref}] {label}'+(f' · [원본]({url})' if url else ''))
            # Up to 12 references, in three fields below Discord's 1024-character field limit.
            for start in range(0,len(links),4):
                embed.add_field(name='근거 자료' if start==0 else '근거 자료 (계속)',value='\n'.join(links[start:start+4]),inline=False)
            scope = '선택된 자료 일부 기준' if partial else '선택 자료 기준'
            if period:
                scope += ' · 해당 기간의 최근 대화 최대 12개'
            reuse = ' · 기존 결과 재사용' if result.get('cached') else ''
            embed.set_footer(text=f'{scope} · 최대 12개 구간/12,000글자 · 본인에게만 표시{reuse}')
            await self.send(interaction,embed)
