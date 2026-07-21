import { http, HttpResponse, delay } from "msw";
import { setupServer } from "msw/node";

export const REPRESENTATIVE_QUERY =
  "每周一上午 9 点，汇总近 7 天四川和重庆与「智算中心、服务器」有关、" +
  "预算 500 万以上的招标信息。";

export const RUN_ID = "11111111-1111-1111-1111-111111111111";

export const handlers = [
  http.post("/api/runs", async () => {
    await delay(0);
    return HttpResponse.json({
      id: RUN_ID,
      status: "pending",
      user_request: REPRESENTATIVE_QUERY,
    });
  }),

  http.get("/api/runs/:id", async () => {
    await delay(0);
    return HttpResponse.json({
      id: RUN_ID,
      status: "awaiting_confirmation",
      user_request: REPRESENTATIVE_QUERY,
    });
  }),

  http.post("/api/runs/:id/confirm", async () => {
    await delay(0);
    return HttpResponse.json({ id: RUN_ID, status: "completed" });
  }),

  http.get("/api/runs/:id/events", async () => {
    await delay(0);
    return HttpResponse.json({
      events: [
        { seq: 0, node: "parse_intent", event: "intent_parsed", status: "ok" },
        { seq: 1, node: "confirm_intent", event: "needs_confirmation", status: "ok" },
      ],
    });
  }),

  http.get("/api/reports/:id", async () => {
    await delay(0);
    return HttpResponse.json({
      id: RUN_ID,
      run_id: RUN_ID,
      conditions: { region: "四川", budget: "≥500万" },
      items: [
        {
          title: "四川省智算中心服务器采购招标公告",
          source: "synthetic_demo",
          url: "https://example.invalid/demo-001",
          retrieved_at: "2026-07-18T00:00:00+00:00",
          hash_prefix: "aaaaaaaa",
          freshness_days: 2,
        },
      ],
    });
  }),

  http.get("/api/reports/:id/docx", async () => {
    await delay(0);
    return new HttpResponse(new Uint8Array([0x50, 0x4b]), {
      headers: {
        "content-type":
          "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "content-disposition": `attachment; filename="bidscope-${RUN_ID}.docx"`,
      },
    });
  }),
];

export const server = setupServer(...handlers);
