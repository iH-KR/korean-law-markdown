# korean-law-markdown

`korean-law-markdown`은 법제처 Open API가 제공하는 **대한민국 법령·행정규칙 XML 원문**을 수집하고, AI 활용에 적합한 Markdown 문서로 정리하는 Python 도구입니다.

이 프로젝트는 XML에서 글자만 꺼내는 단순 변환기가 아닙니다. 법령의 장·절·조·항·호·목, 부칙, 별표·서식, 문서 메타데이터와 원문 링크를 가능한 한 보존하면서 Markdown 문법과 충돌하는 표현을 선별적으로 정리합니다. 생성된 문서는 일반 문서 열람뿐 아니라 NotebookLM, 문서 검색, 내부 지식베이스와 RAG의 입력 자료로 활용할 수 있습니다.

> 이 저장소는 법령 정보를 더 편리하게 수집·정리하기 위한 도구입니다. 생성 결과는 공식 법령 원문이나 법령 해석을 대체하지 않으며, 실제 업무에 사용할 때에는 국가법령정보센터의 최신 원문과 시행일을 반드시 확인해야 합니다.

## 왜 XML 원본을 다시 정리하나요?

법령 API의 XML은 정확한 구조와 풍부한 정보를 제공하지만, 그대로는 문서 검색 및 AI 도구 활용에 불편합니다. 단순히 태그만 제거하면 법령 고유의 계층과 문단 경계가 무너지거나, 원문의 번호와 들여쓰기가 Markdown 문법으로 잘못 해석될 수도 있습니다.

| XML 원본에서 생기는 문제 | 이 프로젝트의 처리 방식 | 얻는 장점 |
| --- | --- | --- |
| 장·절·조·항·호·목이 여러 요소로 나뉨 | 법령 계층에 맞는 제목과 문단으로 재구성 | 긴 문서도 조문 단위로 빠르게 탐색 |
| 별표 내용이 여러 CDATA 조각과 불규칙한 줄바꿈으로 분리됨 | 내용과 순서를 보존하며 문단 경계를 정규화 | 표·서식 관련 내용을 본문 흐름에서 확인 |
| `1.`, 들여쓰기, 백틱 등이 Markdown 목록·코드로 오인될 수 있음 | 법령 표기는 유지하고 충돌하는 문법만 선별적으로 중화 | 원문 훼손을 줄이면서 안정적으로 렌더링 |
| 메타데이터와 첨부 링크가 본문과 떨어져 있음 | 문서 ID, 공포·발령일, 시행일, 소관부처와 링크를 함께 기록 | 최신성 확인과 원문 추적이 쉬움 |
| 단위 호환문자 때문에 `65mm` 검색이 `65 ㎜`와 어긋남 | Markdown 생성 시 NFKC 검색 정규화를 항상 적용하고 원문은 XML로 보존 | 중복 산출물 없이 검색과 원문 대조를 모두 지원 |
| 대량 처리의 성공·실패 여부를 눈으로 확인하기 어려움 | 원본 XML 캐시, 매니페스트, 실패 목록, 실행 로그 생성 | 재현·검수·재처리가 쉬움 |

결과적으로 법령마다 일관된 구조를 확보할 수 있습니다. 특히 정확한 제목·조문 경계·시행 정보를 유지하므로 키워드 검색과 근거 확인이 편해지고, AI가 서로 다른 조문을 한 문단으로 오인할 가능성도 줄일 수 있습니다.

## 주요 기능

