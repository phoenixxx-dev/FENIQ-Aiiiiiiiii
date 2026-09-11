"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api, type AskOut } from "@/lib/api";
import type { Evidence } from "./EvidenceDrawer";
import { t } from "@/i18n";

export function Ask({
  datasetId,
  onEvidence,
}: {
  datasetId: string;
  onEvidence: (e: Evidence) => void;
}) {
  const [question, setQuestion] = useState("");
  // الاقتراحات تأتي من المحرك لا من الواجهة: هو وحده يعرف ما يستطيع
  // الإجابة عنه في هذا الملف بالذات (ملف بلا تاريخ لا يُقترح عليه سؤال زمني)
  const suggestions = useQuery({
    queryKey: ["suggestions", datasetId],
    queryFn: () => api.getSuggestions(datasetId),
  });
  const [answer, setAnswer] = useState<AskOut | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reportId, setReportId] = useState<string | null>(null);

  async function send(q: string) {
    if (!q.trim()) return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    try {
      setAnswer(await api.ask(datasetId, q));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : t("common.error"));
      // معرّف الطلب يُعرض عند الأعطال غير المتوقَّعة فقط: بلا رقم لا يستطيع
      // المستخدم أن يخبرنا أي طلب فشل، ومع كل خطأ يصير ضجيجاً.
      setReportId(
        err instanceof ApiError && err.code === "internal_error"
          ? (err.requestId ?? null)
          : null,
      );
    } finally {
      setBusy(false);
    }
  }

  const evidence = (answer?.evidence ?? null) as Evidence | null;

  return (
    <section className="flex flex-col gap-3 rounded-xl border border-line bg-surface p-4">
      <h2 className="text-sm font-semibold">{t("dashboard.ask")}</h2>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(question);
        }}
        className="flex gap-2"
      >
        <input
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder={t("ask.placeholder")}
          className="min-w-0 flex-1 rounded-lg border border-line px-3 py-2.5 text-sm outline-none focus:border-phoenix-500"
        />
        <button
          type="submit"
          disabled={busy}
          className="shrink-0 rounded-lg bg-phoenix-700 px-4 text-sm font-semibold text-white disabled:opacity-60"
        >
          {busy ? t("ask.thinking") : t("ask.send")}
        </button>
      </form>

      <div className="flex flex-wrap gap-2">
        {(suggestions.data ?? []).map((s) => (
          <button
            key={s}
            onClick={() => {
              setQuestion(s);
              send(s);
            }}
            className="rounded-full border border-line px-3 py-1 text-xs text-ink-600 hover:border-phoenix-500"
          >
            {s}
          </button>
        ))}
      </div>

      {error && (
        <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-danger">
          {error}
          {reportId && (
            <span className="mt-1 block text-xs text-ink-600">
              {t("common.reportId")}: <span className="num">{reportId}</span>
            </span>
          )}
        </p>
      )}

      {answer?.ai_paused_over_budget && (
        <p className="rounded-lg bg-orange-50 px-3 py-2 text-xs text-ink-700">
          {t("ask.aiPaused")}
        </p>
      )}

      {answer && (
        <div className="rounded-lg bg-canvas p-3">
          {/* نص الإجابة يأتي من المحرك كما هو — الواجهة لا تصوغ أي رقم */}
          {/* break-words ضرورية لا تجميلية: اسم صنف طويل بلا مسافات
              (كود مورّد مثلاً) يمدّ الفقرة فتنزلق الصفحة كلها أفقياً على
              شاشة الهاتف — والانزلاق الأفقي يكسر كل التخطيط لا هذه الفقرة */}
          <p className="whitespace-pre-line text-sm leading-relaxed break-words">
            {answer.answer_ar}
          </p>
          {evidence && (
            <button
              onClick={() => onEvidence(evidence)}
              className="mt-2 text-xs text-phoenix-700 underline-offset-4 hover:underline"
            >
              {t("dashboard.evidence")}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
