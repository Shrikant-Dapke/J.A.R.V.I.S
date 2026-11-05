/**
 * API contract fidelity tests: the committed desktop/openapi.json must
 * match the backend contract the UI is built against, and the generated
 * typed module must exist and cover the same surface. These assertions
 * pin the audit's endpoint inventory so a backend contract change fails
 * the desktop build loudly instead of drifting silently.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

type Operation = {
  parameters?: Array<{ name: string; required?: boolean; in?: string }>;
};

type Spec = {
  paths: Record<string, Partial<Record<"get" | "post", Operation>>>;
  components: { schemas: Record<string, unknown> };
};

// Runtime layout: dist-test/tests/*.test.js → desktop root is two up.
const DESKTOP_ROOT = resolve(__dirname, "..", "..");

function loadSpec(): Spec {
  return JSON.parse(readFileSync(resolve(DESKTOP_ROOT, "openapi.json"), "utf8")) as Spec;
}

const EXPECTED_PATHS = [
  "/api/chat",
  "/api/conversations",
  "/api/conversations/{conversation_id}",
  "/api/approvals",
  "/api/approvals/{approval_id}",
  "/api/approvals/{approval_id}/approve",
  "/api/approvals/{approval_id}/reject",
  "/api/system-info",
  "/api/tools/{name}/invoke",
  "/api/plans",
  "/api/plans/{plan_id}",
  "/api/plans/{plan_id}/steps/{step_id}/request",
  "/api/plans/{plan_id}/steps/{step_id}/resolve-recovery",
  "/api/health",
  "/",
].sort();

test("the contract snapshot exposes exactly the audited backend surface", () => {
  const spec = loadSpec();
  assert.deepEqual(Object.keys(spec.paths).sort(), EXPECTED_PATHS);
});

test("every protected route documents the required auth headers", () => {
  const spec = loadSpec();
  const protectedPaths = EXPECTED_PATHS.filter(
    (path) => path !== "/api/health" && path !== "/",
  );
  for (const path of protectedPaths) {
    const operations = spec.paths[path] ?? {};
    for (const [method, operation] of Object.entries(operations)) {
      assert.ok(operation, `${method.toUpperCase()} ${path} missing`);
      const names = new Set((operation.parameters ?? []).map((parameter) => parameter.name));
      assert.ok(
        names.has("authorization"),
        `${method.toUpperCase()} ${path}: bearer auth must be documented`,
      );
      assert.ok(
        names.has("X-JARVIS-Token"),
        `${method.toUpperCase()} ${path}: native token header must be documented`,
      );
      assert.ok(
        names.has("X-JARVIS-Session"),
        `${method.toUpperCase()} ${path}: session header must be documented`,
      );
    }
  }
});

test("health and root endpoints stay unauthenticated", () => {
  const spec = loadSpec();
  for (const path of ["/api/health", "/"]) {
    const operation = spec.paths[path]?.get;
    assert.ok(operation, `GET ${path} must exist`);
    const names = (operation.parameters ?? []).map((parameter) => parameter.name);
    assert.equal(names.includes("authorization"), false, `${path} takes no bearer auth`);
    assert.equal(names.includes("X-JARVIS-Session"), false, `${path} takes no session header`);
  }
});

test("response/request schemas the UI consumes exist with expected shapes", () => {
  const spec = loadSpec();
  const schemas = spec.components.schemas;

  const chatResponse = schemas["ChatResponse"] as {
    required: string[];
    properties: Record<string, unknown>;
  };
  assert.ok(chatResponse, "ChatResponse schema present");
  for (const field of ["conversation_id", "response"]) {
    assert.ok(chatResponse.required.includes(field), `ChatResponse requires ${field}`);
  }
  assert.ok("status" in chatResponse.properties, "ChatResponse documents status");

  for (const name of [
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalSummary",
    "PlanDetail",
    "PlanStep",
    "PlanStepStatus",
    "TaskPlan",
    "ToolResult",
    "ConversationHistoryResponse",
    "ConversationListResponse",
  ]) {
    assert.ok(schemas[name], `${name} schema present`);
  }

  const toolResult = schemas["ToolResult"] as { required: string[] };
  assert.ok(toolResult.required.includes("tool_name"), "ToolResult requires tool_name");
  assert.ok(toolResult.required.includes("status"), "ToolResult requires status");

  const approvalRequest = schemas["ApprovalRequest"] as { required: string[] };
  for (const field of ["approval_id", "tool_name", "action_description", "risk_level", "expires_at"]) {
    assert.ok(approvalRequest.required.includes(field), `ApprovalRequest requires ${field}`);
  }
});

test("chat request contract: bounded 1..4000 message, optional conversation", () => {
  const spec = loadSpec();
  const chatRequest = spec.components.schemas["ChatRequest"] as {
    required: string[];
    properties: {
      message: { maxLength: number; minLength: number };
      conversation_id?: unknown;
    };
  };
  assert.ok(chatRequest, "ChatRequest schema present");
  assert.deepEqual(chatRequest.required, ["message"], "conversation_id is optional");
  assert.equal(chatRequest.properties.message.maxLength, 4000);
  assert.equal(chatRequest.properties.message.minLength, 1);
  assert.ok("conversation_id" in chatRequest.properties);
});

test("conversation list/history/clear contracts match the client wire guards", () => {
  const spec = loadSpec();
  const schemas = spec.components.schemas;

  const list = schemas["ConversationListResponse"] as {
    required: string[];
    properties: Record<string, { enum?: string[] }>;
  };
  assert.ok(list, "ConversationListResponse schema present");
  for (const field of ["conversations", "total", "limit", "offset"]) {
    assert.ok(list.required.includes(field), `ConversationListResponse requires ${field}`);
  }

  const history = schemas["ConversationHistoryResponse"] as {
    required: string[];
    properties: Record<string, unknown>;
  };
  assert.ok(history, "ConversationHistoryResponse schema present");
  for (const field of ["conversation_id", "turns", "turn_count", "total_turn_count"]) {
    assert.ok(history.required.includes(field), `ConversationHistoryResponse requires ${field}`);
  }

  const clear = schemas["ConversationClearResponse"] as {
    required: string[];
    properties: { status: { const?: string } };
  };
  assert.ok(clear, "ConversationClearResponse schema present");
  assert.deepEqual(clear.required, ["conversation_id"]);
  assert.equal(clear.properties.status.const, "deleted", "clear status pins to 'deleted'");

  // Page-size bounds live on the GET /api/conversations query parameter,
  // not on the response schema.
  const listOp = spec.paths["/api/conversations"]?.["get"];
  assert.ok(listOp, "GET /api/conversations must exist");
  const limitParam = listOp.parameters?.find((parameter) => parameter.name === "limit") as
    | { schema?: { minimum?: number; maximum?: number } }
    | undefined;
  assert.ok(limitParam?.schema, "limit query parameter documents its schema");
  assert.equal(limitParam.schema?.minimum, 1, "page size lower bound documents 1");
  assert.equal(limitParam.schema?.maximum, 200, "page size upper bound documents 200");
});

test("the generated typed module matches the snapshot", () => {
  const generated = readFileSync(
    resolve(DESKTOP_ROOT, "electron", "main", "api", "schema.gen.ts"),
    "utf8",
  );
  assert.ok(generated.length > 1000, "generated types are substantive");
  for (const marker of [
    '"/api/chat"',
    '"/api/health"',
    '"/api/system-info"',
    "ChatResponse",
    "ApprovalRequest",
    "PlanDetail",
    "ToolResult",
  ]) {
    assert.ok(generated.includes(marker), `generated types contain ${marker}`);
  }
});

/* -------------------------------------------------------------------- */
/* Phase 6.3: approval + plan workspace contract fidelity                */
/* -------------------------------------------------------------------- */

