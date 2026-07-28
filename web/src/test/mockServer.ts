import { http, HttpResponse, delay } from "msw";
import { setupServer } from "msw/node";

export const REPRESENTATIVE_QUERY =
  "每周一上午 9 点，汇总近 7 天四川和重庆与「智算中心、服务器」有关、" +
  "预算 500 万以上的招标信息。";

export const RUN_ID = "11111111-1111-1111-1111-111111111111";
export const TEST_ADMIN_TOKEN = "test-admin-token";

function unauthorizedResponse(request: Request): Response | undefined {
  return request.headers.get("X-Admin-Token") === TEST_ADMIN_TOKEN
    ? undefined
    : HttpResponse.json({ detail: "invalid admin token" }, { status: 401 });
}

/**
 * A report payload matching the full backend DTO (`_serialize_report` in
 * `backend/src/bidscope/api/routes/reports.py`): provenance, citations, and
 * claims included. The synthetic capture_kind and example.invalid URL exercise
 * the synthetic-demo labelling + plain-text URL rules.
 */
export const reportWithEvidence = {
  id: "report-1",
  run_id: "run-1",
  export_key: "export-1",
  conditions: { region: "四川", budget: "≥500万" },
  freshness_window: "7d",
  source_availability: ["ccgp"],
  completeness_warning: null,
  generated_at: "2026-07-18T00:00:00+00:00",
  items: [
    {
      title: "四川省智算中心服务器采购招标公告",
      source: "ccgp",
      url: "https://example.invalid/demo-001",
      retrieved_at: "2026-07-18T00:00:00+00:00",
      hash_prefix: "aaaaaaaa",
      freshness_days: "2",
      known_fields: {
        region: "四川",
        budget: "5000000",
        budget_currency: "CNY",
        deadline: "2026-08-30",
      },
      unknown_fields: ["foo"],
      relevance_reason: "Matches 智算中心 and 服务器 keywords.",
      risk_note: "Short deadline.",
      provenance: {
        source: "ccgp",
        source_title: "四川省智算中心服务器采购招标公告",
        source_url: "https://example.invalid/demo-001",
        capture_kind: "synthetic_fixture",
        source_version_id: "version-1",
        parser_version: "ccgp-v1",
      },
      citations: [
        {
          evidence_id: "evidence-1",
          span_hash: "span-1",
          start: 0,
          end: 42,
          excerpt: "本项目预算金额为人民币 500 万元整。",
          label: "预算金额证据",
        },
      ],
      claims: [
        {
          text: "预算金额为 500 万元",
          citation_ids: ["evidence-1"],
        },
      ],
    },
  ],
};

export const handlers = [
  http.post("/api/runs", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
    await delay(0);
    return HttpResponse.json({
      id: RUN_ID,
      status: "awaiting_confirmation",
      user_request: REPRESENTATIVE_QUERY,
    });
  }),

  http.get("/api/runs/:id", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
    await delay(0);
    return HttpResponse.json({
      id: RUN_ID,
      status: "awaiting_confirmation",
      user_request: REPRESENTATIVE_QUERY,
    });
  }),

  http.post("/api/runs/:id/confirm", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
    await delay(0);
    return HttpResponse.json({ id: RUN_ID, status: "completed" });
  }),

  http.get("/api/runs/:id/events", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
    await delay(0);
    const body = [
      "event: intent_parsed",
      "id: 0",
      'data: {"seq":0,"node":"parse_intent","event":"intent_parsed","status":"ok","message":null,"details":null}',
      "",
      "event: needs_confirmation",
      "id: 1",
      'data: {"seq":1,"node":"confirm_intent","event":"needs_confirmation","status":"ok","message":null,"details":null}',
      "",
      "event: terminal",
      "id: terminal",
      'data: {"status":"awaiting_confirmation","terminal":true}',
      "",
    ].join("\n");
    return new HttpResponse(body, {
      headers: { "content-type": "text/event-stream" },
    });
  }),

  http.get("/api/reports/:id", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
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

  http.get("/api/reports/:id/docx", async ({ request }) => {
    const unauthorized = unauthorizedResponse(request);
    if (unauthorized) return unauthorized;
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
