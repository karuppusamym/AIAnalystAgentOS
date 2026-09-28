import { render, screen } from "@testing-library/react";
import { Component, Suspense, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { lazyWithReload } from "../lib/lazyReload";

class Boundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: Error) {
    return { error: e.message };
  }
  render() {
    return this.state.error ? <p>failed: {this.state.error}</p> : this.props.children;
  }
}

const reload = vi.fn();

function mount(load: () => Promise<{ default: () => JSX.Element }>) {
  const Page = lazyWithReload(load);
  render(<Boundary><Suspense fallback={<p>loading</p>}><Page /></Suspense></Boundary>);
}

describe("a page chunk that no longer exists after a redeploy", () => {
  afterEach(() => {
    sessionStorage.clear();
    reload.mockReset();
    vi.unstubAllGlobals();
  });

  it("reloads the app once to pick up the new build", async () => {
    vi.stubGlobal("location", { ...window.location, reload });
    mount(() => Promise.reject(new TypeError("Failed to fetch dynamically imported module")));
    await vi.waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
    expect(screen.getByText("loading")).toBeTruthy();
  });

  it("shows the error when it fails again right after the reload", async () => {
    vi.stubGlobal("location", { ...window.location, reload });
    sessionStorage.setItem("analystos.chunkReloadAt", String(Date.now()));
    mount(() => Promise.reject(new TypeError("Failed to fetch dynamically imported module")));
    expect(await screen.findByText(/failed: Failed to fetch/)).toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
  });

  it("loads normally when the chunk is there", async () => {
    mount(() => Promise.resolve({ default: () => <p>catalog page</p> }));
    expect(await screen.findByText("catalog page")).toBeTruthy();
  });
});