test("approval listing contract: metadata page without arguments", () => {
  const spec = loadSpec();
  const schemas = spec.components.schemas;

  const list = schemas["ApprovalListResponse"] as {
    required: string[];
    properties: Record<string, { $ref?: string; items?: { $ref: string }; default?: number }>;
  };
  assert.ok(list, "ApprovalListResponse schema present");
  assert.deepEqual(list.required, ["approvals", "total", "limit", "offset"]);
  assert.equal(list.properties.approvals.items?.$ref, "#/components/schemas/ApprovalSummary");

  const summary = schemas["ApprovalSummary"] as {
    required: string[];
    properties: Record<string, unknown>;
  };
  assert.ok(summary, "ApprovalSummary schema present");
  for (const field of ["approval_id", "tool_name", "action_description", "risk_level", "expires_at"]) {
    assert.ok(summary.required.includes(field), `ApprovalSummary requires ${field}`);
  }
  assert.ok("resolved_at" in summary.properties, "resolved_at is an optional terminal stamp");
  // The listing is strictly metadata: arguments are structurally absent.
  assert.ok(
    "arguments" in summary.properties === false,
    "ApprovalSummary must not expose arguments",
  );

  // The authoritative detail binds exactly the validated arguments.
  const request = schemas["ApprovalRequest"] as {
    properties: Record<string, { maxProperties?: number; additionalProperties?: { type: string } }>;
  };
  const args = request.properties["arguments"];
  assert.ok(args, "ApprovalRequest carries the bound arguments");
  const argsProp = args as unknown as {
    additionalProperties?: { type?: string };
    maxProperties?: number;
  };
  assert.equal(argsProp.additionalProperties?.type, "string");
  assert.equal(
    argsProp.maxProperties,
    undefined,
    "the arguments ceiling lives in backend validation, not the schema",
  );
});

