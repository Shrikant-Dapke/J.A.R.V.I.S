/**
 * Conversation inventory rail: New Chat, metadata list (newest activity
 * first), refresh, and delete-with-confirmation. Metadata only — message
 * content never lives here. switching/deleting is blocked while a chat
 * request is in flight (`sending`).
 */

import { useState } from "react";
import { cn } from "../../lib/cn";
import { Badge, ErrorNote, SectionTitle } from "../primitives";
import { Button } from "../controls";
import { PlusIcon, RefreshIcon, TrashIcon } from "../icons";
import type { ConversationBook } from "../../hooks/useConversations";
import { failureDetail, failureHeadline } from "./failures";

function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function ConversationRail({
  book,
  activeId,
  sending,
  onSelect,
  onNew,
  onDelete,
}: {
  book: ConversationBook;
  activeId: string | null;
  sending: boolean;
  onSelect: (conversationId: string) => void;
  onNew: () => void;
  /** Called AFTER the human confirms; ChatView owns the actual removal. */
  onDelete: (conversationId: string) => void;
}) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const lockedList = sending || book.deletingId !== null;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="mb-3 flex items-center justify-between">
        <SectionTitle title="Conversations" suffix={`${book.total}`} />
        <Button
          variant="ghost"
          small
          onClick={book.load}
          disabled={book.loading || sending}
        >
          <RefreshIcon className={cn("h-3 w-3", book.loading && "animate-pulse-soft")} />
          {sending ? "Wait…" : "Sync"}
        </Button>
      </div>

      <Button variant="primary" onClick={onNew} disabled={sending}>
        <PlusIcon className="h-3.5 w-3.5" />
        New chat
      </Button>

      {book.failure ? (
        <div className="mt-3 animate-fade-in-up">
          <ErrorNote detail={failureHeadline(book.failure)} />
          <p className="mt-1 text-[11px] leading-snug text-slate-500">
            {failureDetail(book.failure)}
          </p>
          {book.failure.scope === "load" ? (
            <div className="mt-2">
              <Button variant="ghost" small onClick={book.load} disabled={book.loading}>
                <RefreshIcon className="h-3 w-3" />
                Retry
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="scroll-hud mt-3 min-h-0 flex-1">
        {book.loading && !book.loaded ? (
          <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.16em] text-slate-500">
            <span aria-hidden className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-accent" />
            Loading conversations…
          </p>
        ) : book.loaded && book.items.length === 0 ? (
          <p className="px-1 py-3 text-[12.5px] leading-relaxed text-slate-500">
            No conversations yet. New chats appear here once JARVIS replies.
          </p>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {book.items.map((item) => {
              const active = item.conversationId === activeId;
              const confirming = confirmingId === item.conversationId;
              const deleting = book.deletingId === item.conversationId;
              return (
                <li key={item.conversationId} className="animate-fade-in-up">
                  {confirming ? (
                    <div className="rounded-lg border border-danger/40 bg-danger/5 px-3 py-2">
                      <p className="text-[11.5px] leading-snug text-danger">
                        Delete this conversation? This clears its stored history.
                      </p>
                      <div className="mt-2 flex gap-2">
                        <Button
                          variant="danger"
                          small
                          disabled={deleting}
                          onClick={() => {
                            setConfirmingId(null);
                            onDelete(item.conversationId);
                          }}
                        >
                          {deleting ? "Deleting…" : "Delete"}
                        </Button>
                        <Button
                          variant="ghost"
                          small
                          disabled={deleting}
                          onClick={() => setConfirmingId(null)}
                        >
                          Keep
                        </Button>
                      </div>
                    </div>
                  ) : (
                    <div
                      role="button"
                      tabIndex={lockedList ? -1 : 0}
                      aria-current={active ? "true" : undefined}
                      onClick={() => {
                        if (!lockedList) onSelect(item.conversationId);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" && !lockedList) onSelect(item.conversationId);
                      }}
                      className={cn(
                        "group flex cursor-pointer items-center gap-2 rounded-lg border px-2.5 py-2 transition-colors focus-ring",
                        active
                          ? "border-accent/40 bg-accent/10"
                          : "border-transparent hover:border-line hover:bg-white/[0.03]",
                      )}
                    >
                      <div className="min-w-0 flex-1">
                        <p className="font-mono text-[10.5px] text-slate-300">
                          <span className={active ? "text-accent-soft" : "text-slate-400"}>
                            {formatWhen(item.updatedAt)}
                          </span>{" "}
                          · {item.turnCount} turns
                        </p>
                        <p className="truncate font-mono text-[9.5px] text-slate-600">
                          {item.conversationId}
                        </p>
                      </div>
                      <button
                        type="button"
                        aria-label={`Delete conversation ${item.conversationId}`}
                        disabled={lockedList}
                        onClick={(event) => {
                          event.stopPropagation();
                          setConfirmingId(item.conversationId);
                        }}
                        className={cn(
                          "focus-ring rounded p-1 text-slate-500 transition-colors hover:text-danger",
                          lockedList && "invisible",
                        )}
                      >
                        <TrashIcon className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="mt-2 flex items-center justify-between border-t border-line/60 pt-2">
        <Badge tone="idle">metadata only</Badge>
        <button
          type="button"
          onClick={onNew}
          disabled={sending}
          className="focus-ring font-mono text-[10px] uppercase tracking-[0.14em] text-slate-500 hover:text-accent-soft disabled:opacity-40"
        >
          new chat
        </button>
      </div>
    </div>
  );
}
