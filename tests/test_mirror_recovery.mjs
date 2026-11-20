import assert from "node:assert/strict";
import test from "node:test";
import {
  chooseRecoveryPreset,
  MAX_AUTO_RECOVERY_ATTEMPTS,
  MirrorRecoveryGuard,
  recoveryDelay,
  shouldPauseAutoRecovery,
} from "../frontend/js/mirror-recovery.mjs";

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

async function attemptRelaunch(guard, generation, waitForDevice, relaunch) {
  await waitForDevice;
  if (!guard.isCurrent(generation)) return false;
  await relaunch();
  return true;
}

test("Stop invalidates a reconnect already waiting on device detection", async () => {
  const guard = new MirrorRecoveryGuard();
  const generation = guard.capture();
  const detection = deferred();
  let relaunches = 0;
  const pending = attemptRelaunch(
    guard, generation, detection.promise,
    async () => { relaunches += 1; },
  );

  guard.invalidate(); // Stop pressed while the ADB request is in flight.
  detection.resolve();
  assert.equal(await pending, false);
  assert.equal(relaunches, 0);
});

test("switching phones invalidates a pending retry for the old selection", async () => {
  const guard = new MirrorRecoveryGuard();
  const generation = guard.capture();
  const retry = deferred();
  let launchedSerial = null;
  const pending = attemptRelaunch(
    guard, generation, retry.promise,
    async () => { launchedSerial = "old-phone"; },
  );

  guard.invalidate(); // The selection-change handler invalidates the old serial.
  retry.resolve();
  assert.equal(await pending, false);
  assert.equal(launchedSerial, null);
});

test("recovery preserves the interrupted preset, while a fresh manual launch uses selection", () => {
  assert.equal(chooseRecoveryPreset({
    automatic: true, linkLost: true, activePreset: "low-bitrate", selectedPreset: "fullscreen",
  }), "low-bitrate");
  assert.equal(chooseRecoveryPreset({
    automatic: false, linkLost: true, activePreset: "default", selectedPreset: "record",
  }), "default");
  assert.equal(chooseRecoveryPreset({
    automatic: false, linkLost: false, activePreset: "default", selectedPreset: "fullscreen",
  }), "fullscreen");
});

test("status polls invalidate older responses and guard against Stop", () => {
  const guard = new MirrorRecoveryGuard();
  const oldPoll = guard.beginStatusPoll();
  const newerPoll = guard.beginStatusPoll();
  assert.equal(guard.isCurrentStatusPoll(oldPoll), false);
  assert.equal(guard.isCurrentStatusPoll(newerPoll), true);
  guard.invalidate();
  assert.equal(guard.isCurrentStatusPoll(newerPoll), false);
});

test("recording sessions do not auto-relaunch and retries stay bounded", () => {
  assert.equal(shouldPauseAutoRecovery("record"), true);
  assert.equal(shouldPauseAutoRecovery("fullscreen"), false);
  assert.equal(MAX_AUTO_RECOVERY_ATTEMPTS, 6);
  assert.deepEqual(
    Array.from({ length: MAX_AUTO_RECOVERY_ATTEMPTS }, (_, i) => recoveryDelay(i)),
    [1000, 2000, 4000, 8000, 16000, 30000],
  );
  assert.equal(recoveryDelay(99), 30000);
});
