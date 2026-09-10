"""يولّد ملف Excel اختباري يحاكي فوضى الملفات العربية الحقيقية.

⚠️ هذا FIXTURE للاختبار وليس بيانات حقيقية.
كل الأرقام التي يعرضها المحرك تُحسب من هذا الملف فعلياً — لا قيم مكتوبة يدوياً.
استبدله بملفك الحقيقي بأمر:  python -m phoenix.cli run <ملفك.xlsx>

الفوضى المقصودة (كل واحدة تختبر جزءاً من المحرك):
  1. صف عنوان الشركة فوق الجدول + صف فارغ  → header_finder
  2. أسماء أعمدة بإملاء متغيّر (الكميه/المنطقه)  → التطبيع + fuzzy
  3. أرقام هندية في بعض الخلايا (١٢)            → normalize_digits
  4. أسعار كنص مع عملة ("1,250.00 ر.س")        → parse_numbers
  5. تواريخ بصيغ مختلطة                          → parse_dates
  6. مسافات زائدة في أسماء العملاء               → trim_whitespace
  7. تباين همزة/تاء مربوطة في نفس الاسم          → fuzzy_entity_merge
  8. صفوف مكرّرة كاملة                            → deduplicate_rows
  9. تواريخ مفقودة                                → fill_missing / جودة
 10. قيمة شاذة كبيرة                              → flag_outliers
 11. عمود ثابت + عمود شبه فارغ                    → جودة البيانات
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

import xlsxwriter

random.seed(20260825)   # حتمي: نفس الملف في كل تشغيل

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "sales_ar_messy.xlsx"

PRODUCTS = [
    ("لابتوب ديل انسبايرون", 2450.0), ("طابعة اتش بي ليزر", 890.0),
    ("شاشة سامسونج 27 بوصة", 1150.0), ("كيبورد لوجيتك لاسلكي", 185.0),
    ("ماوس لاسلكي", 95.0), ("هارد ديسك خارجي 1 تيرا", 320.0),
    ("راوتر تي بي لينك", 240.0), ("سماعات بلوتوث", 175.0),
    ("كاميرا ويب", 210.0), ("حامل لابتوب معدني", 130.0),
    ("كرسي مكتبي دوّار", 780.0), ("مكيّف صغير", 1980.0),
]

# تباين الإملاء متعمّد — نفس العميل بأشكال مختلفة
CUSTOMERS = [
    "شركة النور للتجارة", "شركه النور للتجاره", "  شركة النور للتجارة ",
    "مؤسسة الفجر", "مؤسسه الفجر", "الرياض للتوريدات", "الرياض للتوريدات ",
    "متجر الأمانة", "متجر الامانة", "شركة البركة", "التقنية الحديثة",
    "مكتب السلام", "شركة الوفاء", "مؤسسة الإبداع",
]
REPS = ["أحمد الغامدي", "سارة القحطاني", "محمد العتيبي", "نورة الشمري", "خالد الدوسري"]
REGIONS = ["الرياض", "جدة", "الدمام", "مكة", "المدينة"]


def ar_digits(n: int) -> str:
    return str(n).translate({ord(w): a for w, a in zip("0123456789", "٠١٢٣٤٥٦٧٨٩")})


def build_rows() -> list[list]:
    rows = []
    start = date(2026, 1, 1)
    inv = 1000

    for i in range(480):
        d = start + timedelta(days=random.randint(0, 209))   # 7 أشهر
        prod, base_price = random.choice(PRODUCTS)

        # نمو مقصود عبر الزمن: يجب أن يكتشفه محرك الاكتشافات بنفسه
        month_factor = 1.0 + (d.month - 1) * 0.09
        qty = max(1, int(random.gauss(4 * month_factor, 2)))
        price = round(base_price * random.uniform(0.95, 1.05), 2)
        total = round(qty * price, 2)

        inv += 1
        # صيغ تاريخ مختلطة
        r = random.random()
        if r < 0.6:
            dv = f"{d.year}-{d.month:02d}-{d.day:02d}"
        elif r < 0.9:
            dv = f"{d.day:02d}/{d.month:02d}/{d.year}"
        else:
            dv = ""                                    # تاريخ مفقود

        # الكمية: بعضها بأرقام هندية، بعضها كنص
        qv = ar_digits(qty) if random.random() < 0.07 else qty
        # السعر: بعضه نص مع عملة وفواصل
        pv = f"{price:,.2f} ر.س" if random.random() < 0.18 else price
        tv = f"{total:,.2f}" if random.random() < 0.12 else total

        rows.append([
            f"INV-{inv}", dv, prod, qv, pv, tv,
            random.choice(CUSTOMERS), random.choice(REPS), random.choice(REGIONS),
            "نقدي", "" if random.random() < 0.85 else "ملاحظة",
        ])

    # قيمة شاذة واحدة كبيرة (طلبية جملة)
    rows.append(["INV-9001", "2026-05-14", "لابتوب ديل انسبايرون", 120, 2450.0,
                 294000.0, "شركة البركة", "خالد الدوسري", "الرياض", "نقدي", ""])

    # صفوف مكرّرة كاملة (خطأ إدخال شائع)
    for _ in range(6):
        rows.append(list(random.choice(rows[:400])))

    random.shuffle(rows)
    return rows


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    wb = xlsxwriter.Workbook(str(OUT))
    ws = wb.add_worksheet("المبيعات")
    ws.right_to_left()

    title = wb.add_format({"bold": True, "font_size": 14})
    hdr = wb.add_format({"bold": True, "bg_color": "#F1F5F9", "border": 1})

    # فوضى #1: عنوان الشركة ثم صف فارغ — العناوين ليست في الصف الأول
    ws.write(0, 0, "شركة النور للتجارة — تقرير المبيعات", title)
    ws.write(1, 0, "الفترة: 2026/01/01 - 2026/07/31")
    # الصف 2 فارغ

    headers = ["رقم الفاتورة", "التاريخ", "اسم الصنف", "الكميه", "سعر الوحدة",
               "الاجمالي", "العميل", "المندوب", "المنطقه", "طريقة الدفع", "ملاحظات"]
    for c, h in enumerate(headers):
        ws.write(3, c, h, hdr)

    for r, row in enumerate(build_rows(), start=4):
        for c, v in enumerate(row):
            ws.write(r, c, v)

    # شيت فارغ — يجب أن يتجاهله المحرك
    wb.add_worksheet("ورقة2")
    wb.close()
    print(f"✓ {OUT}  ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
