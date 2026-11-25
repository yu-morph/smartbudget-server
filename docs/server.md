# 서버 실행 및 설정

Python 3.14를 사용합니다. 구현해야 할 전체 API 계약은 [openapi.json](openapi.json)을 기준으로 합니다. 인증 세부 설명은 [authentication.md](authentication.md), 개발 검증은 [harness.md](harness.md)를 참고합니다.

## 사전 준비
본 프로젝트에서는 패키지 정합성을 유지하고, CI 안정성 및 venv 사용성을 유지하기 위해 uv 패키지 매니저를 사용합니다.
시스템에 uv 패키지 매니저가 설치되지 않은 상태라면 터미널에 아래 명령을 입력하여 설치합니다.

### Windows
```sh
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Linux, macOS
```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 로컬 실행

프로젝트 루트에서 다음 명령들을 실행합니다.

```sh
uv python install 3.14
uv sync --frozen
cp .env.example .env
```

`uv sync`는 기본적으로 개발 의존성 그룹까지 설치합니다. 생성한 키를 로컬 `.env`의 JWT_SECRET에 넣은 다음 실행합니다. 운영 환경처럼 런타임 의존성만 설치할 때는 `uv sync --frozen --no-dev`를 사용합니다. `uv.lock`은 재현 가능한 설치를 위해 커밋하며, 의존성을 변경한 뒤에는 `uv lock`으로 갱신합니다.

아래 명령어들 중 하나를 사용하여 최소 43자의 랜덤 문자열을 생성합니다. 그리고 해당 문자열의 `.env` 파일의 `JWT_SECRET` 항목에 붙여넣습니다.

```sh
# Method 1(공통)
uv run --frozen python -c "import secrets; print(secrets.token_urlsafe(32))"

# Method 2(Linux, macOS)
openssl rand -base64 32
```

초기 세팅이 완료되었다면 아래 명령을 입력하여 서버를 실행합니다.

```sh
uv run --frozen python -m smartbudget_server
```

기본 주소는 `http://localhost:8000`입니다. 실행 중 서버의 `/docs`와 `/openapi.json`은 현재 구현 상태를 보여주며, `docs/openapi.json`은 구현해야 할 목표 계약입니다. 설정 오류는 시작을 중단하며 임시 서명키로 실행하지 않습니다.

## Docker 실행 및 자동 업데이트

먼저 `.env.example`을 `.env`로 복사하고 `JWT_SECRET`을 설정합니다. 로컬 소스에서 이미지를 빌드해 실행하려면 다음 명령을 사용합니다.

### Linux, macOS

```sh
BUILD_VERSION=$(git rev-parse HEAD) BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ) docker compose up -d --build
```

### Windows PowerShell

```powershell
$env:BUILD_VERSION = git rev-parse HEAD
$env:BUILD_TIMESTAMP = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
docker compose up -d --build
```

API는 지정된 포트로 공개되고 SQLite 파일은 호스트 경로 기준 `./data/smartbudget.sqlite3`에 보존됩니다. `docker compose down`은 이 볼륨을 삭제하지 않으며, 데이터를 함께 삭제하려는 경우에만 `docker compose down --volumes`를 사용합니다.

원격 저장소의 이미지를 기반으로 컨테이너를 실행하려면 다음 명령을 사용합니다.

```sh
docker compose pull
docker compose up -d
```

### Azure 개발 서버 배포

Azure VM의 실제 배포 상태, CI/CD의 활성 여부와 실행 흐름, 버전 확인 및 수동 복구 방법은 [Azure 개발 서버 배포 및 CI/CD 운영](azure-deployment-design.md)에 기록합니다. 이 문서는 로컬 서버 실행·설정과 앱 구조를 설명하고, 배포 운영 사실은 해당 문서를 기준으로 합니다.

유지보수 및 기타 목적으로 인해 서버를 잠시 다운시켜야 할 경우에는 다음 명령을 사용합니다.

```sh
docker compose down
```

## 서버 버전 API

인증 없이 `GET /api/v1/version`을 호출하면 실행 중인 서버의 빌드 정보를 공통 응답 봉투로 반환합니다. 쿼리 파라미터와 요청 본문은 허용하지 않습니다.

