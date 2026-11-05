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
import { SendIcon } from "../icons";

export function ChatComposer({
  draft,
  onDraftChange,
  onSubmit,
  sending,
  disabled,
  disabledReason,
  maxLength,
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
