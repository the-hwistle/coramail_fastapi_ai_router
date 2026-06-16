# CoRA Mail FastAPI AI Router

`coramail_fastapi_ai_router`는 기존 `coramail-test1` 코드를 직접 수정하지 않고, 필요한 메일 수집/인덱싱/검색/분류 파이프라인을 `pipeline/` 아래에 복제해 독립 실행할 수 있게 만든 CoRA 메일 라우팅 앱입니다.

핵심 목적은 Gmail에서 업무 메일과 첨부파일을 가져와 PostgreSQL에 저장하고, Qdrant 로컬 벡터 DB와 Ollama 모델을 사용해 검색, 요약, 분류, 담당자 라우팅을 제공하는 것입니다.

## 구성 요약

- App server: FastAPI API + Jinja 템플릿 + HTMX UI (`app.py`, `templates/`, `static/app.css`)
- Data store: PostgreSQL `emails` 테이블
- Vector store: 로컬 Qdrant 파일 저장소 (`pipeline/qdrant_storage`)
- AI runtime: Docker Compose `ollama` 서비스 또는 로컬 Ollama

## 디렉터리 구조

```text
coramail_fastapi_ai_router/
├── app.py                              # FastAPI 백엔드
├── templates/                          # Jinja/HTMX 서버 렌더링 화면
├── static/app.css                      # FastAPI UI 스타일
├── static/index.html                   # 이전 Stitch HTML SPA 참고본
├── pipeline/
│   ├── gmail_postgres_fetcher.py       # Gmail API 수집 및 PostgreSQL 저장
│   ├── import_emails_json_to_postgres.py
│   ├── auto_ingest.py                  # 수집, 인덱싱, DB 분류 저장 자동 실행
│   ├── index.py                        # PostgreSQL 메일/첨부파일 Qdrant 인덱싱
│   ├── qdrant_hybridsearch.py          # 벡터+메타데이터 하이브리드 검색
│   └── email_classification.py         # Qdrant 근거 기반 LLM 분류
├── docs/postgresql_schema_design.md    # 향후 정규화 스키마 설계 문서
├── reference_html/                     # 원본 Stitch HTML 참고본
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
└── uv.lock
```

`pipeline/` 아래에는 실행 중 생성되는 파일도 생길 수 있습니다. 예를 들어 Gmail 인증 토큰, 첨부파일, `emails.json` 미러 파일, `qdrant_storage/` 등이 여기에 위치합니다. 웹 대시보드의 분류 결과는 파일이 아니라 PostgreSQL `email_classifications` 테이블에서 읽습니다.

## 동작 흐름

1. Gmail API로 메일을 조회하고 첨부파일을 저장합니다.
2. 수집한 메일은 PostgreSQL `emails` 테이블에 저장됩니다.
3. `pipeline/index.py`가 PostgreSQL 메일과 `pipeline/data/`의 첨부파일 원본을 읽어 Qdrant 컬렉션을 재생성합니다.
4. 검색 API는 Qdrant에서 벡터 검색과 메타데이터 필터를 결합해 관련 메일/첨부 근거를 찾습니다.
5. 분류 API는 Qdrant 근거와 이메일 원문을 LLM에 전달해 메일 유형, 첨부 문서 유형, 업무 참조값, 라우팅 라벨을 생성합니다.
6. FastAPI가 Jinja 템플릿을 렌더링하고 HTMX가 메일 목록, 상세, 검색 결과, 자동 동기화 상태를 부분 갱신합니다.

## 주요 모델과 저장소

현재 코드 기본값은 다음과 같습니다.

- Embedding model: `nomic-embed-text`
- LLM model: `qwen3.5:2b`
- Vision model: `moondream`
- Qdrant collections:
  - `coramail_emails`
  - `coramail_attachments`

Docker Compose로 실행하면 `ollama` 서비스가 함께 뜨고, API는 `http://ollama:11434`로 해당 서비스에 연결합니다. 로컬에서 Docker 없이 직접 실행할 때만 별도로 로컬 Ollama가 필요합니다.

```bash
ollama pull nomic-embed-text
ollama pull qwen3.5:2b
ollama pull moondream
```

## 패키지 관리

