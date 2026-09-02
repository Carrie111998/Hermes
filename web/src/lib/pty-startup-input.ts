const ASCII_PRINTABLE_RE = /^[ -~]$/;

export function shouldIgnoreSyntheticStartupInput({
  data,
  now,
  startupWindowUntil,
  lastKeyboardEventAt,
  lastCompositionEventAt,
}: {
  data: string;
  now: number;
  startupWindowUntil: number;
  lastKeyboardEventAt: number;
  lastCompositionEventAt: number;
}): boolean {
  if (now > startupWindowUntil) return false;
  if (!ASCII_PRINTABLE_RE.test(data)) return false;
  if (lastKeyboardEventAt > 0 && now - lastKeyboardEventAt < 250) return false;
  if (lastCompositionEventAt > 0 && now - lastCompositionEventAt < 250) return false;
  return true;
}