- `법령`, `행정규칙`, `3단비교` 세 유형을 CSV 목록으로 관리하고 `--input`으로 목록 선택
- 법제처 Open API XML 원문 수집 및 로컬 캐시 저장
- 법령·행정규칙의 기본정보, 조문, 부칙, 별표·서식과 첨부 링크를 Markdown으로 구성
- 법률·시행령·시행규칙의 인용조문 3단비교 문서 생성
- 법령식 번호와 들여쓰기를 보존하면서 Markdown 오인 요소를 선별적으로 처리
- 항·호·목을 빈 줄로 구분해 NotebookLM 등 Markdown 뷰어에서도 각 열거 문단을 유지
- 실제 존재하는 편·장·절·관만 따라 H2부터 헤딩 레벨을 연속 배치
- 한글 YAML frontmatter 하나에 문서정보와 수집 provenance를 통합 기록
- 모든 Markdown에 `NFKC_SEARCH_V1` 검색 정규화를 기본 적용하고 원문은 XML로 보존
- API 응답 명칭과 요청 명칭의 차이, 조문 수, 별표·링크 수 등을 매니페스트에 기록
- HTTP 429 및 일시적인 서버 오류 재시도
- API 인증값을 로그와 매니페스트에서 마스킹
- 전체 재수집, 캐시 전용 재생성, 누락분만 수집하는 실행 모드 제공

## 준비 사항

- Python 3
- 법제처 국가법령정보 공동활용 Open API 인증값(`OC`)
- 인터넷 연결(API를 호출하는 실행 모드에 한함)

