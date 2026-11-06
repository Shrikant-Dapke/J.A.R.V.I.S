/**
 * Conversation timeline: message bubbles with timestamps, the genuine
 * pending/thinking state, honest failure chips (draft restored), and
 * structured proposal cards for `action_proposed` / `plan_proposed`
 * exchanges. Auto-scroll sticks to the bottom unless the user scrolled
 * up to read older turns.
 */

import { useLayoutEffect, useRef } from "react";
import {
  type ChatMessage,
  type ChatSessionState,
} from "../../../../shared/chat-engine";
import { cn } from "../../lib/cn";
import { Badge, ErrorNote } from "../primitives";
import { Button } from "../controls";
import { RefreshIcon, SpeakerIcon, StopIcon } from "../icons";
import { failureDetail, failureHeadline } from "./failures";
import type { VoicePlaybackView } from "../../hooks/useVoicePlayback";

const WELCOME_EXAMPLES = [
  "What is my system information?",
  "Remember that I prefer concise answers.",
  "Create a directory for my project notes.",
];

function formatStamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** Bounded stringification for proposal argument values. */
function stringifyValue(value: unknown): string {
  let text: string;
  if (typeof value === "string") text = value;
  else {
    try {
      text = JSON.stringify(value) ?? "…";
    } catch {
      text = "…";
    }
  }
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

function riskTone(risk: string): "ok" | "warn" | "danger" | "idle" {
  if (risk === "READ_ONLY") return "idle";
  if (risk === "LOW_RISK") return "warn";
  if (risk === "HIGH_RISK") return "danger";
  return "idle";
}

function statusTone(status: string): "ok" | "warn" | "danger" | "idle" {
  if (status === "COMPLETED" || status === "EXECUTED" || status === "APPROVED") return "ok";
  if (status === "PENDING" || status === "AWAITING_APPROVAL") return "warn";
  if (status === "FAILED" || status === "RECOVERY_REQUIRED" || status === "REJECTED") return "danger";
  return "idle";
}

function UserBubble({ message }: { message: ChatMessage }) {
  const failed = message.kind === "failed";
  const detail = message.failure ? failureDetail(message.failure) : null;
  return (
    <div className="flex justify-end">
      <div
        className={cn(
          "max-w-[78%] rounded-2xl border px-4 py-2.5",
          failed
            ? "border-danger/45 bg-danger/5"
            : message.kind === "pending"
              ? "border-accent/30 bg-accent/5 opacity-90"
              : "border-accent/40 bg-accent/10",
        )}
      >
        <p className="text-[13.5px] leading-relaxed whitespace-pre-wrap break-words text-slate-100">
          {message.content}
        </p>
        {failed && message.failure ? (
          <div className="mt-2 rounded-lg border border-danger/40 bg-danger/10 px-2.5 py-1.5">
            <p className="text-[11.5px] leading-snug text-danger">
              Not delivered — {failureHeadline(message.failure)}
            </p>
            {detail ? <p className="mt-0.5 text-[11px] leading-snug text-slate-400">{detail}</p> : null}
          </div>
        ) : null}
        <p className="mt-1 text-right font-mono text-[10px] text-slate-500">
          {formatStamp(message.createdAt)}
        </p>
      </div>
    </div>
  );
}

function ExecutedToolLine({ message }: { message: ChatMessage }) {
  const result = message.proposal?.toolResult;
  if (!result) return null;
  return (
    <div className="mt-2 rounded-lg border border-line bg-hull/50 px-3 py-2 font-mono text-[11px] text-slate-300">
      <span className={result.success ? "text-ok" : "text-danger"}>●</span>{" "}
      <span className="text-accent-soft">{result.toolName}</span> → {result.status} ·{" "}
      <span className="text-slate-400">{result.message}</span>
    </div>
  );
}

function ActionProposalCard({ message }: { message: ChatMessage }) {
  const action = message.proposal?.action;
  if (!action) return null;
  const argumentEntries = Object.entries(action.arguments);
  return (
    <div className="mt-2 rounded-lg border border-accent/30 bg-accent/5 p-3">
      <div className="mb-1.5 flex flex-wrap items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-accent-soft">
          Proposed action
        </span>
        <Badge tone={riskTone(action.riskLevel)}>{action.riskLevel}</Badge>
        <Badge tone={statusTone(action.approvalStatus)}>{action.approvalStatus}</Badge>
      </div>
      <p className="text-[12.5px] leading-relaxed text-slate-200">{action.description}</p>
      <p className="mt-1 font-mono text-[10.5px] text-slate-400">
        tool <span className="text-slate-200">{action.toolName}</span>
        {" · approval "}
        <span className="font-mono text-slate-300">{action.approvalId}</span>
      </p>
      {argumentEntries.length > 0 ? (
        <div className="mt-2 rounded bg-void/40 px-2.5 py-1.5">
          {argumentEntries.map(([key, value]) => (
            <p key={key} className="font-mono text-[10.5px] text-slate-400">
              <span className="text-slate-500">{key}</span>: {stringifyValue(value)}
            </p>
          ))}
        </div>
      ) : null}
      <p className="mt-2 text-[11px] leading-snug text-slate-500">
        Approval expires {new Date(action.expiresAt).toLocaleString()} — execution happens once,
        through the backend approval flow after your explicit decision there.
      </p>
    </div>
  );
}

function PlanProposalCard({ message }: { message: ChatMessage }) {
  const plan = message.proposal?.plan;
  if (!plan) return null;
  return (
    <div className="mt-2 rounded-lg border border-violet/35 bg-violet/5 p-3">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-violet-soft">
          Proposed plan
        </span>
        <Badge tone="idle">{plan.steps.length} steps</Badge>
      </div>
      <p className="text-[12.5px] leading-relaxed text-slate-200">{plan.goal}</p>
      <ol className="mt-2 space-y-1.5">
        {plan.steps.map((step, index) => (
          <li key={step.stepId} className="flex items-start gap-2">
            <span className="mt-0.5 font-mono text-[10px] text-slate-500">
              {String(index + 1).padStart(2, "0")}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-[12.5px] leading-snug text-slate-200">{step.description}</p>
              <p className="font-mono text-[10.5px] text-slate-500">
                {step.stepId} · {step.toolName}
                {step.dependsOn.length > 0 ? ` · after ${step.dependsOn.join(", ")}` : ""}
              </p>
            </div>
            <Badge tone={statusTone(step.status)}>{step.status}</Badge>
          </li>
        ))}
      </ol>
      <p className="mt-2 text-[11px] leading-snug text-slate-500">
        Steps execute one at a time, each through policy and approval, in the Plans workspace.
      </p>
    </div>
  );
}

function playbackLabel(state: VoicePlaybackView["state"]): string {
  switch (state) {
    case "preparing":
      return "Preparing voice model";
    case "synthesizing":
      return "Synthesizing locally";
    case "playing":
      return "JARVIS is speaking";
    case "completed":
      return "Playback completed";
    case "cancelled":
      return "Playback cancelled";
    case "failed":
      return "Playback failed";
    default:
      return "";
  }
}

function AssistantBubble({
  message,
  playback,
}: {
  message: ChatMessage;
  playback: VoicePlaybackView;
}) {
  const current = playback.activeMessageId === message.id;
  const active =
    current &&
    (playback.state === "preparing" ||
      playback.state === "synthesizing" ||
      playback.state === "playing");
  const showStatus = current && playback.state !== "idle";
  return (
    <div className="flex justify-start">
      <div className="max-w-[86%] rounded-2xl border border-line bg-hull/60 px-4 py-2.5">
        <div className="flex items-start gap-3">
          <p className="min-w-0 flex-1 text-[13.5px] leading-relaxed whitespace-pre-wrap break-words text-slate-200">
            {message.content}
          </p>
          <button
            type="button"
            onClick={() => (active ? playback.stop() : playback.play(message.id, message.content))}
            className={cn(
              "focus-ring mt-0.5 shrink-0 rounded-lg border p-1.5 transition-colors",
              active
                ? "border-warn/50 bg-warn/10 text-warn hover:bg-warn/20"
                : "border-line text-slate-500 hover:border-accent/45 hover:text-accent-soft",
            )}
            aria-label={active ? "Stop speaking this response" : "Speak this response"}
            title={active ? "Stop speaking" : "Speak response"}
          >
            {active ? <StopIcon className="h-3.5 w-3.5" /> : <SpeakerIcon className="h-3.5 w-3.5" />}
          </button>
        </div>
        {message.proposal ? <ExecutedToolLine message={message} /> : null}
        {message.proposal ? <ActionProposalCard message={message} /> : null}
        {message.proposal ? <PlanProposalCard message={message} /> : null}
        {showStatus ? (
          <div className="mt-2 border-t border-line/60 pt-2" role="status" aria-live="polite">
            <p
              className={cn(
                "font-mono text-[10px] uppercase tracking-[0.14em]",
                active ? "text-accent-soft" : playback.state === "failed" ? "text-danger" : "text-slate-500",
              )}
            >
              {playbackLabel(playback.state)}
            </p>
            {playback.failure ? (
              <p className="mt-1 text-[11px] leading-relaxed text-danger">{playback.failure.message}</p>
            ) : null}
            {playback.failure?.code === "MODEL_MISSING" ? (
              <button
                type="button"
                onClick={playback.prepareModels}
                disabled={playback.preparing}
                className="focus-ring mt-2 rounded-lg border border-warn/45 bg-warn/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-warn transition-colors hover:bg-warn/20 disabled:cursor-wait disabled:opacity-60"
              >
                {playback.preparing ? "Preparing voice models…" : "Prepare voice models"}
              </button>
            ) : null}
          </div>
        ) : null}
        <p className="mt-1 font-mono text-[10px] text-slate-500">
          {formatStamp(message.createdAt)}
        </p>
      </div>
    </div>
  );
}

function ThinkingRow() {
  return (
    <div className="flex items-center gap-2.5 px-1 py-1" role="status" aria-live="polite">
      <span className="flex gap-1">
        <span className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
        <span
          className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent"
          style={{ animationDelay: "150ms" }}
        />
        <span
          className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent"
          style={{ animationDelay: "300ms" }}
        />
      </span>
      <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-slate-500">
        JARVIS is thinking
      </span>
    </div>
  );
}

function WelcomeScreen({ onUseExample }: { onUseExample: (text: string) => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <span
        aria-hidden
        className="mb-6 flex h-14 w-14 items-center justify-center rounded-2xl border border-accent/30 bg-accent/5 shadow-glow"
      >
        <span className="h-2.5 w-2.5 animate-pulse-soft rounded-full bg-accent" />
      </span>
      <h3 className="font-mono text-[13px] tracking-[0.3em] text-slate-100">CONSOLE ONLINE</h3>
      <p className="mt-3 max-w-md text-[13px] leading-relaxed text-slate-400">
        Your local assistant is ready. Messages are processed by the JARVIS backend on your machine;
        tools run only within policy and approval boundaries.
      </p>
      <div className="mt-6 flex flex-col items-stretch gap-2">
        {WELCOME_EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => onUseExample(example)}
            className="focus-ring rounded-lg border border-line px-4 py-2 text-left text-[12.5px] text-slate-300 transition-colors hover:border-accent/40 hover:text-accent-soft"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}

export function ChatTranscript({
  state,
  onRetryHistory,
  onUseExample,
  playback,
}: {
  state: ChatSessionState;
  onRetryHistory: () => void;
  onUseExample: (text: string) => void;
  playback: VoicePlaybackView;
}) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);

  // Stick to the newest turn unless the user deliberately scrolled up.
  const handleScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    stickRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 160;
  };

  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (!element || !stickRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [state.messages.length, state.sending, state.loading, state.truncated, state.conversationId]);

  const isWelcome =
    state.conversationId === null &&
    !state.loading &&
    state.messages.length === 0 &&
    state.loadError === null;

  return (
    <div
      ref={scrollRef}
      onScroll={handleScroll}
      className="scroll-hud h-full min-h-0 flex-1 px-4 py-4"
      aria-live="polite"
    >
      {state.loading ? (
        <p className="flex items-center gap-2.5 font-mono text-[11px] uppercase tracking-[0.16em] text-slate-500">
          <span aria-hidden className="h-2 w-2 animate-pulse-soft rounded-full bg-accent" />
          Loading conversation…
        </p>
      ) : null}

      {!state.loading && state.loadError ? (
        <div className="animate-fade-in-up">
          <ErrorNote detail={failureHeadline(state.loadError)} />
          <div className="mt-3">
            <Button variant="ghost" small onClick={onRetryHistory} disabled={false}>
              <RefreshIcon className="h-3 w-3" />
              Retry
            </Button>
          </div>
        </div>
      ) : null}

      {isWelcome ? <WelcomeScreen onUseExample={onUseExample} /> : null}

      <div className="flex flex-col gap-3">
        {state.messages.map((message) =>
          message.role === "user" ? (
            <UserBubble key={message.id} message={message} />
          ) : (
            <AssistantBubble key={message.id} message={message} playback={playback} />
          ),
        )}
      </div>

      {state.truncated ? (
        <p className="mt-4 text-center font-mono text-[10.5px] uppercase tracking-[0.14em] text-slate-500">
          showing the newest window of this history · older turns stay stored on the backend
        </p>
      ) : null}

      {state.sending ? <ThinkingRow /> : null}
    </div>
  );
}
