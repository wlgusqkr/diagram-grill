# diagram-grill

> 추측하기 전에 묻는 아키텍처 다이어그램 Agent Skill.

`diagram-grill`은 저장소의 코드, Terraform, Kubernetes, Docker Compose를
근거로 클라우드·소프트웨어 시스템 아키텍처를 그리는 오픈소스 Agent
Skill입니다. 코드만으로 알 수 없는 결정은 사용자에게 질문하고, 승인된
Architecture Brief만 제한된 JSON 명세와 신뢰된 Python 렌더러를 거쳐
SVG와 manifest(필요시 PNG)로 만듭니다.

현재 public alpha 개발 중입니다. 실제 평가 결과가 공개되기 전에는 성능,
클라이언트 호환성, Before/After 효과를 주장하지 않습니다.

## 사용 예시

저장소에서 skill을 명시적으로 호출합니다.

```text
$diagram-grill

Inspect the Terraform and Kubernetes files in this repository. Ask me about
any architecture decisions the files cannot establish, then render the
approved system architecture.
```

Skill은 먼저 근거가 연결된 Architecture Brief를 보여주고, 사용자가
요청된 방식으로 승인하기 전에는 다이어그램 파일을 쓰지 않도록
설계되어 있습니다. 이 gate는 지침과 validator에 구현되어 있지만,
아직 end-to-end 평가를 완료했다고 주장하지는 않습니다.

결과물은 기존 파일을 덮어쓰지 않는 번들로 생성됩니다.

```text
diagrams/<slug>/
├── diagram.py
├── diagram.svg
├── diagram.json
├── render-manifest.json
└── diagram.png (optional)
```

테마 선택 기능은 없습니다. 읽기 쉬운 고정 기본값을 사용합니다. 범위는
클라우드·소프트웨어 시스템 아키텍처이며 ERD, 시퀀스 다이어그램, UML,
범용 플로차트는 지원 대상이 아닙니다. 정확한 제품 근거 또는 사용자 승인이
없으면 브랜드 아이콘 대신 명시적으로 표시된 generic node를 사용합니다.

## 설치

첫 공개 태그 이후 전역 복사 방식으로 설치합니다.

```sh
npx skills@1.5.23 add \
  https://github.com/wlgusqkr/diagram-grill/tree/v0.1.0-alpha/skills/diagram-grill \
  --skill diagram-grill --global --copy
```

Python 3.9 이상과 Graphviz `dot`은 별도로 설치해야 합니다. 설치 명령에만
Node.js 22.20 이상과 `npx`가 필요합니다. 프로젝트 내부 설치는 지원하지
않습니다. 설치된 skill 디렉터리는 대상 저장소와 workspace 밖에 두어야
하며, CLI가 경로 중첩을 거부합니다. 위 명령은 예정된 `v0.1.0-alpha`
태그와 installer 1.5.23을 고정합니다.

## 평가 상태

저장소에는 12개 고정 synthetic case와 baseline/skill을 번갈아 실행하는
24-run 사전 평가 프로토콜이 포함되어 있습니다. 아직 완료된 평가나 개선
수치를 뜻하지 않습니다. 전용 API 프로젝트의 예산·rate limit을 확인하고,
24회 실행과 결과 검토가 끝난 뒤에만 집계 결과와 실제 데모를 공개합니다.

전체 문서와 기여 안내의 기준 언어는 [영문 README](README.md)입니다.

## 라이선스

MIT. 제3자 고지는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를
참조하세요.