API 이용 신청과 상세 규격은 [국가법령정보 공동활용 Open API 활용가이드](https://open.law.go.kr/LSO/openApi/guideResult.do?htmlName=mobLsInfoGuide)에서 확인할 수 있습니다.

## 빠른 시작

### 1. 저장소 내려받기 및 가상환경 구성

Windows PowerShell 기준입니다.

```powershell
git clone https://github.com/iH-KR/korean-law-markdown.git
cd korean-law-markdown

py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

macOS 또는 Linux에서는 가상환경 활성화 명령만 다음과 같이 바꾸면 됩니다.

```bash
source .venv/bin/activate
```

### 2. Open API 인증값 설정

제공된 예제 파일을 복사해 `.env`를 만들고 발급받은 값을 입력합니다.

```powershell
Copy-Item .env.example .env
```

```dotenv
LAW_OC=발급받은_API_인증값
```

`.env`는 Git 추적에서 제외되어 있습니다. 인증값을 코드, CSV, README 또는 커밋 기록에 직접 넣지 마세요.

### 3. 수집 대상 작성

공개 예제를 복사해 실제 수집 목록인 `law_targets.csv`를 만듭니다.

```powershell
Copy-Item law_targets.example.csv law_targets.csv
```

`law_targets.csv`의 필수 열은 `문서유형`, `명칭`입니다. 파일은 UTF-8 형식으로 저장하는 것을 권장합니다.

```csv
문서유형,명칭
법령,행정기본법
법령,공공기관의 정보공개에 관한 법률
3단비교,건축법
```

`law_targets.csv`는 사용자별 작업 목록이므로 Git 추적에서 제외됩니다. 공개 저장소에는 일반적인 사용 예만 담은 `law_targets.example.csv`가 포함됩니다.

- `법령`: 법률·시행령·시행규칙 등 법령 본문을 수집합니다.
- `행정규칙`: 고시·훈령·예규 등 행정규칙 본문을 수집합니다.
- `3단비교`: 법률과 하위 법령의 인용조문 비교 자료를 생성합니다. 현재 API의 `thdCmp`, `knd=1` 방식으로 동작합니다.
- `명칭`은 API의 `LM` 검색값으로 그대로 사용되므로 국가법령정보센터의 정확한 명칭을 입력하는 것이 좋습니다.
- 동일한 `문서유형 + 명칭` 조합은 최초 한 건만 처리합니다.

### 4. 실행

처음 실행하거나 최신 원문을 모두 다시 받을 때:

```powershell
python collect_law_markdown.py
```

이미 저장된 XML은 사용하고, 없는 항목만 API에서 받을 때:

```powershell
python collect_law_markdown.py --missing-only
```

네트워크 호출 없이 저장된 XML만 다시 파싱할 때:

```powershell
python collect_law_markdown.py --from-cache
```

Markdown에는 별도 옵션 없이 항상 `NFKC_SEARCH_V1` 검색 정규화가 적용됩니다.
원문 표기가 필요하면 `output/raw_xml`의 API XML을 확인하고, 현재 캐시에서
Markdown만 다시 만들 때에는 `--from-cache`를 사용합니다.

호·목 사이에는 빈 줄을 넣어 각 항목을 독립된 문단으로 출력합니다. 번호는
원문 그대로 보존하고, 목의 들여쓰기가 코드 블록으로 표시되지 않도록 처리합니다.
기존 파일에도 적용하려면 `--from-cache`로 다시 생성한 뒤 NotebookLM의 기존
소스를 새 Markdown 파일로 교체하세요.

별표·서식은 본문과 달리 인쇄용 공백과 줄바꿈이 포함된 평면 텍스트로 제공되는
경우가 많습니다. 텍스트 부분은 기존 문단을 보존하고 `1.`, `가.`, `(1)`,
`(가)`, `1)` 등으로 시작하는 항목을 독립 문단으로 구분합니다. 같은 문단의
이어지는 원문 행에는 Markdown 강제 줄바꿈을 적용합니다. 선 문자(`┌`, `│`,
`━` 등)로 그린 표·서식 부분은 고정폭 텍스트 블록으로 보존하며, 표 안의
이미지는 해당 위치에 참조를 남기고 블록 밖에 이미지와 확인 링크를 표시합니다.
XML에 없는 병합 셀 구조나 인쇄 줄바꿈에서 끊긴 단어는 추정해 복원하지 않으며,
복잡한 표·서식의 정확한 배치는 함께 제공되는 원문 파일 링크로 확인할 수 있습니다.

기본 파일 대신 다른 수집 목록을 사용할 때:

```powershell
python collect_law_markdown.py --input .\my_law_targets.csv
```

## 출력 구조

```text
output/
├─ raw_xml/
│  ├─ laws/                    # 법령 API 원본 XML
│  ├─ administrative_rules/   # 행정규칙 API 원본 XML
│  └─ thdcmp/                  # 인용조문 3단비교 원본 XML
├─ markdown/
│  ├─ laws/                    # 검색 정규화된 법령 Markdown
│  ├─ administrative_rules/   # 검색 정규화된 행정규칙 Markdown
│  └─ thdcmp/                  # 검색 정규화된 인용조문 3단비교 Markdown
├─ logs/                       # 실행 로그
├─ manifest.csv                # 전체 항목 처리 결과와 실제 출력 경로
└─ failed_items.csv            # 실패 항목과 오류 사유
```

새 API 응답을 정상적으로 검증하고 저장하면 원본 XML 옆에 같은 이름의 provenance sidecar가 생성됩니다. 예를 들어 `행정기본법.xml`의 sidecar는 `행정기본법.xml.meta.json`입니다. sidecar에는 실제 API 수신시각과 원본 XML 해시가 기록되며, 인증값은 저장하지 않습니다.

sidecar는 **API에서 새로 받은 원문에만** 생성됩니다. `--from-cache`와 기존 캐시를 사용하는 `--missing-only` 실행은 없는 sidecar를 현재 시각으로 새로 만들지 않습니다. 따라서 과거 버전에서 만든 XML처럼 sidecar가 없는 legacy cache는 원문 수신시각을 알 수 없는 자료로 처리하며, 파일 수정시각(mtime)을 API 수신시각으로 간주하지 않습니다.

### 기본 검색 정규화

`output/markdown`은 별도 옵션 없이 항상 검색용 `NFKC_SEARCH_V1` 프로필을
적용하며, frontmatter의 `텍스트정규화`에도 같은 값이 기록됩니다. Markdown은
재생성 가능한 가공 산출물이고 API 원문은 `output/raw_xml`에 보존되므로,
비정규화 Markdown이나 별도의 병행 출력 폴더는 만들지 않습니다. 프로필은 다음
규칙을 적용합니다.

- 본문과 제목·조회명칭 등 검색 대상 메타데이터 문자열에 Unicode NFKC 적용
- `ㆍ`, `・`, `･`를 검색 토큰 경계로 쓰기 쉬운 `·`로 보정
- NFKC가 만드는 분수 슬래시 `∕`를 ASCII `/`로 보정
- 예시 단위 `㎜`, `㎥`, `㎧`, `℃`는 각각 `mm`, `m3`, `m/s`, `°C`로 바꾸고 복합단위를 포함해 앞 숫자와 결합
- 법적 열거 구조인 `①`~`⑳`, 문서 ID·번호·날짜, 원문 URL은 변형하지 않음
- 전각 기호의 NFKC 변환으로 새 Markdown 링크·목록·헤딩·HTML이 생기면 리터럴 표기로 중화
- 선두 전각 공백의 변환이 새 목록·헤딩 구조를 만들면 해당 공백은 원문 그대로 보존

따라서 `구경 65 ㎜`는 검색용 본문에서 `구경 65mm`가 됩니다. 정규화는
제목·조회명칭·응답명칭 같은 검색 대상 텍스트 메타데이터에도 적용되지만,
ID·번호·날짜·출처 시각·해시와 URL은 그대로 유지합니다. 표현만 바꿀 뿐 법적
의미의 동등성을 판정하는 기능은 아니므로, 원문 인용이나 대조가 필요할 때에는
항상 기본본 또는 raw XML을 사용하세요.

### YAML frontmatter와 헤딩

모든 Markdown은 파일 맨 앞에 한글 필드명의 flat YAML frontmatter를 둡니다.
별도의 `## 문서정보` 본문은 만들지 않으며, 문서 기본정보와 provenance는 이
frontmatter 한 곳에서 확인합니다. 여는·닫는 `---`와 YAML 내용 사이에는 각각
빈 줄을 한 줄 두어, frontmatter를 지원하지 않는 일부 Markdown 변환기가 인접한
필드를 Setext 제목으로 오인하지 않도록 합니다.

