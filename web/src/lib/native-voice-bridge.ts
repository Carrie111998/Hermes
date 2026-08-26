export type NativeVoiceCommand = "check" | "start" | "stop" | "cancel";
export type NativeVoiceEventName =
  | "availability"
  | "ready"
  | "listening"
  | "partial"
  | "final"
  | "error"
  | "ended";

export interface NativeVoiceEvent {
  version: 1;
  event: NativeVoiceEventName;
  transcript?: string;
  available?: boolean;
  message?: string;
  fatal?: boolean;
}

export interface NativeVoiceMessageTarget {
  postMessage(message: string): void;
  addEventListener(type: "message", listener: (event: MessageEvent) => void): void;
  removeEventListener(type: "message", listener: (event: MessageEvent) => void): void;
}

const EVENT_NAMES = new Set<NativeVoiceEventName>([
  "availability", "ready", "listening", "partial", "final", "error", "ended",
]);

function parseNativeVoiceEvent(data: unknown): NativeVoiceEvent | null {
  if (typeof data !== "string") return null;
  try {
    const value = JSON.parse(data) as Record<string, unknown>;
    if (value.version !== 1 || typeof value.event !== "string" || !EVENT_NAMES.has(value.event as NativeVoiceEventName)) {
      return null;
    }
    if (value.transcript !== undefined && typeof value.transcript !== "string") return null;
    if (value.available !== undefined && typeof value.available !== "boolean") return null;
    if (value.message !== undefined && typeof value.message !== "string") return null;
    if (value.fatal !== undefined && typeof value.fatal !== "boolean") return null;
    return value as unknown as NativeVoiceEvent;
  } catch {
    return null;
  }
}

export interface NativeVoiceBridgeConnection {
  command(command: NativeVoiceCommand): void;
  disconnect(): void;
}

/**
 * Bind the optional AndroidX WebKit WebMessage listener object.
 *
 * The native host owns origin allow-listing when it injects this target. The
 * web app neither assumes a specific Android recognizer nor receives provider
 * credentials. Missing/malformed targets fail closed to ordinary Web Speech.
 */
export function connectNativeVoiceBridge(
  target: NativeVoiceMessageTarget | undefined,
  onEvent: (event: NativeVoiceEvent) => void,
): NativeVoiceBridgeConnection | null {
  if (!target || typeof target.postMessage !== "function" || typeof target.addEventListener !== "function" || typeof target.removeEventListener !== "function") {
    return null;
  }
  const listener = (message: MessageEvent) => {
    const event = parseNativeVoiceEvent(message.data);
    if (event) onEvent(event);
  };
  target.addEventListener("message", listener);
  return {
    command(command) {
      target.postMessage(JSON.stringify({ version: 1, command }));
    },
    disconnect() {
      target.removeEventListener("message", listener);
    },
  };
}
