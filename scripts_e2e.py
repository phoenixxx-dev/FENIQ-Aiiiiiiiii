#!/usr/bin/env python3
"""يشغّل الـAPI + العامل + الواجهة معاً، ويقود متصفحاً حقيقياً عبر المسار كاملاً.

السبب: `npm run build` ينجح على واجهة لا تعمل إطلاقاً (خطأ وقت التشغيل، نداء
API خاطئ، مكوّن لا يُركَّب). الإثبات الوحيد المقبول هو متصفح فعلي يمر بالمسار.

الاستخدام:  python3 scripts_e2e.py [--headed]
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "apps" / "web"
API_PORT, WEB_PORT = 8010, 3010
SHOTS = ROOT / "artifacts" / "screens"


AXE = WEB / "node_modules" / "axe-core" / "axe.min.js"


def audit_accessibility(page, label: str) -> list[str]:
    """فحص وصولية حقيقي بـaxe-core (الخطة تشترط Accessibility > 90).

    نفحص المخالفات الخطيرة فقط (serious/critical): التحذيرات الطفيفة تُغرق
    النتيجة وتجعل الحارس ضجيجاً يُتجاهَل.
    """
    if not AXE.exists():
        return [f"axe-core غير مثبَّت — تخطّي فحص الوصولية ({label})"]
    page.add_script_tag(path=str(AXE))
    result = page.evaluate(
        """async () => {
            const r = await axe.run(document, {
              resultTypes: ['violations'],
              runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa'] },
            });
            return r.violations.map(v => ({
              id: v.id, impact: v.impact, n: v.nodes.length,
              target: (v.nodes[0] && v.nodes[0].target.join(' ')) || '',
              why: (v.nodes[0] && v.nodes[0].failureSummary || '').slice(0, 200),
              html: (v.nodes[0] && v.nodes[0].html || '').slice(0, 160),
            }));
        }"""
    )
    bad = [v for v in result if v["impact"] in ("serious", "critical")]
    return [f"وصولية [{label}] {v['id']} ({v['impact']}, {v['n']} عنصر): "
            f"{v['target'][:60]}\n     {v.get('html','')}\n     {v.get('why','')}"
            for v in bad]


def free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_for(port: int, timeout: int, label: str) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if not free(port):
            return True
        time.sleep(1)
    print(f"   ✗ {label} لم يستجب خلال {timeout}s")
    return False


def main() -> int:
    headed = "--headed" in sys.argv
    SHOTS.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}:{ROOT / 'packages' / 'data-engine'}"
    env["RATE_LIMIT_LOGIN_PER_MINUTE"] = "10000"

    # فحص أوّلي: منفذ مشغول مسبقاً يعني أننا سنتحدث مع خادم قديم بلا أن ندري.
    # حصل فعلاً: خادم تطوير زومبي بقي مستمعاً على المنفذ، فاختبرنا صفحة قديمة
    # فارغة وظننا الواجهة مكسورة. الفشل الصريح هنا أوفر من ساعة تشخيص خاطئ.
    for port, label in ((API_PORT, "الـAPI"), (WEB_PORT, "الواجهة")):
        if not free(port):
            print(f"✗ المنفذ {port} ({label}) مشغول مسبقاً — أوقف العملية القديمة أولاً.")
            return 1

    procs: list[subprocess.Popen] = []
    try:
        print("→ تشغيل الـAPI والعامل")
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "apps.api.src.main:app",
             "--host", "127.0.0.1", "--port", str(API_PORT), "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True))
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "arq", "apps.api.src.workers.worker.WorkerSettings"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True))
        if not wait_for(API_PORT, 40, "الـAPI"):
            return 1
        print("   ✓ الـAPI جاهز")

        # نستخدم بناء الإنتاج لا `next dev`: وضع التطوير يجمّع الصفحة عند أول
        # طلب، فتفتح الصفحة قبل أن يعمل React ويفشل الاختبار لسبب وهمي.
        # الإنتاج أيضاً هو ما سيعمل عند المستخدم فعلاً.
        web_env = env.copy()
        web_env["NEXT_PUBLIC_API_BASE"] = f"http://127.0.0.1:{API_PORT}"

        # نبني دائماً: تشغيل `next dev` مرة واحدة يترك مخرجات وضع التطوير داخل
        # .next، فيقدّمها `next start` بعدها كصفحة فارغة بلا React (وقعنا فيها).
        # البناء ~30 ثانية، وثمنها أقل من ساعة تشخيص خاطئ.
        print("→ بناء الواجهة")
        build = subprocess.run(["npm", "run", "build"], cwd=WEB, env=web_env,
                               capture_output=True, text=True)
        if build.returncode != 0:
            print(build.stdout[-2000:])
            return 1

        print("→ تشغيل الواجهة")
        procs.append(subprocess.Popen(
            ["npx", "next", "start", "--port", str(WEB_PORT)],
            cwd=WEB, env=web_env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            start_new_session=True))
        if not wait_for(WEB_PORT, 90, "الواجهة"):
            return 1
        print("   ✓ الواجهة جاهزة")

        return drive(headed)
    finally:
        for p in procs:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        time.sleep(1.5)


def drive(headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    sample = ROOT / "packages" / "data-engine" / "fixtures" / "sales_ar_messy.xlsx"
    email = f"e2e_{uuid.uuid4().hex[:8]}@example.com"
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        # مقاس iPhone SE — أصغر شاشة شائعة، والخطة تشترط عملها عليها
        page = browser.new_page(viewport={"width": 375, "height": 667})
        page.on("pageerror", lambda e: errors.append(f"خطأ JS: {e}"))
        # فشل تحميل خط جوجل ليس عيباً بالمنتج: هذه البيئة تحجب النطاق، والصفحة
        # ترجع للخط النظامي كما صُمّمت. نتجاهله حتى لا يخفي أعطالاً حقيقية.
        BENIGN = ("ERR_TUNNEL_CONNECTION_FAILED", "fonts.googleapis.com", "fonts.gstatic.com")

        def on_console(m):
            if m.type == "error" and not any(b in m.text for b in BENIGN):
                errors.append(f"console.error: {m.text}")

        page.on("console", on_console)

        # نسجّل الطلبات لنُثبت أن التقدّم وصل بالبث لا بالـpolling — بلا هذا
        # قد يكون البث معطّلاً والواجهة ترتدّ بصمت ونظنّها تعمل.
        requests: list[tuple[str, str]] = []
        page.on("request", lambda r: requests.append((r.method, r.url)))

        base = f"http://127.0.0.1:{WEB_PORT}"

        print("→ فتح الصفحة وإنشاء حساب")
        # ملاحظة مهمة: لا نستخدم networkidle — طلب خط جوجل محجوب بهذه البيئة
        # فيبقى معلّقاً، وnetworkidle لا يستقر أبداً، فتُنفَّذ النقرات قبل أن
        # يعمل React. ننتظر العنصر نفسه بدل انتظار الشبكة.
        def dump(step: str) -> None:
            """عند أي فشل نطبع ما في الصفحة فعلاً بدل تخمين السبب."""
            print(f"   ✗ فشل عند: {step}")
            print("   --- النص الظاهر ---")
            print("   " + (page.inner_text("body")[:500] or "(فارغ)").replace("\n", "\n   "))
            n = page.locator("button").count()
            print(f"   --- الأزرار ({n}) ---")
            for i in range(min(n, 10)):
                print("     •", repr(page.locator("button").nth(i).inner_text()))
            print("   --- عنوان الصفحة:", page.url)
            if errors:
                print("   --- رسائل المتصفح ---")
                for e in list(dict.fromkeys(errors))[:8]:
                    print("     ", e[:220])
            page.screenshot(path=str(SHOTS / f"فشل-{step}.png"))

        def step(name: str, fn) -> bool:
            try:
                fn()
                return True
            except Exception:
                dump(name)
                return False

        page.goto(base, wait_until="domcontentloaded")
        register_tab = page.get_by_role("button", name="ما عندك حساب؟ أنشئ واحداً")
        if not step("شاشة الدخول", lambda: register_tab.wait_for(state="visible", timeout=30000)):
            browser.close()
            return 1
        register_tab.click()
        page.get_by_label("البريد الإلكتروني").fill(email)
        page.get_by_label("كلمة المرور").fill("StrongPass123")
        page.get_by_role("button", name="إنشاء حساب").click()
        if not step("بعد إنشاء الحساب",
                    lambda: page.wait_for_selector("text=ارفع ملفك", timeout=25000)):
            browser.close()
            return 1
        page.screenshot(path=str(SHOTS / "1-الرفع.png"))
        print("   ✓ الحساب أُنشئ وظهرت شاشة الرفع")

        print("→ رفع ملف حقيقي")
        page.set_input_files("input[type=file]", str(sample))
        page.wait_for_url("**/datasets/**", timeout=90000)
        print("   ✓ المعالجة انتهت والانتقال للوحة تم")

        # هل ذهب الملف بالمسار الموقّع فعلاً، أم ارتدّ بصمت للرفع عبر الـAPI؟
        asked_url = [u for m, u in requests if u.endswith("/datasets/upload-url")]
        put_direct = [u for m, u in requests if m == "PUT" and "/uploads/" in u]
        completed = [u for m, u in requests if u.endswith("/complete")]
        through_api = [u for m, u in requests if m == "POST" and u.endswith("/datasets")]
        if not (asked_url and put_direct and completed):
            errors.append("لم يُستعمل مسار الرفع الموقّع (presigned)")
        elif through_api:
            errors.append("ارتدّ للرفع عبر الـAPI رغم توفّر المسار الموقّع")
        else:
            print("   ✓ الملف رُفع بالمسار الموقّع — لم يمر عبر الـAPI")

        streamed = [u for m, u in requests if "/stream" in u]
        polled = [u for m, u in requests if "/jobs/" in u and "/stream" not in u]
        if not streamed:
            errors.append("لم يُستخدم البث اللحظي (SSE) إطلاقاً")
        elif polled:
            errors.append(f"ارتدّ للـpolling رغم نجاح البث ({len(polled)} طلب)")
        else:
            print(f"   ✓ التقدّم وصل بالبث اللحظي (SSE) — بلا polling")

        print("→ انتظار اللوحة")
        if not step("اللوحة",
                    lambda: page.get_by_text("الرسوم", exact=True).first
                                .wait_for(state="visible", timeout=30000)):
            browser.close()
            return 1
        page.wait_for_timeout(2500)          # مهلة لرسم ECharts
        page.screenshot(path=str(SHOTS / "2-اللوحة.png"), full_page=True)

        # ترتيب «الرقم ثم العملة» بصرياً — لا يكفي أن يكون النص صحيحاً.
        # في صفحة RTL، فرض direction:ltr على «1,880,947.36 ر.س» يقلب
        # الترتيب المعروض فتُقرأ العملة أولاً. نقيس إحداثيات الجزأين.
        order = page.evaluate("""() => {
            for (const el of document.querySelectorAll('bdi')) {
                const txt = el.textContent || '';
                const cut = txt.indexOf(' ر.س');
                if (cut <= 0 || el.firstChild?.nodeType !== 3) continue;
                const n = document.createRange();
                n.setStart(el.firstChild, 0); n.setEnd(el.firstChild, cut);
                const c = document.createRange();
                c.setStart(el.firstChild, cut + 1);
                c.setEnd(el.firstChild, txt.length);
                return {num: n.getBoundingClientRect().x,
                        cur: c.getBoundingClientRect().x, txt};
            }
            return null;
        }""")
        if order is None:
            print("   • لا قيمة بعملة في هذه اللوحة — تخطّي فحص الاتجاه")
        elif order["num"] <= order["cur"]:
            errors.append(
                f"«{order['txt']}»: العملة تظهر يمين الرقم — الترتيب مقلوب "
                f"بصرياً (رقم x={order['num']:.0f} · عملة x={order['cur']:.0f})")
        else:
            print("   ✓ القيمة تُعرض «الرقم ثم العملة» كما تُقرأ بالعربية")

        body = page.inner_text("body")
        for needle in ("الرسوم", "اسأل فينيق", "الإجمالي", "عدد الصفوف"):
            if needle not in body:
                errors.append(f"غير موجود بالصفحة: {needle}")
        # مؤشر «عدد الصفوف» يجب أن يحمل رقماً حقيقياً لا صفراً
        if "\n0\n" in body:
            errors.append("مؤشر يعرض صفراً — الأرقام لم تصل من الخادم")
        canvases = page.locator("canvas").count()
        if canvases == 0:
            errors.append("لا يوجد أي رسم مرسوم فعلياً (canvas = 0)")
        print(f"   ✓ اللوحة ظهرت — {canvases} رسم مرسوم")

        print("→ فحص الوصولية (axe-core)")
        a11y = audit_accessibility(page, "اللوحة")
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("text=ملفاتي", timeout=20000)
        a11y += audit_accessibility(page, "القائمة")
        page.go_back(wait_until="domcontentloaded")
        page.get_by_text("الرسوم", exact=True).first.wait_for(state="visible", timeout=30000)
        if a11y:
            errors.extend(a11y)
        else:
            print("   ✓ لا مخالفات وصولية خطيرة (WCAG 2 A/AA)")

        print("→ تصحيح معنى عمود وإعادة الحساب")
        # القيمة الحقيقية للميزة ليست ظهور القائمة، بل أن قرار المستخدم
        # يغيّر الأرقام فعلاً. لذلك نقارن مؤشراً قبل وبعد.
        def kpi_text() -> str:
            return page.locator("section").first.inner_text()

        before_kpis = kpi_text()
        edit = page.get_by_role("button", name="تعديل معاني الأعمدة")
        if edit.count():
            edit.first.click()
            page.wait_for_timeout(400)
        selects = page.locator("select")
        if selects.count() == 0:
            errors.append("واجهة تصحيح الأعمدة غير ظاهرة")
        else:
            selects.first.scroll_into_view_if_needed()
            options = selects.first.locator("option")
            current = selects.first.input_value()
            target = None
            for i in range(options.count()):
                val = options.nth(i).get_attribute("value")
                if val and val != current and val != "__none__":
                    target = val
                    break
            if target is None:
                errors.append("قائمة المفاهيم فارغة — المحرك لم يرسل خياراته")
            else:
                selects.first.select_option(target)
                page.screenshot(path=str(SHOTS / "6-تصحيح-الأعمدة.png"), full_page=True)
                page.get_by_role("button", name="احفظ وأعد الحساب").click()
                page.wait_for_timeout(9000)
                if kpi_text() == before_kpis:
                    errors.append("التصحيح لم يغيّر الأرقام — الشاشة تجميلية")
                else:
                    print("   ✓ تصحيح المستخدم أعاد حساب الأرقام فعلاً")

        print("→ تجربة «كيف حُسب؟»")
        page.get_by_role("button", name="كيف حُسب؟").first.click()
        page.wait_for_selector("text=كيف وصل فينيق لهذه النتيجة؟", timeout=10000)
        page.screenshot(path=str(SHOTS / "3-الدليل.png"))
        if "SELECT" not in page.inner_text("body"):
            errors.append("درج الأدلة لا يعرض الاستعلام الفعلي")
        print("   ✓ درج الأدلة يعرض الاستعلام الحقيقي")
        page.get_by_role("button", name="إغلاق").click()

        print("→ أدلة الاكتشافات")
        # الاكتشاف بلا دليل ادّعاء — نتأكد أن الأزرار تحت الاكتشافات تفتح
        # الدليل نفسه الذي تفتحه المؤشرات، لا نصاً ثابتاً
        insights_section = page.locator("section", has_text="الاكتشافات").last
        ev_buttons = insights_section.get_by_role("button", name="كيف حُسب؟")
        if ev_buttons.count() == 0:
            errors.append("لا يوجد دليل تحت أي اكتشاف")
        else:
            ev_buttons.first.click()
            page.wait_for_selector("text=كيف وصل فينيق لهذه النتيجة؟", timeout=10000)
            if "SELECT" not in page.inner_text("body"):
                errors.append("دليل الاكتشاف لا يعرض استعلاماً حقيقياً")
            else:
                print(f"   ✓ {ev_buttons.count()} اكتشاف يعرض دليله الحقيقي")
            page.get_by_role("button", name="إغلاق").click()

        print("→ سؤال فينيق")
        # الاقتراحات تأتي من المحرك حسب أعمدة هذا الملف — نتأكد أنها وصلت
        # فعلاً لا أنها نصوص ثابتة في الواجهة
        for expected in ("مين أكتر 5 منتجات مبيعاً؟", "المبيعات حسب المنطقة"):
            if page.get_by_role("button", name=expected).count() == 0:
                errors.append(f"اقتراح مبني على الأعمدة لم يظهر: {expected}")
        page.get_by_role("button", name="كم إجمالي المبيعات؟").click()
        page.wait_for_timeout(4500)
        page.screenshot(path=str(SHOTS / "4-السؤال.png"), full_page=True)
        if "إجمالي المبيعات يساوي" not in page.inner_text("body"):
            errors.append("جواب السؤال لم يظهر")
        print("   ✓ الجواب ظهر بالواجهة")

        print("→ بطاقات الأعمدة")
        prof = page.get_by_role("button", name="اعرض تفاصيل الأعمدة")
        if prof.count() == 0:
            errors.append("بطاقات الأعمدة غير ظاهرة")
        else:
            prof.first.click()
            page.wait_for_timeout(1500)
            text = page.inner_text("body")
            for needle in ("قيمة مختلفة", "اسم الصنف"):
                if needle not in text:
                    errors.append(f"بطاقات الأعمدة بلا «{needle}»")
            page.screenshot(path=str(SHOTS / "11-الأعمدة.png"), full_page=True)
            print("   ✓ بطاقات الأعمدة تعرض النوع والقيم الفارغة والمشاكل")

        print("→ شاشة «ماذا تغيّر في بياناتي؟»")
        show = page.get_by_role("button", name="اعرض التفاصيل")
        if show.count() == 0:
            errors.append("شاشة سجل التنظيف غير ظاهرة")
        else:
            show.first.click()
            page.wait_for_timeout(600)
            text = page.inner_text("body")
            if "الملف الأصلي ما انمس" not in text:
                errors.append("سجل التنظيف بلا توضيح أن الأصل لم يُمس")
            if page.get_by_role("button", name="لا تطبّق هذي").count() == 0:
                errors.append("لا يمكن رفض أي عملية تنظيف")
            page.screenshot(path=str(SHOTS / "7-التنظيف.png"), full_page=True)
            print("   ✓ سجل التنظيف يعرض أمثلة قبل/بعد وكل عملية قابلة للرفض")

        print("→ جدول البيانات والتنقّل بين صفحاته")
        # نحصر البحث بقسم الجدول باسمه: الصفحة صار فيها جدول آخر (أمثلة
        # التنظيف)، وlocator("table").first كان يلتقط الخطأ منهما.
        data_section = page.locator('section[aria-labelledby="data-table-title"]')
        table = data_section.locator("table")
        if not step("جدول البيانات",
                    lambda: table.first.wait_for(state="visible", timeout=15000)):
            browser.close()
            return 1
        page.get_by_text("البيانات", exact=True).first.scroll_into_view_if_needed()
        page.wait_for_timeout(600)
        first_page_text = table.first.inner_text()
        rows_count = page.locator("table tbody tr").count()
        if rows_count == 0:
            errors.append("الجدول ظاهر لكن بلا صفوف")
        page.screenshot(path=str(SHOTS / "5-الجدول.png"), full_page=True)

        # زر الصفحة التالية هو الوحيد الفعّال أسفل الجدول
        next_btn = data_section.locator("button").last
        if next_btn.is_enabled():
            next_btn.click()
            page.wait_for_timeout(2500)
            if table.first.inner_text() == first_page_text:
                errors.append("الانتقال للصفحة التالية لم يغيّر محتوى الجدول")
            else:
                print("   ✓ الجدول يعمل والتنقّل بين الصفحات يغيّر البيانات فعلاً")
        else:
            print(f"   ✓ الجدول يعمل ({rows_count} صف، صفحة واحدة)")

        print("→ تثبيت العمود الأول عند التمرير الأفقي (الخطة §8.7)")
        # على هاتف بعرض 375 بكسل وجدول فيه 27 عموداً، التمرير يميناً كان
        # يُخرج عمود الاسم من الشاشة: أرقام بلا صاحب. نمرّر فعلياً ونقارن
        # موقع الخلية الأولى قبل وبعد — لا نكتفي بوجود صنف CSS.
        scroller = data_section.locator("div[role='group']")
        first_cell = data_section.locator("tbody tr td").first
        before = first_cell.bounding_box()
        before_text = first_cell.inner_text()
        scroller.evaluate("el => el.scrollBy(-600, 0)")   # RTL: التمرير سالب
        page.wait_for_timeout(500)
        moved = scroller.evaluate("el => Math.abs(el.scrollLeft)")
        after = first_cell.bounding_box()
        after_text = first_cell.inner_text()
        page.screenshot(path=str(SHOTS / "12-الجدول-ممرَّر.png"))
        # الصفحة نفسها يجب ألا تنزلق أفقياً — التمرير للجدول وحده
        page_overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - window.innerWidth")
        if page_overflow > 2:
            errors.append(
                f"الصفحة كلها تنزلق أفقياً على شاشة الهاتف ({page_overflow}px زائدة)")
        if moved < 50:
            print("   … الجدول لا يحتاج تمريراً أفقياً بهذا العرض — تخطّي")
        elif not after or not before:
            errors.append("تعذّر قياس موقع العمود الأول")
        elif after["x"] < -0.5 or after["x"] + after["width"] > 375.5:
            # القياس الصارم: الخلية **كاملة** داخل الشاشة، لا طرفها وحده
            errors.append(
                f"العمود الأول خرج جزئياً من الشاشة بعد التمرير "
                f"(x={after['x']:.0f}, w={after['width']:.0f}) — التثبيت ناقص")
        elif after_text.strip() != before_text.strip():
            errors.append("ما يظهر بعد التمرير ليس العمود الأول — التثبيت لا يعمل")
        else:
            print(f"   ✓ العمود الأول «{after_text.strip()[:18]}» بقي كاملاً "
                  f"بعد تمرير {moved:.0f} بكسل")

        print("→ تمييز القيم الشاذة وإخفاء أعمدة الوسم")
        headers = data_section.locator("thead th").all_inner_texts()
        internal = [h for h in headers if h.strip().startswith("_")]
        if internal:
            errors.append(f"أعمدة داخلية ظاهرة للمستخدم: {internal}")
        else:
            print("   ✓ لا أعمدة داخلية في جدول المستخدم")
        marked = data_section.locator("td[title*='شاذة']").count()
        print(f"   ✓ {marked} خلية شاذة موسومة بلون في الصفحة الحالية"
              if marked else "   • لا قيم شاذة في هذه الصفحة")

        print("→ الفرز من رأس العمود")
        # الفرز يجري في الخادم على الملف كلّه. لو كان محلياً لرتّب الـ25 صفاً
        # المعروضة فقط — وهو أسوأ من غياب الفرز لأنه يبدو صحيحاً.
        head_btn = data_section.locator("thead button").nth(3)
        first_before = data_section.locator("table tbody tr").first.inner_text()
        head_btn.click()
        page.wait_for_timeout(1600)
        asc_first = data_section.locator("table tbody tr").first.inner_text()
        head_btn.click()
        page.wait_for_timeout(1600)
        desc_first = data_section.locator("table tbody tr").first.inner_text()
        if asc_first == desc_first:
            errors.append("الفرز تصاعدي/تنازلي يعطي النتيجة نفسها")
        elif asc_first == first_before and desc_first == first_before:
            errors.append("الفرز لم يغيّر ترتيب الصفوف")
        else:
            print("   ✓ الفرز يغيّر الترتيب فعلاً في الاتجاهين")

        print("→ البحث العربي داخل الجدول")
        # عيب حقيقي كان هنا: تطبيع نص البحث وحده يجعل أي كلمة فيها «ة»
        # لا تطابق شيئاً. نبحث عن قيمة **ظاهرة في الجدول نفسه**.
        search_box = data_section.get_by_label("ابحث في البيانات")
        if search_box.count() == 0:
            errors.append("لا يوجد بحث في شاشة البيانات")
        else:
            before_rows = data_section.locator("table tbody tr").count()
            search_box.fill("شركة")
            page.wait_for_timeout(1800)
            after_text = data_section.inner_text()
            if "ما في نتائج" in after_text:
                errors.append("البحث عن «شركة» لم يجد شيئاً — التطبيع مكسور")
            elif data_section.locator("table tbody tr").count() == 0:
                errors.append("البحث أفرغ الجدول")
            else:
                page.screenshot(path=str(SHOTS / "10-البحث.png"), full_page=True)
                print(f"   ✓ البحث العربي يعمل ({before_rows} صف قبل، ونتائج بعده)")
            page.get_by_role("button", name="امسح البحث").click()
            page.wait_for_timeout(1200)

        print("→ حالات الشاشة: ملف قيد المعالجة، وملف فاشل")
        # الداخل من رابط مباشر إلى ملف قيد المعالجة كان يرى صفحة فارغة
        # بلا تفسير. نتحقق أن الشاشة تشرح ما يجري وتتحدّث وحدها.
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("text=ملفاتي", timeout=20000)
        page.set_input_files("input[type=file]", str(sample))
        page.wait_for_timeout(250)
        # ندخل لصفحة الملف فور ظهوره في القائمة (قبل اكتمال المعالجة)
        page.wait_for_url("**/datasets/**", timeout=90000)
        page.wait_for_selector("text=الرسوم", timeout=60000)

        bad = ROOT / "artifacts" / "بيانات-غير-صالحة.pdf"
        bad.write_bytes(b"%PDF-1.4 not really a data file")
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("text=ملفاتي", timeout=20000)
        page.set_input_files("input[type=file]", str(bad))
        page.wait_for_timeout(9000)
        body_now = page.inner_text("body")
        # الرسالة تأتي من المحرك ويجب أن تقول **ما العمل** لا أن تشتكي فقط
        if "غير مدعومة" not in body_now:
            errors.append("فشل المعالجة لا يظهر للمستخدم برسالة مفهومة")
        elif "Excel" not in body_now and "CSV" not in body_now:
            errors.append("رسالة الفشل لا تقترح مخرجاً على المستخدم")
        else:
            print("   ✓ الفشل يظهر برسالة عربية تقترح البديل، لا شاشة فارغة")
        page.screenshot(path=str(SHOTS / "9-فشل.png"), full_page=True)

        print("→ حذف الملف من القائمة")
        page.goto(base, wait_until="domcontentloaded")
        page.wait_for_selector("text=ملفاتي", timeout=20000)
        while page.get_by_role("button", name="حذف").count() > 1:
            page.get_by_role("button", name="حذف").first.click()
            page.get_by_role("button", name="نعم، احذف").click()
            page.wait_for_timeout(2500)
        page.get_by_role("button", name="حذف").first.click()
        page.wait_for_timeout(300)
        # تأكيد صريح: الحذف نهائي فلا يجوز أن يتم بنقرة واحدة عرضية
        if page.get_by_text("حذف نهائي؟").count() == 0:
            errors.append("الحذف يتم بلا تأكيد")
        page.screenshot(path=str(SHOTS / "8-الحذف.png"))
        page.get_by_role("button", name="نعم، احذف").click()
        page.wait_for_timeout(3000)
        if page.get_by_text("ما في ملفات بعد").count() == 0:
            errors.append("الملف لم يختفِ من القائمة بعد الحذف")
        else:
            print("   ✓ الحذف يطلب تأكيداً ثم يزيل الملف فعلاً")

        browser.close()

    if errors:
        print("\n✗ مشاكل:")
        for e in dict.fromkeys(errors):
            print("   •", e)
        return 1
    print(f"\n✓ المسار كامل يعمل بمتصفح حقيقي. اللقطات في {SHOTS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
