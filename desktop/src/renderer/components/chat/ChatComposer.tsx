/**
 * Multiline chat composer.
 *
 * - Enter sends, Shift+Enter inserts a newline (IME-composition safe),
 * - hard character cap from the shared contract (4000),
 *   with a live counter and a ≥90% warning,
 * - every disabled state is genuine and explained (sending in flight,
 *   empty draft, auth unconfigured),
 * - the textarea auto-resizes up to a bounded height.
 */

import { useEffect, useRef, type KeyboardEvent } from "react";
import { cn } from "../../lib/cn";
import type { MicCaptureState, MicFailure } from "../../hooks/useMicCapture";
import { MicIcon, SendIcon } from "../icons";

type VoiceComposerState = {
  state: MicCaptureState;
  partial: string;
  transcript: string;
  failure: MicFailure | null;
  preparing: boolean;
  onStart: () => void;
  onPrepareModels: () => void;
  onStop: () => void;
  onCancel: () => void;
};

function voiceStateLabel(state: MicCaptureState, failure: MicFailure | null): string {
  if (state === "failed" && failure) {
    switch (failure.kind) {
      case "MICROPHONE_DENIED":
        return "Microphone denied";
      case "DEVICE_UNAVAILABLE":
        return "Device unavailable";
      case "ENGINE_UNAVAILABLE":
        return "Voice engine unavailable";
      case "MODEL_MISSING":
        return "Voice models unavailable";
      case "STT_FAILURE":
        return "Recognition failed";
      default:
        return "Microphone failed";
    }
  }
  switch (state) {
    case "requesting_permission":
      return "Requesting microphone permission";
    case "listening":
      return "Listening";
    case "transcribing":
      return "Transcribing";
    case "transcript_ready":
      return "Transcript ready — review before sending";
    case "cancelled":
      return "Cancelled — nothing was sent";
    case "failed":
      return "Microphone failed";
    default:
      return "Microphone ready";
  }
}