이 프로젝트는 `uv`로 Python 의존성을 관리합니다. `requirements.txt`는 사용하지 않습니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
uv sync --frozen
```

의존성을 변경할 때는 [pyproject.toml](/home/ysh/workspace/coramail_fastapi_ai_router/pyproject.toml)을 수정한 뒤 잠금 파일을 갱신합니다.

```bash
uv lock
uv sync
```

## 환경 변수

| 이름 | 기본값 | 설명 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://coramail:coramail@localhost:5432/coramail` | PostgreSQL 연결 문자열 |
| `CORAMAIL_DATABASE_URL` | 없음 | `DATABASE_URL`보다 우선 적용되는 메일 DB 연결 문자열 |
| `CORAMAIL_PIPELINE_DIR` | `<repo>/pipeline` | FastAPI가 불러올 파이프라인 코드/데이터 디렉터리 |
| `OLLAMA_HOST` | Ollama 기본값 | Ollama API 주소. Compose에서는 `http://ollama:11434` |
| `FEBMAIL_MAX_EMAILS` | `100` | Gmail fetch 기본 최대 수집 수 |
| `FEBMAIL_MAX_BODY_LENGTH` | `3000` | 저장할 이메일 본문 최대 길이 |
| `FEBMAIL_API_RETRIES` | `3` | Gmail API 재시도 횟수 |
| `FEBMAIL_API_BACKOFF_SEC` | `2` | Gmail API 재시도 대기 시간 |

## 로컬 실행

### 1. PostgreSQL 준비

로컬 PostgreSQL을 직접 띄우거나 Docker Compose의 `postgres` 서비스만 사용할 수 있습니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
docker compose up -d postgres
```

### 2. 기존 `emails.json`을 PostgreSQL로 가져오기

`pipeline/emails.json` 스냅샷이 있다면 다음 명령으로 PostgreSQL에 적재합니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
DATABASE_URL=postgresql://coramail:coramail@localhost:5432/coramail \
  uv run python pipeline/import_emails_json_to_postgres.py
```

Gmail에서 새로 수집하려면 루트 `.env`에 `GOOGLE_CREDENTIALS_JSON`을 넣어둔 뒤 API 또는 CLI로 fetch를 실행합니다. 첫 OAuth 인증이 끝나면 `GOOGLE_TOKEN_JSON`이 같은 `.env`에 자동으로 저장됩니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
DATABASE_URL=postgresql://coramail:coramail@localhost:5432/coramail \
  uv run python pipeline/gmail_postgres_fetcher.py --max 100
```

### 3. Qdrant 인덱스 생성

PostgreSQL에 이메일이 저장된 뒤 실행합니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
DATABASE_URL=postgresql://coramail:coramail@localhost:5432/coramail \
  uv run python pipeline/index.py
```

### 4. FastAPI 앱 실행

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
DATABASE_URL=postgresql://coramail:coramail@localhost:5432/coramail \
  uv run uvicorn app:app --host 127.0.0.1 --port 8011
```

브라우저에서 `http://127.0.0.1:8011`을 엽니다.

확인:

```bash
curl http://127.0.0.1:8011/api/health
```

## 웹 대시보드 사용설명서

웹 대시보드는 FastAPI가 직접 렌더링합니다. `http://127.0.0.1:8011`에서 Dashboard, Inbox, Search, Settings 메뉴와 자동 Gmail 동기화 상태를 확인할 수 있습니다.

### 공통 사용 순서

1. PostgreSQL에 이메일을 적재합니다.
   - 기존 `pipeline/emails.json`이 있으면 import 스크립트를 사용합니다.
   - Gmail에서 새로 가져오는 작업은 FastAPI 서버의 자동 동기화가 주기적으로 실행합니다. 수동 실행이 필요하면 `/api/fetch`를 사용할 수 있습니다.
2. Qdrant 인덱스를 만듭니다.
   - 자동 동기화 또는 `/api/reindex`를 사용합니다.
   - 인덱싱이 끝나야 Search와 Classify 품질이 정상적으로 나옵니다.
