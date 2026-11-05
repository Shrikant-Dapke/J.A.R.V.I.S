/**
 * Bounded, understanding-oriented chat failure copy. The renderer only
 * ever sees IpcErrorCode + a bounded backend message — this composes the
 * honest headline ("what the backend said") with what the failure MEANS
 * (policy denial, validation, provider outage…).
 */

import {
  CHAT_ERROR_CONSEQUENCES,
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
export function failureHeadline(failure: FailureShape): string {
  if (failure.code === "HTTP_STATUS" && failure.message.length > 0) {
    return failure.message;
  }
  if (failure.code === "BAD_PAYLOAD" && failure.message.length > 0) {
    return failure.message;
  }
  return IPC_ERROR_COPY[failure.code] ?? "The request failed.";
}

/** Meaning + consequence lines (may be null when nothing to add). */
export function failureDetail(failure: FailureShape): string | null {
  const hint =
    failure.httpStatus === undefined ? undefined : HTTP_STATUS_HINTS[failure.httpStatus];
  const consequence = CHAT_ERROR_CONSEQUENCES[failure.code];
  const lines = [hint, consequence].filter(
    (line): line is string => typeof line === "string",
  );
  if (lines.length === 0) return null;
  return lines.join(" ");
}
