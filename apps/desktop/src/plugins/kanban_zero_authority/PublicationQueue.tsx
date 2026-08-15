import React, { useEffect, useState } from "react";
import { KanbanSecurityApi } from "./api";
import type { PublicationIntent } from "./types";

export function PublicationQueue({ api }: { api: KanbanSecurityApi }) {
  const [items, setItems] = useState<PublicationIntent[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    api.publications().then((value) => active && setItems(value)).catch((reason) => {
      if (active) setError(String(reason));
    });
    return () => { active = false; };
  }, [api]);

  if (error) return <p role="alert">{error}</p>;
  return (
    <section aria-labelledby="kanban-publication-heading">
      <h2 id="kanban-publication-heading">Pending publication</h2>
      {items.length === 0 ? <p>No pending publication intents.</p> : null}
      <ul>
        {items.map((item) => (
          <li key={item.intent_id}>
            <strong>{item.kind}</strong> — {item.state}
            <code>{item.wire_sha256}</code>
            <button
              type="button"
              onClick={async () => {
                await api.decide(item.intent_id, item.wire_sha256, "approve");
                setItems((current) => current.filter((entry) => entry.intent_id !== item.intent_id));
              }}
            >
              Approve exact bytes
            </button>
            <button
              type="button"
              onClick={async () => {
                await api.decide(item.intent_id, item.wire_sha256, "reject");
                setItems((current) => current.filter((entry) => entry.intent_id !== item.intent_id));
              }}
            >
              Reject
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