3. Dashboard에서 전체 메일 수, 분류 수, 첨부파일 수, Qdrant point 수를 확인합니다.
4. Inbox에서 개별 이메일을 선택하고 Classify를 실행해 라우팅 결과를 확인합니다.
5. Search에서 업무 참조번호, 문서 유형, 납기, 금액 같은 자연어 질문을 입력해 근거 문서를 검색합니다.

FastAPI 서버가 시작되면 기본 60초 간격으로 Gmail 수집, Qdrant 증분 인덱싱, LLM 분류 결과 PostgreSQL 저장이 자동 실행됩니다. 대시보드는 10초마다 최신 상태를 다시 불러와 새 메일과 분류 상태를 반영합니다.

### Dashboard 화면

Dashboard는 운영 현황을 빠르게 보는 화면입니다.

- `Total Mails`: PostgreSQL `emails` 테이블에 저장된 이메일 수
- `AI Auto-Routed`: PostgreSQL `email_classifications` 기준 분류 완료 비율
- `Duplicate Risk`: thread 또는 제목 기준 중복 가능성 추정값
- `Attachments`: 이메일에 연결된 첨부파일 수
- `Recent Mail Streams`: 최근 이메일 목록과 분류/라우팅 상태
- `Category Distribution`: 메일 카테고리별 분포
- `Qdrant Points`: 이메일/첨부파일 벡터 컬렉션 point 수

상단 `Refresh` 버튼은 최신 서버 렌더링 데이터를 다시 불러옵니다.

### Inbox 화면

Inbox는 개별 이메일을 검토하고 LLM 분류를 실행하는 화면입니다.

사용 방법:

1. 이메일 목록 또는 선택 상자에서 확인할 메일을 고릅니다.
2. 왼쪽/본문 영역에서 발신자, 제목, 본문, 첨부파일 목록을 확인합니다.
3. `Classify` 버튼을 누르면 `/api/classify`가 호출됩니다.
4. 오른쪽 분석 영역에서 다음 결과를 확인합니다.
   - 메일 카테고리: `rfq`, `quote`, `order`, `payment`, `delivery`, `technical`, `general`
   - 라우팅 라벨: 영업, 견적, 발주, 회계, 물류, 기술 등 담당 영역
   - 업무 참조값: 예: `FM250016318`, `C0000071`
   - 선박명 후보
   - 첨부파일 문서 유형과 신뢰도
   - LLM 판단 사유

`Confirm Routing`은 현재 UI 확인용 동작입니다. 실제 Gmail 전달, 라벨링, 담당자 알림 API는 아직 연결되어 있지 않습니다.

메일이 `PENDING` 또는 `미분류`로 보이는 경우는 다음 중 하나입니다.

- 해당 메일에 대한 PostgreSQL `email_classifications` 레코드가 아직 없습니다.
- Gmail 수집은 끝났지만 Qdrant 재인덱싱 또는 LLM 분류 작업이 아직 끝나지 않았습니다.
- Ollama 모델이 실행 중이 아니거나 분류 중 오류가 발생했습니다.
- `Classify`를 누르기 전의 메일입니다.

Inbox에서 `Classify`를 직접 누르면 해당 메일의 분류 결과가 PostgreSQL `email_classifications` 테이블에 저장되고, 새로고침 후 대시보드에도 반영됩니다.

### Search 화면

Search는 Qdrant 하이브리드 검색과 LLM 답변 생성을 사용하는 화면입니다.

사용 방법:

1. 검색창에 자연어 질문을 입력합니다.
2. `Limit`으로 검색 결과 수를 고릅니다.
3. `Search` 버튼을 누릅니다.
4. `Generated Answer`에서 LLM이 검색 근거를 바탕으로 생성한 답변을 확인합니다.
5. `Query Intent`에서 시스템이 추출한 검색 의도를 확인합니다.
6. `Evidence Results`에서 실제 근거 payload, 점수, 원문 미리보기를 확인합니다.

검색 예시:

- `FM250016318 견적서의 납기와 총액을 찾아줘`
- `C0000105 관련 견적서 찾아줘`
- `도면 스캔본이 첨부된 메일`
- `payment 입금요청서 관련 메일`
- `납기 지연 가능성이 있는 메일`

