/**
 * Bounded, understanding-oriented workspace failure copy (approvals /
 * plans). Same honest pattern as the chat failures module: the renderer
 * only ever sees IpcErrorCode + a bounded backend message; this composes
 * the backend's structured message with what the failure MEANS.
 */

import {
  HTTP_STATUS_HINTS,
  IPC_ERROR_COPY,
  type IpcErrorCode,
} from "../../../../shared/ipc-contract";

export type FailureShape = {
  code: IpcErrorCode;
  message: string;
  httpStatus?: number;
};

/** Main headline: the backend's structured message when it exists. */
export function workspaceHeadline(failure: FailureShape): string {
  if (failure.code === "HTTP_STATUS" && failure.message.length > 0) {
    return failure.message;
  }
  if (failure.code === "BAD_PAYLOAD" && failure.message.length > 0) {
    return failure.message;
  }
  return IPC_ERROR_COPY[failure.code] ?? "The request failed.";
}

/**
 * Meaning + consequence lines (may be null when nothing to add).
 * `consequences` is per-workspace chat/approval/plan consequence copy.
 */
export function workspaceDetail(
  failure: FailureShape,
  consequences: Partial<Record<IpcErrorCode, string>>,
): string | null {
  const hint =
    failure.httpStatus === undefined ? undefined : HTTP_STATUS_HINTS[failure.httpStatus];
  const consequence = consequences[failure.code];
  const lines = [hint, consequence].filter(
    (line): line is string => typeof line === "string",
  );
  return lines.length === 0 ? null : lines.join(" ");
}
