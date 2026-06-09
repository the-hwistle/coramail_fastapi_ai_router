# CoRA Mail AI Router

기존 `coramail-test1` 코드는 수정하지 않고, 필요한 파이프라인 코드를 `pipeline/`에 복제해 사용하는 독립 앱입니다.
백엔드와 프론트엔드는 분리되어 있습니다.

- Backend: FastAPI (`app.py`)
- Frontend A: Streamlit (`streamlit_app.py`)에서 기존 `static/index.html` UI를 그대로 렌더링
- Frontend B: Streamlit (`streamlit_native_app.py`) 네이티브 컴포넌트만 사용, `index.html` 미사용

## 실행

## 패키지 관리

이 프로젝트는 `uv`로 Python 의존성을 관리합니다. `requirements.txt`는 사용하지 않습니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
uv sync --frozen
```

의존성을 변경할 때는 [pyproject.toml](/home/ysh/workspace/coramail_fastapi_ai_router/pyproject.toml)을 수정한 뒤:

```bash
uv lock
uv sync
```

## 로컬 실행

터미널 1: FastAPI 백엔드

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
uv run uvicorn app:app --host 127.0.0.1 --port 8011
```

터미널 2: Streamlit 프론트엔드 A (`index.html` 사용)

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
CORAMAIL_API_BASE_URL=http://127.0.0.1:8011 \
  uv run streamlit run streamlit_app.py --server.port 8501 --server.headless true
```

브라우저에서 `http://127.0.0.1:8501`을 열면 됩니다.

터미널 3: Streamlit 프론트엔드 B (`index.html` 미사용)

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
CORAMAIL_API_BASE_URL=http://127.0.0.1:8011 \
  uv run streamlit run streamlit_native_app.py --server.port 8502 --server.headless true
```

브라우저에서 `http://127.0.0.1:8502`를 열면 됩니다.

## Docker 실행

Docker Compose는 백엔드와 두 프론트엔드를 분리된 서비스로 실행합니다.

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
docker compose up --build
```

서비스:

- FastAPI backend: `http://127.0.0.1:8011`
- Streamlit frontend A (`index.html` 사용): `http://127.0.0.1:8501`
- Streamlit frontend B (`index.html` 미사용): `http://127.0.0.1:8502`

Compose는 `./pipeline`을 컨테이너의 `/app/pipeline`에 마운트합니다. 로컬 Ollama는 컨테이너에서 `http://host.docker.internal:11434`로 접근하도록 `OLLAMA_HOST`를 설정했습니다.

## 구성

- `app.py`: FastAPI 백엔드. Gmail fetch, Qdrant hybrid search, LLM 분류, reindex API를 제공합니다.
- `streamlit_app.py`: Streamlit 프론트엔드 A. 기존 UI HTML을 그대로 로드하고 FastAPI API 주소만 주입합니다.
- `streamlit_native_app.py`: Streamlit 프론트엔드 B. HTML 파일을 읽지 않고 Streamlit 컴포넌트로 화면을 구성합니다.
- `static/index.html`: Stitch HTML 시안 기반 SPA UI.
- `pipeline/`: Gmail SQLite 수집, Qdrant 인덱싱, hybrid search, email classification 코드와 로컬 데이터 복제본입니다.
- `reference_html/`: 원본 Stitch HTML 사본. 실행에는 직접 사용하지 않지만 화면 이관 근거로 보관합니다.
- `pyproject.toml`: uv 의존성 선언.
- `uv.lock`: 재현 가능한 uv 잠금 파일.
- `Dockerfile`: uv 기반 컨테이너 이미지.
- `docker-compose.yml`: FastAPI, Streamlit A, Streamlit B 서비스 구성.

## 주요 API

- `GET /api/health`
- `GET /api/summary`
- `GET /api/emails`
- `GET /api/emails/{email_index}`
- `POST /api/search`
- `POST /api/classify`
- `POST /api/fetch`
- `POST /api/reindex`
