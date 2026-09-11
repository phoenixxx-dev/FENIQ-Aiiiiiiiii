/**
 * اختبارات عميل الـAPI — منطق حقيقي يستحق الفحص:
 * تجديد التوكن المنتهي، وقراءة رسالة الخطأ العربية من الخادم.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, tokens } from "../api";

const store: Record<string, string> = {};

beforeEach(() => {
  for (const k of Object.keys(store)) delete store[k];
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => {
      store[k] = v;
    },
    removeItem: (k: string) => {
      delete store[k];
    },
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

describe("رسائل الخطأ", () => {
  it("يعرض رسالة الخادم العربية كما هي", async () => {
    tokens.set("a", "r");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          { error_code: "not_ready", message_ar: "الملف لم تكتمل معالجته بعد." },
          409,
        ),
      ),
    );

    await expect(api.getDashboard("d1")).rejects.toThrow(
      "الملف لم تكتمل معالجته بعد.",
    );
  });

  it("يحمل رمز الخطأ ليتصرّف الكود بناءً عليه", async () => {
    tokens.set("a", "r");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ error_code: "rate_limited", message_ar: "محاولات كثيرة." }, 429),
      ),
    );

    await expect(api.listDatasets()).rejects.toMatchObject({
      code: "rate_limited",
      status: 429,
    });
  });

  it("لا ينهار لو لم يكن رد الخادم JSON", async () => {
    tokens.set("a", "r");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 502,
        json: async () => {
          throw new Error("ليس JSON");
        },
      })) as unknown as typeof fetch,
    );

    const err = await api.listDatasets().catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBeTruthy();
  });
});

describe("تجديد الجلسة تلقائياً", () => {
  it("يجدّد التوكن مرة واحدة عند 401 ثم يعيد الطلب", async () => {
    tokens.set("old-token", "refresh-1");

    const calls: string[] = [];
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push(url);
      if (url.endsWith("/auth/refresh")) {
        return jsonResponse({ access_token: "new-token", refresh_token: "refresh-2" });
      }
      const auth = new Headers(init?.headers).get("Authorization");
      if (auth === "Bearer old-token") {
        return jsonResponse({ error_code: "invalid_token", message_ar: "منتهية." }, 401);
      }
      return jsonResponse([{ id: "d1" }]);
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    const result = await api.listDatasets();

    expect(result).toEqual([{ id: "d1" }]);
    expect(calls.filter((c) => c.endsWith("/auth/refresh"))).toHaveLength(1);
    expect(tokens.access).toBe("new-token");
  });

  it("لا يدخل حلقة لا نهائية لو فشل التجديد أيضاً", async () => {
    tokens.set("old-token", "refresh-bad");

    const fetchMock = vi.fn(async (url: string) => {
      if (url.endsWith("/auth/refresh")) return jsonResponse({}, 401);
      return jsonResponse({ error_code: "invalid_token", message_ar: "منتهية." }, 401);
    });
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

    await expect(api.listDatasets()).rejects.toBeInstanceOf(ApiError);
    // طلب أصلي + تجديد واحد فاشل فقط — بلا تكرار
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(3);
  });
});

describe("تخزين الجلسة", () => {
  it("يمسح التوكنين عند الخروج", () => {
    tokens.set("a", "r");
    expect(tokens.access).toBe("a");
    tokens.clear();
    expect(tokens.access).toBeNull();
    expect(tokens.refresh).toBeNull();
  });
});

/** يبني استجابة بث SSE من نصّها الخام. */
function sseResponse(text: string, status = 200) {
  const chunks = new TextEncoder().encode(text);
  let sent = false;
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => ({}),
    body: {
      getReader: () => ({
        read: async () =>
          sent ? { done: true, value: undefined } : ((sent = true), { done: false, value: chunks }),
        cancel: async () => undefined,
      }),
    },
  } as unknown as Response;
}

