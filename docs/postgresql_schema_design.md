# CoRA Mail PostgreSQL Schema Design

## 목적

Gmail 수신 메일을 PostgreSQL에 정규화해서 저장하고, 첨부파일 파싱 결과와 LLM 분석 결과, 담당자 라우팅 정보를 분리 관리한다. 목표는 다음과 같다.

- 원본 이메일과 첨부파일을 중복 없이 보관한다.
- 이메일 본문, 첨부파일 텍스트, 이미지 캡션, PDF 파싱 결과를 분석 가능한 형태로 저장한다.
- LLM 분석 결과를 원본 데이터와 분리해 재분석 이력을 남길 수 있게 한다.
- 담당자 라우팅 정책을 코드가 아니라 DB 데이터로 관리할 수 있게 한다.

## 핵심 설계 원칙

- `emails`는 Gmail 원본 메타데이터와 본문 중심의 원천 테이블이다.
- `attachments`는 이메일과 1:N 관계를 가진다. 이메일 테이블에 첨부파일 ID 하나만 두면 다중 첨부 메일을 표현하기 어렵기 때문에, 첨부파일은 별도 테이블로 분리한다.
- `analysis_results`는 이메일별 LLM 분석 결과 테이블이다. 모델/프롬프트 버전이 바뀌면 같은 이메일에 여러 분석 결과가 생길 수 있으므로 분석 이력을 허용한다.
- `routing_table`은 담당자 마스터 테이블이다. 실제 라우팅 결과는 `analysis_results.assignee_id`가 참조한다.
- 운영 안정성을 위해 Gmail ID, thread ID, content hash, 분석 버전 컬럼을 둔다.

## ERD 개요

```text
emails 1 ─── N attachments
emails 1 ─── N analysis_results
routing_table 1 ─── N analysis_results
```

## Table 1. emails

이메일 원본과 검색/분석에 필요한 기본 메타데이터를 저장한다.

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| email_id | BIGSERIAL PK | 내부 이메일 고유 ID |
| email_uid | TEXT UNIQUE NOT NULL | 시스템 고유 이메일 ID. 예: gmail:{message_id} |
| gmail_message_id | TEXT UNIQUE | Gmail message id |
| gmail_thread_id | TEXT | Gmail thread id |
| rfc_message_id | TEXT | RFC Message-ID 헤더 |
| thread_key | TEXT | 중복/스레드 그룹핑용 정규화 키 |
| sender_email | TEXT | 발신자 이메일 주소 |
| sender_name | TEXT | 발신자 이름 |
| recipient_emails | TEXT[] | 수신자 이메일 목록 |
| cc_emails | TEXT[] | 참조 이메일 목록 |
| subject | TEXT | 제목 |
| received_at | TIMESTAMPTZ | 수신 일시 |
| body_text | TEXT | 이메일 본문 텍스트 |
| body_hash | TEXT | 본문 중복 감지용 해시 |
| has_attachments | BOOLEAN | 첨부파일 존재 여부 |
| raw_headers | JSONB | 원본 헤더 중 필요한 값 |
| created_at | TIMESTAMPTZ | DB 생성 시각 |
| updated_at | TIMESTAMPTZ | DB 갱신 시각 |

권장 인덱스:

- `UNIQUE (email_uid)`
- `UNIQUE (gmail_message_id)`
- `INDEX (gmail_thread_id)`
- `INDEX (received_at DESC)`
- `INDEX (sender_email)`
- `GIN (recipient_emails)`

## Table 2. attachments

첨부파일의 저장 경로, 파일 유형, 파싱 결과를 저장한다.

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| attachment_id | BIGSERIAL PK | 내부 첨부파일 고유 ID |
| attachment_uid | TEXT UNIQUE NOT NULL | 첨부파일 고유 ID. content hash 기반 권장 |
| email_id | BIGINT FK | 부모 이메일 ID |
| gmail_attachment_id | TEXT | Gmail attachment id |
| filename | TEXT | 원본 파일명 |
| file_ext | TEXT | 확장자. 예: pdf, jpg, png, xlsx |
| file_type | TEXT | 상위 유형. 예: pdf, image, excel, word, text, other |
| mime_type | TEXT | MIME type |
| file_size_bytes | BIGINT | 파일 크기 |
| content_hash | TEXT | 중복 파일 감지용 해시 |
| storage_path | TEXT | 로컬 또는 오브젝트 스토리지 경로 |
| image_caption | TEXT | 이미지/VLM 캡션 |
| parsed_text | TEXT | PDF/Excel/Word 등 파싱 텍스트 |
| parse_status | TEXT | pending, parsed, failed, skipped |
| parse_error | TEXT | 파싱 실패 메시지 |
| created_at | TIMESTAMPTZ | DB 생성 시각 |
| updated_at | TIMESTAMPTZ | DB 갱신 시각 |

