export type DaemonState = "running" | "paused" | "stopped" | "unknown";

export const DAEMON_TRANSITIONS = ["start", "pause", "stop"] as const;

export type DaemonAction = (typeof DAEMON_TRANSITIONS)[number];
