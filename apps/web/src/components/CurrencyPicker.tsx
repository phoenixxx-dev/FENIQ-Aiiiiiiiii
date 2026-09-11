"use client";

/**
 * عملة الملف — تُعرض دائماً، لأن السكوت عنها هو ما أخفى العيب.
 *
 * كان المحرك يكتب «ر.س» على كل مبلغ لأنها القيمة الافتراضية في دالة التنسيق.
 * صار الآن لا يكتب رمزاً بلا دليل — وهذا صادق لكنه ناقص وحده: صاحب الملف يعرف
 * عملته ولا وسيلة لديه ليقولها. فهذه البطاقة تفعل ثلاثة أشياء:
 *
 *  1. تقول ما عرفه فينيق ومن أين (من الملف، أم منك).
 *  2. تعطيك اختياراً حين لا دليل — وتصحيحك يُعيد حساب كل الأرقام.
 *  3. تُنذر حين يحمل الملف عملتين: لا مجموع واحد فوقهما، ولا تحويل بيننا.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "@/lib/api";
import { t } from "@/i18n";

const NONE = "__none__";

export function CurrencyPicker({ datasetId }: { datasetId: string }) {
  const qc = useQueryClient();
  const [picked, setPicked] = useState<string | null>(null);

  const schema = useQuery({
    queryKey: ["schema", datasetId],
    queryFn: () => api.getSchema(datasetId),
  });
  const currencies = useQuery({
    queryKey: ["currencies"],
    queryFn: () => api.listCurrencies(),
  });

  const save = useMutation({
    mutationFn: async (code: string | null) => {
      const { job_id } = await api.patchSchema(datasetId, [], { code });
      return api.followJob(job_id, () => undefined);
    },
    onSuccess: () => {
      setPicked(null);
      qc.invalidateQueries({ queryKey: ["schema", datasetId] });
      qc.invalidateQueries({ queryKey: ["dashboard", datasetId] });
      qc.invalidateQueries({ queryKey: ["insights", datasetId] });
    },
  });

  const cur = schema.data?.currency;
  if (!cur) return null;

  const current = picked ?? cur.code ?? NONE;
  const dirty = current !== (cur.code ?? NONE);

  return (
    <section className="flex flex-col gap-2 rounded-xl border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-sm font-semibold">{t("currency.title")}</h2>
        {cur.code ? (
          <span className="text-sm font-medium">
            <bdi>{cur.symbol_ar}</bdi>
            <span className="ms-2 text-[11px] text-ink-600">
              {cur.source === "user" ? t("currency.fromUser") : t("currency.detected")}
            </span>
          </span>
        ) : null}
      </div>

      {cur.mixed ? (
        <p className="text-xs text-warn-700">{t("currency.mixed")}</p>
      ) : !cur.code ? (
        <p className="text-xs text-ink-600">{t("currency.unknown")}</p>
      ) : null}

      {cur.evidence_ar ? (
        <p className="text-[11px] text-ink-500" dir="auto">
          {cur.evidence_ar}
        </p>
      ) : null}

      {/* الاختيار متاح دائماً: حتى العملة المستنتجة قد تكون خاطئة، وصاحب
          الملف أدرى. الاستنتاج اقتراح لا حكم. */}
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="cur-pick" className="sr-only">
          {t("currency.choose")}
        </label>
        <select
          id="cur-pick"
          value={current}
          onChange={(e) => setPicked(e.target.value)}
          className="rounded-lg border border-line bg-canvas px-3 py-2 text-sm"
        >
          <option value={NONE}>{t("currency.none")}</option>
          {(currencies.data ?? []).map((c) => (
            <option key={c.code} value={c.code}>
              {c.symbol_ar} — {c.code}
            </option>
          ))}
        </select>
        <button
          onClick={() => save.mutate(current === NONE ? null : current)}
          disabled={!dirty || save.isPending}
          className="rounded-lg bg-phoenix-700 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
        >
          {save.isPending ? t("currency.saving") : t("currency.save")}
        </button>
      </div>
    </section>
  );
}
