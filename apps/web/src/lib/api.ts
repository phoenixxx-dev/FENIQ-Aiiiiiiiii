/**
 * عميل الـAPI الوحيد. كل نداء للخادم يمر من هنا.
 *
 * قاعدة: لا يوجد نص عربي مكتوب هنا — رسائل الخطأ تأتي من الخادم بالعربية
 * (message_ar) لأنه هو من يعرف السبب الحقيقي.
 */
import { t } from "@/i18n";
import type { paths } from "./generated-types";

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000";

const ACCESS_KEY = "phoenix_access";
const REFRESH_KEY = "phoenix_refresh";

export type Dashboard =
  paths["/datasets/{dataset_id}/dashboard"]["get"]["responses"][200]["content"]["application/json"];
export type RowsPage =
  paths["/datasets/{dataset_id}/rows"]["get"]["responses"][200]["content"]["application/json"];
export type DatasetOut =
  paths["/datasets/{dataset_id}"]["get"]["responses"][200]["content"]["application/json"];
export type JobOut =
  paths["/jobs/{job_id}"]["get"]["responses"][200]["content"]["application/json"];
/** كلها من العقد المولّد — لا نوع مكتوب يدوياً (الخطة §13.1). */
export type SemanticSchemaOut =
  paths["/datasets/{dataset_id}/schema"]["get"]["responses"][200]["content"]["application/json"];
export type SemanticColumn = SemanticSchemaOut["columns"][number];
export type CurrencyOut =
  paths["/datasets/currencies"]["get"]["responses"][200]["content"]["application/json"][number];
export type CleaningOut =
  paths["/datasets/{dataset_id}/cleaning"]["get"]["responses"][200]["content"]["application/json"];
export type CleaningOperation = CleaningOut["recipe"]["operations"][number];
export type OperationResult = CleaningOut["changelog"]["results"][number];

export type ProfileOut =
  paths["/datasets/{dataset_id}/profile"]["get"]["responses"][200]["content"]["application/json"];
export type ColumnProfile = ProfileOut["columns"][number];

export type ConceptOut =
  paths["/datasets/concepts"]["get"]["responses"][200]["content"]["application/json"][number];

export type UploadUrlOut =
  paths["/datasets/upload-url"]["post"]["responses"][201]["content"]["application/json"];
export type AskOut =
  paths["/datasets/{dataset_id}/ask"]["post"]["responses"][200]["content"]["application/json"];

export type WarehouseOut =
  paths["/v1/warehouses"]["get"]["responses"][200]["content"]["application/json"][number];
export type TokenInfo =
  paths["/v1/warehouses/{warehouse_id}/tokens"]["get"]["responses"][200]["content"]["application/json"][number];
export type TokenOut =
  paths["/v1/warehouses/{warehouse_id}/tokens"]["post"]["responses"][201]["content"]["application/json"];
export type SyncRunOut =
  paths["/v1/warehouses/{warehouse_id}/syncs"]["get"]["responses"][200]["content"]["application/json"][number];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    /** معرّف الطلب — يُعرض للمستخدم عند الخطأ غير المتوقَّع ليبلّغ عنه */
    readonly requestId?: string,
  ) {
    super(message);
  }
}

