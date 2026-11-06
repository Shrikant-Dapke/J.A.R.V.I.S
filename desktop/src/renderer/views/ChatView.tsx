/**
 * Fully functional chat workspace (Phase 6.2): conversation rail,
 * message timeline, composer — all over the typed IPC bridge. On failed
 * sends the exact text is restored to the composer so the user's message
 * is never lost; sends are serialized and never retried automatically.
 */

import { useCallback, useState } from "react";
import { ChatComposer } from "../components/chat/ChatComposer";
import { ChatTranscript } from "../components/chat/ChatTranscript";
import { ConversationRail } from "../components/chat/ConversationRail";
import { CloseIcon, MenuIcon } from "../components/icons";
import { Badge } from "../components/primitives";
import { useChatSession, type SendOutcome } from "../hooks/useChatSession";
import { useConversations } from "../hooks/useConversations";
import { useMicCapture } from "../hooks/useMicCapture";
import type { BackendMonitor } from "../hooks/useBackendMonitor";
import { CHAT_MESSAGE_MAX_LENGTH } from "../../../shared/ipc-contract";
import { cn } from "../lib/cn";
import { mergeVoiceTranscript } from "../../../shared/voice-capture";

function shortId(conversationId: string | null): string {
  if (!conversationId) return "New conversation";
  return `Conversation ${conversationId.slice(0, 8)}…`;
}

function ConnectionBadge({ monitor }: { monitor: BackendMonitor }) {
  if (monitor.health === null) return <Badge tone="idle">checking…</Badge>;
  if (monitor.health.ok) {
    return <Badge tone="ok">backend up · {monitor.health.data.latencyMs} ms</Badge>;
  }
  return <Badge tone="danger">backend offline</Badge>;
}

function AuthBanner() {
  return (
    <div className="mx-3 mb-2 rounded-lg border border-warn/40 bg-warn/5 px-3 py-2">
      <p className="text-[12.5px] leading-relaxed text-warn">
        Backend authentication is not configured. Set <code className="font-mono">JARVIS_API_TOKEN</code>{" "}
        (same source the backend reads) and restart — chat needs the local API to answer.
      </p>
    </div>
  );
}

export function ChatView({ monitor }: { monitor: BackendMonitor }) {
  const session = useChatSession();
  const conversations = useConversations();
  const [draft, setDraft] = useState("");
  const [railOpen, setRailOpen] = useState(false);
  const handleTranscriptReady = useCallback((spoken: string) => {
    setDraft((current) => mergeVoiceTranscript(current, spoken));
  }, []);
  const microphone = useMicCapture({ onTranscriptReady: handleTranscriptReady });

  const tokenConfigured =
    monitor.status?.ok === true ? monitor.status.data.tokenConfigured : null;
  const authMissing = tokenConfigured === false;

  const handleSubmit = (content: string) => {
    void (async () => {
      const outcome: SendOutcome = await session.sendMessage(content);
      if (outcome === "failed") {
        // Never lose the user's unsent message: restore the exact text.
        setDraft(content);
      }
      if (outcome === "sent") {
        setDraft("");
        // Refresh conversation metadata now that a turn was stored.
        conversations.load();
      }
    })();
  };

  const handleDelete = (conversationId: string) => {
    void (async () => {
      const deleted = await conversations.remove(conversationId);
      if (deleted && session.state.conversationId === conversationId) {
        session.startNewChat();
      }
    })();
  };

  const railProps = {
    book: conversations,
    activeId: session.state.conversationId,
    sending: session.state.sending,
    onSelect: (id: string) => {
      session.openConversation(id);
      setRailOpen(false);
    },
    onNew: () => {
      session.startNewChat();
      setRailOpen(false);
    },
    onDelete: handleDelete,
  };

  return (
    <div className="flex h-full min-h-0 w-full">
      {/* Conversation rail — permanent on md+, drawer overlay below. */}
      <aside className="hidden h-full w-[290px] shrink-0 border-r border-line/70 bg-abyss/50 p-4 md:flex md:flex-col md:min-h-0">
        <ConversationRail {...railProps} />
      </aside>

      {railOpen ? (
        <div className="fixed inset-0 z-40 flex md:hidden">
          <button
            type="button"
            aria-label="Close conversations"
            onClick={() => setRailOpen(false)}
            className="h-full flex-1 bg-void/70 backdrop-blur-sm"
          />
          <aside className="flex h-full w-[300px] shrink-0 flex-col border-r border-line bg-abyss p-4 md:min-h-0">
            <div className="mb-2 flex items-center justify-between">
              <span className="font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
                Conversations
              </span>
              <button
                type="button"
                aria-label="Close conversations"
                onClick={() => setRailOpen(false)}
                className="focus-ring rounded p-1 text-slate-400 hover:text-accent-soft"
              >
                <CloseIcon className="h-4 w-4" />
              </button>
            </div>
            <ConversationRail {...railProps} />
          </aside>
        </div>
      ) : null}

      {/* Chat column */}
      <section className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b border-line/70 bg-abyss/20 px-4 py-2.5">
          <button
            type="button"
            aria-label="Open conversations"
            aria-expanded={railOpen}
            onClick={() => setRailOpen(true)}
            className={cn(
              "focus-ring rounded-lg border border-line p-2 text-slate-300 hover:border-accent/40 hover:text-accent-soft md:hidden",
            )}
          >
            <MenuIcon className="h-4 w-4" />
          </button>
          <h2 className="min-w-0 truncate font-mono text-[11px] uppercase tracking-[0.24em] text-slate-300">
            {session.state.loading
              ? "Loading…"
              : shortId(session.state.conversationId)}
          </h2>
          <div className="ml-auto hidden sm:block">
            <ConnectionBadge monitor={monitor} />
          </div>
        </header>

        {authMissing ? <AuthBanner /> : null}

        <div className="flex min-h-0 flex-1 flex-col">
          <ChatTranscript
            state={session.state}
            onRetryHistory={session.retryHistory}
            onUseExample={(example) => setDraft(example)}
          />
        </div>

        <div className="border-t border-line/70 bg-abyss/30 p-3">
          <ChatComposer
            draft={draft}
            onDraftChange={setDraft}
            onSubmit={handleSubmit}
            sending={session.state.sending}
            disabled={authMissing}
            disabledReason="Configure JARVIS_API_TOKEN to enable chat."
            maxLength={CHAT_MESSAGE_MAX_LENGTH}
            voice={{
              state: microphone.state,
              partial: microphone.partial,
              transcript: microphone.transcript,
              failure: microphone.failure,
              preparing: microphone.preparing,
              onStart: microphone.start,
              onPrepareModels: microphone.prepareModels,
              onStop: microphone.stop,
              onCancel: microphone.cancel,
            }}
          />
        </div>
      </section>
    </div>
  );
}