export function ChatComposer({
  draft,
  onDraftChange,
  onSubmit,
  sending,
  disabled,
  disabledReason,
  maxLength,
  voice,
}: {
  draft: string;
  onDraftChange: (next: string) => void;
  /** Called with the trimmed, bounded text once, when sending is allowed. */
  onSubmit: (content: string) => void;
  sending: boolean;
  /** Hard block (auth unconfigured). */
  disabled: boolean;
  disabledReason: string;
  maxLength: number;
  voice?: VoiceComposerState;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize to content, bounded.
  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 180)}px`;
  }, [draft]);

  const trimmed = draft.trim();
  const overLimit = draft.length > maxLength;
  const nearLimit = draft.length >= Math.floor(maxLength * 0.9) && !overLimit;
  const canSend = !sending && !disabled && trimmed.length > 0 && !overLimit;

  const attempt = () => {
    if (!canSend) return;
    onSubmit(trimmed);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    attempt();
  };

  const handleSend = () => {
    attempt();
  };

  const blockedReason = disabled
    ? disabledReason
    : sending
      ? "Wait for the current reply before sending again."
      : trimmed.length === 0
        ? "Type a message first."
        : undefined;
  const voiceActive =
    voice?.state === "requesting_permission" ||
    voice?.state === "listening" ||
    voice?.state === "transcribing";

  return (
    <div
      className={cn(
        "rounded-xl border bg-hull/60 backdrop-blur-sm transition-colors",
        sending ? "border-accent/35 shadow-glow-sm" : "border-line/90",
      )}
    >
      <textarea
        ref={textareaRef}
        value={draft}
        maxLength={maxLength}
        onChange={(event) => onDraftChange(event.target.value)}
        onKeyDown={handleKeyDown}
        rows={1}
        spellCheck={false}
        aria-label="Message JARVIS"
        placeholder={sending ? "JARVIS is thinking…" : "Message JARVIS — Enter to send, Shift+Enter for a new line"}
        className={cn(
          "block w-full resize-none bg-transparent px-4 pt-3 pb-1 text-[13.5px] leading-relaxed text-slate-100",
          "placeholder:text-slate-500 focus:outline-none",
        )}
        />
      {voice ? (
        <div
          className="border-t border-line/60 px-3 py-2"
          role="status"
          aria-live="polite"
          aria-label="Microphone status"
        >
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            {voiceActive ? (
              <span aria-hidden className="h-2 w-2 animate-pulse rounded-full bg-danger shadow-[0_0_10px_rgba(248,113,113,0.8)]" />
            ) : null}
            <span
              className={cn(
                "font-mono uppercase tracking-[0.12em]",
                voice.state === "failed" ? "text-danger" : voiceActive ? "text-accent-soft" : "text-slate-400",
              )}
            >
              {voiceStateLabel(voice.state, voice.failure)}
            </span>
            {voice.state === "listening" ? (
              <span className="text-slate-500">Audio stays local until recognition completes.</span>
            ) : null}
          </div>
          {voice.partial ? (
            <p className="mt-1 truncate text-[12px] italic text-slate-400">{voice.partial}</p>
          ) : null}
          {voice.state === "transcript_ready" && voice.transcript ? (
            <p className="mt-1 line-clamp-2 text-[12px] text-slate-300">{voice.transcript}</p>
          ) : null}
          {voice.failure ? (
            <p className="mt-1 text-[12px] leading-relaxed text-danger">{voice.failure.message}</p>
          ) : null}
          {voice.failure?.kind === "MODEL_MISSING" ? (
            <button
              type="button"
              onClick={voice.onPrepareModels}
              disabled={voice.preparing}
              className="mt-2 focus-ring rounded-lg border border-warn/45 bg-warn/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-warn transition-colors hover:bg-warn/20 disabled:cursor-wait disabled:opacity-60"
            >
              {voice.preparing ? "Preparing voice models…" : "Prepare voice models"}
            </button>
          ) : null}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            {!voiceActive ? (
              <button
                type="button"
                onClick={voice.onStart}
                className="focus-ring inline-flex items-center gap-1.5 rounded-lg border border-accent/45 bg-accent/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-accent-soft transition-colors hover:bg-accent/20"
                aria-label="Start microphone capture"
              >
                <MicIcon className="h-3.5 w-3.5" />
                {voice.state === "failed" || voice.state === "cancelled" ? "Try microphone again" : "Start microphone"}
              </button>
            ) : null}
            {voice.state === "listening" ? (
              <button
                type="button"
                onClick={voice.onStop}
                className="focus-ring inline-flex items-center rounded-lg border border-warn/45 bg-warn/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-warn transition-colors hover:bg-warn/20"
                aria-label="Stop microphone capture"
              >
                Stop and transcribe
              </button>
            ) : null}
            {voiceActive ? (
              <button
                type="button"
                onClick={voice.onCancel}
                className="focus-ring inline-flex items-center rounded-lg border border-line px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.12em] text-slate-400 transition-colors hover:border-danger/40 hover:text-danger"
                aria-label="Cancel microphone capture"
              >
                Cancel
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
      <div className="flex items-center justify-between gap-3 border-t border-line/60 px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <span
            className={cn(
              "font-mono text-[10px] tabular-nums",
              overLimit ? "text-danger" : nearLimit ? "text-warn" : "text-slate-500",
            )}
          >
            {draft.length}/{maxLength}
          </span>
          <span className="hidden font-mono text-[10px] uppercase tracking-[0.14em] text-slate-600 sm:inline">
            enter to send · shift+enter newline
          </span>
        </div>
        <button
          type="button"
          onClick={handleSend}
          disabled={!canSend}
          title={blockedReason ?? "Send message"}
          aria-label="Send message"
          className={cn(
            "focus-ring inline-flex items-center gap-2 rounded-lg border px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.14em] transition-all",
            canSend
              ? "border-accent/50 bg-accent/10 text-accent-soft hover:bg-accent/20 hover:shadow-glow-sm"
              : "cursor-not-allowed border-line text-slate-500",
          )}
        >
          {sending ? (
            <>
              <span aria-hidden className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
              Sending…
            </>
          ) : (
            <>
              <SendIcon className="h-3.5 w-3.5" />
              Send
            </>
          )}
        </button>
      </div>
    </div>
  );
}