export const tokens = {
  get access() {
    return typeof window === "undefined" ? null : localStorage.getItem(ACCESS_KEY);
  },
  get refresh() {
    return typeof window === "undefined" ? null : localStorage.getItem(REFRESH_KEY);
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

async function toError(res: Response): Promise<ApiError> {
  let code = "http_error";
  // رسالة احتياطية فقط لو الخادم لم يرد بشكل مفهوم (انقطاع شبكة مثلاً)
  let message = t("errors.network");
  let requestId: string | undefined;
  try {
    const body = await res.json();
    code = body.error_code ?? code;
    message = body.message_ar ?? message;
    requestId = body.request_id;
  } catch {
    /* الرد ليس JSON — نُبقي الرسالة الاحتياطية */
  }
  return new ApiError(res.status, code, message, requestId);
}

type Options = RequestInit & { auth?: boolean; retryOnExpired?: boolean };

async function request<T>(path: string, options: Options = {}): Promise<T> {
  const { auth = true, retryOnExpired = true, ...init } = options;
  const headers = new Headers(init.headers);
  if (auth && tokens.access) headers.set("Authorization", `Bearer ${tokens.access}`);

  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });

  // توكن الوصول عمره 30 دقيقة — نجدّده مرة واحدة بصمت بدل إخراج المستخدم
  if (res.status === 401 && auth && retryOnExpired && tokens.refresh) {
    const renewed = await tryRefresh();
    if (renewed) return request<T>(path, { ...options, retryOnExpired: false });
  }
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// طلب تجديد واحد مشترك.
//
// شاشة الملف تُطلق سبعة طلبات متوازية. عند انتهاء التوكن ترجع كلها 401
// معاً، فيرسل كلٌّ منها طلب تجديد مستقلاً: سبعة نداءات بدل واحد، وسباق
// على الكتابة في التخزين، وخطر اصطدام بحد المحاولات. وإن أُضيف يوماً
// إبطال التوكن القديم عند التدوير، صار هذا خروجاً عشوائياً للمستخدم.
let inFlightRefresh: Promise<boolean> | null = null;

function tryRefresh(): Promise<boolean> {
  if (!inFlightRefresh) {
    inFlightRefresh = doRefresh().finally(() => {
      inFlightRefresh = null;
    });
  }
  return inFlightRefresh;
}

async function doRefresh(): Promise<boolean> {
  try {
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: tokens.refresh }),
    });
    if (!res.ok) return false;
    const body = await res.json();
    tokens.set(body.access_token, body.refresh_token);
    return true;
  } catch {
    return false;
  }
}

const POLL_INTERVAL_MS = 1500;

function isFinal(job: JobOut): boolean {
  return job.status === "succeeded" || job.status === "failed";
}

/** يتابع الوظيفة عبر بث SSE. يرمي خطأً ليرتدّ المتصل للـpolling. */
async function streamJob(
  id: string,
  onUpdate: (job: JobOut) => void,
  signal?: AbortSignal,
): Promise<JobOut> {
  const res = await fetch(`${API_BASE}/jobs/${id}/stream`, {
    headers: tokens.access ? { Authorization: `Bearer ${tokens.access}` } : {},
    signal,
  });
  if (!res.ok || !res.body) throw await toError(res);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let last: JobOut | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // الأحداث مفصولة بسطر فارغ؛ آخر جزء قد يكون ناقصاً فيبقى بالمخزن
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() ?? "";
    for (const block of blocks) {
      let name = "";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (!name || !data) continue;
      const job = JSON.parse(data) as JobOut;
      last = job;
      onUpdate(job);
      if (name === "done") {
        await reader.cancel();
        return job;
      }
      // مهلة البث: الوظيفة ما زالت تعمل — نُكمل بالـpolling
      if (name === "timeout") {
        await reader.cancel();
        return pollJob(id, onUpdate, signal);
      }
    }
  }
  // انقطع البث قبل النهاية — الـpolling يُكمل من حيث توقّف
  if (last && isFinal(last)) return last;
  return pollJob(id, onUpdate, signal);
}

/** شبكة الأمان: سؤال دوري عن الحالة. */
async function pollJob(
  id: string,
  onUpdate: (job: JobOut) => void,
  signal?: AbortSignal,
): Promise<JobOut> {
  for (;;) {
    if (signal?.aborted) throw new ApiError(0, "aborted", "");
    const job = await request<JobOut>(`/jobs/${id}`);
    onUpdate(job);
    if (isFinal(job)) return job;
    await new Promise((r) => setTimeout(r, POLL_INTERVAL_MS));
  }
}

