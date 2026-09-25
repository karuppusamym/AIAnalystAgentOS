import { useCallback, useEffect, useRef, useState } from "react";
import { errorMessage } from "../api";
import { useTheme } from "./theme";

export interface AsyncState<T> {
  data: T | undefined;
  error: string | null;
  loading: boolean;
  reload: () => Promise<void>;
  setData: (d: T | undefined | ((prev: T | undefined) => T | undefined)) => void;
}

/** Load data with a promise factory; re-runs when deps change. Stale responses are discarded. */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(fn, deps);

  const reload = useCallback(async () => {
    const my = ++seq.current;
    setLoading(true);
    try {
      const d = await run();
      if (my === seq.current) {
        setData(d);
        setError(null);
      }
    } catch (err) {
      if (my === seq.current) setError(errorMessage(err));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, [run]);

  useEffect(() => {
    void reload();
  }, [reload]);

  return { data, error, loading, reload, setData };
}

/** Wrap an async action with busy + error state. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = useCallback(async <R,>(fn: () => Promise<R>): Promise<R | undefined> => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(errorMessage(err));
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []);
  return { busy, error, setError, run };
}

/** True when the dark theme is in effect: the user's pinned theme, else the OS preference (tracks changes). */
export function usePrefersDark(): boolean {
  return useTheme().resolved === "dark";
}
