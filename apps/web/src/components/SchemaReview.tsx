"use client";

/**
 * واجهة تصحيح معاني الأعمدة — الخطة §5.4 تجعلها إجبارية.
 *
 * لماذا تستحق شاشة؟ لأن الكشف الدلالي أذكى جزء وأخطر جزء: عمود «المبلغ ص»
 * قد يكون الإجمالي أو الخصم. لو أخطأ فينيق وسكت، صارت كل الأرقام بعده خاطئة
 * بثقة. فنعرض ما لم نتأكد منه، ونجعل قرار المستخدم يعيد الحساب فعلاً.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type SemanticColumn } from "@/lib/api";
import { t } from "@/i18n";

const NONE = "__none__";

export function SchemaReview({ datasetId }: { datasetId: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<Record<string, string>>({});

  const schema = useQuery({
    queryKey: ["schema", datasetId],
    queryFn: () => api.getSchema(datasetId),
  });
  const concepts = useQuery({ queryKey: ["concepts"], queryFn: () => api.listConcepts() });

  const save = useMutation({
    mutationFn: async (columns: { column_name: string; concept: string | null }[]) => {
      const { job_id } = await api.patchSchema(datasetId, columns);
      // إعادة الحساب وظيفة كاملة — ننتظرها بالبث نفسه الذي يستعمله الرفع
      return api.followJob(job_id, () => undefined);
    },
    onSuccess: () => {
      setPicked({});
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["schema", datasetId] });
      qc.invalidateQueries({ queryKey: ["dashboard", datasetId] });
      qc.invalidateQueries({ queryKey: ["rows", datasetId] });
    },
  });

  if (!schema.data) return null;

  const needsReview = schema.data.columns.filter(
    (c: SemanticColumn) => c.confidence < 0.7 && !c.user_overridden,
  );
  const shown = open ? schema.data.columns : needsReview;
  if (shown.length === 0 && !open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="self-start text-xs text-ink-400 underline-offset-4 hover:underline"
      >
        {t("schema.edit")}
      </button>
    );
  }

  const label = (concept: string | null) =>
    concepts.data?.find((c) => c.concept === concept)?.label_ar ?? t("schema.none");

  function submit() {
    const columns = Object.entries(picked).map(([column_name, value]) => ({
      column_name,
      concept: value === NONE ? null : value,
    }));
    if (columns.length) save.mutate(columns);
  }

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-warn/30 bg-orange-50 p-4">
      <div>
        <h2 className="text-sm font-semibold">{t("schema.title")}</h2>
        <p className="mt-1 text-xs text-ink-600">{t("schema.hint")}</p>
      </div>

      <ul className="flex flex-col gap-3">
        {shown.map((c: SemanticColumn) => (
          <li key={c.column_name} className="flex flex-col gap-1">
            <label
              htmlFor={`col-${c.column_name}`}
              className="text-xs font-medium"
              dir="auto"
            >
              <bdi>{c.column_name}</bdi>
              {c.user_overridden ? (
                <span className="ms-2 text-[10px] text-ok-700">{t("schema.confirmed")}</span>
              ) : null}
            </label>
            <select
              id={`col-${c.column_name}`}
              value={picked[c.column_name] ?? c.concept ?? NONE}
              onChange={(e) =>
                setPicked((p) => ({ ...p, [c.column_name]: e.target.value }))
              }
              className="rounded-lg border border-line bg-surface px-3 py-2 text-sm"
            >
              <option value={NONE}>{t("schema.none")}</option>
              {(concepts.data ?? []).map((k) => (
                <option key={k.concept} value={k.concept}>
                  {k.label_ar}
                </option>
              ))}
            </select>
            {!c.user_overridden && c.concept ? (
              <p className="text-[11px] text-ink-600">
                {t("schema.guess")}: {label(c.concept)}
              </p>
            ) : null}
            {/* عمودٌ أُخرج من الحساب يحتاج سبباً ظاهراً، وإلا رآه المستخدم
                بلا معنى ولم يعرف لماذا فظنّه عطلاً. والأهمّ منه العمود الثابت
                (رقم مستودع، عدد سجلات): إخراجه قرارٌ يمسّ الأرقام، فيُشرح
                ويُترك للمستخدم أن يعترض عليه. */}
            {!c.user_overridden && !c.concept && c.evidence_ar ? (
              <p className="text-[11px] text-ink-500" dir="auto">
                {c.role === "constant" ? `${t("schema.constantHint")} ` : ""}
                {c.evidence_ar}
              </p>
            ) : null}
          </li>
        ))}
      </ul>

      <button
        onClick={submit}
        disabled={save.isPending || Object.keys(picked).length === 0}
        className="rounded-lg bg-phoenix-700 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
      >
        {save.isPending ? t("schema.saving") : t("schema.save")}
      </button>
    </section>
  );
}
