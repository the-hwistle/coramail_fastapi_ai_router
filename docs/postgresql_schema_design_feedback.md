## 1. 한 줄 총평

**현재 4개 테이블은 LLM 분류 데모용 MVP로는 가능하지만, 하루 400~600건을 운영하는 실무 MVP로는 위험합니다.**
가장 큰 이유는 **최종 확정 상태**, **사람의 수정 이력**, **Gmail 동기화 상태**, **담당자 전달 이력**, **중복 전달 방지 장치**가 빠져 있기 때문입니다. 첨부한 설계는 `emails`, `attachments`, `analysis_results`, `routing_table`의 역할을 분리하려는 방향 자체는 좋지만, 운영 제어 테이블이 부족합니다. 

---

## 2. 점수

**72 / 100점**

감점 이유는 명확합니다.

| 항목                |   평가 |
| ----------------- | ---: |
| 이메일 원본 저장 구조      | 8/10 |
| 첨부파일 분리           | 9/10 |
| LLM 분석 이력 구조      | 7/10 |
| Gmail 동기화 안정성     | 4/10 |
| 중복 수집 방지          | 7/10 |
| 중복 담당자 전달 방지      | 2/10 |
| 사람 수정 이력          | 2/10 |
| 라우팅 모델링           | 5/10 |
| PostgreSQL 제약 조건  | 6/10 |
| FastAPI/프론트엔드 적합성 | 6/10 |

가장 큰 감점은 `analysis_results`가 **LLM 추천 결과**, **현재 업무 상태**, **최종 확정값**을 모두 떠안을 위험이 있다는 점입니다. 이 구조로 가면 “AI가 추천한 값”과 “사람이 최종 확정한 값”이 섞입니다. 운영 화면에서 누가 언제 담당자를 바꿨는지, 이미 담당자에게 알림을 보냈는지, 다시 보내면 안 되는지 판단하기 어려워집니다.

---

## 3. PostgreSQL 초보자용 핵심 설명

### `PRIMARY KEY`

각 행을 하나씩 구분하는 내부 번호입니다. 현재 `email_id`, `attachment_id`, `analysis_id`, `assignee_id`가 이 역할을 합니다. 내부 시스템에서는 Gmail ID 대신 이 숫자 ID로 조인하는 방식이 좋습니다.

### `FOREIGN KEY`

테이블끼리 연결하는 장치입니다. 예를 들어 `attachments.email_id`는 “이 첨부파일이 어느 이메일에 속하는가”를 나타냅니다. 현재 `attachments.email_id → emails.email_id`, `analysis_results.email_id → emails.email_id`, `analysis_results.assignee_id → routing_table.assignee_id` 구조는 방향이 맞습니다. 

### `UNIQUE`

중복 저장을 DB가 막게 하는 장치입니다. `gmail_message_id`에 `UNIQUE`를 걸면 같은 Gmail 메일을 두 번 저장하지 못합니다. 단, 공용 Gmail 계정 하나만 다루면 `UNIQUE (gmail_message_id)`로 충분하지만, 여러 Gmail 계정을 나중에 다룰 수 있다면 `UNIQUE (mailbox_email, gmail_message_id)`가 더 안전합니다.

### `INDEX`

검색 속도를 높이는 목차입니다. 예를 들어 `received_at DESC` 인덱스는 “최근 메일 목록” 화면에 유리합니다. `sender_email` 인덱스는 “특정 거래처가 보낸 메일”을 찾는 데 유리합니다.

### `GIN INDEX`

배열이나 JSONB처럼 “값 안에 여러 값이 들어 있는 컬럼”을 검색할 때 쓰는 특수 목차입니다. PostgreSQL 공식 문서도 GIN을 배열, JSONB, 전문 검색처럼 내부 원소를 찾아야 하는 자료형에 적합한 인덱스라고 설명합니다. 현재 `recipient_emails TEXT[]`, `routing_labels TEXT[]`, `raw_headers JSONB` 같은 컬럼이 여기에 해당합니다. ([PostgreSQL][1])

### `TEXT[]`

문자열 배열입니다. `recipient_emails`, `cc_emails`, `business_refs`, `vessel_names`, `routing_labels`에 사용했습니다. “값이 몇 개 안 되고 단순히 포함 여부만 찾을 때”는 괜찮습니다. 하지만 나중에 수신자별 집계, 권한, 상태, 정규화된 검색이 필요하면 별도 테이블로 분리하는 편이 낫습니다.

### `JSONB`

구조가 고정되지 않은 원본 데이터를 저장할 때 좋습니다. `raw_headers`, `raw_result`에 쓰는 것은 적절합니다. 다만 자주 검색하는 값은 JSONB 안에만 넣지 말고 별도 컬럼으로 빼야 합니다. PostgreSQL은 JSONB에 GIN 인덱스를 걸어 키/값 검색을 빠르게 만들 수 있지만, 쿼리 형태에 따라 인덱스를 못 쓰는 경우도 있습니다. ([PostgreSQL][2])

### `CHECK`

