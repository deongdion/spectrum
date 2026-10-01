# Spectrum (Photon) 문서 정리 — 타 언어 SDK 포팅용

> - 원문: https://photon.codes/docs (문서 버전 **Stable**, 2026-10-01 기준)
> - 전체 원문 덤프: https://photon.codes/docs/llms-full.txt · 페이지별 원문은 URL 끝에 `.md` 를 붙이면 받을 수 있음
> - 기준 패키지: `spectrum-ts` / `@spectrum-ts/*` **12.10.1**, `@photon-ai/advanced-imessage` **2.2.0** (모두 MIT)
> - 소스: https://github.com/photon-hq/spectrum-ts , https://github.com/photon-hq/advanced-imessage-ts
>
> 표기 규칙: **[문서]** = 공식 문서 내용, **[소스]** = 문서에는 없고 npm 배포본(dist) 코드에서 확인한 내용, **[미확인]** = 포팅 전에 직접 검증해야 하는 부분.

---

## 0. 한눈에 보기 (포팅 관점 요약)

`spectrum-ts`는 "클라우드 API"가 아니라 **클라이언트 측 프레임워크**다. 메시지 송수신은 HTTP API가 없고(문서: "HTTP send-message API is on the roadmap"), SDK가 플랫폼별 전송 계층에 직접 붙는다.

```
┌──────────────────────────── 내 앱 (agent loop) ────────────────────────────┐
│  for await (const [space, message] of app.messages) { space.send(...) }    │
└───────────────┬────────────────────────────────────────────────────────────┘
                │ Spectrum core (@spectrum-ts/core)
                │  - Message / Space / User / Content 추상화, Content builders
                │  - provider 등록(definePlatform), 스트림 병합, webhook 파서, 토큰 갱신
   ┌────────────┼───────────────┬──────────────────┬───────────────────┐
   ▼            ▼               ▼                  ▼                   ▼
 iMessage     WhatsApp        Telegram           Slack              Terminal
 (cloud)      Business        (Fusor)                               (tuichat 바이너리
 gRPC/TLS     Meta Cloud API  Fusor WS +                            JSON-RPC)
 advanced-    / cloud mode    Bot API
 imessage
   │
   ▼
Control plane: https://spectrum.photon.codes  (HTTP Basic projectId:projectSecret)
  └ POST /projects/{id}/imessage/tokens  → 라인별 단기 토큰(LightAuth) 발급
```

포팅에 필요한 최소 구성요소:

| # | 구성요소 | 난이도 | 근거 |
|---|---|---|---|
| 1 | Spectrum API(HTTP) 클라이언트 — 토큰 발급/프로젝트 조회 | 쉬움 | OpenAPI 공개 (§2) |
| 2 | **iMessage gRPC 클라이언트** — `.proto` 로 코드 생성 | 중간 | `.proto` 가 npm 패키지에 포함 (§10) |
| 3 | 토큰 자동 갱신(TTL 80%) + 라인 reconcile | 중간 | §3.2 |
| 4 | gRPC 이벤트 → Spectrum `Message`/`Content` 정규화 | **어려움(핵심)** | §4, §7 — 매핑 규칙 대부분이 TS 소스에 있음 |
| 5 | Content builder → gRPC 요청 변환(markdown→formatting range, streaming text→edit 등) | 어려움 | §5 |
| 6 | Webhook 서명 검증 + JSON 역직렬화 | 쉬움 | §8 (wire format 완전 공개) |
| 7 | 내구성 이벤트 로그 catch-up(sequence cursor) | 중간 | §10.4 |

---

## 1. 핵심 개념 [문서]

### 1.1 Primitives

| Primitive | 의미 |
|---|---|
| `Message` | 수신된 콘텐츠(텍스트/첨부/구조화 데이터). 모든 메시지는 `(Space, Message)` 튜플로 도착 |
| `Space` | 대화 컨텍스트(DM, 그룹, 터미널 세션). 메시지를 *space 안으로* 보냄 |
| `User` | 플랫폼별 ID로 식별되는 참여자 |
| Platform provider | 플랫폼 프로토콜 ↔ Spectrum 통합 인터페이스 어댑터 |

### 1.2 `Spectrum(options)` → `SpectrumInstance`

```ts
const app = await Spectrum({
  projectId, projectSecret,          // 생략 시 env SPECTRUM_PROJECT_ID / SPECTRUM_PROJECT_SECRET
  providers: [imessage.config(), terminal.config()],
  webhookSecret,                     // 생략 시 env SPECTRUM_WEBHOOK_SECRET
  telemetry: true,                   // OpenTelemetry (기본 Photon OTLP, OTEL_EXPORTER_OTLP_* 로 override)
  options: { logLevel: "debug" },    // 기본 info, LOG_LEVEL env 보다 명시값 우선
});

app.messages                 // AsyncIterable<[Space, Message]> — 모든 provider 병합, 도착순
await app.send(space, ...)
await app.responding(space, fn)   // typing 표시하며 fn 실행
await app.webhook(req, handler)   // 웹훅 1건 처리
await app.stop()                  // graceful shutdown (idempotent)
app.<customEvent>                 // provider가 선언한 커스텀 이벤트 스트림 (lazy 생성)
```

- **설정 우선순위**: 명시값 > 환경변수. provider 설정은 `SPECTRUM_<PLATFORM>_<FIELD>` 규칙 (`<PLATFORM>` = provider id 대문자, 공백/구두점 → `_`; `<FIELD>` = UPPER_SNAKE_CASE). 예: `SPECTRUM_TELEGRAM_BOT_TOKEN`, `SPECTRUM_WHATSAPP_BUSINESS_PHONE_NUMBER_ID`.
- 로그는 토큰/시크릿 필드를 redact 함.
- `app.stop()`: 메시지 스트림 종료 → 커스텀 이벤트 스트림 drain/dispose → 각 provider `lifecycle.destroyClient` → 텔레메트리 flush. **SIGINT/SIGTERM 핸들러를 등록하지 않음**(12.9.x 이하는 등록했음 — 라이브러리는 프로세스를 소유하지 않는다는 원칙).
- projectless provider(terminal 등)는 자격증명 없이 사용 가능.

### 1.3 `Message` 인터페이스

| 멤버 | 설명 |
|---|---|
| `id: string` (readonly) | |
| `content: Content` | 판별 유니온 (§4) |
| `direction: "inbound" \| "outbound"` | 자신이 보낸 메시지 필터링에 사용 |
| `platform` | provider id (`"imessage"`, `"local_imessage"`, `"whatsapp_business"`, `"telegram"`, `"terminal"`, `"slack"` …) |
| `sender: User \| undefined` | 그룹 이벤트 등에서 actor 미기록 시 `undefined` |
| `space: Space` | |
| `timestamp: Date` | |
| `react(emoji)` | = `space.send(reaction(emoji, this))`. 반응 `Message` 반환(나중에 unsend 핸들), 미지원 플랫폼이면 `undefined` |
| `reply(...content)` | = `space.send(reply(c, this))`. 단일 인자 → `Message\|undefined`, 가변 인자 → `Message[]` |
| `edit(newContent)` | outbound 전용 |
| `unsend()` | outbound 전용, inbound에 호출 시 throw |
| `read()` | inbound 전용, outbound에 호출 시 throw |

