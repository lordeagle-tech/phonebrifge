// Phone-side voice call wiring. Reuses the shared bidirectional
// controller — phone is "caller" on room call-p2l and listens for
// incoming calls from the laptop on room call-l2p.
import { createCallController } from "./call-core.js";

const ctrl = createCallController({
  outRoom: "call-p2l",
  inRoom: "call-l2p",
  peerLabel: "laptop",
  ids: {
    btn: "call-btn",
    overlay: "call-overlay",
    status: "call-status",
    timer: "call-timer",
    muteBtn: "call-mute-btn",
    endBtn: "call-end-btn",
    ringOverlay: "call-ring",
    acceptBtn: "call-accept-btn",
    declineBtn: "call-decline-btn",
  },
});

export function initCall() { ctrl.init(); }
export function teardownCall() { ctrl.teardown(); }