컬럼에 들어갈 수 있는 값을 제한하는 장치입니다. 예를 들어 `urgency`는 `low`, `normal`, `high`, `urgent`만 허용해야 합니다. 지금 DDL에는 `CHECK`가 거의 없어서 오타가 들어갈 수 있습니다. 예를 들어 `urgnet`, `hihg`, `donee` 같은 값도 DB가 막지 못합니다.

### `TIMESTAMPTZ`

타임존이 포함된 시각입니다. Gmail, 서버, 사용자가 서로 다른 시간대에 있을 수 있으므로 `created_at`, `updated_at`, `received_at`, `assigned_at`, `sent_at` 같은 시각은 `TIMESTAMPTZ`가 맞습니다.

---

## 4. 테이블별 피드백

## 4.1 `emails`

### 잘한 점

`emails`를 Gmail 원본 메타데이터와 본문 중심의 원천 테이블로 둔 것은 맞습니다. `email_uid`, `gmail_message_id`, `gmail_thread_id`, `rfc_message_id`, `body_hash`, `raw_headers`를 둔 것도 좋은 방향입니다. 첨부 설계에서도 `emails`를 원본 메일 중심 테이블로 정의하고 있습니다. 

### 문제점

가장 큰 문제는 Gmail 운영에 중요한 필드가 빠져 있다는 점입니다.

현재 스키마에는 다음이 없습니다.

```text
gmail_history_id
gmail_internal_date_ms
mailbox_email
gmail_label_ids
size_estimate
raw_payload 저장 여부
```

Gmail API의 `Message` 리소스에는 `id`, `threadId`, `historyId`, `internalDate`가 있습니다. `id`는 변경되지 않는 메시지 ID이고, `historyId`는 해당 메시지를 마지막으로 바꾼 history record ID이며, `internalDate`는 Gmail 내부 생성 시각입니다. 특히 일반 수신 메일에서는 `internalDate`가 메일 헤더의 `Date`보다 더 신뢰할 수 있는 수신 기준 시각입니다. ([Google for Developers][3])

또한 `messages.list`는 기본적으로 `id`와 `threadId`만 반환하고, 상세 내용은 `messages.get`으로 가져와야 합니다. 그래서 수집기 설계상 `gmail_message_id`만 저장하면 부족하고, 어떤 방식으로 상세 조회를 마쳤는지 추적할 컬럼이 필요합니다. ([Google for Developers][4])

### 추가할 컬럼

```sql
mailbox_email TEXT NOT NULL
gmail_history_id TEXT
gmail_internal_date_ms BIGINT
gmail_label_ids TEXT[] NOT NULL DEFAULT '{}'
gmail_size_estimate INTEGER
fetched_at TIMESTAMPTZ
body_html TEXT
snippet TEXT
is_deleted BOOLEAN NOT NULL DEFAULT FALSE
```

`mailbox_email`은 지금 공용 Gmail 하나만 쓴다면 없어도 당장 동작은 합니다. 하지만 나중에 회사 계정이 늘어나거나 테스트/운영 계정을 분리하면 반드시 필요합니다.

`received_at`은 사람이 읽기 쉬운 시각으로 두고, Gmail 원본값은 `gmail_internal_date_ms`로 따로 저장하는 것이 좋습니다.

### 이름을 바꿀 컬럼

`received_at`은 의미를 명확히 해야 합니다.

현재 이름만 보면 “메일 헤더의 Date인지”, “Gmail internalDate인지”, “DB에 저장된 시각인지” 애매합니다.

추천은 다음입니다.

```text
received_at              -- 사람이 화면에서 보는 수신 시각
gmail_internal_date_ms   -- Gmail 원본 internalDate
fetched_at               -- 우리 수집기가 Gmail에서 가져온 시각
```

### 필요한 제약 조건

```sql
ALTER TABLE emails
ADD CONSTRAINT uq_emails_mailbox_gmail_message
UNIQUE (mailbox_email, gmail_message_id);

ALTER TABLE emails
ADD CONSTRAINT chk_emails_gmail_message_not_empty
CHECK (gmail_message_id <> '');

ALTER TABLE emails
ADD CONSTRAINT chk_emails_internal_date_positive
CHECK (gmail_internal_date_ms IS NULL OR gmail_internal_date_ms > 0);
```

공용 Gmail 하나만 쓴다면 `UNIQUE (gmail_message_id)`도 작동합니다. 하지만 장기적으로는 `UNIQUE (mailbox_email, gmail_message_id)`가 더 안전합니다.

### 필요한 인덱스

```sql
CREATE INDEX idx_emails_mailbox_received
ON emails (mailbox_email, received_at DESC);

CREATE INDEX idx_emails_thread_received
ON emails (gmail_thread_id, received_at DESC);

CREATE INDEX idx_emails_sender_received
ON emails (sender_email, received_at DESC);

CREATE INDEX idx_emails_label_ids
ON emails USING GIN (gmail_label_ids);
```

`received_at DESC` 단독 인덱스도 좋지만, 운영 화면에서는 보통 “특정 메일함의 최근 메일”을 조회하므로 `(mailbox_email, received_at DESC)`가 더 실용적입니다.

---

## 4.2 `attachments`

### 잘한 점

