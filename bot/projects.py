"""One shared project boundary for collection, web, Discord and AI.

Existing category values remain the original 11-person project. New team
categories are durable scope tags, so no source DB migration is required.
"""
import re

GUILD_ID = '1554511087800549396'
TEAM_CHANNEL_IDS = {'1557202255843692626': 'robot_1a'}
TEAM_NAMES = {'로봇-1a': 'robot_1a', '로봇-1b': 'robot_1b'}
LABELS = {'main': '기존 프로젝트', '1a': '추가 프로젝트 · 1팀', '1b': '추가 프로젝트 · 2팀'}
TEAM_CATEGORIES = {'1a': 'robot_1a', '1b': 'robot_1b'}


def category_scope(category):
    return next((key for key, value in TEAM_CATEGORIES.items() if value == category), 'main')


def validate(scope):
    if scope not in LABELS:
        raise ValueError('프로젝트·팀을 선택해 주세요.')
    return scope


def expression(column='category'):
    # Column names are internal constants, never user input.
    return f"CASE {column} WHEN 'robot_1a' THEN '1a' WHEN 'robot_1b' THEN '1b' ELSE 'main' END"


def predicate(scope, column='category'):
    return '(' + expression(column) + "='" + validate(scope) + "')"


def select(project='main', team=''):
    if project == 'main' and team in {'', None}:
        return 'main'
    if project == 'additional' and team in TEAM_CATEGORIES:
        return team
    raise ValueError('프로젝트·팀을 선택해 주세요.')


def channel_category(channel):
    """Discover only exact team names in the verified Discord server."""
    if str(getattr(getattr(channel, 'guild', None), 'id', '')) != GUILD_ID:
        return None
    if str(channel.id) in TEAM_CHANNEL_IDS:
        return TEAM_CHANNEL_IDS[str(channel.id)]
    # Discord names in the screenshot have a trailing emoji.
    name = re.sub(r'[^가-힣a-zA-Z0-9_-]+$', '', str(getattr(channel, 'name', '')))
    return TEAM_NAMES.get(name)


def discover(channels):
    grouped = {}
    for channel in channels:
        category = channel_category(channel)
        if category:
            grouped.setdefault(category, []).append(str(channel.id))
    # Ambiguous names are left uncollected rather than guessed.
    return {ids[0]: category for category, ids in grouped.items() if len(ids) == 1}


def invocation_scope(interaction, selected=None):
    if selected is not None:
        return validate(selected)
    channel = getattr(interaction, 'channel', None)
    category = channel_category(channel) if channel else None
    if not category and getattr(channel, 'parent', None):
        category = channel_category(channel.parent)
    if not category and channel and str(getattr(getattr(channel, 'guild', None), 'id', '')) == GUILD_ID:
        if __package__:
            from . import database
        else:
            import database
        from contextlib import closing
        with closing(database.get_connection()) as db:
            saved = stored_channels(db)
            category = saved.get(str(channel.id)) or saved.get(str(getattr(channel, 'parent_id', '')))
    return category_scope(category)


def option_scope(interaction):
    # Discord Namespace uses the displayed (renamed Korean) option name.
    namespace = getattr(interaction, 'namespace', None)
    for name in ('자료범위', 'scope'):
        value = getattr(namespace, name, None)
        if isinstance(value, str) and value in LABELS:
            return value
    return None


def stored_channels(db):
    """Reuse team and photo channel IDs after a rename, without a schema migration."""
    found = dict(db.execute("SELECT DISTINCT discord_channel_id,category FROM files WHERE discord_guild_id=? AND category IN ('robot_1a','robot_1b','photos')", (GUILD_ID,)))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_messages'").fetchone():
        found.update(db.execute("SELECT DISTINCT channel_id,category FROM conversation_messages WHERE guild_id=? AND category IN ('robot_1a','robot_1b','photos')", (GUILD_ID,)))
    return found


def scoped_channels(db, channels, scope):
    """Narrow live permission candidates by persisted scope before any paging."""
    validate(scope)
    found = {str(r[0]) for r in db.execute(
        'SELECT discord_channel_id FROM files WHERE ' + predicate(scope))}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_messages'").fetchone():
        found.update(str(r[0]) for r in db.execute(
            'SELECT channel_id FROM conversation_messages WHERE ' + predicate(scope)))
    return [str(cid) for cid in channels if str(cid) in found]
