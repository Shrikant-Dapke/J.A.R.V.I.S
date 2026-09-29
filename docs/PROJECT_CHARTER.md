# JARVIS — Project Charter
**Version:** 1.0 | **Status:** Baseline

## Vision
Build a practical, hybrid AI desktop assistant for Windows that understands natural-language requests, plans bounded tasks, uses explicitly permitted tools, retains user-approved context, and verifies outcomes.

## Purpose
JARVIS is a flagship software engineering project focused on reliable assistance rather than an unrestricted autonomous agent or a cinematic interface alone.

## Goals
- Desktop text interaction, with voice as a planned capability.
- Cloud and local model providers behind a replaceable adapter.
- Small allowlisted desktop toolset.
- Approval for sensitive or mutating actions.
- Local, user-controlled memory and task history.
- Verify tool outcomes and report failures honestly.
- Keep basic local functions usable when cloud services are unavailable.

## Initial platform
Windows desktop. Target development hardware: mainstream laptop with 16 GB RAM and 6 GB GPU; local model compatibility and performance must be measured.

## Product principles
1. Safety before autonomy.
2. Explicit permissions and least privilege.
3. Verification before claiming completion.
4. Modular, replaceable providers.
5. Local-first user-controlled storage.
6. Build only features justified by a real use case.

## v0.1 non-goals
- Custom OS or programming language.
- Unrestricted shell or arbitrary code execution.
- Autonomous financial transactions or external messaging.
- Always-on recording or surveillance.
- Multi-agent orchestration.
- Guaranteed offline LLM reasoning.
- 3D holographic UI as a prerequisite.

## Initial success criteria
- Converse through a configured model.
- Retrieve basic system information.
- Launch an allowlisted application.
- Create a directory only under an approved root after authorization.
- Validate, log, and independently verify mutating actions.
- Handle provider errors honestly.
- Allow the user to inspect and delete stored memory.

## Governance
This charter is the scope baseline. Changes to goals or safety principles require an explicit documented decision.