첨부파일을 `emails`에서 분리한 것은 맞습니다. 이메일 하나에 첨부파일이 여러 개 있을 수 있으므로 `attachments.email_id`로 1:N 관계를 만든 설계는 정확합니다. 첨부 설계에도 “이메일 테이블에 첨부파일 ID 하나만 두면 다중 첨부 메일을 표현하기 어렵다”고 적혀 있는데, 이 판단은 맞습니다. 

### 문제점

`attachment_uid`를 “content hash 기반 권장”으로 둔 점은 위험합니다.

왜냐하면 같은 파일이 여러 메일에 반복 첨부될 수 있기 때문입니다. 예를 들어 같은 발주서 PDF가 A 메일과 B 메일에 모두 붙어 있다면 `content_hash`는 같습니다. 그런데 `attachment_uid`를 content hash로 만들고 `UNIQUE`를 걸면 두 번째 첨부파일 행을 저장하지 못할 수 있습니다.

첨부파일에는 두 가지 개념이 있습니다.

```text
첨부 인스턴스: 이 메일에 붙어 있던 파일 1개
파일 내용물: 실제 바이너리 파일 내용
```

현재 `attachments`는 “첨부 인스턴스” 테이블이어야 합니다. 그러면 `content_hash`는 중복 파일 감지용 인덱스일 뿐, 단독 `UNIQUE`가 되면 안 됩니다.

### 추가할 컬럼

```sql
gmail_part_id TEXT
is_inline BOOLEAN NOT NULL DEFAULT FALSE
content_disposition TEXT
parser_name TEXT
parser_version TEXT
parse_started_at TIMESTAMPTZ
parsed_at TIMESTAMPTZ
ocr_text TEXT
parse_metadata JSONB NOT NULL DEFAULT '{}'::jsonb
```

Gmail MIME 구조에서는 첨부파일이 message part로 들어갑니다. Gmail 문서의 `MessagePart`에는 `partId`, `mimeType`, `filename`, `headers`, `body`, `parts`가 있습니다. 첨부파일은 일반적으로 이 `MessagePart` 구조 안에서 식별됩니다. ([Google for Developers][3])

### 이름을 바꿀 컬럼

```text
file_type → attachment_kind
parsed_text → extracted_text
image_caption → vision_caption
parse_status → extraction_status
```

`parsed_text`도 나쁘지는 않지만, PDF/Excel/OCR/VLM 결과를 통합해서 저장한다면 `extracted_text`가 더 넓고 명확합니다.

### 필요한 제약 조건

```sql
ALTER TABLE attachments
ADD CONSTRAINT uq_attachments_email_part
UNIQUE (email_id, gmail_part_id);

ALTER TABLE attachments
ADD CONSTRAINT chk_attachments_file_size
CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0);

ALTER TABLE attachments
ADD CONSTRAINT chk_attachments_parse_status
CHECK (parse_status IN ('pending', 'running', 'parsed', 'failed', 'skipped'));
```

`gmail_part_id`를 못 얻는 경우가 있다면 다음처럼 대체할 수 있습니다.

```sql
UNIQUE (email_id, filename, file_size_bytes, content_hash)
```

다만 이 방식은 Gmail MIME part ID보다 약합니다.

### 필요한 인덱스

```sql
CREATE INDEX idx_attachments_email_id
ON attachments (email_id);

CREATE INDEX idx_attachments_parse_status_created
ON attachments (parse_status, created_at);

CREATE INDEX idx_attachments_content_hash
ON attachments (content_hash);

CREATE INDEX idx_attachments_extracted_text_fts
ON attachments USING GIN (to_tsvector('simple', coalesce(parsed_text, '')));
```

첨부 텍스트 검색을 FastAPI에서 제공할 계획이면 `parsed_text` 전문 검색 인덱스를 고민할 수 있습니다. 다만 한국어 형태소 검색까지 하려면 별도 검색 엔진이나 한국어 설정을 추가 검토해야 합니다.

---

## 4.3 `analysis_results`

### 잘한 점

LLM 결과를 원본 이메일과 분리한 것은 매우 좋습니다. 모델명과 프롬프트 버전을 저장하는 것도 맞습니다. 같은 이메일이라도 모델/프롬프트가 바뀌면 분석 결과가 달라질 수 있기 때문입니다. 첨부 설계에서도 `analysis_results`는 이메일별 LLM 분석 결과이며, 모델/프롬프트 버전별 이력을 허용한다고 설명합니다. 

### 문제점

`analysis_results`에 **LLM 추천값**과 **최종 확정값**을 같이 두면 안 됩니다.

현재 컬럼을 보면 다음 값들이 있습니다.

```text
email_type
assignee_id
urgency
status
```

이 값들이 “LLM이 추천한 값”인지, “사람이 확정한 값”인지, “현재 업무 상태”인지 애매합니다.

운영에서는 다음이 반드시 분리되어야 합니다.

```text
LLM 추천값        → analysis_results
사람이 확정한 값   → email_work_items 또는 email_final_states
수정 이력          → email_events 또는 audit_logs
```