검색 결과가 비어 있으면 먼저 Dashboard의 `Qdrant Points`가 0이 아닌지 확인하고, 0이면 Reindex를 실행합니다.

### Settings 화면

Settings는 현재 AI/검색 환경과 라우팅 규칙을 보는 화면입니다.

- LLM 연결: Ollama 사용 여부
- Vector DB: Qdrant 이메일/첨부파일 point 수
- Duplicate Prevention: 중복 방지 기준값 표시
- Email Classification Rules: 카테고리별 라우팅 규칙 표시

현재 Settings의 규칙 편집 버튼은 표시용입니다. 라우팅 정책을 DB에서 편집하는 기능은 아직 구현되어 있지 않습니다.

### 대시보드 자동 동기화

`http://127.0.0.1:8011` 대시보드는 Gmail 동기화 버튼 없이 자동 동기화 상태를 표시합니다.

- `Triage Inbox`: Inbox 화면으로 이동합니다.

자동 동기화는 기본적으로 `pipeline/auto_ingest.py`를 실행합니다. 이 작업은 Gmail fetch, Qdrant reindex, LLM classify를 순서대로 수행하고 PostgreSQL `email_classifications` 테이블을 갱신합니다.

주기는 `.env`의 `CORAMAIL_AUTO_SYNC_INTERVAL_SECONDS`로 조절할 수 있고, 실행 중인 PID와 로그 경로는 `/api/auto-sync` 및 대시보드 상단 상태 영역에서 확인할 수 있습니다.

## Docker Compose 실행

Docker Compose는 PostgreSQL, Ollama, FastAPI 앱을 함께 실행합니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
docker compose up -d --build
```

서비스 주소:

- FastAPI app: `http://127.0.0.1:8011`
- PostgreSQL: `127.0.0.1:5432`
- Ollama: Compose 내부 주소 `http://ollama:11434`
- Ollama host publish: 기본 `127.0.0.1:11435 -> container 11434` (`CORAMAIL_OLLAMA_HOST_PORT`로 변경 가능)

Compose 환경에서는 API 컨테이너가 같은 Compose 네트워크의 `ollama` 컨테이너를 직접 호출합니다. 그래서 호스트에 이미 로컬 Ollama가 `11434`를 쓰고 있어도, 기본 설정만 유지하면 Compose 쪽은 `11435`로 publish되어 충돌하지 않습니다. 꼭 `11434`로 노출해야 하면 `CORAMAIL_OLLAMA_HOST_PORT=11434`를 지정하세요.

처음 띄운 Ollama 컨테이너에는 모델이 없으므로, 최초 1회 모델을 내려받아야 합니다.

```bash
docker compose exec ollama ollama pull nomic-embed-text
docker compose exec ollama ollama pull qwen3.5:2b
docker compose exec ollama ollama pull moondream
docker compose exec ollama ollama list
```

`nomic-embed-text`는 검색/인덱싱 임베딩에 필요하고, `qwen3.5:2b`는 검색 답변과 메일 분류에 필요합니다. `moondream`은 이미지 첨부파일 캡션 생성이 필요할 때 사용합니다.

Docker 이미지는 Dockerfile 안에서 `uv sync --frozen --no-dev`를 실행하므로, 단순 Docker 실행만 할 때는 로컬에서 `uv sync`를 먼저 실행할 필요가 없습니다. 의존성을 바꾼 경우에만 `uv lock` 후 다시 build하면 됩니다.

Compose 실행 후 기존 `pipeline/emails.json`을 PostgreSQL에 넣으려면 다음을 한 번 실행합니다.

```bash
docker compose run --rm api uv run python pipeline/import_emails_json_to_postgres.py
```

## 주요 API

| Method | Path | 설명 |
| --- | --- | --- |
| `GET` | `/api/health` | 앱, 파이프라인, DB, 모델 설정 상태 |
| `GET` | `/api/summary` | 이메일 수, 첨부 수, 분류 수, Qdrant point 수 요약 |
| `GET` | `/api/emails` | 이메일 목록 조회. `q`, `category`, `limit`, `offset` 지원 |
| `GET` | `/api/emails/{email_index}` | 이메일 상세 조회 |
| `POST` | `/api/search` | Qdrant 하이브리드 검색 및 선택적 답변 생성 |
| `POST` | `/api/classify` | 단일 이메일 LLM 분류 후 PostgreSQL `email_classifications` 저장 |
| `POST` | `/api/fetch` | 기본값으로 Gmail 수집, Qdrant 재인덱싱, LLM 분류 백그라운드 실행 |
| `POST` | `/api/reindex` | 백그라운드 Qdrant 재인덱싱 프로세스 실행 |

