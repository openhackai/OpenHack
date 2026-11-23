/** A non-2xx dashboard API response; `status` is the HTTP status. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request<T>(
  path: string,
  { timeoutMs = 30_000, ...options }: RequestInit & { timeoutMs?: number } = {},
): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const response = await fetch(`/api${path}`, {
    ...options,
    signal: options.signal
      ? AbortSignal.any([options.signal, timeout])
      : timeout,
    headers: {
      "Content-Type": "application/json",
      "X-OpenHack-Client": "dashboard",
      ...options.headers,
    },
    cache: "no-store",
  });
  const payload: unknown = await response.json().catch((error: unknown) => {
    if (error instanceof SyntaxError) return null;
    throw error;
  });
  if (!response.ok) {
    const message =
      payload &&
      typeof payload === "object" &&
      "error" in payload &&
      typeof payload.error === "string"
        ? payload.error
        : `Request failed (${response.status}).`;
    throw new ApiError(message, response.status);
  }
  if (payload === null || typeof payload !== "object") {
    throw new Error(
      "The local dashboard returned an invalid response. Please refresh and try again.",
    );
  }
  return payload as T;
}

export const errorMessage = (error: unknown): string =>
  error instanceof Error && error.name === "TimeoutError"
    ? "The local dashboard did not respond in time. Check the current state before retrying."
    : error instanceof Error
      ? error.message
      : "Something went wrong. Please try again.";