### 1.4 `Space` 인터페이스

| 멤버 | 설명 (모두 `send(builder)` 의 sugar) |
|---|---|
| `id`, `__platform` | |
| `send(content)` / `send(...contents)` | 가변 인자는 **순차적으로 개별 send** (복합 메시지 아님). `send(reaction(...))` 는 반응 Message 반환 |
| `edit(message, content)` / `unsend(message)` | `Message \| undefined` 수용(체이닝용), undefined면 throw |
| `read(message)` | 읽음 처리 |
| `startTyping()` / `stopTyping()` / `responding(fn)` | `responding` 은 예외 시에도 typing 해제 보장 |
| `rename(name)` / `avatar(input, opts)` / `getAvatar()` / `getDisplayName()` | |
| `add(users)` / `remove(users)` / `leave()` / `getMembers()` | `MemberInput` = `User` 또는 id 문자열(단일/배열) |
| `getMessage(id)` | id로 메시지 materialize (`reaction()` 대상 등에 필요) |

플랫폼 제약은 `UnsupportedError` 로 표출.

### 1.5 `User`

`{ __platform, id, kind?: "agent" }` + provider 의 `user.schema` 확장 필드. 예: iMessage → `address?`, `country?`, `service?: "iMessage"|"SMS"|"RCS"|"unknown"`.

### 1.6 Platform narrowing

provider export(`imessage`, `localIMessage`, `slack`, `terminal`, `whatsappBusiness`, `telegram`)는 **callable** 이고 세 가지 입력을 받는다:

- `imessage(app)` → `PlatformInstance`: `user(id)`, `space.create(user | users[], params?)`, `space.get(id, params?)`, 커스텀 actions/events
- `imessage(space)` → 플랫폼 전용 필드 (`type`, `phone` …)
- `imessage(message)` → `message.schema` 확장 필드
- 잘못된 플랫폼으로 narrowing 시 런타임 경고 → 항상 `message.platform` 으로 먼저 gate.
- iMessage 는 `imessage.is(message)` 타입 가드도 제공.

> 포팅 시: 정적 타입 언어라면 narrowing = 다운캐스트/타입 가드, 동적 언어라면 플랫폼별 wrapper 객체로 구현.

---

## 2. Control plane — Spectrum API [문서 + OpenAPI]

- Base URL: `https://spectrum.photon.codes` (HTTPS only, [소스] env `SPECTRUM_CLOUD_URL` 로 override)
- OpenAPI: https://spectrum.photon.codes/openapi/json (title "Spectrum Cloud External API" 1.0.0)
- 인증: **HTTP Basic** `base64(projectId:projectSecret)`. 자격증명은 프로젝트 단위, 만료 없음(`photon projects regenerate-secret` 으로 회전).
- 응답 envelope: 성공 `{ "succeed": true, "data": ... }`, 실패 `{ "succeed": false, "message": "..." }` — **HTTP status 가 진실의 원천**.
- 상태코드: 200 / 401(자격증명) / 404 / 409(충돌) / 422(스키마) / 5xx(재시도 가능)
- Rate limit: **프로젝트당 5 req/s** 기본, 초과 시 429.

### 2.1 SDK 런타임이 쓰는 엔드포인트 [소스: core `cloud` 객체]

| 메서드 | 경로 | 용도 | 응답 `data` |
|---|---|---|---|
| GET | `/projects/{id}/` | 프로젝트 메타(`projectConfig`) | `{ name, slug, profile{firstName,lastName,avatarUrl,imessageSynced} }` |
| POST | `/projects/{id}/imessage/tokens` | **iMessage 라인 토큰 발급** | dedicated: `{ type:"dedicated", auth:{instanceId→token}, numbers:{instanceId→phone}, expiresIn }` / shared: `{ type:"shared", token, expiresIn }` |
| GET | `/projects/{id}/imessage/` | shared/dedicated 여부 | `{ type }` |
| POST | `/projects/{id}/whatsapp-business/tokens` | WA 라인 토큰 | `{ auth:{phone_number_id→token}, numbers:{phone_number_id→display\|null}, expiresIn }` |
| POST | `/projects/{id}/slack/tokens` | Slack 설치별 토큰 | `{ auth:{team_id→token}, teams:{team_id→{teamName,botUserId,appId,grantedScopes}}, expiresIn }` (Slack 비활성 시 403) |
| POST | `/projects/{id}/fusor/token` | Fusor용 LightAuth JWT (`aud`=`codes.photon.spectrum.fusor`, `sub`=projectId) | `{ token, expiresIn }` |
| POST | `/projects/{id}/voice/tokens` | voice 토큰 | `{ token, expiresIn }` |
| GET/PATCH | `/projects/{id}/platforms/` | 플랫폼 조회/토글 `{platform, enabled}` | |
| GET | `/projects/{id}/billing/subscription` | 구독 정보 | |

`expiresIn` 은 초 단위 TTL. 토큰 종류 이름은 "LightAuth".

### 2.2 관리용 엔드포인트 (전체 목록)

- Projects: `GET /projects/{id}/`, `GET|PATCH /profile`, `POST /profile/avatar/upload|commit`, `POST|GET /profile/sync`, `PATCH /slug/`
- Users: `POST|GET /users/`, `GET|DELETE /users/{userId}/`, `GET /users/{userId}/redirect`(공유 라인 사용자 리다이렉트)
  - `POST /users/` body: `{type:"shared", phoneNumber(E.164), firstName?, lastName?, email?}` 또는 `{type:"dedicated", phoneNumber, assignedPhoneNumber, ...}`
  - **Free/Pro(공유 풀)에서는 Users 에 등록된 대상에게만 발송 가능** → 미등록 시 `Target not allowed for this project`
- Lines: `GET|POST /lines/` (`?platform=imessage|whatsapp_business|voice|slack`), `GET /lines/route`(신규 사용자를 최적 라인에 라우팅), `GET|PATCH /lines/{lineId}/profile`, avatar upload/commit, `DELETE /lines/{lineId}`
- Webhooks: `GET|POST /webhooks/`, `GET /webhooks/egress-ips`, `PATCH|DELETE /webhooks/{webhookId}`, `POST /webhooks/{webhookId}/secret/rotate`
- WhatsApp templates: `/whatsapp-business/accounts`, `.../{accountId}/templates/` CRUD
- Slack: `GET|PUT|DELETE /slack/`, installations, `POST /slack/setup`
- Voice: `GET|PATCH|DELETE /voice/sip-inbound/`
- Billing: `/billing/subscription`, `/billing/status`

### 2.3 Dashboard API / OAuth (사용자 단위, 별개 인증체계)