/** رفع ببايتات خام مع نسبة تقدّم حقيقية (fetch لا يعطي تقدّم رفع). */
function putWithProgress(
  url: string,
  file: File,
  headers: Record<string, string>,
  onProgress?: (percent: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    for (const [k, v] of Object.entries(headers)) xhr.setRequestHeader(k, v);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress)
        onProgress(Math.round((e.loaded / e.total) * 100));
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      let code = "http_error";
      let message = t("errors.uploadFailed");
      try {
        const body = JSON.parse(xhr.responseText);
        code = body.error_code ?? code;
        message = body.message_ar ?? message;
      } catch {
        /* الخادم لم يرد بشكل مفهوم */
      }
      reject(new ApiError(xhr.status, code, message));
    };
    xhr.onerror = () => reject(new ApiError(0, "network_error", t("errors.network")));
    xhr.send(file);
  });
}

/** الطريق الاحتياطي: الملف يمر عبر الـAPI. يعمل، لكنه لا يتوسّع. */
function uploadThroughApi(file: File, onProgress?: (percent: number) => void) {
  return new Promise<{ dataset_id: string; job_id: string; duplicate_of: string | null }>(
    (resolve, reject) => {
      const form = new FormData();
      form.append("file", file);
      const xhr = new XMLHttpRequest();
      xhr.open("POST", `${API_BASE}/datasets`);
      if (tokens.access) xhr.setRequestHeader("Authorization", `Bearer ${tokens.access}`);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && onProgress)
          onProgress(Math.round((e.loaded / e.total) * 100));
      };
      xhr.onload = () => {
        try {
          const body = JSON.parse(xhr.responseText);
          if (xhr.status >= 200 && xhr.status < 300) resolve(body);
          else
            reject(new ApiError(xhr.status, body.error_code ?? "http_error",
                                body.message_ar ?? t("errors.uploadFailed")));
        } catch {
          reject(new ApiError(xhr.status, "http_error", t("errors.uploadFailed")));
        }
      };
      xhr.onerror = () => reject(new ApiError(0, "network_error", t("errors.network")));
      xhr.send(form);
    },
  );
}