`analysis_results.status`도 위험합니다. 이것은 “분석 작업 상태”인지 “메일 업무 상태”인지 헷갈립니다. 예를 들어 `completed`는 LLM 분석이 끝났다는 뜻이지, 메일 업무가 끝났다는 뜻이 아닙니다.

### 추가할 컬럼

```sql
analysis_kind TEXT NOT NULL DEFAULT 'initial_classification'
input_hash TEXT
prompt_hash TEXT
routing_policy_version TEXT
llm_request_id TEXT
latency_ms INTEGER
input_tokens INTEGER
output_tokens INTEGER
cost_usd NUMERIC(12,6)
started_at TIMESTAMPTZ
completed_at TIMESTAMPTZ
```

LLM 비용, 장애 원인, 재분석 비교를 위해 호출 로그성 컬럼이 필요합니다. 아주 엄격하게 나누려면 별도 `llm_runs` 테이블로 빼는 것이 더 좋습니다.

### 이름을 바꿀 컬럼

```text
email_type → predicted_email_type
assignee_id → predicted_assignee_id
urgency → predicted_urgency
status → analysis_status
error_message → analysis_error_message
```

이렇게 바꾸면 사람이 확정한 값과 섞이지 않습니다.

### 필요한 제약 조건

```sql
ALTER TABLE analysis_results
ADD CONSTRAINT chk_analysis_confidence
CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));

ALTER TABLE analysis_results
ADD CONSTRAINT chk_analysis_status
CHECK (status IN ('pending', 'running', 'completed', 'failed', 'needs_review'));

ALTER TABLE analysis_results
ADD CONSTRAINT chk_analysis_urgency
CHECK (urgency IN ('low', 'normal', 'high', 'urgent'));
```

현재 `confidence NUMERIC(4,3)`만으로는 1.234 같은 값도 들어갈 수 있습니다. `NUMERIC(4,3)`은 자릿수 제한일 뿐, 0~1 범위 제한이 아닙니다. 반드시 `CHECK`가 필요합니다.

### 필요한 인덱스

```sql
CREATE INDEX idx_analysis_email_created
ON analysis_results (email_id, created_at DESC);

CREATE INDEX idx_analysis_status_created
ON analysis_results (status, created_at);

CREATE INDEX idx_analysis_predicted_assignee
ON analysis_results (assignee_id, created_at DESC);

CREATE INDEX idx_analysis_raw_result_gin
ON analysis_results USING GIN (raw_result);
```

현재 `UNIQUE (email_id, model_name, prompt_version)`는 애매합니다. 재시도, 같은 프롬프트 재실행, 입력 텍스트 변경, 첨부 파싱 변경이 생기면 같은 모델/프롬프트라도 다시 분석해야 할 수 있습니다.

추천은 둘 중 하나입니다.

```sql
-- 같은 입력에 대한 완전 중복 실행만 막고 싶을 때
UNIQUE (email_id, model_name, prompt_version, input_hash)

-- 모든 실행 이력을 남기고 싶을 때
비고유 인덱스만 사용
```

운영에서는 두 번째가 더 안전합니다.

---

## 4.4 `routing_table`

### 잘한 점

담당자 이름, 부서, 이메일, 담당 라벨, 우선순위, 활성 여부를 둔 것은 MVP용 담당자 마스터로는 쓸 수 있습니다. 첨부 설계에서도 `routing_table`을 담당자/부서/역할 저장 테이블로 설명하고 있습니다. 

### 문제점

테이블 이름이 역할과 맞지 않습니다.

현재 `routing_table`은 실제로는 **담당자 테이블**입니다. 그런데 이름은 **라우팅 정책 테이블**처럼 보입니다.

이 차이는 중요합니다.

```text
담당자 테이블: 김철수, 영업팀, kim@example.com
라우팅 정책 테이블: 발주 메일 + 고객사 A + urgent이면 김철수에게 배정
```

현재 `routing_labels TEXT[]`는 간단한 담당 영역 표시에는 좋지만, 조건 기반 라우팅 정책을 표현하기에는 부족합니다.

### 추가할 컬럼

`routing_table`을 `assignees`로 바꾼다는 전제에서 다음을 추천합니다.

```sql
display_name TEXT NOT NULL
email_address TEXT NOT NULL
department TEXT
role_name TEXT
is_active BOOLEAN NOT NULL DEFAULT TRUE
fallback_assignee_id BIGINT
max_daily_assignments INTEGER
timezone TEXT
```

### 이름을 바꿀 테이블/컬럼

```text
routing_table → assignees
assignee_name → display_name
position → role_name
routing_labels → skill_labels 또는 responsibility_labels
```

그리고 별도로 다음 테이블을 둡니다.

```text
routing_rules
```

### 필요한 제약 조건

```sql
ALTER TABLE assignees
ADD CONSTRAINT uq_assignees_email
UNIQUE (email_address);

ALTER TABLE assignees
ADD CONSTRAINT chk_assignees_priority
CHECK (priority >= 0);
```

### 필요한 인덱스

```sql
CREATE INDEX idx_assignees_active_priority
ON assignees (is_active, priority);

CREATE INDEX idx_assignees_department
ON assignees (department);

CREATE INDEX idx_assignees_labels
ON assignees USING GIN (responsibility_labels);
```