```markdown
---

YAML스키마버전: "2"
제목: "예시 행정규칙"
문서유형: "행정규칙"
API대상: "admrul"
문서ID: "90001"
텍스트정규화: "NFKC_SEARCH_V1"

---

# 예시 행정규칙
```

공통 필드는 `제목`, `문서유형`, `문서ID`, 요청·응답 명칭, 소관부처,
공포·발령일자와 번호, 시행일자, 현행여부, 담당부서·연락처 및 수집 provenance를
포함합니다. 법령의 약칭·법종, 행정규칙의 일련번호·종류·제개정구분·조문형식,
3단비교의 시행령·시행규칙 명칭과 요약정보처럼 유형별로만 존재하는 값도 같은
frontmatter에 이어서 기록합니다.

문자열은 ID의 선행 0, `Y`/`N`, 날짜, 콜론·해시가 YAML 형 변환이나 주석으로
오인되지 않도록 항상 따옴표로 기록하고, API에 없는 값은 추정하지 않고 `null`로
둡니다. `현행여부`는 해당 값을 실제로 제공하는 행정규칙에만 기록되며,
3단비교 응답에 없는 소관부처·시행일자·현행여부는 `null`입니다.

법령·조문형 행정규칙의 본문 헤딩은 H1 문서 제목 아래에서 실제 존재하는
`편 > 장 > 절 > 관 > 조` 구조만 따라 H2부터 연속 배치합니다. 숫자 체계형
NFTC/NFPC는 `1.`을 H2, `1.1`을 H3로 렌더하고 더 깊은 번호는 본문에
보존합니다. 3단비교는 편·장·절·관이 없으면 법률 조문을 H2,
시행령·시행규칙 조문을 H3에 두고, 구조 블록이 있으면 H2부터 실제 상위
구조 수만큼 한 단계씩 내립니다. 내부 조항 번호는 원문 텍스트로 유지하며
자동 앵커나 자동 링크를 삽입하지 않습니다.

## 결과 확인

작업이 끝나면 폴더의 파일 수보다 `output/manifest.csv`를 기준으로 결과를 확인하는 것이 안전합니다. 목록에서 삭제하거나 이름을 바꾼 항목의 과거 파일이 출력 폴더에 남아 있을 수 있기 때문입니다.

