"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { use, useEffect, useState } from "react";
import { AuthGate } from "@/components/AuthGate";
import { Chart } from "@/components/Chart";
import { DataTable } from "@/components/DataTable";
import { EvidenceDrawer, type Evidence } from "@/components/EvidenceDrawer";
import { Ask } from "@/components/Ask";
import { SchemaReview } from "@/components/SchemaReview";
import { CleaningReview } from "@/components/CleaningReview";
import { CurrencyPicker } from "@/components/CurrencyPicker";
import { ColumnProfiles } from "@/components/ColumnProfiles";
import { api } from "@/lib/api";
import { t } from "@/i18n";

export default function DatasetPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return (
    <AuthGate>
      <DatasetView id={id} />
    </AuthGate>
  );
}

function DatasetView({ id }: { id: string }) {
  const [evidence, setEvidence] = useState<Evidence | null>(null);

  const meta = useQuery({ queryKey: ["dataset", id], queryFn: () => api.getDataset(id) });
  const status = meta.data?.status;
  const busy = status === "pending" || status === "processing";
  const dash = useQuery({
    queryKey: ["dashboard", id],
    queryFn: () => api.getDashboard(id),
    enabled: status === "ready",
  });

  // ملف قيد المعالجة: نسأل عن حالته كل ثانيتين حتى تكتمل.
  //
  // لماذا لا نستعمل بث الوظيفة هنا؟ لأن الداخل من رابط مباشر لا يملك
  // job_id — يملك معرّف الملف فقط. سؤال خفيف كل ثانيتين أبسط من إضافة
  // مسار جديد لمجرد هذه الحالة.
  const refetchMeta = meta.refetch;
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => refetchMeta(), 2000);
    return () => clearInterval(timer);
  }, [busy, refetchMeta]);

  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-6">
      <div className="flex items-center gap-2">
        <Link href="/" className="flip text-ink-400" aria-label={t("common.back")}>
          ←
        </Link>
        <h1 className="min-w-0 truncate text-lg font-bold">
          {/* bdi يعزل اسم الملف عن اتجاه الفقرة — بدونه ينقلب ترتيبه */}
          <bdi>{meta.data?.name ?? ""}</bdi>
        </h1>
      </div>

      {meta.data?.duplicate_of ? (
        <Link
          href={`/datasets/${meta.data.duplicate_of}`}
          className="rounded-xl border border-warn/30 bg-orange-50 p-3 text-xs text-ink-700"
        >
          {t("dataset.duplicateNotice")}
        </Link>
      ) : null}

      {meta.data?.warnings?.length ? (
        <ul className="flex flex-col gap-1 rounded-xl border border-warn/30 bg-orange-50 p-3 text-xs text-ink-700">
          {meta.data.warnings.map((w, i) => (
            <li key={i}>• {w}</li>
          ))}
        </ul>
      ) : null}

      {status === "ready" && <SchemaReview datasetId={id} />}
      {status === "ready" && <CurrencyPicker datasetId={id} />}

      {busy && (
        <section
          role="status"
          className="rounded-xl border border-line bg-surface p-5 text-center"
        >
          <p className="text-sm font-medium">{t("job.processing")}</p>
          <p className="mt-1 text-xs text-ink-600">{t("job.processingHint")}</p>
        </section>
      )}

      {status === "failed" && (
        <section role="alert" className="rounded-xl border border-danger/40 bg-red-50 p-5">
          <p className="text-sm font-medium text-danger">{t("job.failed")}</p>
          {meta.data?.error_ar ? (
            <p className="mt-1 text-xs text-ink-700">{meta.data.error_ar}</p>
          ) : null}
          <Link href="/" className="mt-3 inline-block text-xs text-phoenix-700 underline">
            {t("job.backToFiles")}
          </Link>
        </section>
      )}

      {dash.isLoading && status === "ready" && (
        <p className="text-sm text-ink-400">{t("common.loading")}</p>
      )}

      {dash.isError && (
        <section role="alert" className="rounded-xl border border-danger/40 bg-red-50 p-5">
          <p className="text-sm text-danger">{t("common.error")}</p>
          <button
            onClick={() => dash.refetch()}
            className="mt-2 text-xs text-phoenix-700 underline"
          >
            {t("common.retry")}
          </button>
        </section>
      )}

      {dash.data && (
        <>
          <section className="grid grid-cols-2 gap-3">
            {/* البطاقات العريضة أولاً: لو جاءت بالوسط تركت فجوة في الشبكة */}
            {[...dash.data.kpis]
              .sort((a, b) => {
                const len = (x: unknown) =>
                  String((x as Record<string, unknown>).formatted_ar).length > 12 ? 0 : 1;
                return len(a) - len(b);
              })
              .map((k) => {
              const kpi = k as Record<string, unknown>;
              // القيم المالية الطويلة تأخذ عرض الشاشة كاملاً: بطاقة بعرض
              // نصف شاشة 375px تكسر «1,880,947.36 ر.س» على سطرين
              const wide = String(kpi.formatted_ar).length > 12;
              return (
                <article
                  key={String(kpi.key)}
                  className={`rounded-xl border border-line bg-surface p-4 ${
                    wide ? "col-span-2" : ""
                  }`}
                >
                  <p className="text-xs text-ink-600">{String(kpi.label_ar)}</p>
                  {/* text-lg مع leading ضيّق: «1,880,947.36 ر.س» يتّسع بسطر واحد
                      على شاشة 375px بدل أن تنفصل «ر.س» وحدها */}
                  {/* الفقرة تبقى RTL ليحاذي الرقم العنوانَ على اليمين،
                      والعنصر bdi ذو الصنف num يعزل الرقم عن اتجاه الفقرة.
                      القيم الطويلة («1,880,947.36 ر.س») تصغُر خطوةً حتى لا
                      تنفصل «ر.س» وحدها على سطر ثانٍ في شاشة 375px */}
                  <p
                    className="mt-1 text-lg leading-snug font-bold"
                  >
                    <bdi className="numval">{String(kpi.formatted_ar)}</bdi>
                  </p>
                  {kpi.evidence ? (
                    <button
                      onClick={() => setEvidence(kpi.evidence as Evidence)}
                      className="mt-2 text-xs text-phoenix-700 underline-offset-4 hover:underline"
                    >
                      {t("dashboard.evidence")}
                    </button>
                  ) : null}
                </article>
              );
            })}
          </section>

          <Ask datasetId={id} onEvidence={setEvidence} />

          <section className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold text-ink-600">{t("dashboard.charts")}</h2>
            {dash.data.charts.length === 0 && (
              <p className="text-sm text-ink-400">{t("dashboard.noCharts")}</p>
            )}
            {dash.data.charts.map((c, i) => (
              <Chart key={i} spec={c} />
            ))}
          </section>

          {dash.data.insights.length > 0 && (
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-semibold text-ink-600">{t("dashboard.insights")}</h2>
              {dash.data.insights.map((ins) => {
                const item = ins as Record<string, unknown>;
                return (
                  <article
                    key={String(item.id)}
                    className="rounded-xl border border-line bg-surface p-4"
                  >
                    <p className="text-sm font-medium">{String(item.title_ar)}</p>
                    {item.body_ar ? (
                      <p className="mt-1 text-xs text-ink-600">{String(item.body_ar)}</p>
                    ) : null}
                    {/* الاكتشاف بلا دليل ادّعاء. من له دليل يفتحه هنا */}
                    {item.evidence ? (
                      <button
                        onClick={() => setEvidence(item.evidence as Evidence)}
                        className="mt-2 text-xs text-phoenix-700 underline-offset-4 hover:underline"
                      >
                        {t("dashboard.evidence")}
                      </button>
                    ) : null}
                  </article>
                );
              })}
            </section>
          )}

          <ColumnProfiles datasetId={id} />

          <CleaningReview datasetId={id} />

          <DataTable datasetId={id} />

          <section className="flex gap-2">
            <a
              href={api.exportUrl(id, "csv")}
              className="flex-1 rounded-lg border border-line bg-surface py-2.5 text-center text-sm font-medium"
            >
              {t("dashboard.exportCsv")}
            </a>
            <a
              href={api.exportUrl(id, "xlsx")}
              className="flex-1 rounded-lg border border-line bg-surface py-2.5 text-center text-sm font-medium"
            >
              {t("dashboard.exportXlsx")}
            </a>
          </section>
        </>
      )}

      <EvidenceDrawer evidence={evidence} onClose={() => setEvidence(null)} />
    </main>
  );
}
