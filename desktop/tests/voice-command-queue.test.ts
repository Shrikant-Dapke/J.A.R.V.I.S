import test from "node:test";
import assert from "node:assert/strict";
import { createVoiceCommandQueue } from "../shared/voice-command-queue";

test("voice lifecycle commands execute in submission order", async () => {
  const queue = createVoiceCommandQueue();
  const order: string[] = [];

  const first = queue.run(async () => {
    order.push("arm");
    await Promise.resolve();
    order.push("begin");
  });
  const second = queue.run(async () => {
    order.push("cancel");
  });

  await Promise.all([first, second]);
  assert.deepEqual(order, ["arm", "begin", "cancel"]);
});

test("a failed voice command does not block cancellation or recovery", async () => {
  const queue = createVoiceCommandQueue();
  const order: string[] = [];

  await assert.rejects(
    queue.run(async () => {
      order.push("failed-begin");
      throw new Error("begin failed");
    }),
    /begin failed/u,
  );
  await queue.run(async () => {
    order.push("cancel");
  });
  await queue.run(async () => {
    order.push("arm-again");
  });

  assert.deepEqual(order, ["failed-begin", "cancel", "arm-again"]);
  await queue.idle();
});