---

## 5. 반드시 추가해야 할 테이블

운영 MVP 전에 최소한 아래 5개는 추가하는 것을 권합니다.

## 5.1 `gmail_sync_state`

**왜 필요한가:**
Gmail 수집기가 어디까지 수집했는지 기억해야 합니다. Gmail 공식 동기화 가이드는 최초에는 전체 동기화를 수행하고, 이후에는 `history.list`에 최근 `historyId`를 넘겨 변경분만 가져오는 방식을 설명합니다. 또한 history 기록이 보관 범위를 벗어나면 404가 발생할 수 있고, 이때는 전체 동기화를 다시 수행해야 합니다. ([Google for Developers][5])

**역할:**

```text
공용 Gmail 계정별 마지막 history_id
마지막 전체 동기화 시각
마지막 부분 동기화 시각
watch 만료 시각
동기화 실패 메시지
```

---

## 5.2 `email_work_items`

**왜 필요한가:**
현재 이메일의 최종 업무 상태를 저장해야 합니다. `analysis_results`는 AI 추천 이력이고, `email_work_items`는 사람이 보는 현재 상태입니다.

**예시 상태:**

```text
new
analyzing
needs_review
assigned
notified
in_progress
done
ignored
failed
```

---

## 5.3 `email_events`

**왜 필요한가:**
사람이 분류, 담당자, 긴급도, 상태를 바꾸면 “누가, 언제, 무엇을, 왜 바꿨는지” 남겨야 합니다.

**예시 이벤트:**

```text
classification_changed
assignee_changed
status_changed
manual_note_added
analysis_selected
notification_sent
```

이 테이블이 없으면 운영 중 분쟁이 생깁니다. 예를 들어 “왜 이 발주 메일이 A 담당자에게 갔나?”라는 질문에 답할 수 없습니다.

---

## 5.4 `assignment_history`

**왜 필요한가:**
담당자 배정은 현재값만 있으면 안 됩니다. A에게 갔다가 B로 바뀐 이력을 남겨야 합니다.

**저장해야 할 값:**

```text
email_id
from_assignee_id
to_assignee_id
assigned_by
assignment_source
reason
created_at
```

`assignment_source`는 `llm`, `rule`, `human`, `fallback` 같은 값이면 됩니다.

---

## 5.5 `delivery_log`

**왜 필요한가:**
중복 전달을 막아야 합니다. 담당자에게 Slack, Gmail, Teams, 사내 알림으로 같은 메일을 여러 번 보내면 운영 신뢰가 크게 떨어집니다.

**핵심은 `delivery_key UNIQUE`입니다.**

예:

```text
email:123:assignee:45:channel:gmail:assignment:v1
```

이 키에 `UNIQUE`를 걸면 같은 이메일을 같은 담당자에게 같은 채널로 두 번 보내는 일을 DB가 막습니다.

---

## 6. 가장 위험한 설계 실수 TOP 5

## 1위. `analysis_results`에 최종 확정값까지 넣는 것

**왜 위험한가:**
AI 추천값과 사람이 확정한 값이 섞입니다. 나중에 “LLM이 이렇게 추천했나, 사람이 바꿨나?”를 구분할 수 없습니다.

**어떻게 고칠지:**
`analysis_results`는 `predicted_*` 컬럼만 저장하고, 최종값은 `email_work_items`에 저장하세요.

---

## 2위. Gmail `history_id`, `internal_date`를 제대로 저장하지 않는 것

**왜 위험한가:**
동기화가 끊기거나 서버가 재시작되면 어디서부터 다시 가져와야 하는지 불명확해집니다. Gmail의 `internalDate`는 일반 SMTP 수신 메일 기준으로 Gmail이 받아들인 시각이라 메일 헤더의 `Date`보다 수신 순서 판단에 더 안정적입니다. ([Google for Developers][3])

**어떻게 고칠지:**
`emails`에 `gmail_history_id`, `gmail_internal_date_ms`를 추가하고, 별도 `gmail_sync_state`에 계정별 마지막 `history_id`를 저장하세요.

---

## 3위. 담당자 전달 이력 없이 `assignee_id`만 저장하는 것

**왜 위험한가:**
이미 알림을 보냈는지, 누가 배정했는지, 왜 바뀌었는지 알 수 없습니다. 중복 알림도 막기 어렵습니다.

**어떻게 고칠지:**
`assignment_history`와 `delivery_log`를 추가하세요.

---

## 4위. `routing_table` 이름과 역할이 불일치하는 것

**왜 위험한가:**
지금 테이블은 담당자 목록인데 이름은 라우팅 정책처럼 보입니다. 나중에 정책 조건이 늘어나면 이 테이블이 비대해지고 의미가 흐려집니다.

**어떻게 고칠지:**
`routing_table`은 `assignees`로 바꾸고, 조건 기반 정책은 `routing_rules`로 분리하세요.

---

## 5위. `CHECK` 제약 조건이 부족한 것

