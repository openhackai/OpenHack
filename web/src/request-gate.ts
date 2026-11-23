/** Tracks request ownership, independently of React's batched state updates. */
export class RequestGate {
  private controller: AbortController | undefined;

  begin(replace = false): AbortSignal | undefined {
    if (this.controller && !replace) return;
    this.cancel();
    this.controller = new AbortController();
    return this.controller.signal;
  }

  owns(signal: AbortSignal): boolean {
    return !signal.aborted && this.controller?.signal === signal;
  }

  finish(signal: AbortSignal): void {
    if (this.controller?.signal === signal) this.controller = undefined;
  }

  cancel(): void {
    this.controller?.abort();
    this.controller = undefined;
  }
}
