/** Pure capture/composer policies shared by the renderer and tests. */

/** Preserve typed text and append a spoken transcript on a new line. */
export function mergeVoiceTranscript(draft: string, transcript: string): string {
  const spoken = transcript.trim();
  if (spoken.length === 0) return draft;
  if (draft.length === 0) return spoken;
  return draft.endsWith("\n") ? `${draft}${spoken}` : `${draft}\n${spoken}`;
}
