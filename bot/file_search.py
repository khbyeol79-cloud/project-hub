"""Private, permission-aware slash commands for the existing file catalogue."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import logging
import re
import sqlite3
import unicodedata

import discord
from discord import app_commands

if __package__:
    from . import database
    from . import content_search
    from . import message_archive
    from .drive import CATEGORY_FOLDERS
else:
    import database
    import content_search
    import message_archive
    from drive import CATEGORY_FOLDERS

logger = logging.getLogger("project-hub")
PAGE_SIZE = 5
KST = timezone(timedelta(hours=9))
CATEGORY_LABELS = {"common": "공통", "3d_model": "기구제작", "vision": "PC",
                   "plc": "PLC", "meeting": "게시물", "robot": "로봇",
                   "arduino": "아두이노", "final": "최종자료"}
CHOICES = [app_commands.Choice(name=CATEGORY_LABELS.get(key, value), value=key)
           for key, value in CATEGORY_FOLDERS.items()]


def normalize(value):
    return unicodedata.normalize("NFC", value or "").casefold()


def filters(guild_id, keyword, category):
    if not str(guild_id).isascii() or not str(guild_id).isdecimal():
        raise ValueError("Invalid server")
    if len(keyword) > 100 or (category is not None and category not in CATEGORY_FOLDERS):
        raise ValueError("Invalid search")
    clause, values = "discord_guild_id = ?", [str(guild_id)]
    if keyword:
        clause += " AND instr(search_name(original_filename), ?) > 0"
        values.append(normalize(keyword))
    if category:
        clause += " AND category = ?"
        values.append(category)
    return clause, values


def candidate_channels(guild_id, keyword="", category=None):
    clause, values = filters(guild_id, keyword, category)
    with closing(database.get_connection()) as conn:
        conn.create_function("search_name", 1, normalize, deterministic=True)
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT discord_channel_id FROM files WHERE " + clause, values)]


def find_files(guild_id, channels, keyword="", category=None, page=1):
    """Apply visibility before counting/paging; never return private local paths."""
    if not 1 <= page <= 10000:
        raise ValueError("Invalid page")
    clause, values = filters(guild_id, keyword, category)
    with closing(database.get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.create_function("search_name", 1, normalize, deterministic=True)
        conn.execute("CREATE TEMP TABLE visible_channels (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO visible_channels VALUES (?)",
                         [(str(channel),) for channel in channels])
        clause += " AND discord_channel_id IN (SELECT id FROM visible_channels)"
        total = conn.execute("SELECT COUNT(*) FROM files WHERE " + clause, values).fetchone()[0]
        rows = conn.execute("""SELECT id, original_filename, uploaded_at, category,
            discord_guild_id, discord_channel_id, discord_message_id,
            google_drive_file_id, upload_status FROM files WHERE """ + clause +
            " ORDER BY julianday(uploaded_at) DESC, id DESC LIMIT ? OFFSET ?",
            [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        return [dict(row) for row in rows], total


def find_versions(guild_id, channels, keyword, category=None, page=1):
    """Number content changes within a visible channel/category/filename history.

    Consecutive equal hashes share a version; reverting A -> B -> A creates V3.
    Do not use legacy version/duplicate_of fields: those were global across files.
    """
    if not 1 <= page <= 10000 or not keyword.strip():
        raise ValueError("Invalid version search")
    clause, values = filters(guild_id, keyword, category)
    clause += " AND discord_channel_id IN (SELECT id FROM visible_channels)"
    # All permission and server filtering happens before any window function.
    history = """WITH source AS (
        SELECT id, original_filename, uploaded_at, category, discord_guild_id,
               discord_channel_id, discord_message_id, google_drive_file_id,
               upload_status, sha256, search_name(original_filename) AS filename_key,
               julianday(uploaded_at) AS stamp,
               CAST(discord_message_id AS INTEGER) AS message_number
        FROM files WHERE """ + clause + """
    ), compared AS (
        SELECT *, LAG(sha256) OVER (
            PARTITION BY discord_channel_id, category, filename_key
            ORDER BY stamp, message_number, id) AS previous_hash
        FROM source
    ), numbered AS (
        SELECT *, SUM(CASE WHEN sha256 != '' AND sha256 = previous_hash THEN 0 ELSE 1 END)
            OVER (PARTITION BY discord_channel_id, category, filename_key
                  ORDER BY stamp, message_number, id ROWS UNBOUNDED PRECEDING) AS version_number
        FROM compared
    ), copies AS (
        SELECT *,
            MAX(version_number) OVER (
                PARTITION BY discord_channel_id, category, filename_key) AS version_total,
            COUNT(*) OVER (
                PARTITION BY discord_channel_id, category, filename_key, version_number) AS upload_count,
            ROW_NUMBER() OVER (
                PARTITION BY discord_channel_id, category, filename_key, version_number
                ORDER BY stamp DESC, message_number DESC, id DESC) AS copy_rank,
            FIRST_VALUE(CASE WHEN upload_status = 'uploaded' AND google_drive_file_id != ''
                             THEN google_drive_file_id END) OVER (
                PARTITION BY discord_channel_id, category, filename_key, version_number
                ORDER BY (upload_status = 'uploaded' AND COALESCE(google_drive_file_id, '') != '') DESC,
                         stamp DESC, message_number DESC, id DESC) AS completed_drive_id
        FROM numbered
    ), versions AS (
        SELECT id, original_filename, uploaded_at, category, discord_guild_id,
               discord_channel_id, discord_message_id, version_number, version_total, upload_count,
               COALESCE(completed_drive_id, google_drive_file_id) AS google_drive_file_id,
               CASE WHEN completed_drive_id IS NOT NULL THEN 'uploaded' ELSE upload_status END AS upload_status,
               stamp, message_number
        FROM copies WHERE copy_rank = 1
    ) """
    with closing(database.get_connection()) as conn:
        conn.row_factory = sqlite3.Row
        conn.create_function("search_name", 1, normalize, deterministic=True)
        conn.execute("CREATE TEMP TABLE visible_channels (id TEXT PRIMARY KEY)")
        conn.executemany("INSERT OR IGNORE INTO visible_channels VALUES (?)",
                         [(str(channel),) for channel in channels])
        total = conn.execute(history + "SELECT COUNT(*) FROM versions", values).fetchone()[0]
        rows = conn.execute(history + """SELECT * FROM versions
            ORDER BY stamp DESC, message_number DESC, id DESC LIMIT ? OFFSET ?""",
            [*values, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        return [dict(row) for row in rows], total


async def visible_channels(client, member, guild_id, channel_ids):
    if not isinstance(member, discord.Member) or member.guild.id != guild_id:
        return []
    semaphore = asyncio.Semaphore(4)

    async def readable(channel_id):
        async with semaphore:
            try:
                channel = client.get_channel(int(channel_id))
                if channel is None:
                    channel = await client.fetch_channel(int(channel_id))
                if not getattr(channel, "guild", None) or channel.guild.id != guild_id:
                    return None
                permissions = channel.permissions_for(member)
                if not (permissions.view_channel and permissions.read_message_history):
                    return None
                # Thread.permissions_for only checks the parent's overwrites.
                # Private membership needs a separate, current server check.
                if isinstance(channel, discord.Thread) and channel.is_private() and not permissions.manage_threads:
                    await channel.fetch_member(member.id)
                return str(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.ClientException, ValueError, TypeError):
                return None
    return [item for item in await asyncio.gather(*(readable(cid) for cid in channel_ids)) if item]


def display_text(value, limit=180):
    value = " ".join(str(value).split())
    value = value.replace("<", "‹").replace(">", "›")
    value = discord.utils.escape_mentions(discord.utils.escape_markdown(value, ignore_links=False))
    return value[:limit - 1] + "…" if len(value) > limit else value


def result_embed(rows, total, keyword="", category=None, page=1, *, versions=False):
    title = "파일 버전" if versions else ("파일 검색" if keyword else "최근 파일")
    description = "파일명: " + display_text(keyword) + "\n" if keyword else ""
    description += "분류: " + CATEGORY_LABELS.get(category, "전체")
    if versions:
        description += "\n같은 채널·분류·파일명별 이력입니다. 최신 표시는 현재 조회 가능한 수집 이력 기준입니다."
    embed = discord.Embed(title=title, description=description, colour=0x386FAD)
    if not rows:
        embed.description += ("\n이 페이지에는 파일이 없습니다. 앞 페이지를 확인해 주세요."
                              if total else "\n조회할 수 있는 파일이 없습니다.")
    for index, row in enumerate(rows, (page - 1) * PAGE_SIZE + 1):
        try:
            date = datetime.fromisoformat(row["uploaded_at"])
            date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date
            stamp = date.astimezone(KST).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            stamp = "날짜 정보 없음"
        detail = f"{CATEGORY_LABELS.get(row['category'], '기타')} · {stamp}"
        if versions:
            channel_id = str(row['discord_channel_id'])
            if channel_id.isascii() and channel_id.isdecimal():
                detail += f" · <#{channel_id}>"
            if row['upload_count'] > 1:
                detail += f"\n동일 내용 {row['upload_count']}회 업로드 · 날짜는 마지막 업로드 기준"
        links = []
        drive_id = row.get("google_drive_file_id")
        if row.get("upload_status") == "uploaded" and drive_id and re.fullmatch(r"[A-Za-z0-9_-]+", drive_id):
            links.append(f"[Drive 열기](https://drive.google.com/file/d/{drive_id}/view)")
        else:
            status = {"pending": "업로드 대기", "failed": "업로드 재시도 대기",
                      "needs_review": "업로드 확인 필요"}.get(row.get("upload_status"), "Drive 링크 없음")
            detail += f" · {status}"
        ids = [str(row.get(key, "")) for key in
               ("discord_guild_id", "discord_channel_id", "discord_message_id")]
        if all(part.isascii() and part.isdecimal() for part in ids):
            links.append("[원본 메시지](https://discord.com/channels/" + "/".join(ids) + ")")
        if links:
            detail += "\n" + " · ".join(links)
        label = ""
        if versions:
            state = "최신" if row['version_number'] == row['version_total'] else "이전"
            label = f"[{state} V{row['version_number']}/{row['version_total']}] "
        embed.add_field(name=f"{index}. {label}{display_text(row['original_filename'])}", value=detail, inline=False)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    unit = "개 버전" if versions else "개"
    footer = f"{total}{unit} · {page}/{pages}페이지 · 한국 시간 · 본인에게만 표시"
    if page < pages:
        footer += f" · 다음: 페이지 {page + 1}"
    embed.set_footer(text=footer)
    return embed


class SearchTree(app_commands.CommandTree):
    async def on_error(self, interaction, error):
        logger.error("SEARCH_COMMAND_FAILED | error=%s", type(error).__name__)
        message = ("너무 빠르게 요청했습니다. 잠시 후 다시 검색해 주세요."
                   if isinstance(error, app_commands.CommandOnCooldown)
                   else "검색에 실패했습니다. 잠시 후 다시 시도해 주세요.")
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        else:
            await interaction.response.send_message(message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class SearchCommands:
    def __init__(self, client):
        self.client = client
        self.tree = SearchTree(client)
        self.synced = set()
        self.sync_lock = asyncio.Lock()

        @self.tree.command(name="검색", description="저장된 파일을 파일명으로 검색합니다.")
        @app_commands.guild_only()
        @app_commands.rename(keyword="키워드", category="분류", page="페이지")
        @app_commands.describe(keyword="파일명에 들어 있는 글자", category="찾을 자료 분류", page="5개씩 표시할 페이지")
        @app_commands.choices(category=CHOICES)
        @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
        async def search(interaction: discord.Interaction, keyword: app_commands.Range[str, 1, 100],
                         category: str = None, page: app_commands.Range[int, 1, 10000] = 1):
            if not keyword.strip():
                await interaction.response.send_message("검색할 파일명을 입력해 주세요.", ephemeral=True)
                return
            await self.respond(interaction, keyword.strip(), category, page)

        @self.tree.command(name="최근파일", description="최근에 올라온 파일을 최신순으로 봅니다.")
        @app_commands.guild_only()
        @app_commands.rename(category="분류", page="페이지")
        @app_commands.describe(category="찾을 자료 분류", page="5개씩 표시할 페이지")
        @app_commands.choices(category=CHOICES)
        @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
        async def recent(interaction: discord.Interaction, category: str = None,
                         page: app_commands.Range[int, 1, 10000] = 1):
            await self.respond(interaction, "", category, page)

        @self.tree.command(name="버전", description="같은 파일의 최신본과 이전 버전 이력을 봅니다.")
        @app_commands.guild_only()
        @app_commands.rename(keyword="키워드", category="분류", page="페이지")
        @app_commands.describe(keyword="버전을 확인할 파일명의 일부", category="찾을 자료 분류", page="5개씩 표시할 페이지")
        @app_commands.choices(category=CHOICES)
        @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
        async def versions(interaction: discord.Interaction, keyword: app_commands.Range[str, 1, 100],
                           category: str = None, page: app_commands.Range[int, 1, 10000] = 1):
            if not keyword.strip():
                await interaction.response.send_message("버전을 확인할 파일명을 입력해 주세요.", ephemeral=True)
                return
            await self.respond(interaction, keyword.strip(), category, page, versions=True)

        @self.tree.command(name="내용검색", description="PDF·TXT·Word·Excel·사진에서 내용을 찾습니다. 스캔·사진 OCR 지원")
        @app_commands.guild_only()
        @app_commands.rename(keyword="키워드", category="분류", page="페이지")
        @app_commands.describe(keyword="본문에서 찾을 단어나 문구", category="찾을 자료 분류", page="5개씩 표시할 페이지")
        @app_commands.choices(category=CHOICES)
        @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
        async def content(interaction: discord.Interaction, keyword: app_commands.Range[str, 1, 100],
                          category: str = None, page: app_commands.Range[int, 1, 10000] = 1):
            if not content_search.clean_text(keyword):
                await interaction.response.send_message("본문에서 찾을 검색어를 입력해 주세요.", ephemeral=True)
                return
            await self.respond_content(interaction, keyword.strip(), category, page)

        @self.tree.command(name="대화검색", description="저장된 채널·게시물 대화에서 단어나 문구를 찾습니다.")
        @app_commands.guild_only()
        @app_commands.rename(keyword="키워드", category="분류", page="페이지")
        @app_commands.describe(keyword="대화에서 찾을 단어나 문구", category="찾을 자료 분류", page="5개씩 표시할 페이지")
        @app_commands.choices(category=CHOICES)
        @app_commands.checks.cooldown(1, 3.0, key=lambda i: (i.guild_id, i.user.id))
        async def conversations(interaction: discord.Interaction, keyword: app_commands.Range[str, 1, 100],
                                category: str = None, page: app_commands.Range[int, 1, 10000] = 1):
            if not content_search.clean_text(keyword):
                await interaction.response.send_message("대화에서 찾을 검색어를 입력해 주세요.", ephemeral=True)
                return
            await self.respond_conversations(interaction, keyword.strip(), category, page)

        if __package__:
            from .assistant_commands import CommandSuite
        else:
            from assistant_commands import CommandSuite
        self.extra_commands = CommandSuite(self)

    async def respond_conversations(self, interaction, keyword, category, page):
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        candidates = await asyncio.to_thread(message_archive.candidate_channels, interaction.guild_id, category)
        allowed = await asyncio.wait_for(
            visible_channels(self.client, interaction.user, interaction.guild_id, candidates), timeout=25)
        rows, total = await asyncio.to_thread(message_archive.find_messages,
            interaction.guild_id, allowed, keyword, category, page)
        embed = discord.Embed(title="대화 검색", colour=0x386FAD,
            description=f"대화: {display_text(keyword)}\n분류: {CATEGORY_LABELS.get(category, '전체')}")
        if not rows:
            embed.description += "\n이 페이지에 일치하는 대화가 없습니다."
        for number, row in enumerate(rows, (page - 1) * PAGE_SIZE + 1):
            date = datetime.fromisoformat(row['created_at']).astimezone(KST).strftime('%Y-%m-%d %H:%M')
            ids = [str(row[key]) for key in ('guild_id', 'channel_id', 'message_id')]
            detail = f"{CATEGORY_LABELS.get(row['category'], '기타')} · {date}"
            if all(value.isascii() and value.isdecimal() for value in ids):
                detail += f" · <#{ids[1]}>\n[원본 대화](https://discord.com/channels/" + '/'.join(ids) + ')'
                reply = str(row.get('reply_message_id') or '')
                if reply.isascii() and reply.isdecimal():
                    detail += '\n[답글 대상](https://discord.com/channels/' + '/'.join(ids[:2] + [reply]) + ')'
            if row['stored_files']:
                detail += f" · 함께 저장된 파일 {row['stored_files']}개"
            if row['edited_at']:
                detail += " · 수정됨"
            detail += '\n> ' + display_text(row['excerpt'], 360)
            embed.add_field(name=f"{number}. {display_text(row['author_name'], 160)}", value=detail, inline=False)
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        footer = f"{total}개 대화 · {page}/{pages}페이지 · 한국 시간 · 본인에게만 표시"
        if page < pages:
            footer += f" · 다음: 페이지 {page + 1}"
        embed.set_footer(text=footer)
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def respond_content(self, interaction, keyword, category, page):
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        candidates = await asyncio.to_thread(candidate_channels, interaction.guild_id, "", category)
        allowed = await asyncio.wait_for(
            visible_channels(self.client, interaction.user, interaction.guild_id, candidates), timeout=25)
        rows, total, states = await asyncio.to_thread(
            content_search.find_content, interaction.guild_id, allowed, keyword, category, page)
        embed = result_embed(rows, total, "", category, page)
        embed.title = "파일 내용 검색"
        embed.description = f"본문: {display_text(keyword)}\n분류: {CATEGORY_LABELS.get(category, '전체')} · PDF/TXT/DOCX/XLSX/사진 OCR"
        if not rows:
            embed.description += "\n이 페이지에 일치하는 본문이 없습니다."
        for index, row in enumerate(rows):
            field = embed.fields[index]
            suffix = row['original_filename'].lower().rsplit('.', 1)[-1]
            fallback = f"PDF {row['page']}쪽" if suffix == 'pdf' else ('TXT 본문' if suffix == 'txt' else '문서 본문')
            location = display_text(row.get('location') or fallback, 240)
            if row['matching_pages'] > 1:
                unit = '페이지' if suffix == 'pdf' else '위치'
                location += f" · {row['matching_pages']}개 {unit}에서 일치"
            if row['content_status'] == 'partial':
                location += " · 일부 내용만 추출됨"
            elif row['content_status'] == 'ocr_pending':
                location += " · OCR 일부 재시도 대기"
            embed.set_field_at(index, name=field.name,
                value=field.value + f"\n{location}\n> {display_text(row['excerpt'], 360)}", inline=False)
        pending = states.get('pending', 0) + states.get('ocr_pending', 0)
        excluded = sum(value for key, value in states.items() if key not in ('indexed', 'partial', 'pending', 'ocr_pending'))
        partial = states.get('partial', 0)
        footer = embed.footer.text + f" · 준비 중 {pending} · 추출 제외 {excluded} · 일부 추출 {partial}"
        if any('OCR' in row.get('location', '') for row in rows):
            footer += " · OCR 인식 내용은 원본으로 확인하세요"
        embed.set_footer(text=footer)
        await interaction.edit_original_response(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    async def respond(self, interaction, keyword, category, page, *, versions=False):
        if interaction.guild_id is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("서버 안에서 사용해 주세요.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        candidates = await asyncio.to_thread(candidate_channels, interaction.guild_id, keyword, category)
        allowed = await asyncio.wait_for(
            visible_channels(self.client, interaction.user, interaction.guild_id, candidates), timeout=25)
        query = find_versions if versions else find_files
        rows, total = await asyncio.to_thread(query, interaction.guild_id, allowed, keyword, category, page)
        await interaction.edit_original_response(embed=result_embed(rows, total, keyword, category, page, versions=versions),
                                                 allowed_mentions=discord.AllowedMentions.none())

    async def sync(self, configured_channels):
        """Upsert our commands individually; preserve other registered commands."""
        async with self.sync_lock:
            guilds = set()
            for channel_id in configured_channels:
                try:
                    channel = self.client.get_channel(int(channel_id))
                    if channel is None:
                        channel = await self.client.fetch_channel(int(channel_id))
                    guilds.add(channel.guild.id)
                except (discord.HTTPException, discord.ClientException, AttributeError):
                    logger.error("SEARCH_GUILD_RESOLVE_FAILED | channel=%s", channel_id)
            for guild_id in guilds - self.synced:
                try:
                    # discord.py 2.7.1 has no public single-command creation API.
                    # Its HTTP client handles Discord rate limits for this upsert.
                    for command in self.tree.get_commands():
                        await self.client.http.upsert_guild_command(
                            self.client.application_id, guild_id, payload=command.to_dict(self.tree))
                    self.synced.add(guild_id)
                    logger.info("SEARCH_COMMANDS_READY | guild=%s | commands=%d", guild_id, len(self.tree.get_commands()))
                except discord.HTTPException as error:
                    logger.error("SEARCH_SYNC_FAILED | guild=%s | error=%s", guild_id, type(error).__name__)