test("plan listing and detail contracts: backend-derived status fields", () => {
  const spec = loadSpec();
  const schemas = spec.components.schemas;

  const list = schemas["PlanListResponse"] as { required: string[] };
  assert.ok(list, "PlanListResponse schema present");
  assert.deepEqual(list.required, ["plans", "total", "limit", "offset"]);

  const summary = schemas["PlanSummary"] as {
    required: string[];
    properties: Record<string, unknown>;
  };
  assert.ok(summary, "PlanSummary schema present");
  for (const field of [
    "plan_id",
    "goal",
    "created_at",
    "updated_at",
    "step_count",
    "status_counts",
    "recovery_required",
    "awaiting_approval",
    "needs_attention",
  ]) {
    assert.ok(summary.required.includes(field), `PlanSummary requires ${field}`);
  }

  const detail = schemas["PlanDetail"] as {
    required: string[];
    properties: Record<string, { $ref?: string }>;
  };
  assert.ok(detail, "PlanDetail schema present");
  // Derived status lives on the authoritative read — the client must rely
  // on these, never compute its own.
  for (const field of [
    "overall_status",
    "ready_step_ids",
    "recovery_required",
    "awaiting_approval",
    "needs_attention",
    "steps",
  ]) {
    assert.ok(detail.required.includes(field), `PlanDetail requires ${field}`);
  }
  assert.ok(
    detail.properties["steps"] !== undefined,
    "detail steps reference the PlanStep schema",
  );
  const stepsProp = detail.properties["steps"] as unknown as {
    type?: string;
    maxItems?: number;
    minItems?: number;
    items?: { $ref?: string };
  };
  assert.equal(stepsProp.type, "array");
  assert.equal(stepsProp.minItems, 1, "a plan has at least one step");
  assert.equal(stepsProp.maxItems, 8, "the backend bounds plans at 8 steps");
  assert.equal(stepsProp.items?.$ref, "#/components/schemas/PlanStep");

  const overall = schemas["PlanOverallStatus"] as { enum?: string[] };
  assert.ok(overall?.enum, "PlanOverallStatus documents its enum");
  assert.deepEqual(
    [...overall.enum].sort(),
    [
      "recovery_required",
      "awaiting_approval",
      "actionable",
      "failed",
      "in_progress",
      "completed",
    ].sort(),
  );

  const step = schemas["PlanStep"] as {
    required: string[];
    properties: Record<string, unknown>;
  };
  for (const field of ["step_id", "description", "tool_name"]) {
    assert.ok(step.required.includes(field), `PlanStep requires ${field}`);
  }
  for (const field of ["arguments", "depends_on", "status", "approval_id", "result"]) {
    assert.ok(field in step.properties, `PlanStep documents ${field}`);
  }
  assert.ok("approval_id" in step.properties, "PlanStep can reference its approval");
  assert.ok("result" in step.properties, "PlanStep can record a ToolResult");
});

test("step request documents the 202 approval branch; recovery returns TaskPlan", () => {
  const spec = loadSpec();

  type OperationDetail = {
    responses: Record<
      string,
      {
        content?: Record<string, { schema?: { $ref?: string } }>;
      }
    >;
  };

  const requestOp = spec.paths["/api/plans/{plan_id}/steps/{step_id}/request"]?.post;
  assert.ok(requestOp, "POST request step must exist");
  const accepted = (requestOp as unknown as OperationDetail).responses["202"];
  assert.ok(accepted, "the 202 approval-required branch must be documented");
  assert.equal(
    accepted.content?.["application/json"]?.schema?.$ref,
    "#/components/schemas/ApprovalRequest",
  );

  const recoveryOp = spec.paths[
    "/api/plans/{plan_id}/steps/{step_id}/resolve-recovery"
  ]?.post;
  assert.ok(recoveryOp, "POST resolve-recovery must exist");
  assert.equal(
    (recoveryOp as unknown as OperationDetail).responses["200"].content?.["application/json"]
      ?.schema?.$ref,
    "#/components/schemas/TaskPlan",
  );

  // Identifiers are patterned in the schemas, so path injection cannot
  // occur client-side without the wire guard refusing it.
  const detailSchema = spec.components.schemas["PlanDetail"] as {
    properties: Record<
      string,
      { maxLength?: number; minLength?: number; pattern?: string }
    >;
  };
  const planIdProp = detailSchema.properties["plan_id"];
  assert.ok(planIdProp, "PlanDetail documents plan_id");
  assert.equal(planIdProp.pattern, "^[a-zA-Z0-9_-]{1,64}$");
  assert.equal(planIdProp.maxLength, 64);
});