권장 인덱스:

- `UNIQUE (attachment_uid)`
- `INDEX (email_id)`
- `INDEX (content_hash)`
- `INDEX (file_type)`
- `INDEX (parse_status)`

## Table 3. analysis_results

LLM이 이메일 본문과 첨부파일 내용을 함께 분석한 결과를 저장한다.

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| analysis_id | BIGSERIAL PK | 분석 결과 고유 ID |
| email_id | BIGINT FK | 분석 대상 이메일 ID |
| model_name | TEXT | 사용한 LLM 모델명 |
| prompt_version | TEXT | 프롬프트/분류 기준 버전 |
| summary | TEXT | 이메일 본문+첨부파일 통합 executive summary |
| email_type | TEXT | 발주, 문의, 서비스, 기술, 기타 등 |
| assignee_id | BIGINT FK NULL | 추천 담당자 ID |
| urgency | TEXT | low, normal, high, urgent |
| confidence | NUMERIC(4,3) | 분석 신뢰도. 0.000~1.000 |
| business_refs | TEXT[] | 견적번호, 발주번호 등 업무 참조값 |
| vessel_names | TEXT[] | 선박명 등 도메인 엔티티 |
| due_date | DATE | 납기/회신 필요일 등 추출 결과 |
| total_amount | NUMERIC(18,2) | 견적/발주 금액 추출 결과 |
| currency | TEXT | KRW, USD, EUR 등 |
| routing_reason | TEXT | 담당자 추천 사유 |
| raw_result | JSONB | LLM 원본 구조화 응답 |
| status | TEXT | pending, completed, failed, needs_review |
| error_message | TEXT | 분석 실패 메시지 |
| created_at | TIMESTAMPTZ | 분석 생성 시각 |

권장 인덱스:

- `INDEX (email_id)`
- `INDEX (email_type)`
- `INDEX (assignee_id)`
- `INDEX (urgency)`
- `INDEX (status)`
- `INDEX (created_at DESC)`
- `UNIQUE (email_id, model_name, prompt_version)` 또는 이력 보존 정책에 따라 비고유 인덱스

## Table 4. routing_table

담당자/부서/역할 정보를 저장한다. 이름은 향후 정책 테이블과 구분하려면 `assignees`가 더 명확하지만, 현재 요구사항에 맞춰 `routing_table`로 둔다.

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| assignee_id | BIGSERIAL PK | 담당자 고유 ID |
| assignee_name | TEXT NOT NULL | 담당자 이름 |
| department | TEXT | 부서 |
| position | TEXT | 직위 |
| email_address | TEXT UNIQUE NOT NULL | 담당자 이메일 주소 |
| routing_labels | TEXT[] | 담당 영역. 예: order, quote, service, technical |
| priority | INTEGER | 같은 영역 내 우선순위 |
| is_active | BOOLEAN | 활성 여부 |
| created_at | TIMESTAMPTZ | DB 생성 시각 |
| updated_at | TIMESTAMPTZ | DB 갱신 시각 |

권장 인덱스:

- `UNIQUE (email_address)`
- `INDEX (department)`
- `GIN (routing_labels)`
- `INDEX (is_active)`

## PostgreSQL DDL 초안