**왜 위험한가:**
`urgency`, `status`, `parse_status`, `email_type`에 오타가 들어가도 DB가 막지 못합니다. 프론트엔드 필터, 통계, 배치 작업이 깨집니다.

**어떻게 고칠지:**
운영 상태값에는 반드시 `CHECK`를 추가하세요.

---

## 7. 개선된 ERD와 DDL 예시

## 개선 ERD

```text
gmail_sync_state 1 ─── N emails
emails 1 ─── N attachments
emails 1 ─── N analysis_results
emails 1 ─── 1 email_work_items
emails 1 ─── N email_events
emails 1 ─── N assignment_history
emails 1 ─── N delivery_log

assignees 1 ─── N analysis_results
assignees 1 ─── N email_work_items
assignees 1 ─── N assignment_history
assignees 1 ─── N delivery_log

routing_rules N ─── 1 assignees
```

## 핵심 DDL 예시

```sql
CREATE TABLE gmail_sync_state (
    mailbox_email TEXT PRIMARY KEY,
    last_history_id TEXT,
    last_full_sync_at TIMESTAMPTZ,
    last_partial_sync_at TIMESTAMPTZ,
    watch_expiration_at TIMESTAMPTZ,
    sync_status TEXT NOT NULL DEFAULT 'idle',
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (sync_status IN ('idle', 'running', 'failed', 'needs_full_sync'))
);

CREATE TABLE emails (
    email_id BIGSERIAL PRIMARY KEY,
    mailbox_email TEXT NOT NULL REFERENCES gmail_sync_state(mailbox_email),
    email_uid TEXT NOT NULL UNIQUE,

    gmail_message_id TEXT NOT NULL,
    gmail_thread_id TEXT NOT NULL,
    gmail_history_id TEXT,
    gmail_internal_date_ms BIGINT,
    rfc_message_id TEXT,
    thread_key TEXT,

    sender_email TEXT,
    sender_name TEXT,
    recipient_emails TEXT[] NOT NULL DEFAULT '{}',
    cc_emails TEXT[] NOT NULL DEFAULT '{}',
    gmail_label_ids TEXT[] NOT NULL DEFAULT '{}',

    subject TEXT NOT NULL DEFAULT '',
    snippet TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ,

    body_text TEXT NOT NULL DEFAULT '',
    body_html TEXT,
    body_hash TEXT,
    has_attachments BOOLEAN NOT NULL DEFAULT FALSE,
    raw_headers JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (mailbox_email, gmail_message_id),
    CHECK (gmail_message_id <> ''),
    CHECK (gmail_internal_date_ms IS NULL OR gmail_internal_date_ms > 0)
);

CREATE TABLE attachments (
    attachment_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,

    gmail_part_id TEXT,
    gmail_attachment_id TEXT,
    filename TEXT NOT NULL DEFAULT '',
    file_ext TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT 'other',
    mime_type TEXT,
    file_size_bytes BIGINT,
    content_hash TEXT,
    storage_path TEXT,

    is_inline BOOLEAN NOT NULL DEFAULT FALSE,
    image_caption TEXT,
    parsed_text TEXT,
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT,
    parser_version TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (email_id, gmail_part_id),
    CHECK (file_size_bytes IS NULL OR file_size_bytes >= 0),
    CHECK (parse_status IN ('pending', 'running', 'parsed', 'failed', 'skipped'))
);

CREATE TABLE assignees (
    assignee_id BIGSERIAL PRIMARY KEY,
    display_name TEXT NOT NULL,
    department TEXT,
    role_name TEXT,
    email_address TEXT NOT NULL UNIQUE,
    responsibility_labels TEXT[] NOT NULL DEFAULT '{}',
    priority INTEGER NOT NULL DEFAULT 100,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (priority >= 0)
);

CREATE TABLE analysis_results (
    analysis_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,

    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT,

    summary TEXT NOT NULL DEFAULT '',
    predicted_email_type TEXT NOT NULL DEFAULT '기타',
    predicted_assignee_id BIGINT REFERENCES assignees(assignee_id) ON DELETE SET NULL,
    predicted_urgency TEXT NOT NULL DEFAULT 'normal',
    confidence NUMERIC(4,3),

    business_refs TEXT[] NOT NULL DEFAULT '{}',
    vessel_names TEXT[] NOT NULL DEFAULT '{}',
    due_date DATE,
    total_amount NUMERIC(18,2),
    currency TEXT,
    routing_reason TEXT,

    raw_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    analysis_status TEXT NOT NULL DEFAULT 'completed',
    analysis_error_message TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    CHECK (predicted_urgency IN ('low', 'normal', 'high', 'urgent')),
    CHECK (analysis_status IN ('pending', 'running', 'completed', 'failed', 'needs_review'))
);

CREATE TABLE email_work_items (
    email_id BIGINT PRIMARY KEY REFERENCES emails(email_id) ON DELETE CASCADE,
    selected_analysis_id BIGINT REFERENCES analysis_results(analysis_id) ON DELETE SET NULL,

    final_email_type TEXT,
    final_assignee_id BIGINT REFERENCES assignees(assignee_id) ON DELETE SET NULL,
    final_urgency TEXT NOT NULL DEFAULT 'normal',
    workflow_status TEXT NOT NULL DEFAULT 'new',

    reviewed_by TEXT,
    reviewed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (final_urgency IN ('low', 'normal', 'high', 'urgent')),
    CHECK (workflow_status IN (
        'new',
        'analyzing',
        'needs_review',
        'assigned',
        'notified',
        'in_progress',
        'done',
        'ignored',
        'failed'
    ))
);

CREATE TABLE email_events (
    event_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    event_type TEXT NOT NULL,
    before_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    after_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (actor_type IN ('system', 'llm', 'human')),
    CHECK (event_type <> '')
);

CREATE TABLE assignment_history (
    assignment_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    from_assignee_id BIGINT REFERENCES assignees(assignee_id) ON DELETE SET NULL,
    to_assignee_id BIGINT REFERENCES assignees(assignee_id) ON DELETE SET NULL,
    assigned_by TEXT,
    assignment_source TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (assignment_source IN ('llm', 'rule', 'human', 'fallback'))
);

CREATE TABLE delivery_log (
    delivery_id BIGSERIAL PRIMARY KEY,
    email_id BIGINT NOT NULL REFERENCES emails(email_id) ON DELETE CASCADE,
    assignee_id BIGINT REFERENCES assignees(assignee_id) ON DELETE SET NULL,
    channel TEXT NOT NULL,
    delivery_key TEXT NOT NULL UNIQUE,
    delivery_status TEXT NOT NULL DEFAULT 'pending',
    sent_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CHECK (channel IN ('gmail', 'slack', 'teams', 'web')),
    CHECK (delivery_status IN ('pending', 'sent', 'failed', 'skipped'))
);

CREATE INDEX idx_emails_mailbox_received
ON emails (mailbox_email, received_at DESC);

CREATE INDEX idx_emails_thread_received
ON emails (gmail_thread_id, received_at DESC);

CREATE INDEX idx_emails_sender_received
ON emails (sender_email, received_at DESC);

CREATE INDEX idx_emails_recipient_emails
ON emails USING GIN (recipient_emails);

CREATE INDEX idx_attachments_email_id
ON attachments (email_id);

CREATE INDEX idx_attachments_parse_status_created
ON attachments (parse_status, created_at);

CREATE INDEX idx_analysis_email_created
ON analysis_results (email_id, created_at DESC);

CREATE INDEX idx_work_items_status_updated
ON email_work_items (workflow_status, updated_at DESC);

CREATE INDEX idx_work_items_assignee_status
ON email_work_items (final_assignee_id, workflow_status);

CREATE INDEX idx_events_email_created
ON email_events (email_id, created_at DESC);

CREATE INDEX idx_delivery_email_assignee
ON delivery_log (email_id, assignee_id);
```