- `https://app.photon.codes`, Bearer 토큰(CLI device flow 또는 OAuth 2.1). OpenAPI: https://photon.codes/docs/api-reference/dashboard-openapi.json
- 엔드포인트: `GET|POST /api/projects/`, `GET /api/projects/{id}`, `POST /api/auth/device/code`, `POST /api/auth/device/token`
- OAuth 2.1 + OIDC: issuer `https://app.photon.codes/api/auth`, PKCE **S256 필수**, `client_credentials` 미지원, refresh token rotation(30일), id_token 은 **EdDSA(Ed25519)**. Discovery: `https://app.photon.codes/.well-known/openid-configuration/api/auth`
- Scopes: `openid profile email offline_access projects:{read,write} members:{read,write} webhooks:{read,write} billing:{read,write} spectrum:{read,write} payments:{read,write}`. access token 수명 1h, `:write` 포함 시 15m, `billing:write` 5m.

---

## 3. 전송 계층 (Transport)

### 3.1 iMessage cloud — gRPC [문서 + 소스]

- [문서] cloud provider 는 "managed iMessage infrastructure via **gRPC**". Edge/Workers 런타임 미지원(Node `http2`, `tls` 필요).
- [문서] 직접 지정 예: `imessage.config({ clients: [{ address: "line-1.imsg.photon.codes:443", token, phone }] })` — 이 경우 토큰 갱신은 사용자 책임.
- [소스 `@spectrum-ts/imessage` `createCloudClients`] 주소 결정 규칙:

| 모드 | gRPC 주소 | 토큰 | `phone` |
|---|---|---|---|
| shared (Free/Pro) | `imessage.spectrum.photon.codes:443` (env `SPECTRUM_IMESSAGE_ADDRESS` 로 override) | `data.token` | 리터럴 `"shared"` |
| dedicated (Business) | `` `${instanceId}.imsg.photon.codes:443` `` (instanceId 마다 클라이언트 1개) | `data.auth[instanceId]` | `data.numbers[instanceId]` (E.164) |

- gRPC 클라이언트 옵션: `tls: true`, `retry: true`, `autoIdempotency: true`, 인증 메타데이터 `authorization: Bearer <token>` [소스 advanced-imessage].
- `@photon-ai/advanced-imessage` 의 `createClient({ address, token, tls?, timeout?, retry? })` 가 실제 gRPC 클라이언트 (§10).
- Self-host 도 가능: `@photon-ai/advanced-imessage` 서버 주소(`host:port`, `https://` 아님) + API key.

### 3.2 토큰 갱신 & 라인 reconcile [문서 + 소스]

- [문서] 자동 discovery 모드에서 토큰은 **TTL의 80% 시점**에 갱신.
- [소스] `RENEWAL_RATIO = 0.8`, `MIN_RENEWAL_DELAY_MS = 5000`, 강제 갱신 최소 간격 5s (`FORCE_REFRESH_MIN_INTERVAL_MS`).
- [소스] dedicated 모드 갱신 시 `reconcile`:
  - 응답 `auth` 에 새로 생긴 instanceId → 클라이언트 생성/추가 (phone 없으면 skip + 경고)
  - 사라진 instanceId → 클라이언트 close & 제거 (빈 `auth` = 라인 0개로 간주, `auth` 자체가 없으면 throw)
- [문서] 런타임 중 추가된 라인은 **다음 토큰 갱신 때까지 보이지 않음** — 그 사이 그 라인으로 온 메시지는 앱에 전달되지 않음.
- [문서] dedicated 라인이 2개 이상이 되는 순간:
  - `space.get(chatGuid)` 는 `params.phone` 필수 (1개면 추론)
  - `space.create()` 에 `phone` 없으면 라인 중 **랜덤** 선택. 이후 해당 space 의 모든 동작이 그 라인으로 라우팅.

### 3.3 Fusor (webhook 기반 provider 용 중계) [문서 + 소스]

- 웹훅으로만 수신하는 플랫폼(예: Telegram)을 위해 Photon 이 원 요청을 받아 **protobuf envelope** 로 전달하는 계층.
- [소스] 스트리밍 수신 시 WebSocket `wss://fusor-ws.spectrum.photon.codes/v1/subscribe` (env `SPECTRUM_FUSOR_WS_URL`), 토큰은 `POST /fusor/token`.
- [소스] protobuf 정의: npm `@photon-ai/proto` (photon.fusor.v1, photon.slack.v1), repo `photon-hq/proto`.
- `app.webhook()` 은 payload 모양(JSON vs protobuf)으로 native/Fusor 를 판별.
- cloud 모드 Telegram provider 는 시작 시 Fusor 웹훅을 자동 등록.

### 3.4 기타 provider 전송

