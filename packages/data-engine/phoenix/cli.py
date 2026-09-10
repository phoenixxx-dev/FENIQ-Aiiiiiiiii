"""واجهة سطر الأوامر.

    python -m phoenix.cli run <ملف.xlsx>
    python -m phoenix.cli ask <مجلد_التشغيل> "كم إجمالي المبيعات؟"
    python -m phoenix.cli demo <ملف.xlsx>      # المسار الكامل + كل الأسئلة
"""
from __future__ import annotations

import sys
from pathlib import Path

from .arabic_text import fmt_number
from .ask import AskPhoenix
from .pipeline import load_run, process

W = 78


def _hr(ch: str = "─"):
    print(ch * W)


def _title(t: str):
    print()
    _hr("═")
    print(f"  {t}")
    _hr("═")


def cmd_run(path: str) -> Path:
    _title(f"معالجة الملف: {Path(path).name}")

    def prog(key, label, pct):
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  [{bar}] {pct:3d}%  {label}")

    r = process(path, on_progress=prog)

    _title("١. الملف")
    print(f"  الصيغة        : {r.file_info.detected_format}")
    if r.file_info.sheet_names:
        print(f"  الشيتات       : {'، '.join(r.file_info.sheet_names)} "
              f"(المختار: {r.file_info.selected_sheet})")
    print(f"  صف العناوين   : {r.file_info.header_row + 1} "
          f"(ثقة {r.file_info.header_confidence:.0%})")
    print(f"  الأبعاد       : {fmt_number(r.profile.row_count)} صف × "
          f"{r.profile.column_count} عمود")
    print(f"  زمن المعالجة  : {r.duration_s} ثانية")

    _title("٢. فهم الأعمدة")
    for c in r.schema.columns:
        p = r.profile.column(c.column_name)
        mark = "⚠️" if c.needs_review else "✓"
        print(f"  {mark} {c.column_name:14} {str(c.concept or '—'):14} "
              f"{c.role:10} {c.confidence:.0%}  [{c.detection_method}]")
    review = [c for c in r.schema.columns if c.needs_review]
    if review:
        print(f"\n  ⚠️  {len(review)} عمود بثقة منخفضة يحتاج تأكيدك:")
        for c in review:
            print(f"      «{c.column_name}» — {c.evidence_ar}")

    _title("٣. مشاكل الجودة المكتشفة")
    issues = [i for c in r.profile.columns for i in c.quality_issues] + r.profile.dataset_issues
    for i in sorted(issues, key=lambda x: {"critical": 0, "warning": 1, "info": 2}[x.severity]):
        icon = {"critical": "🚨", "warning": "⚠️ ", "info": "ℹ️ "}[i.severity]
        print(f"  {icon} {i.message_ar}")

    _title("٤. ماذا تغيّر في بياناتك؟")
    print(f"  الصفوف: {fmt_number(r.changelog.rows_before)} → "
          f"{fmt_number(r.changelog.rows_after)}   |   "
          f"خلايا معدّلة: {fmt_number(r.changelog.total_cells_changed)}")
    print()
    for res in r.changelog.results:
        if res.cells_changed or res.rows_affected:
            print(f"  • {res.summary_ar}")
            for ex in res.examples[:2]:
                print(f"      مثال: {ex['before']!r} → {ex['after']!r}")
    print(f"\n  الملف الأصلي محفوظ كما هو: {r.raw_path}")

    _title("٥. أهم الاكتشافات")
    for i in r.insights[:5]:
        print(f"  {i.icon} {i.title_ar}")
        print(f"     {i.description_ar}")
        print()

    print(f"  📂 مجلد النتائج: {r.run_dir}")
    return r.run_dir


def cmd_ask(run_dir: str, question: str) -> None:
    r = load_run(run_dir)
    eng = r.engine()
    phoenix = AskPhoenix(eng, r.schema)
    a = phoenix.ask(question)

    print()
    print(f"❓ {question}")
    _hr()
    print(f"🔧 فُهم كـ: {a.understood_as}")
    for tc in a.tool_calls:
        args = "، ".join(f"{k}={v}" for k, v in tc.arguments.items() if v is not None)
        print(f"   الأداة: {tc.tool}({args})")
    print()
    print(a.answer_ar)
    if a.metrics:
        print()
        _hr("┈")
        print("📎 كيف وصل فينيق لهذه النتيجة؟")
        for line in a.metrics[0].evidence.explain_ar().splitlines():
            print(f"   {line}")
    eng.close()


def cmd_demo(path: str) -> None:
    run_dir = cmd_run(path)
    r = load_run(run_dir)
    eng = r.engine()
    phoenix = AskPhoenix(eng, r.schema)

    _title("٦. اسأل فينيق")
    print("  الأسئلة المقترحة (مبنية على أعمدة ملفك الفعلية):")
    for q in phoenix.suggested_questions():
        print(f"    • {q}")

    questions = [
        "كم إجمالي المبيعات؟",
        "مين أكتر 5 منتجات مبيعاً؟",
        "كم عدد الفواتير؟",
        "متوسط قيمة الفاتورة؟",
        "المبيعات حسب الشهر",
        "مين أفضل مندوب؟",
        "قارنلي بين شهر شباط وشهر حزيران",
        "أقل 3 منتجات مبيعاً",
        "شو الأصناف الراكدة؟",
    ]
    for q in questions:
        a = phoenix.ask(q)
        print()
        _hr()
        print(f"❓ {q}")
        print(f"   🔧 {a.tool_calls[0].tool} — {a.understood_as}")
        print()
        for line in a.answer_ar.splitlines():
            print(f"   {line}")
        if a.metrics and a.metrics[0].evidence.sql:
            print(f"\n   📎 {a.metrics[0].evidence.sql[:110]}")
    eng.close()


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "run":
        cmd_run(sys.argv[2])
    elif cmd == "ask":
        cmd_ask(sys.argv[2], sys.argv[3])
    elif cmd == "demo":
        cmd_demo(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