```json
{
  "status": 200,
  "code": "SUCCESS",
  "message": "The request has been accepted and processed.",
  "data": {
    "build_timestamp": "2026-09-23T21:30:00+09:00",
    "version": "18ae094000000000000000000000000000000000",
    "channel": "remote_registry"
  }
}
```

`build_timestamp`는 Docker 이미지 빌드 시각이며, 직접 실행에서는 앱 생성 시각입니다. `version`은 Git 커밋 해시 또는 Docker 이미지 해시를 사용합니다. Git 정보를 읽을 수 없는 직접 실행 또는 빌드 인자를 생략한 로컬 Docker 빌드에서는 `unknown`입니다. 위 로컬 Docker 빌드 명령은 현재 시각과 커밋을 빌드 인자로 전달하며, GHCR 게시 워크플로우는 빌드 시각과 `github.sha`를 자동 전달합니다.

`channel` 값은 다음과 같습니다.

| 값 | 의미 |
| --- | --- |
| `local_direct` | Python으로 로컬 소스를 직접 실행 |
| `local_docker` | 로컬 소스에서 빌드한 Docker 이미지 실행 |
| `remote_registry` | GHCR 게시 워크플로우가 빌드한 원격 저장소 이미지 실행 |

## 환경 설정

우선순위는 **시스템 환경 변수 > 실행 디렉터리의 .env > 기본값**입니다. DB 상대 경로는 설정을 읽는 작업 디렉터리 기준의 절대 경로로 고정합니다. 사용하지 않는 .env 항목은 무시합니다.

| 변수 | 기본값 | 규칙 |
| --- | --- | --- |
| JWT_SECRET | 없음, 필수 | 최소 43자; secrets.token_urlsafe(32)로 무작위 생성 |
| JWT_ISSUER | smartbudget-server | 비어 있지 않은 발급자 |
| JWT_AUDIENCE | smartbudget-server-api | 비어 있지 않은 수신 대상 |
| TOKEN_SECONDS | 432000 | 양의 정수 초; 기본 5일 |
| DATABASE_PATH | ./data/smartbudget.sqlite3 | SQLite 파일 경로 |
| HOST | 127.0.0.1 | 비어 있지 않은 바인딩 주소; 공유 `.env.example`과 컨테이너는 0.0.0.0 사용 |
| PORT | 8000 | 1–65535 정수 |
| WEB_ORIGINS | [] | 정확한 HTTP/HTTPS origin의 JSON 배열 |
| AZURE_OPENAI_ENDPOINT | 없음 | 소비 분석에 사용할 Azure OpenAI HTTPS 리소스 주소 |
| AZURE_OPENAI_KEY | 없음 | 소비 분석 서비스의 비밀 키 |
| AZURE_OPENAI_DEPLOYMENT | 없음 | 소비 분석 모델 배포 이름 |
| AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT | 없음 | 영수증 분석에 사용할 Document Intelligence HTTPS 리소스 주소 |
| AZURE_DOCUMENT_INTELLIGENCE_KEY | 없음 | 영수증 분석 서비스의 비밀 키 |

위 Azure 설정은 선택 항목이지만 해당 기능의 실제 외부 호출에 필요합니다. 값이 없으면 소비 분석은 제공자 오류를 반환하고 OCR은 파일별 실패로 반환합니다. 가상 데이터로 로컬에서 Azure 호출을 확인했으며, 개발 서버 배포 및 운영 환경은 아직 검증되지 않았습니다.

소비 분석 요청은 분석 기간의 거래일·유형·카테고리·금액과 요청에 포함된 선택적 사용자 정보·기사 제목을 한 줄 JSON으로 Azure에 전달합니다. 새 리포트는 XML 템플릿을 사용합니다. 초기 Markdown 리포트가 저장되어 있다면 기존 내용이 상세 응답에 반환될 수 있습니다. 앱은 읽을 수 있는 XML 섹션만 표시합니다. 필드와 섹션 계약은 [OpenAPI 명세](openapi.json)를 참고합니다.

