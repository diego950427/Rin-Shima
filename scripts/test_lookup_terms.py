"""Browser regression on the local lookup, with no student session or uploads."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
expect.set_options(timeout=20000)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={'width': 1366, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('response', lambda response: print('DATA', response.status, response.url.rsplit('/', 1)[-1], flush=True)
            if 'semester_courses_' in response.url else None)
    page.goto('http://localhost:8521/', wait_until='domcontentloaded')
    page.locator('#js-hamburger').click()
    page.locator('a[href="#course-lookup"]').click()
    try:
        expect(page.locator('#lookup-status')).to_have_text('1910 門課')
    except AssertionError:
        print('JS ERRORS', errors)
        print('NETWORK', page.evaluate("performance.getEntriesByType('resource').filter(e=>e.name.includes('semester_courses')).map(e=>({duration:e.duration,size:e.transferSize,end:e.responseEnd}))"))
        raise
    terms = ['115_1'] + [term for term in ('114_2', '114_1')
                        if (ROOT / 'assets' / f'semester_courses_{term}.json').exists()] + ['115_1']
    for term in terms:
        print('TEST', term, flush=True)
        page.locator('.lookup-term-picker summary').click()
        page.locator(f'[data-lookup-term="{term}"]').click()
        data = json.loads((ROOT / 'assets' / f'semester_courses_{term}.json').read_text(encoding='utf-8'))
        expect(page.locator('#lookup-status')).to_have_text(f'{len(data["courses"])} 門課')
        expect(page.locator(f'[data-lookup-term="{term}"]')).to_have_attribute('aria-current', 'true')
        expect(page.locator('.lookup-source')).to_contain_text(f'{term[:3]} {"上" if term.endswith("1") else "下"}學期')
        assert page.locator('.lookup-course').count() == min(20, len(data['courses']))
    page.locator('#lookup-grade').select_option('1')
    page.locator('#lookup-department').select_option('地球環境暨生物資源學系')
    expect(page.locator('#lookup-status')).to_have_text('6 門課')
    page.locator('.lookup-term-picker summary').click()
    page.locator('[data-lookup-term="114_2"]').click()
    expect(page.locator('.lookup-source')).to_contain_text('114 下學期')
    expect(page.locator('#lookup-grade')).to_have_value('1')
    expect(page.locator('#lookup-department')).to_have_value('地球環境暨生物資源學系')
    for width in (1366, 390):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    assert not errors, errors
    browser.close()
print(f'PASS: {len(set(terms))} terms, counts, labels, return switch, filters retained, responsive overflow, no JS errors')