export const api = {
  async register(email: string, password: string) {
    const body = await request<{ access_token: string; refresh_token: string }>(
      "/auth/register",
      {
        method: "POST",
        auth: false,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      },
    );
    tokens.set(body.access_token, body.refresh_token);
  },

  async login(email: string, password: string) {
    const body = await request<{ access_token: string; refresh_token: string }>(
      "/auth/login",
      {
        method: "POST",
        auth: false,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      },
    );
    tokens.set(body.access_token, body.refresh_token);
  },

  listDatasets: () => request<DatasetOut[]>("/datasets"),
  getDataset: (id: string) => request<DatasetOut>(`/datasets/${id}`),
  deleteDataset: (id: string) => request<void>(`/datasets/${id}`, { method: "DELETE" }),
  getJob: (id: string) => request<JobOut>(`/jobs/${id}`),

  /**
   * يتابع وظيفة حتى تنتهي.
   *
   * المسار المفضّل: بث SSE — الخادم يدفع كل تغيّر فور حدوثه، فلا تأخير
   * ولا طلب كل ثانية ونصف. لو تعذّر البث لأي سبب (شبكة، وسيط لا يمرّر
   * البث، مهلة) نرتدّ للـpolling بصمت: التجربة تبطؤ ولا تتعطّل.
   */
  followJob(
    id: string,
    onUpdate: (job: JobOut) => void,
    signal?: AbortSignal,
  ): Promise<JobOut> {
    return streamJob(id, onUpdate, signal).catch(() =>
      pollJob(id, onUpdate, signal),
    );
  },
  getDashboard: (id: string) => request<Dashboard>(`/datasets/${id}/dashboard`),
  getSchema: (id: string) => request<SemanticSchemaOut>(`/datasets/${id}/schema`),
  getProfile: (id: string) => request<ProfileOut>(`/datasets/${id}/profile`),
  listConcepts: () => request<ConceptOut[]>("/datasets/concepts"),
  getCleaning: (id: string) => request<CleaningOut>(`/datasets/${id}/cleaning`),

  /** الحالة النهائية للمفاتيح المعطَّلة — يُرجع وظيفة إعادة حساب. */
  patchCleaning: (id: string, disabled: string[]) =>
    request<{ dataset_id: string; job_id: string }>(`/datasets/${id}/cleaning`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disabled }),
    }),

  listCurrencies: () => request<CurrencyOut[]>("/datasets/currencies"),

  /** تصحيح معاني الأعمدة أو تحديد العملة — يُرجع وظيفة إعادة حساب لا نتيجة فورية.
   *  العملة تُرسل فقط عند طلبها صراحةً (set_currency)، فـ`null` تعني «امسح
   *  اختياري وعُد للاستنتاج»، وغيابُها يعني «لا تلمس ما اخترته سابقاً». */
  patchSchema: (
    id: string,
    columns: { column_name: string; concept: string | null }[],
    currency?: { code: string | null },
  ) =>
    request<{ dataset_id: string; job_id: string }>(`/datasets/${id}/schema`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(
        currency
          ? { columns, currency: currency.code, set_currency: true }
          : { columns },
      ),
    }),

  getRows: (
    id: string,
    page = 1,
    pageSize = 25,
    search = "",
    sortBy = "",
    descending = false,
  ) =>
    request<RowsPage>(
      `/datasets/${id}/rows?page=${page}&page_size=${pageSize}` +
        (search ? `&search=${encodeURIComponent(search)}` : "") +
        (sortBy ? `&sort_by=${encodeURIComponent(sortBy)}&descending=${descending}` : ""),
    ),

  getSuggestions: (id: string) => request<string[]>(`/datasets/${id}/suggestions`),

  ask: (id: string, question: string) =>
    request<AskOut>(`/datasets/${id}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    }),

  /**
   * الرفع: رابط موقّع ← الملف يذهب للتخزين مباشرة ← تأكيد يبدأ المعالجة.
   *
   * لماذا هكذا؟ لأن ملف 100 ميغابايت يمر عبر الـAPI يعني ذاكرة ومهلة وعنق
   * زجاجة. لو تعذّر هذا المسار (خادم أقدم مثلاً) نرتدّ للرفع المباشر عبر
   * الـAPI — يعمل، لكنه لا يتوسّع.
   */
  async upload(file: File, onProgress?: (percent: number) => void) {
    let ticket: UploadUrlOut;
    try {
      ticket = await request<UploadUrlOut>("/datasets/upload-url", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, size: file.size }),
      });
    } catch (e) {
      // الحد الأقصى يُرفض هنا قبل إرسال بايت واحد — نُظهره كما هو
      if (e instanceof ApiError && e.code === "file_too_large") throw e;
      return uploadThroughApi(file, onProgress);
    }

    const url = ticket.url.startsWith("http") ? ticket.url : `${API_BASE}${ticket.url}`;
    await putWithProgress(url, file, ticket.headers ?? {}, onProgress);

    const done = await request<{ dataset_id: string; job_id: string }>(
      `/datasets/${ticket.dataset_id}/complete`,
      { method: "POST" },
    );
    return { ...done, duplicate_of: null as string | null };
  },

  // --- المستودعات ومزامنة FeniqSync (المرحلة ١) ---
  listWarehouses: () => request<WarehouseOut[]>("/v1/warehouses"),
  createWarehouse: (name: string) =>
    request<WarehouseOut>("/v1/warehouses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  listTokens: (warehouseId: string) =>
    request<TokenInfo[]>(`/v1/warehouses/${warehouseId}/tokens`),
  /** المفتاح الصريح يرجع هنا مرة واحدة فقط — الخادم لا يحفظه. */
  createToken: (warehouseId: string, label: string | null) =>
    request<TokenOut>(`/v1/warehouses/${warehouseId}/tokens`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label }),
    }),
  revokeToken: (warehouseId: string, tokenId: string) =>
    request<void>(`/v1/warehouses/${warehouseId}/tokens/${tokenId}`, { method: "DELETE" }),
  listSyncs: (warehouseId: string) =>
    request<SyncRunOut[]>(`/v1/warehouses/${warehouseId}/syncs?limit=10`),
  syncEndpoint: () => `${API_BASE}/v1/sync/catalog`,

  exportUrl: (id: string, fmt: "csv" | "xlsx") =>
    `${API_BASE}/datasets/${id}/export?fmt=${fmt}`,
};