describe("متابعة الوظيفة", () => {
  const running = {
    id: "j1", dataset_id: "d1", status: "running",
    progress: 35, stage: "profiling", stage_ar: "توصيف الأعمدة", error_ar: null,
  };
  const finished = { ...running, status: "succeeded", progress: 100, stage_ar: "جاهز" };

  it("يقرأ أحداث البث ويعيد اللقطة النهائية", async () => {
    tokens.set("a", "r");
    const text =
      `event: progress\ndata: ${JSON.stringify(running)}\n\n` +
      `event: done\ndata: ${JSON.stringify(finished)}\n\n`;
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse(text)));

    const seen: string[] = [];
    const final = await api.followJob("j1", (j) => seen.push(j.stage_ar));

    expect(seen).toEqual(["توصيف الأعمدة", "جاهز"]);
    expect(final.status).toBe("succeeded");
    expect(final.progress).toBe(100);
  });

  it("يرتدّ للـpolling عند فشل البث — لا يتعطّل", async () => {
    tokens.set("a", "r");
    let call = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        call += 1;
        // البث يفشل (وسيط لا يمرّره مثلاً)
        if (String(url).endsWith("/stream")) return jsonResponse({ error_code: "x" }, 502);
        return jsonResponse(call === 2 ? running : finished);
      }),
    );

    const seen: number[] = [];
    const final = await api.followJob("j1", (j) => seen.push(j.progress));

    expect(seen).toEqual([35, 100]);
    expect(final.status).toBe("succeeded");
  });

  it("لا يبتلع فشل الوظيفة: يعيدها بحالتها وسببها العربي", async () => {
    tokens.set("a", "r");
    const failed = { ...running, status: "failed", error_ar: "صيغة الملف غير مدعومة." };
    const text = `event: progress\ndata: ${JSON.stringify(failed)}\n\n` +
                 `event: done\ndata: ${JSON.stringify(failed)}\n\n`;
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse(text)));

    const final = await api.followJob("j1", () => undefined);
    expect(final.status).toBe("failed");
    expect(final.error_ar).toBe("صيغة الملف غير مدعومة.");
  });

  it("يتعامل مع حدث مقسوم على دفعتين", async () => {
    tokens.set("a", "r");
    const parts = [
      `event: progress\ndata: ${JSON.stringify(running)}\n\nevent: do`,
      `ne\ndata: ${JSON.stringify(finished)}\n\n`,
    ].map((p) => new TextEncoder().encode(p));
    let i = 0;
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true, status: 200, json: async () => ({}),
      body: {
        getReader: () => ({
          read: async () =>
            i < parts.length ? { done: false, value: parts[i++] } : { done: true, value: undefined },
          cancel: async () => undefined,
        }),
      },
    } as unknown as Response)));

    const final = await api.followJob("j1", () => undefined);
    expect(final.status).toBe("succeeded");
  });
});