프롬프트는 일반 사용자가 모바일에서 빠르게 읽을 수 있도록 짧고 친근한 해요체를 사용합니다. 자료가 적어도 거래와 사용자 정보, 기사 제목으로 해석할 수 있는 내용을 설명하고 관련 통계나 일반적인 소비 경향으로 보완할 수 있습니다. 금융 조언에 투자 종목이나 분야를 제시할 수 있으며 손실 위험과 감당할 수 있는 예산 범위를 함께 설명합니다.

리포트에는 해석한 내용과 조언을 직접 제시합니다. 입력 항목의 제공 여부나 프롬프트 지시 사항은 언급하지 않습니다.

소비 분석에서 `502 REPORT_PROVIDER_ERROR`의 `message`는 서버의 AI 설정 문제, 통신 실패, Azure의 인증·접근·배포·호출 한도 등 HTTP 오류, 또는 비어 있거나 형식이 잘못된 리포트 응답을 구분합니다. Azure의 HTTP 오류는 메시지에 상태 번호를 표시합니다. Azure 4xx·5xx 응답에 본문이 있으면 `data.provider_error_body`에 텍스트로 담고 서버 API 키는 마스킹합니다. 시간 초과는 `504 REPORT_PROVIDER_TIMEOUT`을 반환합니다. 클라이언트는 `code`로 분기하고 `message`를 화면에 표시할 수 있습니다.

Azure 호출 시도 이후 실패한 502·504 응답의 `data.request_prompt_fulltext`는 요청에 사용한 `system`·`user` 메시지 배열을 JSON으로 직렬화한 문자열입니다. 거래 내역과 앱 입력이 포함되며 인증된 요청자에게 반환합니다. Azure 호출 전 설정 검증에서 실패하면 `data`는 null입니다.

소비 분석 설정이 모두 있으면 단일 서버 프로세스에서 한국 시간 기준 예약 작업을 실행합니다. 예약 작업은 해당 기간의 거래를 사용하며 앱 입력은 비어 있습니다. 작업 상태와 마지막 예약 날짜는 SQLite에 기록합니다. 서버가 재시작되면 첫 설정 이후 누락된 날짜의 예약을 생성합니다. 실행 중 중단되어 외부 호출 결과를 알 수 없는 작업은 `unknown`으로 남기고 자동 재호출하지 않습니다.

### 중단된 외부 작업 복구

먼저 모든 서버 프로세스를 중지하고 SQLite DB를 백업합니다. 아래 명령의 `--confirm-server-stopped`는 운영자가 서버를 중지했다는 확인이며 실제 실행 여부를 자동 검사하지 않습니다. 살아 있는 서버에서 실행하면 중복 호출이나 데이터 충돌이 발생할 수 있습니다. 조회 결과에는 사용자 ID와 요청 키가 있으므로 외부에 공유하지 않습니다.

```sh
uv run --frozen python -m smartbudget_server.recovery --confirm-server-stopped list
uv run --frozen python -m smartbudget_server.recovery --confirm-server-stopped request --user-id <사용자ID> --operation <ocr|report> --key <요청키>
uv run --frozen python -m smartbudget_server.recovery --confirm-server-stopped scheduled --user-id <사용자ID> --period-type <daily|weekly|monthly|yearly> --start <YYYY-MM-DD> --decision <retry|done>
```

`request`는 미완료 키만 해제하며 성공 응답은 삭제하지 않습니다. 리포트 잠금은 다른 요청의 것일 수 있어 삭제하지 않으며, 만료까지 최대 120초 기다려야 할 수 있습니다. 예약 작업은 결과가 이미 저장됐는지 확인한 뒤 `done`으로 종료하거나, 필요할 때만 `retry`로 재요청을 허용합니다. 외부 호출이 이미 처리됐을 수 있으므로 키를 해제하거나 재시도하면 중복 호출과 비용이 발생할 수 있습니다. 복구 후 서버를 다시 시작합니다.

Docker 개발 서버에서는 해당 서비스 컨테이너를 중지한 뒤 `docker compose run --rm --no-deps --entrypoint python smartbudget-server -m smartbudget_server.recovery --confirm-server-stopped list`와 같은 방식으로 실행합니다. 키 또는 예약 복구도 `list` 대신 위 명령의 `request` 또는 `scheduled` 인자를 전달합니다.