검색 예시:

```bash
curl -X POST http://127.0.0.1:8011/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"FM250016 견적서 납기", "limit":5, "with_answer":true}'
```

분류 예시:

```bash
curl -X POST http://127.0.0.1:8011/api/classify \
  -H 'Content-Type: application/json' \
  -d '{"email_index":0, "retrieval_limit":5}'
```

Gmail fetch 예시:

```bash
curl -X POST http://127.0.0.1:8011/api/fetch \
  -H 'Content-Type: application/json' \
  -d '{"max_emails":100, "has_attachment":true}'
```

수집만 하고 자동 인덱싱/분류를 건너뛰려면 `auto_process`를 `false`로 보냅니다.

```bash
curl -X POST http://127.0.0.1:8011/api/fetch \
  -H 'Content-Type: application/json' \
  -d '{"max_emails":100, "auto_process":false}'
```

## 파이프라인 스크립트

- `pipeline/gmail_postgres_fetcher.py`
  - Gmail OAuth 인증을 사용해 메일과 첨부파일을 가져옵니다.
  - 메일은 PostgreSQL `emails` 테이블에 저장하고, 첨부파일은 `pipeline/attachments/`에 저장합니다.
  - 기본적으로 `pipeline/emails.json` 미러 파일도 생성합니다.

- `pipeline/import_emails_json_to_postgres.py`
  - 기존 JSON 스냅샷을 PostgreSQL `emails` 테이블로 가져옵니다.

- `pipeline/auto_ingest.py`
  - Gmail 수집, Qdrant 재인덱싱, LLM 분류 결과 PostgreSQL 저장을 순서대로 실행합니다.
  - FastAPI 자동 동기화와 `/api/fetch` 기본 동작에서 사용합니다.

- `pipeline/index.py`
  - PostgreSQL 이메일과 `pipeline/data/` 첨부파일 원본을 읽어 Qdrant 컬렉션을 재생성합니다.
  - PDF는 PyMuPDF로 텍스트를 추출하고, 이미지는 `moondream`으로 캡션을 생성합니다.

- `pipeline/qdrant_hybridsearch.py`
  - 업무 참조번호, 문서 유형, 파일 그룹 등 메타데이터 필터와 벡터 검색을 조합합니다.

- `pipeline/email_classification.py`
  - 이메일 원문과 Qdrant 근거를 LLM 프롬프트에 넣어 메일 카테고리와 라우팅 라벨을 생성합니다.
  - 기본 저장소는 PostgreSQL `email_classifications` 테이블입니다. `--output-json`은 별도 export가 필요할 때만 사용합니다.

## 현재 구현상 주의사항

- `docs/postgresql_schema_design.md`는 향후 정규화 설계안입니다. 현재 실행 코드는 단일 `emails` 테이블과 JSONB `attachments` 컬럼을 사용합니다.
- `/api/auto-sync`는 자동 Gmail 동기화의 실행 여부, 최근 PID, 로그 경로, 종료 코드를 반환합니다. `/api/reindex`와 수동 `/api/fetch`는 백그라운드 프로세스를 시작하고 PID와 로그 경로를 반환합니다.
- 웹 대시보드는 `classification_report.json`을 사용하지 않습니다. `/api/summary`와 `/api/emails`는 PostgreSQL `email_classifications` 테이블의 분류 결과를 표시합니다.
- Qdrant는 임베디드 로컬 파일 저장소를 사용하므로 동시에 여러 프로세스가 같은 `pipeline/qdrant_storage`를 열면 잠금 문제가 생길 수 있습니다.
- Gmail fetch를 처음 실행할 때는 OAuth 브라우저 인증이 필요하며 루트 `.env`의 `GOOGLE_CREDENTIALS_JSON`이 있어야 합니다.