describe("الرفع", () => {
  function fakeXhr(status: number, responseText = "") {
    const instances: Record<string, unknown>[] = [];
    class FakeXHR {
      status = status;
      responseText = responseText;
      upload = { onprogress: null as ((e: ProgressEvent) => void) | null };
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      method = "";
      url = "";
      open(m: string, u: string) {
        this.method = m;
        this.url = u;
      }
      setRequestHeader() {}
      send() {
        instances.push({ method: this.method, url: this.url });
        this.upload.onprogress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent);
        this.onload?.();
      }
    }
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    return instances;
  }

  const file = new File(["hello"], "sales.xlsx");

  it("يستعمل الرابط الموقّع: طلب رابط ← PUT ← تأكيد", async () => {
    tokens.set("a", "r");
    const sent = fakeXhr(204);
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(String(url));
        if (String(url).endsWith("/datasets/upload-url"))
          return jsonResponse({ dataset_id: "d1", url: "/uploads/tkt", method: "PUT",
                                headers: {}, expires_in: 900, direct: false }, 201);
        return jsonResponse({ dataset_id: "d1", job_id: "j1" }, 202);
      }),
    );

    const percents: number[] = [];
    const out = await api.upload(file, (p) => percents.push(p));

    expect(sent[0].method).toBe("PUT");
    expect(String(sent[0].url)).toContain("/uploads/tkt");
    expect(calls.some((u) => u.endsWith("/datasets/d1/complete"))).toBe(true);
    expect(percents).toContain(50);
    expect(out.job_id).toBe("j1");
  });

  it("يرتدّ للرفع عبر الـAPI لو تعذّر الرابط الموقّع", async () => {
    tokens.set("a", "r");
    const sent = fakeXhr(202, JSON.stringify({ dataset_id: "d2", job_id: "j2" }));
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error_code: "x" }, 404)));

    const out = await api.upload(file);
    expect(sent[0].method).toBe("POST");
    expect(String(sent[0].url)).toContain("/datasets");
    expect(out.job_id).toBe("j2");
  });

  it("لا يرتدّ عند تجاوز الحجم: يُظهر الرفض كما هو", async () => {
    tokens.set("a", "r");
    fakeXhr(204);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ error_code: "file_too_large", message_ar: "الملف يتجاوز الحد المسموح." }, 413),
      ),
    );
    await expect(api.upload(file)).rejects.toMatchObject({ code: "file_too_large" });
  });
});

describe("تجديد التوكن المتزامن", () => {
  it("سبعة طلبات تنتهي صلاحيتها معاً تُنتج نداء تجديد واحداً", async () => {
    tokens.set("expired", "r1");
    let refreshCalls = 0;
    let firstRound = true;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.endsWith("/auth/refresh")) {
          refreshCalls += 1;
          // تأخير مقصود: بدونه ينتهي كل تجديد قبل أن يبدأ التالي فيمرّ
          // الاختبار حتى بلا الإصلاح
          await new Promise((r) => setTimeout(r, 10));
          return jsonResponse({ access_token: "fresh", refresh_token: "r2" });
        }
        if (firstRound) return jsonResponse({ error_code: "invalid_token" }, 401);
        return jsonResponse({ ok: true });
      }),
    );

    const calls = Array.from({ length: 7 }, () => api.listDatasets());
    setTimeout(() => {
      firstRound = false;
    }, 5);
    await Promise.all(calls);

    expect(refreshCalls).toBe(1);
  });

  it("يسمح بتجديد لاحق بعد انتهاء الأول", async () => {
    tokens.set("expired", "r1");
    let refreshCalls = 0;
    let round = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.endsWith("/auth/refresh")) {
          refreshCalls += 1;
          return jsonResponse({ access_token: "fresh", refresh_token: "r2" });
        }
        round += 1;
        return round % 2 === 1
          ? jsonResponse({ error_code: "invalid_token" }, 401)
          : jsonResponse({ ok: true });
      }),
    );

    await api.listDatasets();
    await api.listDatasets();
    expect(refreshCalls).toBe(2);
  });
});

describe("معرّف البلاغ في الأخطاء", () => {
  it("يلتقط request_id من جسم الخطأ", async () => {
    tokens.set("a", "r");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse(
          {
            error_code: "internal_error",
            message_ar: "صار خطأ غير متوقَّع. جرّب مرة ثانية.",
            request_id: "abc123def456",
          },
          500,
        ),
      ),
    );
    await expect(api.listDatasets()).rejects.toMatchObject({
      code: "internal_error",
      requestId: "abc123def456",
    });
  });

  it("لا ينكسر عندما لا يُرسل الخادم معرّفاً", async () => {
    tokens.set("a", "r");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => jsonResponse({ error_code: "x", message_ar: "خطأ" }, 500)),
    );
    await expect(api.listDatasets()).rejects.toMatchObject({ requestId: undefined });
  });
});