매니페스트에서 우선 확인할 열은 다음과 같습니다.

- `status`: `OK`, `FAIL`, `SKIP_NO_CACHE` 등 처리 상태
- `requested_title`, `api_title`: 요청 명칭과 API 응답 명칭
- `raw_xml_path`, `markdown_path`: 실제 생성 파일 경로
- `promulgation_or_issue_date`, `effective_date`: 공포·발령일과 시행일
- `article_count`, `appendix_count`: 추출된 조문과 별표 수
- `error_message`: 명칭 차이 또는 처리 오류 설명

### 매니페스트 v4와 수집시각

매니페스트 v4는 단일 Markdown 출력만 기록합니다. 종전 병행 출력용
`normalized_markdown_*`와 `has_normalized_markdown` 열은 제거하고, 실제
Markdown에 적용한 정규화 프로필과 변경된 텍스트 필드 수만 남깁니다.

| 구분 | 주요 열 | 의미 |
| --- | --- | --- |
| 스키마와 실행 | `manifest_schema_version`, `renderer_version`, `run_id` | 결과 형식, 렌더러 규칙, 해당 실행을 식별합니다. |
| 원문 출처 | `source_mode`, `source_retrieved_at`, `source_timestamp_basis` | API 신규 수집인지 캐시 재생인지, 확인된 실제 API 수신시각과 그 근거를 기록합니다. |
| 처리시각 | `processed_at`, `collected_at` | 둘 다 ISO 8601 UTC `Z` 형식으로 기록합니다. `collected_at`은 기존 열 호환용이며 실제 API 수신시각으로 사용하지 않습니다. |
| 파일 무결성 | `raw_xml_sha256`, `raw_xml_size_bytes`, `markdown_sha256`, `markdown_size_bytes` | 매니페스트가 가리키는 XML·Markdown의 실제 바이트와 해시를 검증합니다. |
| 링크 품질 | `normalized_links_count`, `unique_normalized_link_count`, `duplicate_link_count`, `invalid_link_count`, `has_attachment_links`, `has_any_links` | 유효 링크 발생 수, 정규화 후 고유 수, 중복·무효 수와 링크 보유 여부를 구분합니다. |
| 본문 이미지 | `embedded_image_reference_count`, `embedded_image_link_count`, `has_embedded_image_references` | XML 본문 안의 이미지 참조와 유효한 `src` 또는 숫자 `id`에서 복원한 원문 링크를 별표 이미지 링크와 분리해 셉니다. |
| 3단비교 | `thdcmp_referenced_article_count`, `thdcmp_rendered_article_count`, `thdcmp_duplicate_reference_count` | XML에 나타난 시행령·시행규칙 참조 발생 수, 각 법률 조문 안의 중복 제거 후 실제 렌더 수, 그 차이를 구분합니다. 다른 법률 조문에서 다시 인용하면 본문도 다시 출력합니다. 참조 조문은 첨부파일 링크가 아닙니다. |
| 검색 정규화 | `normalization_profile`, `normalization_changed_field_count` | 단일 Markdown에 항상 적용한 프로필과 실제로 달라진 텍스트 필드 수를 기록합니다. |

`source_mode=API_FETCH`인 신규 수집은 `source_retrieved_at`과 `source_timestamp_basis=HTTP_FETCH`를 기록합니다. 검증된 sidecar를 사용한 캐시 재생은 원래 수신시각을 유지하고 `source_timestamp_basis=CACHE_METADATA`로 표시합니다. sidecar가 없는 legacy cache는 `source_retrieved_at`을 비워 두고 `UNKNOWN_LEGACY_CACHE`로 표시합니다.

