"use client";
import { ActivityEvent, Snapshot, fetchSnapshot } from "./api";
import { useEffect, useRef, useState } from "react";

/** One WebSocket, incremental patches over an initial snapshot. */
export function useLive() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [live, setLive] = useState(true);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let alive = true;
    let backoff = 500;

    function mergeSnapshot(s: Snapshot) {
      setSnap((prev) => {
        if (!prev) return s;
        const seen = new Set<number>();
        const merged: ActivityEvent[] = [];
        for (const a of [...prev.activity, ...s.activity]) {
          if (seen.has(a.id)) continue;
          seen.add(a.id);
          merged.push(a);
        }
        merged.sort((a, b) => b.id - a.id);
        return { ...s, activity: merged.slice(0, 160) };
      });
    }
    function patch(p: Partial<Snapshot>) {
      setSnap((prev) => (prev ? { ...prev, ...p } : prev));
    }
    function addActivity(a: ActivityEvent) {
      setSnap((prev) => {
        if (!prev) return prev;
        if (prev.activity.some((x) => x.id === a.id)) return prev;
        return { ...prev, activity: [a, ...prev.activity].slice(0, 160) };
      });
    }

    async function seed() {
      try {
        const s = await fetchSnapshot();
        if (alive) mergeSnapshot(s);
      } catch {}
    }

    function connect() {
      const url = process.env.NEXT_PUBLIC_WS_URL || "ws://127.0.0.1:4110/ws";
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => { backoff = 500; setLive(true); };
      ws.onclose = () => {
        setLive(false);
        if (alive) setTimeout(connect, Math.min(backoff, 4000));
        backoff *= 1.6;
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          switch (m.kind) {
            case "SNAPSHOT": mergeSnapshot(m.body as Snapshot); break;
            case "KPI": patch({ kpis: m.body }); break;
            case "SIGNALS": patch({ signals: m.body }); break;
            case "TRADES": patch({ open_trades: m.body }); break;
            case "ACCOUNT": patch({ account: m.body }); break;
            case "MODEL": patch({ model: m.body }); break;
            case "SAE": patch({ sae: m.body }); break;
            case "ACTIVITY": addActivity(m.body as ActivityEvent); break;
          }
        } catch {}
      };
    }

    seed();
    connect();
    return () => { alive = false; wsRef.current?.close(); };
  }, []);

  return { snap, live };
}
