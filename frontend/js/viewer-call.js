// Laptop viewer voice-call wiring. Reuses the shared bidirectional
// controller — laptop is "caller" on room call-l2p and listens for
// incoming calls from the phone on room call-p2l.
import { createCallController } from "./call-core.js";

const ctrl = createCallController({
  outRoom: "call-l2p",
  inRoom: "call-p2l",
  peerLabel: "phone",
  statusPillId: "vcall-status",
  ids: {
    btn: "vcall-btn",
    overlay: "vcall-active",
    status: "vcall-active-status",
    timer: "vcall-timer",
    muteBtn: "vcall-mute-btn",
    endBtn: "vcall-end-btn",
    ringOverlay: "vcall-ring",
    acceptBtn: "vcall-accept-btn",
    declineBtn: "vcall-decline-btn",
  },
});

export function initViewerCall() { ctrl.init(); }
export function teardownViewerCall() { ctrl.teardown(); }
