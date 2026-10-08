"""Discover the requested photo-sharing channel in the verified project guild."""
import os
import re
from pathlib import Path

if __package__:
    from . import projects
else:
    import projects

CATEGORY = 'photos'
IMAGE_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff',
                  '.heic', '.heif', '.avif'}


def discover(channels):
    configured = os.environ.get('PROJECT_HUB_PHOTO_CHANNEL_ID', '').strip()
    if configured and (not configured.isascii() or not configured.isdecimal() or int(configured) <= 0):
        raise ValueError('PROJECT_HUB_PHOTO_CHANNEL_ID must be a positive Discord channel ID')
    candidates = []
    for channel in channels:
        if str(getattr(getattr(channel, 'guild', None), 'id', '')) != projects.GUILD_ID:
            continue
        if configured:
            matches = str(channel.id) == configured
        else:
            name = re.sub(r'[^가-힣a-zA-Z0-9_-]+$', '', str(getattr(channel, 'name', '')))
            matches = re.sub(r'[\s_-]+', '', name) == '사진공유'
        if matches:
            candidates.append(str(channel.id))
    # An explicit ID resolves duplicate names; automatic matching never guesses.
    return {candidates[0]: CATEGORY} if len(candidates) == 1 else {}


def is_photo(attachment):
    suffix = Path(str(getattr(attachment, 'filename', ''))).suffix.lower()
    mime = str(getattr(attachment, 'content_type', '') or '').lower().split(';')[0]
    return suffix in IMAGE_SUFFIXES and (not mime or mime.startswith('image/') or mime == 'application/octet-stream')
