export class VimeoError extends Error {
  constructor(
    message: string,
    public statusCode?: number
  ) {
    super(message);
    this.name = "VimeoError";
  }
}

export class RateLimitError extends VimeoError {
  constructor(
    message: string,
    public retryAfter: number
  ) {
    super(message, 429);
    this.name = "RateLimitError";
  }
}