```sql
CREATE TABLE emails (
    email_id BIGSERIAL PRIMARY KEY,
    email_uid TEXT NOT NULL UNIQUE,
    gmail_message_id TEXT UNIQUE,
    gmail_thread_id TEXT,
    rfc_message_id TEXT,
    thread_key TEXT,
    sender_email TEXT,
    sender_name TEXT,
    recipient_emails TEXT[] NOT NULL DEFAULT '{}',
    cc_emails TEXT[] NOT NULL DEFAULT '{}',
    subject TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ,
    body_text TEXT NOT NULL DEFAULT '',
    body_hash TEXT,
    has_attachments BOOLEAN NOT NULL DEFAULT FALSE,
    raw_headers JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE attachments (
    attachment_id BIGSERIAL PRIMARY KEY,
    attachment_uid TEXT NOT NULL UNIQUE,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    gmail_attachment_id TEXT,
    filename TEXT NOT NULL DEFAULT '',
    file_ext TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT 'other',
    mime_type TEXT,
    file_size_bytes BIGINT,
    content_hash TEXT,
    storage_path TEXT,
    image_caption TEXT,
    parsed_text TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE routing_table (
    assignee_id BIGSERIAL PRIMARY KEY,
    assignee_name TEXT NOT NULL,
    department TEXT,
    position TEXT,
    email_address TEXT NOT NULL UNIQUE,
    routing_labels TEXT[] NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE analysis_results (
    analysis_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    email_type TEXT NOT NULL DEFAULT '기타',
    assignee_id BIGINT REFERENCES routing_table(assignee_id) ON DELETE SET NULL,
    urgency TEXT NOT NULL DEFAULT 'normal',
    confidence NUMERIC(4,3),
    business_refs TEXT[] NOT NULL DEFAULT '{}',
    vessel_names TEXT[] NOT NULL DEFAULT '{}',
    due_date DATE,
    total_amount NUMERIC(18,2),
    currency TEXT,
    routing_reason TEXT,
    raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'completed',
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_emails_gmail_thread_id ON emails(gmail_thread_id);
CREATE INDEX idx_emails_received_at ON emails(received_at DESC);
CREATE INDEX idx_emails_sender_email ON emails(sender_email);
CREATE INDEX idx_emails_recipient_emails ON emails USING GIN(recipient_emails);

CREATE INDEX idx_attachments_email_id ON attachments(email_id);
CREATE INDEX idx_attachments_content_hash ON attachments(content_hash);
CREATE INDEX idx_attachments_file_type ON attachments(file_type);
CREATE INDEX idx_attachments_parse_status ON attachments(parse_status);

CREATE INDEX idx_routing_department ON routing_table(department);
CREATE INDEX idx_routing_labels ON routing_table USING GIN(routing_labels);
CREATE INDEX idx_routing_is_active ON routing_table(is_active);

CREATE INDEX idx_analysis_email_id ON analysis_results(email_id);
CREATE INDEX idx_analysis_email_type ON analysis_results(email_type);
CREATE INDEX idx_analysis_assignee_id ON analysis_results(assignee_id);
CREATE INDEX idx_analysis_urgency ON analysis_results(urgency);
CREATE INDEX idx_analysis_status ON analysis_results(status);
CREATE INDEX idx_analysis_created_at ON analysis_results(created_at DESC);
CREATE UNIQUE INDEX idx_analysis_model_prompt_once
    ON analysis_results(email_id, model_name, prompt_version);
```

## 보완 권장 사항

### 1. emails에 첨부파일고유id는 두지 않는다

이메일 하나에 첨부파일이 여러 개일 수 있으므로 `emails.attachment_id`를 두면 1:N 구조를 표현하기 어렵다. 대신 `attachments.email_id` FK로 연결한다.

### 2. sender는 이름과 이메일을 분리한다

메일 헤더의 `From` 문자열은 검색/집계에 불리하다. `sender_email`, `sender_name`으로 분리 저장하고, 필요하면 원본 헤더는 `raw_headers`에 남긴다.

### 3. analysis_results는 이력을 허용한다

LLM 모델, 프롬프트, 라우팅 정책이 바뀌면 같은 이메일도 다른 분석 결과가 나올 수 있다. `model_name`, `prompt_version`을 저장하고, 운영 정책에 따라 최신 분석만 쓰거나 이력을 비교할 수 있게 한다.

### 4. routing_table은 담당자 마스터와 정책을 분리할 수 있다

초기에는 `routing_table.routing_labels`로 충분하다. 다만 담당 조건이 복잡해지면 다음 테이블을 추가하는 것이 좋다.

- `routing_rules`: 이메일 유형, 고객사, 키워드, 긴급도 기준으로 담당자를 매핑
- `departments`: 부서 마스터
- `customers`: 거래처/발신 도메인 마스터

### 5. 벡터 DB와 PostgreSQL의 연결 키를 명확히 둔다

Qdrant payload에는 반드시 다음 키를 넣는다.

- `email_id`
- `email_uid`
- `attachment_id`
- `attachment_uid`
- `source_type`: email 또는 attachment

이렇게 해야 벡터 검색 결과를 PostgreSQL 원본/분석 결과와 안정적으로 조인할 수 있다.

## 마이그레이션 순서

1. 새 테이블 4개 생성
2. 기존 `emails` JSONB attachments 구조를 `emails` + `attachments`로 분리 이관
3. 기존 `classification_report.json` 또는 현재 분류 결과를 `analysis_results`로 이관
4. 담당자 목록을 `routing_table`에 seed
5. FastAPI 조회 API를 새 스키마 기준으로 수정
6. Gmail fetcher 저장 로직을 새 스키마 기준으로 수정
7. Qdrant indexing payload에 `email_id`, `attachment_id` 추가
