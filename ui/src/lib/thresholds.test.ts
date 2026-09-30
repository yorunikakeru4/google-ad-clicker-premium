import { describe, expect, it } from "vitest";
import {
  CAPTCHA_SHARE_LIMIT,
  REQUESTS_PER_HOUR_TARGET,
  UPTIME_TARGET,
  captchaShareStatus,
  formatCaptchaShare,
  formatUptimeShare,
  requestsPerHourStatus,
  uptimeStatus,
} from "./thresholds";

describe("порог запросов/час", () => {
  it("порог зафиксирован планом: не менее 50 в час", () => {
    expect(REQUESTS_PER_HOUR_TARGET).toBe(50);
  });

  it("49 запросов/час — утверждение не выполнено", () => {
    const status = requestsPerHourStatus(49);
    expect(status.met).toBe(false);
    expect(status.tone).toBe("error");
  });

  it("ровно 50 запросов/час — утверждение выполнено", () => {
    const status = requestsPerHourStatus(50);
    expect(status.met).toBe(true);
    expect(status.tone).toBe("success");
  });

  it("больше порога — выполнено, ноль — нет", () => {
    expect(requestsPerHourStatus(120).met).toBe(true);
    expect(requestsPerHourStatus(0).met).toBe(false);
  });
});

describe("порог доли CAPTCHA", () => {
  it("порог зафиксирован планом: доля менее 5%", () => {
    expect(CAPTCHA_SHARE_LIMIT).toBe(0.05);
  });

  it("нет данных (None) — н/д без цвета", () => {
    const status = captchaShareStatus(null);
    expect(status.known).toBe(false);
    expect(status.tone).toBe("neutral");
  });

  it("0% и доля ниже порога — зелёный", () => {
    expect(captchaShareStatus(0)).toMatchObject({ known: true, withinLimit: true, tone: "success" });
    expect(captchaShareStatus(0.049_99)).toMatchObject({
      withinLimit: true,
      tone: "success",
    });
  });

  it("ровно 5% и выше — красный (условие строго «меньше»)", () => {
    expect(captchaShareStatus(CAPTCHA_SHARE_LIMIT)).toMatchObject({
      withinLimit: false,
      tone: "error",
    });
    expect(captchaShareStatus(0.5)).toMatchObject({ withinLimit: false, tone: "error" });
  });
});

describe("formatCaptchaShare", () => {
  it("нет данных — «н/д»", () => {
    expect(formatCaptchaShare(null)).toBe("н/д");
    expect(formatCaptchaShare(undefined)).toBe("н/д");
  });

  it("доля — процент с одним знаком", () => {
    expect(formatCaptchaShare(0)).toBe("0.0%");
    expect(formatCaptchaShare(0.05)).toBe("5.0%");
    expect(formatCaptchaShare(0.034_56)).toBe("3.5%");
  });
});

describe("порог uptime", () => {
  it("порог зафиксирован планом: доля ≥99%", () => {
    expect(UPTIME_TARGET).toBe(0.99);
  });

  it("нет данных (None) — н/д без цвета, не 0%", () => {
    const status = uptimeStatus(null);
    expect(status.known).toBe(false);
    expect(status.met).toBe(false);
    expect(status.tone).toBe("neutral");
    expect(uptimeStatus(undefined).known).toBe(false);
  });

  it("ровно 99% и выше — зелёный: граница включается", () => {
    expect(uptimeStatus(0.99)).toMatchObject({
      known: true,
      met: true,
      tone: "success",
    });
    expect(uptimeStatus(1)).toMatchObject({ met: true, tone: "success" });
  });

  it("меньше 99% и 0% — красный", () => {
    expect(uptimeStatus(0.989_999)).toMatchObject({
      known: true,
      met: false,
      tone: "error",
    });
    expect(uptimeStatus(0)).toMatchObject({ met: false, tone: "error" });
  });
});

describe("formatUptimeShare", () => {
  it("нет данных — «н/д»", () => {
    expect(formatUptimeShare(null)).toBe("н/д");
    expect(formatUptimeShare(undefined)).toBe("н/д");
  });

  it("доля — процент с двумя знаками", () => {
    expect(formatUptimeShare(0)).toBe("0.00%");
    expect(formatUptimeShare(0.99)).toBe("99.00%");
    expect(formatUptimeShare(1)).toBe("100.00%");
    expect(formatUptimeShare(0.987_65)).toBe("98.77%");
  });
});