웹 예: `WEB_ORIGINS=["http://localhost:3000","https://example.com"]`. 경로·쿼리·사용자 정보·와일드카드는 허용하지 않습니다. CORS는 GET·POST·PUT·PATCH·DELETE, Authorization·Content-Type·Idempotency-Key를 허용하고 Retry-After를 노출합니다. 쿠키 인증을 사용하지 않습니다. CORS는 브라우저 정책이며 안드로이드 인증을 대체하지 않습니다.

`.env`, DB·저널 파일은 Git에서 제외합니다. `.env.example`만 공유합니다. 키·비밀번호·토큰을 로그에 남기지 않습니다. 서명키를 바꾸면 기존 토큰은 검증에 실패합니다. 발급자·수신 대상 변경도 기존 토큰 검증에 영향을 줍니다.

## SQLite와 처리량

시작 시 DB 상위 폴더와 필요한 테이블을 생성합니다. 기존 SQLite의 `users` 테이블에 `profile_image`가 없으면 시작 시 nullable BLOB 컬럼을 자동으로 추가합니다. 기존 계정은 이미지 미설정 상태로 유지됩니다. `create_all`은 기존 테이블의 제약을 변경하지 않습니다. 기존 DB에 리포트 사용자·기간 유일성 인덱스를 추가하려면 서버를 중지하고 DB를 백업한 뒤 프로젝트 루트에서 `uv run --frozen python -m scripts.migrate_report_uniqueness`를 실행합니다. 중복 리포트가 있으면 이전은 중단되며 데이터를 자동 삭제하지 않습니다.

NullPool로 요청 후 DB 연결을 반환합니다. 쓰기는 BEGIN IMMEDIATE로 직렬화하여 로그인 실패 집계·비밀번호 변경·토큰 발급의 경합을 막습니다. 잠금 대기는 최대 5초이며 요청 중 DB 잠금·사용 불가 오류는 503으로 반환합니다. 시작 시 DB를 열거나 테이블을 생성하지 못하면 서버 시작이 실패합니다.

공식 실행 진입점은 Uvicorn 단일 worker, 동시 처리 제한 16, 접근 로그 비활성화입니다. Argon2 해시·검증은 프로세스당 한 번에 하나만 실행하여 피크 메모리를 줄입니다. SQLite 쓰기 잠금 중 로그인 해시 검증도 수행하므로 인증 쓰기 처리량은 직렬 처리 속도에 제한됩니다. 높은 처리량이 필요해질 때 DB·잠금 전략을 재검토합니다. 만료된 로그인 제한 기록은 다음 로그인에서 정리하며 전역 기록 개수 제한은 없습니다.

Uvicorn 자체의 동시 처리 제한으로 반환되는 503은 인증 API의 JSON 응답 형식과 다를 수 있습니다. 애플리케이션에는 HTTPS 프록시와 자동 백업 기능이 포함되어 있지 않습니다. Azure 배포와 HTTPS 구성은 [Azure 개발 서버 배포 및 CI/CD 운영](azure-deployment-design.md)을 참고하세요. DB 파일을 백업할 때는 서버를 완전히 중지한 뒤 복사하고, 복원·업데이트 전에도 서버를 중지합니다.

## 거래 일괄 처리

`POST /api/v1/transaction/batch`와 `DELETE /api/v1/transaction/batch`는 Bearer 인증을 사용합니다. query에 `start_date`와 `end_date`를 `YYYY-MM-DD` 형식으로 모두 전달합니다. 목록 조회와 동일하게 각 날짜를 포함하며 하루만 지정할 때는 같은 날짜를 사용합니다.