---

## 8. FastAPI 개발 순서

## 1단계. Gmail 수집 API/배치부터 만들기

먼저 DB에 안정적으로 메일이 들어와야 합니다.

추천 순서:

```text
POST /internal/gmail/full-sync
POST /internal/gmail/partial-sync
POST /internal/gmail/webhook
GET  /internal/gmail/sync-state
```

이 단계에서 반드시 검증할 것:

```text
같은 gmail_message_id를 두 번 저장하지 않는가
history_id가 갱신되는가
수집 실패 후 재시도해도 중복 행이 생기지 않는가
```

---

## 2단계. 이메일 목록/상세 조회 API

프론트엔드의 기본 화면입니다.

```text
GET /emails
GET /emails/{email_id}
GET /emails/{email_id}/attachments
GET /emails/{email_id}/events
```

목록 필터는 처음부터 넣는 게 좋습니다.

```text
status
assignee_id
email_type
urgency
sender_email
received_from
received_to
has_attachments
```

---

## 3단계. 첨부파일 파싱 작업 API

첨부 파싱은 실패가 잦으므로 상태 관리가 중요합니다.

```text
POST /attachments/{attachment_id}/extract
POST /attachments/extract-pending
GET  /attachments?parse_status=failed
```

---

## 4단계. LLM 분석 API

LLM 결과는 바로 최종값으로 쓰지 말고 `analysis_results`에 저장합니다.

```text
POST /emails/{email_id}/analyze
GET  /emails/{email_id}/analysis-results
POST /emails/{email_id}/analysis-results/{analysis_id}/select
```

`select`를 호출하면 `email_work_items.selected_analysis_id`와 기본 최종값을 갱신하고, `email_events`에 기록합니다.

---

## 5단계. 사람 검수/수정 API

운영자가 쓰는 가장 중요한 API입니다.

```text
PATCH /work-items/{email_id}
POST  /work-items/{email_id}/assign
POST  /work-items/{email_id}/mark-done
POST  /work-items/{email_id}/ignore
```

이 API들은 반드시 `email_events`, `assignment_history`를 함께 남겨야 합니다.

---

## 6단계. 담당자 알림/전달 API

중복 전달 방지가 핵심입니다.

```text
POST /work-items/{email_id}/notify-assignee
GET  /work-items/{email_id}/delivery-log
```

