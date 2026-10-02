Project Hub AI 정리 프로그램 (Python 3.11)

수집 봇과 별도로 실행하는 관리자용 프로그램입니다. 기존 검색 인덱스의 텍스트를
읽기 전용으로 읽어 요약 또는 선택한 문서에 대한 질문을 처리합니다.
Discord 명령이나 웹 공개 API는 아직 연결하지 않았습니다. 로그인/채널별 권한을
검사하는 공개 진입점으로 이 CLI를 그대로 노출하지 마세요.

서버 설정
1. ai.env.example을 ~/.config/project-hub/ai.env로 복사하고 프로젝트/키 경로를 설정합니다.
2. 설정과 키는 600, 상위 디렉터리는 700 권한으로 둡니다.
3. PROJECT_HUB_AI_ENABLED=true로 설정하면 직접 실행한 요청만 처리합니다.
   예약 작업, 자동 전체 업로드, 봇 재시작은 필요하지 않습니다.
   기존 requirements.txt의 google-auth, requests, python-dotenv를 사용합니다.

프로젝트 폴더에서 실행 (Pi의 기존 가상환경 Python 사용)
  python -m organizer status
  python -m organizer smoke
  python -m organizer summary --file-id 123
  python -m organizer ask --file-id 123 --question "점검 일정과 담당자는?"
  python -m organizer --config /path/to/ai.env --db /path/to/project_hub.db summary --file-id 123

smoke는 가상 자료로 연결만 확인합니다. summary/ask는 해당 파일의 추출된 텍스트와
질문을 Google Cloud Gemini로 보냅니다. 실제 파일이나 다른 문서 전체를 보내지 않습니다.
파일 ID는 기존 자료 검색 결과의 ID입니다. 검색 텍스트가 없는 파일은 요청하지 않습니다.
Word/Excel은 추출 구간 번호이며 실제 페이지/셀 위치라고 해석하면 안 됩니다.

출력에는 한국어 answer, sources, partial, model, usage, cached가 포함됩니다.
부분 인덱스 또는 12,000자/24구간 제한으로 잘린 자료는 partial=true입니다.
출처 ID가 유효한지 검사하지만 AI의 내용 정확성을 보증하지는 않습니다.
자료에 없는 사항은 모른다고 답하도록 지시하며 도구 실행 권한은 제공하지 않습니다.

사용량 관리
- 기본 한도: 한국시간 하루 20회, 한 달 300회. 실패/중단 요청도 한도에 포함합니다.
- 입력 최대 12,000자, 질문 1,000자, 출력 최대 2,048토큰. 자동 재시도 없음.
- 같은 모델/내용/질문은 로컬 결과를 재사용합니다. 원문이나 모델이 바뀌면 새 요청입니다.
- 사용 기록과 결과는 PROJECT_HUB_AI_STATE/usage.db에 저장합니다. 삭제하면 한도가
  초기화되므로 보존하세요. 여러 실행기가 한도를 공유하려면 같은 state를 사용해야 합니다.
- 이 제한은 이 프로그램에만 적용됩니다. Google 계정 전체의 과금 상한이 아닙니다.
- 상태 폴더에는 요약과 답변이 있으므로 비공개로 유지하고 Git에 포함하지 않습니다.
- 중지는 ai.env에서 PROJECT_HUB_AI_ENABLED=false로 설정합니다.

모델: gemini-3.1-flash-lite, global, MINIMAL thinking.
공식 모델 정보: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/gemini/3-1-flash-lite
요청 형식: https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/inference
현재 구현은 관리자 로컬 실행용입니다. 다음 단계는 인증된 웹 자료실에서 채널 접근
권한을 확인하고 사용자 요청에 따라 이 모듈을 호출하는 연결입니다.