- POST는 `{"items":[...]}`의 거래 1~500건을 원자적으로 저장합니다. 한 항목의 날짜나 필드라도 유효하지 않으면 전체 요청이 400으로 실패합니다. 성공 시 201과 입력 순서대로 저장된 거래 배열을 반환합니다. 재시도 시 같은 `Idempotency-Key`와 같은 데이터를 사용하면 24시간 동안 최초 성공 응답을 돌려줍니다.
- DELETE는 본문 없이 선택 기간의 본인 거래를 삭제하고 `data.deleted_count`와 `data.next_cursor`를 반환합니다. `limit`을 생략하면 대상 전체를 삭제합니다. `limit`을 1~100으로 지정하면 목록 조회와 같은 날짜·ID 내림차순으로 한 페이지를 삭제합니다. 다음 페이지는 같은 기간과 `limit`에 직전 응답의 `next_cursor`를 전달합니다. 페이지 삭제는 첫 페이지를 재전송하면 이어지는 거래가 선택될 수 있습니다.
- 두 일괄 API는 거래에 실제로 변경이 생긴 날짜를 포함하는 기존 리포트를 같은 DB 트랜잭션에서 갱신 대상으로 표시합니다.

## 검증

```sh
uv sync --frozen
uv run --frozen python scripts/harness.py verify
uv run pytest
```

수동 verify는 Ruff·docstring·pytest 검사입니다. 별도 Luna 검토와 지문 확인은 [종료 훅 절차](harness.md)에 따릅니다. 테스트는 임시 DB·테스트용 키를 사용하며 실제 사용자 데이터를 요구하지 않습니다.

## 소스 구조와 기능 추가

```text
smartbudget_server/
├── __main__.py       # 서버 실행
├── main.py           # 앱 조립·라우터 등록·DB 수명주기
├── config.py         # 환경 설정
├── database.py       # 공통 Base·DB 연결·트랜잭션
├── http.py           # 공통 응답·오류 처리·OpenAPI
├── auth/
│   ├── __init__.py
│   ├── router.py     # 인증 API·토큰 전달 규칙·인증 오류 처리
│   ├── schemas.py    # 인증 요청·응답 모델
│   ├── service.py    # 가입·로그인·계정 변경
│   ├── security.py   # 비밀번호 검증·해시·JWT
│   └── models.py     # User·LoginAttempt 테이블
├── monthly_budget/
│   ├── router.py     # 월별 예산 조회·저장 API 스텁
│   ├── schemas.py    # 월별 예산 요청·응답 모델
│   └── models.py     # 사용자별 월 예산 테이블
├── transaction/
│   ├── router.py     # 거래 CRUD 및 일괄 생성·삭제 API
│   ├── schemas.py    # 거래 요청·응답 모델
│   └── models.py     # 거래 테이블
├── report/
│   ├── router.py     # 소비 분석 생성·조회·삭제 API
│   ├── schemas.py    # 소비 분석 요청·응답 모델
│   ├── prompt.py     # XML 템플릿과 한 줄 거래 입력
│   └── models.py     # 소비 분석 리포트 테이블
├── proxy/
│   ├── router.py     # 영수증 OCR 프록시 API
│   ├── schemas.py    # 파일별 OCR 응답 모델
│   └── models.py     # 영속 모델을 사용하지 않음을 명시
└── version/
    ├── __init__.py
    ├── router.py     # 서버 버전 조회 API
    ├── schemas.py    # 빌드 정보·응답 모델
    └── service.py    # Docker 빌드 정보 또는 로컬 Git 메타데이터 로딩
```

라우트는 Request의 app.state에서 해당 앱의 설정·엔진·버전 정보를 읽습니다. 모듈 전역에 앱별 DB나 설정을 저장하지 않습니다. main.py는 공통 HTTP 처리, 버전·인증 라우터, 가장 바깥의 CORS 순으로 구성합니다. 인증 전송 검사에서 즉시 반환하는 오류에도 CORS가 적용됩니다.

새 기능은 auth/와 같은 수준의 디렉토리로 묶고 main.py에서 라우터를 등록합니다. 기능별 models.py는 공통 database.Base를 상속합니다. OCR 결과는 거래로 자동 저장하지 않지만 중복 방지 응답은 별도 테이블에 보관합니다. build_engine은 연결만 구성하고 테이블 초기화는 앱 수명주기에서 수행합니다. 별도의 repository·추상 인터페이스 계층은 없습니다.
