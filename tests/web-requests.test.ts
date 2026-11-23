import { afterEach, describe, expect, mock, spyOn, test } from "bun:test";
import { errorMessage, request } from "../web/src/api";
import { RequestGate } from "../web/src/request-gate";

afterEach(() => mock.restore());

describe("dashboard request ownership", () => {
  test("a second submit is ignored before React can render its busy state", () => {
    const action = new RequestGate();
    const first = action.begin()!;
    expect(action.begin()).toBeUndefined();
    expect(action.owns(first)).toBe(true);
    action.finish(first);
    expect(action.begin()).toBeInstanceOf(AbortSignal);
    action.cancel();
  });

  test("a superseded refresh cannot publish data or release the newer request", () => {
    const refresh = new RequestGate();
    const old = refresh.begin()!;
    const current = refresh.begin(true)!;
    expect(old.aborted).toBe(true);
    expect(refresh.owns(old)).toBe(false);
    refresh.finish(old);
    expect(refresh.begin()).toBeUndefined();
    expect(refresh.owns(current)).toBe(true);
    refresh.finish(current);
    expect(refresh.owns(current)).toBe(false);
  });

  test("unmount/route cleanup invalidates callbacks while allowing a remount", () => {
    const action = new RequestGate();
    const old = action.begin()!;
    action.cancel();
    const remounted = action.begin()!;
    expect(old.aborted).toBe(true);
    expect(action.owns(old)).toBe(false);
    action.finish(old);
    expect(action.owns(remounted)).toBe(true);
    action.cancel();
  });
});

describe("dashboard API failures", () => {
  test("invalid successful responses fail visibly instead of returning null", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("<html>not the API</html>"),
    );
    await expect(request("/settings")).rejects.toThrow("invalid response");
  });

  test("API errors retain the useful server message", async () => {
    spyOn(globalThis, "fetch").mockResolvedValue(
      Response.json(
        { error: "Project directory does not exist." },
        { status: 400 },
      ),
    );
    await expect(
      request("/scans", { method: "POST", body: "{}" }),
    ).rejects.toThrow("Project directory does not exist.");
  });

  test("caller cancellation reaches fetch alongside the timeout and client headers", async () => {
    const controller = new AbortController();
    let observed: RequestInit | undefined;
    spyOn(globalThis, "fetch").mockImplementation(Object.assign(async (_url: URL | RequestInfo, options?: RequestInit) => {
      observed = options;
      return Response.json({ scans: [] });
    }, { preconnect: fetch.preconnect }));
    await expect(
      request("/scans", { signal: controller.signal }),
    ).resolves.toEqual({ scans: [] });
    expect(new Headers(observed?.headers).get("X-OpenHack-Client")).toBe(
      "dashboard",
    );
    expect(observed?.cache).toBe("no-store");
    expect(observed?.signal?.aborted).toBe(false);
    controller.abort();
    expect(observed?.signal?.aborted).toBe(true);
  });

  test("timeout guidance accounts for mutations that may already have completed", () => {
    expect(errorMessage(new DOMException("timeout", "TimeoutError"))).toContain(
      "Check the current state before retrying",
    );
  });
});