전달 전에 `delivery_key`를 생성하고 `delivery_log.delivery_key UNIQUE`에 먼저 insert하세요. insert가 실패하면 이미 보낸 것으로 판단하고 다시 보내지 않습니다.

---

## 7단계. 담당자/라우팅 관리 API

```text
GET    /assignees
POST   /assignees
PATCH  /assignees/{assignee_id}

GET    /routing-rules
POST   /routing-rules
PATCH  /routing-rules/{rule_id}
```

초기에는 `assignees.responsibility_labels`만으로 시작해도 됩니다. 다만 테이블 이름은 지금부터 `assignees`로 바꾸는 편이 좋습니다.

---

## 9. 수정 체크리스트

## `emails`

* [ ] `mailbox_email` 컬럼 추가
* [ ] `gmail_history_id` 컬럼 추가
* [ ] `gmail_internal_date_ms` 컬럼 추가
* [ ] `gmail_label_ids TEXT[]` 컬럼 추가
* [ ] `fetched_at` 컬럼 추가
* [ ] `received_at`의 기준을 Gmail `internalDate` 기반으로 명시
* [ ] `UNIQUE (gmail_message_id)`를 `UNIQUE (mailbox_email, gmail_message_id)`로 변경
* [ ] `(mailbox_email, received_at DESC)` 인덱스 추가
* [ ] `(gmail_thread_id, received_at DESC)` 인덱스 추가

## `attachments`

* [ ] `attachment_uid`를 content hash 단독 기반으로 만들지 않기
* [ ] `gmail_part_id` 컬럼 추가
* [ ] `UNIQUE (email_id, gmail_part_id)` 추가
* [ ] `content_hash`는 단독 `UNIQUE`가 아니라 일반 인덱스로 유지
* [ ] `parse_status`에 `running` 추가
* [ ] `parse_status` `CHECK` 추가
* [ ] `parser_version`, `parsed_at`, `parse_started_at` 추가
* [ ] `parsed_text`를 `extracted_text`로 바꿀지 검토

## `analysis_results`

* [ ] `email_type`을 `predicted_email_type`으로 변경
* [ ] `assignee_id`를 `predicted_assignee_id`로 변경
* [ ] `urgency`를 `predicted_urgency`로 변경
* [ ] `status`를 `analysis_status`로 변경
* [ ] 최종 확정값을 이 테이블에서 제거
* [ ] `confidence` 0~1 `CHECK` 추가
* [ ] `predicted_urgency` `CHECK` 추가
* [ ] `analysis_status` `CHECK` 추가
* [ ] `input_hash`, `prompt_hash`, `routing_policy_version` 추가
* [ ] `UNIQUE (email_id, model_name, prompt_version)`를 재검토

## `routing_table`

* [ ] `routing_table`을 `assignees`로 변경
* [ ] `assignee_name`을 `display_name`으로 변경
* [ ] `position`을 `role_name`으로 변경
* [ ] `routing_labels`를 `responsibility_labels`로 변경
* [ ] 조건 기반 라우팅이 필요하면 `routing_rules` 추가
* [ ] `(is_active, priority)` 인덱스 추가

## 신규 테이블

* [ ] `gmail_sync_state` 추가
* [ ] `email_work_items` 추가
* [ ] `email_events` 추가
* [ ] `assignment_history` 추가
* [ ] `delivery_log` 추가
* [ ] 필요 시 `routing_rules` 추가
* [ ] 필요 시 `llm_runs` 또는 LLM 호출 로그 컬럼 추가

## 운영 안정성

* [ ] 모든 상태 컬럼에 `CHECK` 추가
* [ ] 사람이 수정하는 모든 API에서 `email_events` 기록
* [ ] 담당자 변경 시 `assignment_history` 기록
* [ ] 담당자 알림 전송 전 `delivery_log.delivery_key`로 중복 확인
* [ ] Gmail partial sync 실패 시 full sync로 전환하는 상태값 추가
* [ ] 프론트엔드는 `analysis_results`가 아니라 `email_work_items`를 기준으로 목록을 표시

최종 판단은 이렇습니다. **지금 설계는 원천 데이터와 LLM 결과 저장의 출발점으로는 좋지만, 운영 MVP로 쓰려면 “현재 업무 상태”와 “이력 테이블”을 반드시 분리해야 합니다.** 이 수정 없이 개발을 시작하면 FastAPI와 프론트엔드가 `analysis_results`에 과도하게 의존하게 되고, 나중에 사람 검수·재배정·중복 알림 방지 로직을 고칠 때 비용이 크게 늘어납니다.

[1]: https://www.postgresql.org/docs/current/gin.html "PostgreSQL: Documentation: 18: 65.4. GIN Indexes"
[2]: https://www.postgresql.org/docs/current/datatype-json.html "PostgreSQL: Documentation: 18: 8.14. JSON Types"
[3]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages "REST Resource: users.messages  |  Gmail  |  Google for Developers"
[4]: https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list "Method: users.messages.list  |  Gmail  |  Google for Developers"
[5]: https://developers.google.com/workspace/gmail/api/guides/sync?utm_source=chatgpt.com "Synchronize clients with Gmail"
