"use client";

import { t } from "@/i18n";

export type Evidence = {
  metric_name?: string;
  sql?: string;
  source_columns?: string[];
  rows_in_scope?: number;
  rows_total?: number;
};

/**
 * «كيف وصل فينيق لهذه النتيجة؟»
 *
 * هذا المكوّن هو الميزة التنافسية: كل رقم بالواجهة يمكن فتح دليله الحقيقي —
 * الاستعلام المنفَّذ فعلاً والأعمدة المصدر وعدد الصفوف المشمولة. لا نص ثابت.
 */
export function EvidenceDrawer({
  evidence,
  onClose,
}: {
  evidence: Evidence | null;
  onClose: () => void;
}) {
  if (!evidence) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end bg-ink-900/40"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      aria-label={t("evidence.title")}
    >
      <div
        className="max-h-[80vh] w-full overflow-y-auto rounded-t-2xl bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="mb-4 text-base font-bold">{t("evidence.title")}</h3>

        <dl className="flex flex-col gap-3 text-sm">
          {evidence.metric_name && (
            <Row label={t("evidence.metric")}>
              <span className="num">{evidence.metric_name}</span>
            </Row>
          )}

          {evidence.source_columns?.length ? (
            <Row label={t("evidence.columns")}>{evidence.source_columns.join(t("common.listSeparator"))}</Row>
          ) : null}

          {typeof evidence.rows_in_scope === "number" && (
            <Row label={t("evidence.scope")}>
              <span className="num">{evidence.rows_in_scope}</span> {t("evidence.of")}{" "}
              <span className="num">{evidence.rows_total}</span>
            </Row>
          )}

          {evidence.sql && (
            <div className="flex flex-col gap-1.5">
              <dt className="text-ink-600">{t("evidence.sql")}</dt>
              {/* الاستعلام LTR دائماً، ويمرّر أفقياً داخل حاويته لا يكسر الصفحة */}
              <dd
                dir="ltr"
                className="overflow-x-auto rounded-lg bg-canvas p-3 text-start font-mono text-xs"
              >
                {evidence.sql}
              </dd>
            </div>
          )}
        </dl>

        <button
          onClick={onClose}
          className="mt-5 w-full rounded-lg bg-canvas py-2.5 font-medium text-ink-700"
        >
          {t("evidence.close")}
        </button>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2 border-b border-line pb-2">
      <dt className="text-ink-600">{label}</dt>
      <dd className="font-medium">{children}</dd>
    </div>
  );
}
