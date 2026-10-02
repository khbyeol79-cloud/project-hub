"""Use the existing AI configuration and quota without changing process environment."""
import os
from pathlib import Path
import sys

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from organizer.ai import AIError, Organizer, Settings


def configured(config=None):
    path = config or Path.home()/'.config/project-hub/ai.env'
    values = {**dotenv_values(path),**os.environ}
    settings = Settings(project=values.get('GOOGLE_CLOUD_PROJECT',''),
        credentials=Path(values.get('GOOGLE_APPLICATION_CREDENTIALS') or ''),
        state=Path(values.get('PROJECT_HUB_AI_STATE') or str(Path.home()/'.local/state/project-hub-ai')),
        enabled=values.get('PROJECT_HUB_AI_ENABLED')=='true',
        model=values.get('PROJECT_HUB_AI_MODEL') or 'gemini-3.1-flash-lite',
        daily_limit=int(values.get('PROJECT_HUB_AI_DAILY_LIMIT') or '20'),
        monthly_limit=int(values.get('PROJECT_HUB_AI_MONTHLY_LIMIT') or '300'))
    if not settings.enabled:
        raise AIError('AI 기능이 꺼져 있습니다.')
    return Organizer(settings)
