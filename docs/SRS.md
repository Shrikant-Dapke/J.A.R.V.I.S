# JARVIS — Software Requirements Specification
**Version:** 1.0 | **Scope:** MVP / v0.1

## 1. Overview
A Windows desktop assistant with a local backend, configurable cloud/local model adapters, controlled tools, approval gates, local persistence, and result verification.

## 2. Roles
- **Owner:** configures providers, permissions, memory, and settings.
- **User:** submits requests, reviews plans, approves/denies actions, and inspects outcomes.

## 3. Functional requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-01 | Accept text requests and display responses. | P0 |
| FR-02 | Connect to a configured cloud model adapter. | P0 |
| FR-03 | Represent tool calls as structured intents, not free-form executable commands. | P0 |
| FR-04 | Expose only registered allowlisted tools. | P0 |
| FR-05 | Retrieve bounded system information. | P0 |
| FR-06 | Launch configured applications by allowlisted identifier. | P0 |
| FR-07 | Create directories only under an approved root after authorization. | P0 |
| FR-08 | Validate arguments and enforce path boundaries. | P0 |
| FR-09 | Require approval for mutating/sensitive actions. | P0 |
| FR-10 | Verify postconditions before reporting success. | P0 |
| FR-11 | Record action, approval, result, timestamp, and error status. | P0 |
| FR-12 | Store conversation history and user-approved memory locally. | P1 |
| FR-13 | Let users inspect and delete memory. | P1 |
| FR-14 | Support optional local model adapter where compatible. | P1 |
| FR-15 | Add speech-to-text and text-to-speech. | P1 |
| FR-16 | Present visible bounded plans for supported multi-step tasks. | P1 |

## 4. Non-functional requirements
- **Security:** no unrestricted shell; least-privilege tool interfaces; secrets excluded from source control and logs.
- **Reliability:** failures/timeouts must not produce false success claims.
- **Privacy:** local memory is user-controlled; cloud-bound content should be clear/configurable.
- **Maintainability:** UI, orchestration, model adapters, tools, storage, and verification remain separate.
- **Usability:** show exact proposed action and target before approval.
- **Performance:** measure on target hardware; do not promise latency before testing.

## 5. Initial tool contracts
### `get_system_info`
Read-only; return a bounded documented set of OS/device/resource fields. Avoid serial numbers, account identifiers, and unrelated personal data.

### `open_application`
Accept only a configured application ID. Never accept arbitrary model-supplied command-line strings. Verify launch using a process or application-specific signal.

### `create_directory`
Accept a validated relative name under a configured root. Resolve and validate final path; reject traversal and unsafe symlink escapes; bind approval to exact operation and target; create and verify directory. Report existing directory distinctly.

## 6. Approval
Approval is bound to exact tool and validated arguments, expires, and is invalidated if parameters change. Denial/cancellation prevents execution.

## 7. Structured outcomes
Tools return `success`, `status`, `verified`, and a safe message. Model text is not evidence of success. If verification fails, report uncertainty/failure.

## 8. Acceptance tests
- Chat reaches configured model and displays response.
- Unknown tools and invalid arguments are rejected.
- No approval means no filesystem mutation.
- Paths outside configured root are rejected.
- Existing directory returns `already_exists`.
- Created directory is independently verified.
- Non-allowlisted app IDs are rejected.
- System info exposes only documented fields.
- Provider outage produces clear error.
- Audit history excludes secrets.

## 9. Out of scope
Arbitrary terminal commands, bulk deletion, external messaging, unattended background actions, unrestricted screen/mouse control.
