export const MAX_AUTO_RECOVERY_ATTEMPTS = 6;
const AUTO_RECOVERY_BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];

export class MirrorRecoveryGuard {
  constructor() {
    this.generation = 0;
    this.pollSequence = 0;
  }

  capture() {
    return this.generation;
  }

  invalidate() {
    this.generation += 1;
    return this.generation;
  }

  isCurrent(generation) {
    return generation === this.generation;
  }

  beginStatusPoll() {
    this.pollSequence += 1;
    return { generation: this.generation, sequence: this.pollSequence };
  }

  isCurrentStatusPoll(token) {
    return token.generation === this.generation
      && token.sequence === this.pollSequence;
  }
}

export function recoveryDelay(completedAttempts) {
  const index = Math.max(0, Math.floor(Number(completedAttempts) || 0));
  return AUTO_RECOVERY_BACKOFF_MS[index] || AUTO_RECOVERY_BACKOFF_MS.at(-1);
}

export function shouldPauseAutoRecovery(preset) {
  return preset === "record";
}

export function chooseRecoveryPreset({ automatic, linkLost, activePreset, selectedPreset }) {
  if ((automatic || linkLost) && activePreset) return activePreset;
  return selectedPreset;
}