`normalized_links_count`는 별표·서식 파일 링크, 행정규칙 첨부 링크, XML 본문의 유효한 `<img src>`, 숫자 `<img id>`에서 복원한 법제처 원문 링크의 **발생 횟수**입니다. 같은 URL이 여러 번 나오면 발생 횟수에는 모두 포함하며, 정규화 후 고유 링크 수와 중복 수는 각각 `unique_normalized_link_count`, `duplicate_link_count`에서 확인할 수 있습니다. 허용하지 않는 scheme 등으로 제외된 값은 `invalid_link_count`에만 기록되고 Markdown 링크로 출력되지 않습니다. 3단비교의 시행령·시행규칙 조문은 `attachment_link_count`나 `normalized_links_count`에 포함하지 않으며, XML 참조 발생 수와 중복 제거 후 렌더 수는 별도 열에서 확인합니다.

### 본문 이미지와 행정규칙 조판

조문, 항·호·목, 참고자료, 부칙, 별표, 개정문과 제·개정이유에 포함된 모든 `<img>` 표기를 보존합니다. 국가법령정보센터의 공식 `flDownload.do?flSeq=숫자` 주소는 표준 Markdown 이미지와 ID가 포함된 `원문 이미지 열기` 링크로 바꾸고, `src`가 없는 숫자 `id`도 `https://www.law.go.kr/flDownload.do?flSeq={id}`로 복원합니다. 의미 있는 `alt`는 대체 텍스트로 보존하고 `alt="img{id}"`처럼 기계적인 값은 읽기 쉬운 설명으로 바꿉니다. 임의의 외부 호스트는 자동으로 불러오지 않고 클릭 링크로만 남기며, 이미지 파일 자체는 내려받거나 저장소에 복제하지 않습니다.

행정규칙의 법령식 조판 여부는 API XML의 `조문형식여부`를 우선 사용합니다. 해당 값이 없는 구형 응답만 실제 본문이 엄격한 `제N조(제목)` 형식으로 시작하는지 확인해 보완하며, 문서 제목의 특정 단어만으로 조판 방식을 결정하지 않습니다.

PowerShell에서는 다음과 같이 상태별 건수를 빠르게 볼 수 있습니다.

```powershell
Import-Csv .\output\manifest.csv -Encoding UTF8 |
    Group-Object status |
    Select-Object Name, Count
```

실패가 있다면 `output/failed_items.csv`와 가장 최근의 `output/logs/run_*.log`를 함께 확인하세요.

## 활용 예시

- 담당 업무에 필요한 법령·행정규칙의 개인 또는 부서용 검색 자료 구축
- NotebookLM 등 문서 기반 AI 도구의 출처 문서 준비
- RAG·전문 검색 시스템의 원문 데이터 전처리
- 법령·시행령·시행규칙 사이 인용 관계 검토
- 정기 재수집을 통한 법령 자료 갱신 및 변경 전후 검수

## 한계와 주의사항

- API 응답은 수집 시점의 자료입니다. 중요한 판단 전에는 반드시 최신 공식 원문을 확인하세요.
- 별표·서식의 PDF, HWP 또는 이미지 파일을 직접 내려받아 변환하지는 않습니다. 본문 이미지는 외부 원문 URL을 참조하므로 열람 환경의 네트워크·보안 정책에 따라 바로 표시되지 않을 수 있으며, 이때 함께 기록된 원문 링크로 확인할 수 있습니다.
- 법제처 이미지 다운로드 주소는 유효한 ID도 `HEAD` 요청에 404를 반환할 수 있습니다. 자동 검증할 때에는 `GET` 응답과 `Content-Type: image/*`를 함께 확인해야 합니다.
- 복잡한 표, 수식, 이미지 중심 서식은 Markdown만으로 원본 모양을 완전히 재현할 수 없습니다.
- API 검색은 정확한 명칭에 영향을 받습니다. `api_title`이 요청 명칭과 다른 경우 결과를 직접 검토하세요.

## 기여

대한민국 법령 자료를 더 정확하고 편리하게 활용하기 위한 오류 제보와 개선 제안을 환영합니다. 파싱 규칙을 수정할 때에는 보기 좋은 결과뿐 아니라 원문 내용·순서·조문 계층이 유지되는지도 함께 확인해 주세요.

## 라이선스

이 프로젝트의 소스 코드는 [MIT License](LICENSE)에 따라 자유롭게 사용·수정·배포할 수 있습니다.
