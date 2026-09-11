"use client";

/**
 * «ماذا تغيّر في بياناتي؟» — الخطة §5.5.
 *
 * أهم شاشة للثقة: المستخدم رفع ملفه ونحن غيّرنا فيه. لو لم نُرِه ماذا فعلنا
 * بالضبط (بأمثلة قبل/بعد من صفوفه هو)، فنحن نطلب منه ثقة عمياء.
 * وكل عملية قابلة للرفض — التنظيف اقتراح لا حكم.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type OperationResult } from "@/lib/api";
import { t, tCount } from "@/i18n";

/**
 * يُظهر المسافات الطرفية كنقاط.
 *
 * عيب حقيقي شُوهد على الشاشة: عملية «إزالة مسافات زائدة» كانت تعرض
 * «شركة النور للتجارة» قبل، و«شركة النور للتجارة» بعد — متطابقتين في نظر
 * المستخدم، فيبدو أن فينيق يدّعي تغييراً لم يحدث. الفرق مسافة غير مرئية.
 */
export function visible(value: unknown): string {
  const s = String(value ?? "");
  // محارف لا تُرى إطلاقاً (مسافة صفرية العرض، علامات اتجاه، BOM) — يزيلها
  // المحرك، ولو لم نُظهرها هنا لبدا «قبل» و«بعد» متطابقين تماماً وعاد نفس
  // العيب الذي وُجدت هذه الدالة لأجله، بمحرف آخر.
  const revealed = s.replace(/[​-‏؜⁦-⁩﻿]/g, "◌");
  const lead = revealed.length - revealed.trimStart().length;
  const trail = revealed.length - revealed.trimEnd().length;
  return "·".repeat(lead) + revealed.trim() + "·".repeat(trail);
}

export function CleaningReview({ datasetId }: { datasetId: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [off, setOff] = useState<string[] | null>(null);

  const data = useQuery({
    queryKey: ["cleaning", datasetId],
    queryFn: () => api.getCleaning(datasetId),
  });

  const apply = useMutation({
    mutationFn: async (disabled: string[]) => {
      const { job_id } = await api.patchCleaning(datasetId, disabled);
      return api.followJob(job_id, () => undefined);
    },
    onSuccess: () => {
      setOff(null);
      qc.invalidateQueries({ queryKey: ["cleaning", datasetId] });
      qc.invalidateQueries({ queryKey: ["dashboard", datasetId] });
      qc.invalidateQueries({ queryKey: ["rows", datasetId] });
    },
  });

  if (!data.data) return null;
  const { recipe, changelog, disabled } = data.data;
  const current = off ?? disabled;

  // العمليات التي لم تغيّر شيئاً لا تُعرض: سطر «0 خلية» ضجيج لا معلومة
  const shown = recipe.operations.filter((op) => {
    if (current.includes(op.key)) return true;
    const res = changelog.results.find((r: OperationResult) => r.operation_id === op.id);
    return Boolean(res && (res.cells_changed || res.rows_affected));
  });

  if (shown.length === 0) {
    return <p className="text-xs text-ink-400">{t("cleaning.noChange")}</p>;
  }

  const removed = changelog.rows_before - changelog.rows_after;
  const dirty = JSON.stringify([...current].sort()) !== JSON.stringify([...disabled].sort());

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center justify-between text-start"
      >
        <span className="text-sm font-semibold">{t("cleaning.title")}</span>
        <span className="text-xs text-phoenix-700">
          {open ? t("cleaning.hide") : t("cleaning.show")}
        </span>
      </button>

      {open && (
        <>
          <p className="text-xs text-ink-600">{t("cleaning.hint")}</p>
          {removed > 0 && (
            <p className="text-xs text-ink-600">
              <bdi>{tCount("cleaning.rowsRemoved", removed)}</bdi>
            </p>
          )}

          <ul className="flex flex-col gap-3">
            {shown.map((op) => {
              const res = changelog.results.find(
                (r: OperationResult) => r.operation_id === op.id,
              );
              const isOff = current.includes(op.key);
              return (
                <li
                  key={op.key}
                  className={`rounded-lg border border-line p-3 ${isOff ? "opacity-60" : ""}`}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="text-xs font-medium">
                        {isOff ? op.reason_ar : (res?.summary_ar ?? op.reason_ar)}
                      </p>
                      {isOff && (
                        <p className="mt-0.5 text-[11px] text-warn-700">{t("cleaning.disabled")}</p>
                      )}
                    </div>
                    <button
                      onClick={() =>
                        setOff(
                          isOff
                            ? current.filter((k) => k !== op.key)
                            : [...current, op.key],
                        )
                      }
                      className="shrink-0 text-[11px] text-phoenix-700 underline-offset-4 hover:underline"
                    >
                      {isOff ? t("cleaning.enable") : t("cleaning.disable")}
                    </button>
                  </div>

                  {!isOff && res?.examples?.length ? (
                    <table className="mt-2 w-full text-[11px]">
                      <tbody>
                        {res.examples.slice(0, 3).map((ex, i) => (
                          <tr key={i} className="border-t border-line">
                            <td className="py-1 text-ink-400">
                              {t("cleaning.row")} <span className="num">{String(ex.row)}</span>
                            </td>
                            <td className="py-1 text-danger" dir="auto">
                              <bdi className="whitespace-pre">{visible(ex.before)}</bdi>
                            </td>
                            <td className="py-1 text-ok-700" dir="auto">
                              <bdi className="whitespace-pre">{visible(ex.after)}</bdi>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  ) : null}
                </li>
              );
            })}
          </ul>

          {dirty && (
            <button
              onClick={() => apply.mutate(current)}
              disabled={apply.isPending}
              className="rounded-lg bg-phoenix-700 py-2.5 text-sm font-semibold text-white disabled:opacity-40"
            >
              {apply.isPending ? t("cleaning.applying") : t("cleaning.apply")}
            </button>
          )}
        </>
      )}
    </section>
  );
}
