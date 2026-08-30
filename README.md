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
| 대량 처리의 성공·실패 여부를 눈으로 확인하기 어려움 | 원본 XML 캐시, 매니페스트, 실패 목록, 실행 로그 생성 | 재현·검수·재처리가 쉬움 |

결과적으로 법령마다 일관된 구조를 확보할 수 있습니다. 특히 정확한 제목·조문 경계·시행 정보를 유지하므로 키워드 검색과 근거 확인이 편해지고, AI가 서로 다른 조문을 한 문단으로 오인할 가능성도 줄일 수 있습니다.

## 주요 기능

- `법령`, `행정규칙`, `3단비교` 세 유형을 CSV 목록으로 관리하고 `--input`으로 목록 선택
- 법제처 Open API XML 원문 수집 및 로컬 캐시 저장
- 법령·행정규칙의 기본정보, 조문, 부칙, 별표·서식과 첨부 링크를 Markdown으로 구성
- 법률·시행령·시행규칙의 인용조문 3단비교 문서 생성
- 법령식 번호와 들여쓰기를 보존하면서 Markdown 오인 요소를 선별적으로 처리
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
│  ├─ laws/                    # 법령 Markdown
│  ├─ administrative_rules/   # 행정규칙 Markdown
│  └─ thdcmp/                  # 인용조문 3단비교 Markdown
├─ logs/                       # 실행 로그
├─ manifest.csv                # 전체 항목 처리 결과와 실제 출력 경로
└─ failed_items.csv            # 실패 항목과 오류 사유
```


## 결과 확인

작업이 끝나면 폴더의 파일 수보다 `output/manifest.csv`를 기준으로 결과를 확인하는 것이 안전합니다. 목록에서 삭제하거나 이름을 바꾼 항목의 과거 파일이 출력 폴더에 남아 있을 수 있기 때문입니다.

매니페스트에서 우선 확인할 열은 다음과 같습니다.

- `status`: `OK`, `FAIL`, `SKIP_NO_CACHE` 등 처리 상태
- `requested_title`, `api_title`: 요청 명칭과 API 응답 명칭
- `raw_xml_path`, `markdown_path`: 실제 생성 파일 경로
- `promulgation_or_issue_date`, `effective_date`: 공포·발령일과 시행일
- `article_count`, `appendix_count`: 추출된 조문과 별표 수
- `error_message`: 명칭 차이 또는 처리 오류 설명

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
- 별표·서식의 PDF, HWP 또는 이미지를 직접 내려받아 변환하지는 않습니다. 별표·서식은 API로 제공된 텍스트와 원문 링크를 기록합니다.
- 복잡한 표, 수식, 이미지 중심 서식은 Markdown만으로 원본 모양을 완전히 재현할 수 없습니다.
- API 검색은 정확한 명칭에 영향을 받습니다. `api_title`이 요청 명칭과 다른 경우 결과를 직접 검토하세요.

## 기여

대한민국 법령 자료를 더 정확하고 편리하게 활용하기 위한 오류 제보와 개선 제안을 환영합니다. 파싱 규칙을 수정할 때에는 보기 좋은 결과뿐 아니라 원문 내용·순서·조문 계층이 유지되는지도 함께 확인해 주세요.

## 라이선스

이 프로젝트의 소스 코드는 [MIT License](LICENSE)에 따라 자유롭게 사용·수정·배포할 수 있습니다.