| Provider | 전송 | 비고 |
|---|---|---|
| Terminal | [tuichat](https://github.com/photon-hq/tuichat) 바이너리를 subprocess 로 띄우고 **JSON-RPC** | GitHub Releases 에서 자동 다운로드. non-TTY 면 readline fallback (CI 테스트용) |
| WhatsApp Business | direct 모드: Meta Cloud API (`accessToken`, `phoneNumberId`, `appSecret`) / cloud 모드: projectId+secret 로 토큰 발급 | 두 env 다 있으면 direct, 하나만 있으면 cloud |
| Telegram | Bot API (`botToken`, `webhookSecret?`, `baseUrl?`=`https://api.telegram.org`) + Fusor | |
| Local iMessage | macOS `~/Library/Messages/chat.db` 직접 읽기 (`@photon-ai/imessage-kit`, better-sqlite3) | platform id `local_imessage`, Full Disk Access 필요 |

---

## 4. Content 모델 [문서]

`Content` 는 `type` 으로 판별되는 유니온. 모든 `ContentInput` 자리에 문자열(= `text()`) 또는 `ContentBuilder` 를 넣을 수 있음.

| `type` | 필드 | 방향 |
|---|---|---|
| `text` | `text` | 양방향 (inbound 는 서식 무관하게 항상 text) |
| `markdown` | `markdown` | outbound 전용 |
| `streamText` | `stream: () => AsyncIterable<string>`, `format?: "plain"\|"markdown"` | outbound |
| `attachment` | `id`, `name`, `mimeType`, `size?`, `read()`, `stream()` | 양방향 |
| `voice` | `name?`, `mimeType`, `duration?`(초), `size?`, `read()`, `stream()` | 양방향 |
| `contact` | `name?{formatted,first,last,middle,prefix,suffix}`, `phones?`, `emails?`, `addresses?`, `org?`, `urls?`, `birthday?`, `note?`, `photo?{mimeType,read()}`, `user?`, `raw?` | 양방향 |
| `richlink` | `url` (OG 메타 fetch 안 함) | 양방향 |
| `effect` | `content`, `effect` (iMessage 효과로 감싸기) | outbound |
| `reaction` | `emoji`, `target: Message` | 양방향 |
| `poll` | `title`, `options: {title}[]` | 양방향 |
| `poll_option` | `option{title}`, `poll`, `selected`, `title` | 양방향(투표) |
| `group` | `items: Message[]` (앨범 등) | 양방향 |
| `reply` | `content`, `target` | outbound (inbound 답장은 text 로 옴) |
| `edit` | `content`, `target` | outbound |
| `unsend` | `target` | outbound |
| `read` | `target` | outbound=읽음 처리, inbound=상대가 내 메시지 읽음(read receipt) |
| `typing` | `state: "start"\|"stop"` | outbound |
| `rename` | `displayName` | 양방향 |
| `avatar` | `action: {kind:"set", read(), mimeType} \| {kind:"clear"}` | 양방향 |
| `addMember` / `removeMember` | `members: string[]` | 양방향 (`sender` = 실행자) |
| `leaveSpace` | (없음, `sender` = 나간 사람) | 양방향 |
| `app` | `url()`, `layout()`, `live?` | outbound |
| `custom` | `raw: unknown` | 양방향 |

**공통 의미 규칙**
- 에이전트 자신의 행위(`space.add`, `space.rename`, `space.read` …)는 inbound 로 **echo 되지 않음**.
- 그룹 이벤트에서 `sender` 는 actor, Apple 이 actor 를 기록하지 않으면 `undefined`.
- inbound `read`: `sender` = 읽은 사람(항상 존재; 귀속 불가 영수증은 drop), `target` = 내가 보낸 메시지(`direction:"outbound"`), `timestamp` = 상대 기기의 읽은 시각. DM 만 신뢰 가능, 그룹은 best-effort. 그룹은 독자별 1건씩.
- 반응 대상이 내 메시지일 수 있으므로, provider 는 중첩 레코드(target)에 `direction` 을 명시할 수 있음 (명시 없으면 바깥 레코드 방향 상속).

---

## 5. Content builders [문서]

모두 `spectrum-ts` 에서 import, `space.send(builder)` 로 전송.

| Builder | 시그니처/동작 | 검증·제약 |
|---|---|---|
| `text(s \| StreamTextSource, {extract?})` | 문자열과 동일. 스트림: AI SDK result / AsyncIterable / ReadableStream. `extract` 생략 시 OpenAI chat/responses, Anthropic messages, AI SDK, plain string 자동 감지 | 스트림은 1회만 전송 가능. 미지원 플랫폼은 완료 후 한 번에 전송 |
| `markdown(s \| stream)` | CommonMark + GFM 테이블/취소선 | Telegram→HTML `parse_mode`, iMessage→UTF-16 formatting range, 그 외 plain fallback |
| `attachment(path \| URL \| Buffer, {name?, mimeType?, id?})` | MIME 은 확장자로 추론 | 추론 불가 + mimeType 없음 → build 시 throw. outbound id 기본 UUID |
| `voice(...)` | attachment 와 동일 입력 + `duration` | 미지원 플랫폼은 일반 오디오 첨부로 downgrade |
| `contact(ContactInput \| vCard string \| vcf \| User, details?)` | `fromVCard()`, `toVCard()` 헬퍼 | |
| `richlink(url)` | URL 만 전달 | 미지원 시 plain text |
| `app(url \| Promise \| thunk, {live?})` | iMessage: Spectrum iMessage App 카드, 그 외: URL | `edit(app(...), card)` 로 제자리 갱신 (cloud 전용, `miniAppCardSession` 메타 필요) |
| `poll(title, ...choices)` / `option(title)` | 투표는 `poll_option` 으로 수신 | |
| `group(...items)` | 한 시각적 묶음 | 중첩 불가, reaction 포함 불가. 미지원 시 순차 전송 |
| `custom(raw)` | provider `send` 가 해석 | |
| `reply(content, target)` | 스레드 답장 | 미지원 플랫폼은 **no-op** (일반 send 로 downgrade 안 함) |
| `edit(content, target)` | outbound 수정, 결과 `undefined` | |
| `unsend(target)` | outbound 회수 | inbound 대상이면 build 시 throw. iMessage 약 2분 제한 |
| `reaction(emoji, target)` | | 대상이 reaction 이면 throw |
| `read(target)` | | outbound 대상이면 throw |
| `typing("start"\|"stop")` | 기본 start | 미지원은 silent no-op |
| `rename(name)` | | 빈 문자열 throw |
| `avatar(path \| Buffer \| "clear", {mimeType?})` | `"clear"` 는 예약 sentinel | Buffer 면 mimeType 필수 |
| `addMember(m)` / `removeMember(m)` / `leaveSpace()` | | 빈 목록은 **send 시점** reject |
| `fusorEvent(name, data)` | Fusor handler 에서 커스텀 이벤트 방출 | |

**래핑 금지 규칙**: `reply()`·`edit()` 는 `reply, edit, reaction, group, typing, rename, avatar, addMember, removeMember, leaveSpace, unsend, read` 를 감쌀 수 없음 (construction 시 throw).

**반환값 규칙**: 실제 메시지를 만드는 send 는 `Message` 반환, fire-and-forget 신호(edit/unsend/read/typing/rename/avatar/membership)는 `undefined`.

`Emoji` 상수 (iMessage tapback 매핑): `love ❤️`, `like 👍`, `dislike 👎`, `laugh 😂`, `emphasize ‼️`, `question ❓`. 그 외 이모지는 일반 emoji reaction.

---

## 6. Provider 기능 매트릭스 [문서]

| 기능 | iMessage cloud | iMessage local | WhatsApp Biz | Telegram | Terminal |
|---|---|---|---|---|---|
| text / attachment | ✅ | ✅ | ✅ | ✅ | ✅ |
| markdown | formatting range | plain | ? | HTML | |
| streaming text | 첫 청크 전송 후 edit | ❌ | | private chat draft preview | |
| reaction | ✅ (tapback) | ❌ | | ✅ | ✅ (`r` 키) |
| reply(thread) | ✅ | ❌ | ✅ | ✅ | ✅ (`replyTo.messageId`) |
| edit / unsend | ✅ (15분 / 2분) | ❌ | | edit ✅ | |
| read 보내기 | 채팅 단위 | UnsupportedError | 메시지 단위(이전 것 포함) | no-op | |
| read receipt 수신 | ✅ (DM 신뢰) | ❌ | | | |
| typing | ✅ | no-op | | ✅ | ✅ |
| 그룹 생성 | dedicated 만 | ❌ | ❌ (1:1 만) | ❌ (bot 은 그룹 생성 불가) | |
| 그룹 이벤트 수신 | dedicated 만 | ❌ | | | |
| effect / background / app card / contact card | ✅ | ❌ | | | |
| custom | | | | `custom({method, params})` = Bot API 호출 | |

User id 포맷: iMessage = E.164 또는 이메일, WhatsApp = 국제번호 숫자만(`15551234567`), Telegram = 숫자 user id 문자열(chat id 도 문자열화).

---

## 7. iMessage provider 상세 [문서]

### 7.1 패키지

- `@spectrum-ts/imessage` (= `spectrum-ts/providers/imessage`): cloud, platform id `imessage`
- `@spectrum-ts/imessage-local`: macOS 전용, platform id `local_imessage`. `imessage.config({local:true})` 는 더 이상 유효하지 않음.
- 두 provider 동시 등록 가능.

### 7.2 라인 모델

| 플랜 | 라인 | 사용자가 보는 번호 | 그룹 |
|---|---|---|---|
| Free / Pro | 공유 풀 (수신자마다 다를 수 있음), `space.phone = "shared"` | 수신자별 상이 | 생성 ❌, 그룹 이벤트 구독 ❌ |
| Business | 전용 라인, `space.phone = E.164` | 항상 동일 | ✅ |

Business 는 opt-in **auto-scale** (라인 포화 시 자동 추가).

### 7.3 Space / ID

- `space.type: "dm" | "group"`, `space.phone`
- Chat GUID: DM `any;-;<recipient>` (예 `any;-;+15551111111`), 그룹 `any;+;<group-id>`
- 메시지 id (webhook 기준): `spc-msg-<uuid>`, 파생 이벤트는 복합 id(`...:reaction:<seq>:<idx>`), 그룹 아이템은 `p:<n>/spc-msg-<uuid>`. **opaque 로 취급, 파싱 금지.**
- 첨부 id 는 iMessage GUID (예 `p:0/GUID`). `im.getAttachment(guid, phone?)` 로 lazy 다운로드.

### 7.4 iMessage 전용 기능

| 기능 | API |
|---|---|
| 메시지 효과 | `effect(content, imessage.effect.message.<name>)` — bubble: `slam`(impact) `loud` `gentle` `invisible`(invisibleink) = `com.apple.MobileSMS.expressivesend.*`; screen: `confetti fireworks balloons heart lasers celebration(HappyBirthday) sparkles spotlight echo` = `com.apple.messages.effect.CK*Effect` |
| 그룹 이름/아이콘/멤버 | `rename`, `avatar`, `getAvatar`, `add/remove/leave`, `getMembers` (cloud + group 만) |
| 채팅 배경 | `imessage(space).background(path\|Buffer\|"clear")`, `background()` builder. iCloud 경유 ~30s 내 동기화 |
| Customized iMessage App | `customizedMiniApp({appName, appStoreId?, extensionBundleId, teamId(10자 대문자영숫자), url, live?, layout{caption, subcaption, trailingCaption, trailingSubcaption, image(JPEG), imageTitle, imageSubtitle, summary}})`, `edit()` 로 갱신 |
| 봇 연락처 카드 공유 | `nativeContactCard()` / `imessage(space).shareContactCard()` |
| 첨부 직접 조회 | `imessage(app).getAttachment(guid, phone?)` |
| typed 멤버 조회 | `imessage(app).getMembers(space)` |
| 네이티브 메타데이터 | `isSent, isDelivered, isDeliveredQuietly, didNotifyRecipient, isDelayed, sendErrorCode(0=정상), dateDelivered/Read/Played/Edited/Retracted/ExpressiveSendPlayed, nativeText, formatting, mentions(UTF-16 offset), subject, balloonBundleId, expressiveSendStyleId, attachmentMetadata, appliedReactions, placedStickers, reactionRecord, itemType, groupTitle, partCount, isAutoReply, isCorrupt, isExpirable, isServiceMessage, isSpam, isSystemMessage` + [소스] `miniAppCardSession, partIndex, parentId` |

### 7.5 Inbound 이벤트 신뢰성

- dedicated 라인의 그룹 이벤트·read receipt 는 메시지와 같은 **내구성 catch-up 로그**를 타서, 앱 다운 중 발생분이 재연결 시 replay 됨. cursor gap 후에는 `getMembers()/getAvatar()` 로 reconcile.
- 아이콘 변경 이벤트는 이벤트 시점 스냅샷; 이미 바뀐 경우 skip.
- read receipt 디버그: `LOG_LEVEL=debug` → `spectrum.imessage.read` 로그. 상대 기기의 "읽음 확인 보내기"가 꺼져 있으면 이벤트 자체가 없음.

### 7.6 Quota

- **서버당 하루 5,000 outbound** (hard limit, 초과 시 Apple ban 위험 급증)
- **라인당 하루 50 신규 대화** (처음 메시지를 보내는 수신자 기준)

---

## 8. Webhooks [문서 + OpenAPI]

### 8.1 개요

- 프로젝트당 여러 URL 등록 가능, **URL마다 모든 이벤트 수신**(병렬 `Promise.allSettled`).
- 현재 이벤트는 `messages` 하나 (OpenAPI 에서는 eventType 이름 `message.received`).
- **inbound 만** 전달. outbound/typing/edit/poll vote/read receipt 는 전달 안 됨 (reaction 은 `content.type` 으로 포함).
- 응답(reply)하려면 별도 SDK 프로세스 필요 — HTTP 발송 API 없음, "get space by id" API 없음 → `im.space.create(await im.user(sender.id))` 로 DM 재구성.
- `app.webhook()` 은 상태 없음, `app.messages` 에 흘리지 않음, 핸들러는 응답 후 fire-and-forget.

### 8.2 등록

```sh
curl -X POST "https://spectrum.photon.codes/projects/$PROJECT_ID/webhooks/" \
  -u "$PROJECT_ID:$PROJECT_SECRET" -H "Content-Type: application/json" \
  -d '{"webhookUrl":"https://example.com/spectrum-webhook"}'
```

Request (OpenAPI): `webhookUrl`, `schemaVersion` (`normalized-events.v1` 기본 | `raw-inbound.v1`), `eventTypes` (기본 `["message.received"]`), `failureNotificationEmail?`
Response: `id, webhookUrl, schemaVersion, eventTypes, enabled, status(active|disabled), disabledAt, disabledReason(manual|receiver_gone|delivery_failures), createdAt, updatedAt, signingSecret, standardSigningSecret(whsec_...)` — **두 시크릿 모두 이 응답에서 1회만 노출**.
에러: 422(URL 스키마), 409(중복 URL), 401.
기타: `PATCH` 로 `raw-inbound.v1` 단방향 업그레이드, `POST .../secret/rotate {overlapSeconds: 기본 86400, 최대 604800}` (Standard Webhooks 시크릿만 회전, 겹치는 기간 동안 신·구 서명 둘 다 포함).

### 8.3 요청 헤더

| 헤더 | 값 |
|---|---|
| `Content-Type` | `application/json` (UTF-8) |
| `User-Agent` | `spectrum-webhook/<version>` |
| `X-Spectrum-Event` | `messages` |
| `X-Spectrum-Webhook-Id` | 웹훅 등록 UUID |
| `X-Spectrum-Timestamp` | 서명 시각 epoch seconds |
| `X-Spectrum-Signature` | `v0=<64 hex>` |
| `webhook-id` / `webhook-timestamp` / `webhook-signature` | [OpenAPI] Standard Webhooks 규격, `v1,<base64>` 공백 구분 다중 서명 |

### 8.4 서명 검증 (두 방식)

**Legacy (`signingSecret`)**
```
expected = "v0=" + hex(HMAC_SHA256(signingSecret, "v0:" + timestamp + ":" + rawBody))
```
1. raw body 바이트를 파싱 전에 확보 2. `|now - timestamp| > 300s` 면 거부 3. 재계산 4. **constant-time 비교**. 실패 시 401(재시도 안 됨). 헤더 누락 400. SDK 는 secret 미설정 시 500.

**Standard Webhooks (`standardSigningSecret`, `whsec_` 접두)**
- signed content = `webhook-id + "." + webhook-timestamp + "." + rawBody`, 표준 Standard Webhooks 라이브러리 사용, `webhook-id` 로 dedupe.

### 8.5 Body

```json
{
  "event": "messages",
  "space":   { "id": "any;-;+15550100", "platform": "iMessage", "type": "dm", "phone": "+15551234567" },
  "message": {
    "id": "spc-msg-00000000-0000-4000-8000-000000000001",
    "platform": "iMessage",
    "direction": "inbound",
    "timestamp": "2026-05-14T19:06:32.000Z",
    "sender": { "id": "+15550100", "platform": "iMessage" },
    "space":  { ...top-level space 복사본 },
    "content": { "type": "text", "text": "hey, what time is dinner?" }
  }
}
```

- SDK 객체에서 함수형 필드(`read()`, `reply()` …)만 제거한 JSON projection. provider `Space` schema 필드는 그대로 forward.
- ⚠️ 문서 예시의 `platform` 값은 `"iMessage"` (SDK 의 `"imessage"` 와 표기 다름) — **opaque 로 취급** 하라고 명시됨.
- 실제 inbound 로 오는 arm: `text`, `attachment`(`id,name,mimeType,size?` — 바이트/URL 없음), `contact`, `richlink`(url만), `reaction`(`emoji`, `target`=MessageRef), `group`(`items` = 완전한 inbound message 배열). 음성메모는 `attachment`(audio/*), 답장은 `text`.
- `MessageRef` = `{ id, platform, timestamp, sender?, contentPreview?(text 첫 80자) }`
- 알 수 없는 `event` / `content.type` 은 `default` 분기로 2xx 반환 (forward-compatible, 기존 필드 의미는 바뀌지 않음).
- avatar `set` 은 metadata-only (`read()` throw) → `space.getAvatar()` 사용.

### 8.6 전달 보장

- **at-least-once**, 순서 보장 없음 (전역/space 단위 모두). 정렬 필요 시 `message.timestamp`.
- dedupe 키: `message.id` (단일 consumer) 또는 `webhookId:message.id`. TTL 24–48h 면 충분.
- 재시도: 총 6회, 지연 기대값 0 → 200ms → 1s → 5s → 10s → 10s (±50% jitter, factor 5×, cap 10s). 시도당 timeout 30s. DLQ 없음.
- 재시도 대상: 5xx, 408, 429(`Retry-After` 무시), 연결 실패, timeout. **그 외 4xx·3xx 는 즉시 fatal**.
- URL guard(매 시도마다): https 만, 공인 IP 만(SSRF 방어), redirect 금지(`redirect:"manual"`). 등록 시점엔 문법만 검사 → 조건 위반 URL 은 조용히 전부 drop.
- 권장: 서명 검증 → 큐 적재 → 즉시 2xx.

### 8.7 SDK 프레임워크 어댑터

`@spectrum-ts/hono`, `@spectrum-ts/express`(전역 `express.json()` 보다 **먼저** mount), `@spectrum-ts/elysia` — `spectrum({ app, onMessage })`.

---

## 9. Provider 작성 모델 — `definePlatform` [문서]

포팅 시 **내부 provider 인터페이스 설계의 기준**으로 삼을 것.

```ts
definePlatform("my_platform", {
  config: z.object({...}),                         // 설정 스키마 (zod)
  user:   { schema?, resolve({input:{userID}, client, config, store}) },
  space:  { schema?, params?, create({input:{users, params?}, ...}), get?({input:{id, params?}, ...}), actions? },
  message:{ schema?, actions? },
  lifecycle: { createClient({config, projectConfig, projectId, projectSecret, store}), destroyClient?({client, store}) },
  async *messages({client, config, projectConfig, store}) { yield rawRecord },   // inbound 스트림
  send({space, content, client, config, store}) → ProviderMessageRecord | undefined,  // 모든 content.type 단일 디스패처
  actions: { getMessage?, getMembers?, getAvatar?, getDisplayName?, [custom]? },     // ctx={client,config,store} 첫 인자 주입
  events:  { [name]: async function*({client,...}) },                                // app.<name> 으로 노출
  static:  { ... },                                                                  // provider 객체에 복사 (예: effect 상수)
});
```

- Platform id 규칙: `/^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$/` (위반 시 reject). `message.platform`, env prefix, telemetry, webhook routing 에 사용.
- `messages` 에서 yield 한 레코드 → inbound, `send` 반환 레코드 → outbound (레코드에 `direction` 명시 시 우선).
- raw record 최소 형태: `{ id, content, sender: {id}, space: {id}, timestamp }`.
- `space.get` 생략 시 `{id}` 로 만들어 `space.schema` 검증.
- 플랫폼 공통 action(`getMessage/getMembers/getAvatar/getDisplayName`) 미구현 시 `UnsupportedError` 를 던지는 기본값이 연결됨.
- 예약어: Space actions 는 `send, edit, unsend, read, getMessage, getMembers, getAvatar, rename, avatar, add, remove, leave, startTyping, stopTyping, responding, id, __platform` 사용 불가; Message actions 는 `react, reply, edit, id, space, sender, content, platform, direction, timestamp` 불가 (경고 후 skip).
- **Fusor 변형**: `lifecycle.createClient` 에서 `fusor(id, verifyAndParse)` 반환, `messages` 는 generator 대신 `({payload, respond, config, projectConfig, store}) => record | record[] | fusorEvent` 핸들러. `projectConfig.profile.<key>` 로 프로젝트 토글 조회.
- `store`: provider 별 in-memory KV.

---

## 10. Advanced iMessage Kit — gRPC 계층 [문서 + 패키지]

`@photon-ai/advanced-imessage` (Node ≥18.17 / Bun). Spectrum cloud iMessage provider 가 내부적으로 사용. **포팅의 실질적 핵심.**

### 10.1 `.proto` 위치 [패키지]

npm 배포본에 그대로 포함됨 → 각 언어 protoc/buf 플러그인으로 stub 생성 가능.

```
proto/photon/imessage/v1/{address,attachment,chat,event,group,location,message,poll}_service.proto
proto/photon/imessage/v1/{address,attachment,chat,group,location,message,poll}_types.proto
proto/photon/imessage/v1/streaming.proto
proto/google/api/{annotations,http}.proto          # google.api.http 매핑 존재 (예: POST /v1/messages:sendText)
```

package `photon.imessage.v1`. 패키지에 `dist/grpc.js` 와 `dist/http.js` 가 있어 HTTP 전송 변형도 존재하는 것으로 보임 **[미확인: hosted 서버가 HTTP 매핑을 노출하는지]**.

### 10.2 서비스 / RPC 목록

| Service | RPC |
|---|---|
| MessageService | SendTextMessage, SendAttachmentMessage, SendMultipartMessage, SendCustomizedMiniAppMessage, UpdateCustomizedMiniAppMessage, EditMessage, UnsendMessage, SetReaction, PlaceSticker, NotifySilencedMessage, GetMessage, ListRecentMessages, ListChatMessages, GetEmbeddedMedia, **SubscribeMessageEvents (stream)** |
| ChatService | CreateChat, GetChat, GetChatCount, MarkChatRead, SetTyping, ShareContactInfo, SetBackground, RemoveBackground, HasBackground, **SubscribeChatEvents (stream)** |
| GroupService | SetDisplayName, AddParticipants, RemoveParticipants, LeaveGroup, SetIcon, RemoveIcon, GetIcon, **SubscribeGroupEvents (stream)** |
| AttachmentService | UploadAttachment, GetAttachmentInfo, **DownloadAttachment (stream)** |
| PollService | CreatePoll, VotePoll, UnvotePoll, AddPollOption, GetPoll, **SubscribePollEvents (stream)** |
| AddressService | GetAddressInfo, GetFocusStatus, GetIMessageAvailability |
| LocationService | ListSharedFriendLocations, GetSharedFriendLocation, RequestFriendLocationSharing, **WatchSharedFriendLocations (stream)** |
| EventService | **CatchUpEvents (stream)** |

인증: gRPC metadata `authorization: Bearer <token>`, TLS 기본.

### 10.3 TS 클라이언트 API 표면 (문서)

```ts
const im = createClient({ address: "host:443", token, tls?: true, timeout?: ms, retry?: boolean | {maxAttempts, initialDelay, maxDelay} });
```

- `im.messages`: `sendText(chat, text, {effect?, formatting?, replyTo?, clientMessageId?})`, `sendAttachment(chat, attachmentGuid, {isAudioMessage?, replyTo?})`, `sendMultipart(chat, parts, {replyTo?})`, `sendCustomizedMiniApp`, `setReaction(chat, msg, {kind:"love"|"like"|"dislike"|"laugh"|"emphasize"|"question"|"emoji", emoji?}, add: boolean, {partIndex?})`, `placeSticker(chat, msg, attGuid, {x,y,scale?,rotation?,width?})`, `edit(chat, msg, text, {backwardCompatText?, partIndex?, clientMessageId?})` (15분 내), `unsend(chat, msg, {partIndex?, clientMessageId?})` (2분 내), `notifySilenced`, `get(guid)`, `listRecent({...})`, `listInChat(chat, {after, before, isFromMe, isRead, pageSize 1..100, pageToken})`, `getEmbeddedMedia`, `subscribeEvents({chat?})`
- Text formatting range: `{type:"bold"|"italic"|"underline"|"strikethrough", start, length}`, `{type:"effect", start, length, effect: TextEffect.big|small|shake|nod|explode|ripple|bloom|jitter}` (UTF-16 offset)
- `im.chats`: `create(addresses[], {message?, effect?, clientMessageId?})` → `{chat}`, `get(guid)`, `count({includeArchived?})`, `markRead`, `setTyping(chat, bool)`, `shareContactInfo`, `setBackground(chat, bytes)`, `hasBackground`, `removeBackground`, `subscribeEvents`
- `im.groups`: `setDisplayName`, `addParticipants`, `removeParticipants`, `setIcon`, `getIcon`, `removeIcon`, `leave`, `subscribeEvents`
- `im.attachments`: `upload({fileName, data, ...livePhoto?})` → `{attachment:{guid}}`, `get(guid)` (transferState: `pending|transferring|failed|finished|unknown`), `downloadStream(guid)` → frames `header{info, companionInfo?}` / `primaryChunk{data}` / `companionChunk{data}` (Live Photo)
- `im.polls`: `create(chat, title, choices≥2)`, `get`, `vote(pollGuid, optionIdentifier)`, `unvote`, `addOption`, `subscribeEvents` (delta: `created|optionAdded|voted|unvoted`)
- `im.addresses`: `isIMessageAvailable(addr)`, `get(addr)`, `isFocusSilenced(addr)`
- `im.locations`: Find My 공유 위치 조회/요청/`watch` (catch-up 불가)
- `im.events`: `catchUp(lastHandledSequence?)`

### 10.4 이벤트 & 복구

- 서버는 message/chat/group/poll 변경에 대한 **durable event log** 유지, 모든 이벤트에 증가하는 `sequence`.
- 공통 필드: `type, sequence, chatGuid, isFromMe, occurredAt, actor?{address, service}`.
- Message event types: `message.received{message}`, `message.edited{messageGuid, content, editedAt}`, `message.read{messageGuid, readAt}`, `message.unsent{messageGuid, retractedAt}`, `message.reactionAdded/Removed{messageGuid, reaction, targetPartIndex?}`, `message.stickerPlaced{...}`
- Chat event types: `chat.backgroundChanged`, `chat.backgroundRemoved`, `chat.markedRead`, `chat.archived`, `chat.unarchived`
- Group change types: `displayNameChanged{displayName}`, `participantAdded/Removed/Left{participant}`, `iconChanged`, `iconRemoved`
- **복구 절차(필수 구현)**:
  1. 저장된 `lastHandledSequence` 읽기
  2. live `subscribeEvents` 스트림들을 **즉시** 연다
  3. **동시에** `catchUp(lastHandledSequence)` (생략 시 처음부터) → 끝에 `{type:"catchup.complete", headSequence}`
  4. 두 소스를 하나의 bounded-concurrency 큐로 합치고 `sequence` 로 dedupe
  5. checkpoint 는 **연속된 sequence 까지만** 전진 (실패 시 전진 중단)
- 스트림은 자동 재시도 안 됨 → 끊기면 위 절차로 재연결.

### 10.5 에러 모델

| 클래스 | gRPC status | 대응 |
|---|---|---|
| `AuthenticationError` | UNAUTHENTICATED, PERMISSION_DENIED | 토큰 갱신 |
| `NotFoundError` | NOT_FOUND | stale GUID 폐기 |
| `RateLimitError` | RESOURCE_EXHAUSTED | `retryable` 이면 나중에 |
| `ValidationError` | INVALID_ARGUMENT, FAILED_PRECONDITION | 재시도 금지 |
| `ConnectionError` | UNAVAILABLE, DEADLINE_EXCEEDED | 재시도 |
| `IMessageError` | 그 외 | 로그 |

필드: `name, message, code, retryable, grpcCode, context, cause?`. 분기는 `code` 로 (message 파싱 금지). [미확인: `code`/`context` 가 gRPC status details 의 어떤 필드로 전달되는지 — proto/dist 확인 필요]

코드: 인증 `unauthenticated tokenExpired tokenBlocked unauthorized` / 한도 `dailyLimitExceeded recipientLimitExceeded uploadRateExceeded contentDuplicateExceeded recipientCoolingDown recipientLocked sendReceiveRatioExceeded` / `duplicateMessage` / not found `chatNotFound messageNotFound attachmentNotFound addressNotFound sharedFriendLocationNotFound groupIconNotFound pollNotFound` / 검증 `invalidArgument preconditionFailed operationNotSupported attachmentNotReady privateApiUnavailable` / 인프라 `serviceUnavailable timeout internalError databaseError networkError`.

**멱등성**: `clientMessageId` — 같은 논리적 write 재시도 시 동일 값 → 서버가 원 결과 반환. (Spectrum provider 는 `autoIdempotency: true` 로 자동 부여 [소스])

---

## 11. 운영 Best practices (SDK 에 녹일 만한 것) [문서]

- **Inbound pipeline**: 메시지 burst 를 debounce(수 초)해 한 턴으로 처리. 큐 테이블에서 handler 가 직접 drain(enqueue 시 payload 에 담지 말 것). 취소된 작업의 drain 분은 `carried_messages` 로 carry-forward. in-flight 취소는 chain 시작시각과 `cancelled_at` 비교.
- **Recovery**: 안정적 `clientGuid`(`${jobId}-${index}`) + `startIndex` resume cursor + `job_failures` 감사 테이블.
- **Memory scope**: `resourceId = senderAddress`(사람 단위), `threadId = chat-${chatId}`(대화 단위).
- **Deliverability**: inbound-first, 연락처 카드 조기 공유, 첫 메시지에 링크/미디어 금지, 2–3회 이상 follow-up 금지, 새벽 발송 금지, 라인당 사용자 400–500(일반)/200–400(집약), 사용률 70–80% 시 신규 배정 중단, 휴면 라인은 ~2개월 후 비활성화.

---

## 12. 기타 표면

- **Photon CLI** (`photon`): 로그인(device flow), projects(생성/조회/`regenerate-secret`), spectrum(profile/users/lines/platforms/avatars), billing, profile. npm 또는 standalone 바이너리(darwin|linux, arm64|x64). 자세한 내용: https://photon.codes/docs/cli/overview.md
- **Voice (SIP)**: iMessage 라인으로 SIP(TLS/TCP) 발신/수신. SDK call control 은 로드맵 단계. `POST /voice/tokens`, `/voice/sip-inbound/`. 상세: `spectrum-ts/providers/voice/*.md` (본 정리에서는 미상세)
- **Integrations**: `chat-adapter-imessage` (Vercel Chat SDK 어댑터, cloud/self-host/local 3모드, 웹훅 `X-Spectrum-Signature` 검증), eve `photonIMessageChannel`.
- **기타 저수준 키트**: WhatsApp Business advanced kit, legacy `@photon-ai/advanced-imessage-kit`, open-source `imessage-kit`(macOS 로컬), `heif2jpeg` — 본 정리에서는 목차만.

---

## 13. 포팅 체크리스트 & 미확인 사항

### 13.1 단계 제안

1. **Webhook 수신 전용 SDK** — 서명검증(legacy + Standard Webhooks) + §8.5 JSON 모델. 외부 의존 최소, 가장 빨리 쓸모 있음.
2. **Spectrum API 클라이언트** — OpenAPI 로 생성 가능, Basic auth, `{succeed,data}` unwrap.
3. **iMessage gRPC 저수준 클라이언트** — `.proto` 코드 생성 + Bearer 메타데이터 + 토큰 provider 콜백 + 에러 매핑 + 스트림 + catch-up.
4. **Cloud auth 매니저** — `/imessage/tokens` 호출, shared/dedicated 분기 주소 결정, TTL×0.8 갱신, reconcile.
5. **Spectrum 추상화 계층** — Message/Space/User/Content, builder 검증 규칙(§5), 이벤트→Content 정규화, outbound echo 억제, 다중 라인 라우팅.
6. 필요 시 Fusor(WS + protobuf) 기반 Telegram/Slack, WhatsApp.

### 13.2 문서만으로 알 수 없어 소스 분석이 필요한 부분

- [ ] gRPC 이벤트(`message.received` 등) → Spectrum `Content` 매핑 규칙 (multipart → `group` 분할, tapback → `reaction`, 그룹 이벤트 → `addMember` 등, read receipt 의 reader 복원 로직)
- [ ] outbound echo 억제 및 "자신의 행위" 판별 방식 (`isActorCurrentAccount`: `phone !== "shared" && actor.address === phone` [소스 일부 확인])
- [ ] Spectrum 메시지 id(`spc-msg-<uuid>`) 와 iMessage GUID 의 관계 (webhook 은 spc-msg, SDK 내부는 GUID?)
- [ ] markdown → UTF-16 formatting range 변환, streaming text 의 "첫 청크 전송 후 edit" 주기/스로틀
- [ ] shared 모드에서 수신자 라우팅(공유 풀) 시 `space.phone` 처리 및 `getAttachment(guid, phone)` 의 phone 사용법
- [ ] gRPC 에러의 `code/context` 전달 방식(status details / trailer)
- [ ] Fusor protobuf envelope 스키마(`@photon-ai/proto`)와 WS 프로토콜
- [ ] `http.js` 전송(google.api.http 매핑)이 hosted 엔드포인트에서 사용 가능한지

### 13.3 원문 링크 (주요)

| 주제 | URL |
|---|---|
| Introduction | https://photon.codes/docs/spectrum-ts/introduction.md |
| Getting started | https://photon.codes/docs/spectrum-ts/getting-started.md |
| Messages / Spaces / Reactions / Narrowing | https://photon.codes/docs/spectrum-ts/{messages,spaces-and-users,reactions-and-replies,platform-narrowing}.md |
| Content (각 builder) | https://photon.codes/docs/spectrum-ts/content/*.md |
| iMessage provider | https://photon.codes/docs/spectrum-ts/providers/imessage/connection-and-routing.md |
| Custom platform | https://photon.codes/docs/spectrum-ts/custom-platforms.md |
| Webhooks | https://photon.codes/docs/webhooks/{overview,events,verifying-signatures,delivery,managing-webhooks}.md |
| Advanced iMessage kit | https://photon.codes/docs/advanced-kits/imessage/{getting-started,messages,chats,groups,attachments,polls,addresses,locations,events,error-handling}.md |
| API reference | https://photon.codes/docs/api-reference/introduction.md · OpenAPI https://spectrum.photon.codes/openapi/json |
| 섹션별 JSON export | https://photon.codes/docs/agent-context/stable/{spectrum,cli,webhooks,low-level-sdks,api-reference}.json |
