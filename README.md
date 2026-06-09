# CoRA Mail AI Router FastAPI App

기존 `coramail-test1` 코드는 수정하지 않고, 필요한 파이프라인 코드를 `pipeline/`에 복제해 사용하는 독립 FastAPI 앱입니다.

## 실행

```bash
cd /home/ysh/workspace/coramail_fastapi_ai_router
python -m uvicorn app:app --host 127.0.0.1 --port 8011
```

브라우저에서 `http://127.0.0.1:8011`을 열면 됩니다.

## 구성

- `app.py`: FastAPI 백엔드. Gmail fetch, Qdrant hybrid search, LLM 분류, reindex API를 제공합니다.
- `static/index.html`: Stitch HTML 시안 기반 SPA 프론트엔드.
- `pipeline/`: Gmail SQLite 수집, Qdrant 인덱싱, hybrid search, email classification 코드와 로컬 데이터 복제본입니다.
- `reference_html/`: 원본 Stitch HTML 사본. 실행에는 직접 사용하지 않지만 화면 이관 근거로 보관합니다.

## 주요 API

- `GET /api/health`
- `GET /api/summary`
- `GET /api/emails`
- `GET /api/emails/{email_index}`
- `POST /api/search`
- `POST /api/classify`
- `POST /api/fetch`
- `POST /api/reindex`
