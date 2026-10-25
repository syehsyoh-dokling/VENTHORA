# ruff: noqa: E501
"""Record the dashboard part of the demo video: a scripted three-minute walk through /live and the Control center.

Runs Microsoft Edge through Playwright at 1920x1080, drives the operations page on the real Airwallex sandbox (the demo
server must be running on a clean store: `python scripts/demo_server.py --reset`), then the Control center, and writes:

  demo_video/dashboard.webm      the recording (no audio; the voice-over is added in editing)
  demo_video/cues.json           when each scene started, in seconds from the first frame
  demo_video/cues.md             the same as a table, next to the voice-over lines in docs/DEMO_VOICEOVER_3MIN.md

A visible cursor moves to each control before it is clicked, scrolling is eased, and the part being described is
outlined. Scene start times are targets: a scene never starts early, and when Airwallex takes longer than planned the
scene starts late and the cue sheet records the real time.

Usage:  python scripts/record_demo.py [--base http://localhost:8001] [--out demo_video] [--headed]
Needs:  pip install playwright (Microsoft Edge is used, so no browser download is needed).
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parents[1]

HELPERS = r"""
(() => {
  if (window.__rec) return;
  const css = `.rec-spot{outline:3px solid #2f6fe4 !important;outline-offset:6px !important;border-radius:10px;
      box-shadow:0 0 0 10px rgba(47,111,228,.16) !important;transition:outline-color .3s}
    #rec-cursor{position:fixed;left:0;top:0;width:24px;height:24px;margin:-12px 0 0 -12px;border-radius:50%;
      background:rgba(47,111,228,.30);border:2px solid #2f6fe4;z-index:2147483647;pointer-events:none;
      transition:left .7s cubic-bezier(.3,.7,.2,1),top .7s cubic-bezier(.3,.7,.2,1),transform .15s,background .15s}
    #rec-cursor.down{transform:scale(.65);background:rgba(47,111,228,.65)}
    html{scroll-behavior:auto !important}`;
  const ease = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  function ensure() {
    if (!document.getElementById('rec-style')) {
      const s = document.createElement('style'); s.id = 'rec-style'; s.textContent = css; document.head.appendChild(s);
    }
    let c = document.getElementById('rec-cursor');
    if (!c) {
      c = document.createElement('div'); c.id = 'rec-cursor';
      c.style.left = (innerWidth * 0.62) + 'px'; c.style.top = (innerHeight * 0.55) + 'px';
      document.body.appendChild(c);
    }
    return c;
  }
  function scrollToY(y, ms) {
    ensure();
    const max = document.documentElement.scrollHeight - innerHeight;
    const y0 = scrollY, d = Math.max(0, Math.min(y, max)) - y0, t0 = performance.now();
    return new Promise(res => {
      if (Math.abs(d) < 2 || ms <= 0) { scrollTo(0, y0 + d); return res(); }
      function f(n) { const p = Math.min(1, (n - t0) / ms); scrollTo(0, y0 + d * ease(p)); p < 1 ? requestAnimationFrame(f) : res(); }
      requestAnimationFrame(f);
    });
  }
  function scrollToEl(el, ms, offset) { const r = el.getBoundingClientRect(); return scrollToY(scrollY + r.top - offset, ms); }
  function spot(el, on) { ensure(); el.classList.toggle('rec-spot', on); }
  // Outline a whole block: an h2 outlines its section; an h3 outlines everything from it to the next h3 (or the end
  // of its container), drawn as an overlay so the page layout does not move.
  function spotBlock(el) {
    ensure();
    if (el.tagName === 'H2' && el.closest('section')) return spot(el.closest('section'), true);
    const host = el.parentElement, a = el.getBoundingClientRect();
    let next = el.nextElementSibling;
    while (next && next.tagName !== 'H3') next = next.nextElementSibling;
    const bottom = next ? next.getBoundingClientRect().top - 8 : host.getBoundingClientRect().bottom - 4;
    const h = host.getBoundingClientRect();
    const o = document.createElement('div'); o.className = 'rec-box';
    Object.assign(o.style, {position: 'absolute', left: (h.left + scrollX - 8) + 'px', top: (a.top + scrollY - 8) + 'px',
      width: (h.width + 16) + 'px', height: (bottom - a.top + 14) + 'px', border: '3px solid #2f6fe4', borderRadius: '12px',
      boxShadow: '0 0 0 10px rgba(47,111,228,.14)', pointerEvents: 'none', zIndex: 50});
    document.body.appendChild(o);
  }
  function clearSpots() {
    document.querySelectorAll('.rec-spot').forEach(e => e.classList.remove('rec-spot'));
    document.querySelectorAll('.rec-box').forEach(e => e.remove());
  }
  async function cursorTo(el) {
    const c = ensure(), r = el.getBoundingClientRect();
    c.style.left = (r.left + Math.min(r.width / 2, 60)) + 'px'; c.style.top = (r.top + r.height / 2) + 'px';
    await sleep(750);
  }
  async function press() { const c = ensure(); c.classList.add('down'); await sleep(160); c.classList.remove('down'); }
  window.__rec = { ensure, scrollToY, scrollToEl, spot, spotBlock, clearSpots, cursorTo, press, sleep };
  if (document.readyState !== 'loading') ensure(); else addEventListener('DOMContentLoaded', ensure);
})();
"""


class Recorder:
    def __init__(self, page: Page, t0: float):
        self.page, self.t0, self.cues, self.checks = page, t0, [], {}

    def now(self) -> float:
        return time.monotonic() - self.t0

    def at(self, target: float, scene: str) -> None:
        """Start a scene no earlier than `target` seconds; record when it really started."""
        wait = target - self.now()
        if wait > 0:
            time.sleep(wait)
        start = round(self.now(), 1)
        self.cues.append({"t": start, "target": target, "scene": scene})
        print(f"{start:6.1f}s  (target {target:5.1f})  {scene}", flush=True)

    def js(self, expr: str, arg=None):
        self.page.evaluate("() => window.__rec && window.__rec.ensure()")
        return self.page.evaluate(expr, arg)

    def el(self, selector: str, text: str | None = None, nth: int = 0):
        loc = self.page.locator(selector, has_text=text) if text else self.page.locator(selector)
        return loc.nth(nth).element_handle(timeout=30_000)

    def block(self, selector: str, text: str | None = None, scroll: bool = True, offset: int = 110, ms: int = 1000) -> None:
        """Scroll to a heading and outline its whole block."""
        self.clear()
        if scroll:
            self.scroll_to(selector, text, ms=ms, offset=offset)
        h = self.el(selector, text)
        self.js("([e]) => window.__rec.spotBlock(e)", [h])

    def spot_nth(self, selector: str, nth: int) -> None:
        self.clear()
        h = self.el(selector, nth=nth)
        self.js("([e]) => window.__rec.spot(e, true)", [h])

    def scroll_to(self, selector: str, text: str | None = None, ms: int = 1100, offset: int = 90) -> None:
        h = self.el(selector, text)
        self.js("([e, ms, off]) => window.__rec.scrollToEl(e, ms, off)", [h, ms, offset])

    def scroll_y(self, y: int, ms: int = 1100) -> None:
        self.js("([y, ms]) => window.__rec.scrollToY(y, ms)", [y, ms])

    def spot(self, selector: str, text: str | None = None, hold: float = 0.0, clear: bool = True) -> None:
        if clear:
            self.js("() => window.__rec.clearSpots()")
        h = self.el(selector, text)
        self.js("([e]) => window.__rec.spot(e, true)", [h])
        if hold:
            time.sleep(hold)

    def clear(self) -> None:
        self.js("() => window.__rec.clearSpots()")

    def click(self, selector: str, text: str | None = None) -> None:
        h = self.el(selector, text)
        h.scroll_into_view_if_needed()
        self.js("([e]) => window.__rec.cursorTo(e)", [h])
        self.js("() => window.__rec.press()")
        h.click()

    def wait_next_ready(self, timeout_s: int = 180) -> None:
        """After a Next click on /live: wait until the step finished (button enabled again, or the run is complete)."""
        time.sleep(0.4)
        self.page.wait_for_function(
            "() => { const b = document.getElementById('nextBtn'); return !b || !b.disabled; }",
            timeout=timeout_s * 1000)
        err = self.page.locator("#busy .err")
        if err.count() and err.first.is_visible():
            raise RuntimeError(f"the step failed on the page: {err.first.inner_text()}")

    def click_button(self, name: str) -> None:
        h = self.page.get_by_role("button", name=name, exact=True).first.element_handle(timeout=30_000)
        h.scroll_into_view_if_needed()
        self.js("([e]) => window.__rec.cursorTo(e)", [h])
        self.js("() => window.__rec.press()")
        h.click()

    def top_card(self, ms: int = 900, offset: int = 200) -> None:
        self.scroll_to("#cards > *", ms=ms, offset=offset)


def run(base: str, out: Path, headed: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "raw"
    tmp.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=not headed)
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(tmp), record_video_size={"width": 1920, "height": 1080},
                                  color_scheme="light")
        ctx.add_init_script(HELPERS)
        t0 = time.monotonic()
        page = ctx.new_page()
        r = Recorder(page, t0)
        page.goto(f"{base}/live", wait_until="networkidle")
        page.wait_for_selector("#grantBtn", timeout=30_000)
        r.js("() => window.scrollTo(0, 0)")

        # ---- 1 · goal and mandate (0:00 - 0:23) -----------------------------------------------------------------
        r.at(0.0, "Goal and mandate: the goal")
        r.block("h3", "Goal", scroll=False)
        r.at(3.0, "Permissions, each with its own limit")
        r.block("h3", "What VENTHORA may do on its own", ms=1100)
        r.at(7.0, "Safeguards: FX exposure, overdue invoices, late interest")
        r.block("h3", "Safeguards VENTHORA keeps")
        r.at(10.0, "Always enforced: no duplicates, verified bank changes")
        r.block("h3", "Always enforced")
        r.at(13.0, "Budget period: monthly, quarterly or annual")
        r.scroll_to("h3", "Budget for the period", ms=900)
        r.clear()
        r.spot("#f-period")
        r.at(16.0, "Approvers and the approval matrix")
        r.block("h3", "Approvers and the approval matrix")
        r.at(19.5, "Requests received: payables, payroll, email")
        r.block("h3", "Requests received")
        r.at(22.0, "Save the mandate and start")
        r.clear()
        r.click("#grantBtn")
        page.wait_for_function("() => { const b = document.getElementById('nextBtn'); return b && !b.disabled; }",
                               timeout=180_000)

        # ---- 2 · initial plan (0:25 - 0:46) --------------------------------------------------------------------
        r.at(25.0, "Initial plan: requested")
        r.click("#nextBtn")
        r.wait_next_ready()
        r.top_card()
        r.at(26.5, "Initial plan: decisions with reasons")
        r.spot("#cards > * table")
        r.at(36.0, "Initial plan: checks and counterfactual alternatives")
        r.clear()
        r.click("#cards > * details summary")
        time.sleep(0.5)
        r.scroll_to("#cards > * details", ms=900, offset=160)
        r.spot("#cards > * details .grid2 > div:last-child")

        # ---- 3 · new information (0:46 - 0:55) -----------------------------------------------------------------
        r.at(46.0, "New information: the customer's delay email, quoted")
        r.clear()
        r.click("#nextBtn")
        r.wait_next_ready()
        r.top_card()
        r.spot("#cards > * .quote")

        # ---- 4 · revised decision (0:55 - 1:06) ----------------------------------------------------------------
        r.at(55.0, "Revised decision: FX limit halved, approval required")
        r.clear()
        r.click("#nextBtn")
        r.wait_next_ready()
        r.top_card(offset=130)
        r.spot("#cards > * .tiles")
        r.at(60.5, "Revised decision: only the affected row changes, the rest locked")
        r.spot("#cards > * tr.changed")

        # ---- 3+4 · incoming payment confirmed (from 1:06; timed from when Airwallex confirms it) ----------------
        r.at(66.0, "Incoming payment: waiting for Airwallex to settle it")
        r.clear()
        r.click("#nextBtn")
        r.wait_next_ready()
        r.top_card(offset=130)
        dep = r.now()
        card = page.locator("#cards > *").first.inner_text()
        r.checks["deposit settled"] = "SETTLED" in card and "not counted" not in card
        r.at(dep, "Incoming payment: SETTLED, balance re-read, re-planned")
        r.spot("#cards > * .tiles")
        r.at(dep + 6, "Incoming payment: the subscription is paid, budget alert")
        r.spot("#cards > * tr.changed")

        # ---- 5 · execution and resulting state (timed from when Airwallex finishes) ----------------------------
        r.at(dep + 11, "Execution on Airwallex: waiting for the rail")
        r.clear()
        r.click("#nextBtn")
        r.wait_next_ready(timeout_s=240)
        r.top_card(offset=130)
        base = r.now()
        card = page.locator("#cards > *").first.inner_text()
        r.checks["execution completed"] = "COMPLETED" in card
        r.checks["four transfers paid"] = sum(1 for line in card.splitlines() if line.startswith("transfer") and "PAID" in line) == 4
        r.checks["two conversions settled"] = sum(1 for line in card.splitlines() if line.startswith("conversion") and "SETTLED" in line) == 2
        r.at(base, "Result: conversions settled, four payments PAID")
        r.spot_nth("#cards > * table", 0)
        r.at(base + 7, "Result: wallet movement equals the book's ledger")
        r.scroll_to("#cards > * table", ms=900, offset=-300)
        r.spot_nth("#cards > * table", 1)
        time.sleep(2.5)
        r.spot_nth("#cards > * table", 2)

        # ---- Control center (timed from the result) ------------------------------------------------------------
        r.at(base + 13, "Control center: open")
        r.clear()
        r.scroll_y(0, ms=700)
        time.sleep(0.8)
        r.click("header a", "Control center")
        page.wait_for_load_state("networkidle")
        r.js("() => window.scrollTo(0, 0)")
        r.at(base + 16, "Platform: two policy engines, hash-chained audit, production refused")
        r.block("h2", "Platform", scroll=False)
        r.at(base + 20, "Budget against plan for the latest run")
        r.block("h2", "Budget · mandate", offset=90)
        r.at(base + 25, "Books, ledgers and tenants")
        r.block("h2", "Books · ledger", offset=90)
        time.sleep(2.5)
        r.block("h2", "Tenants · books", offset=90)
        r.at(base + 31, "Ground-truth checks: a payout the bank returned (Kit 03)")
        r.clear()
        r.scroll_to("h2", "Ground-truth checks", ms=1000, offset=90)
        page.select_option("#gtSuite", "anomalies")
        time.sleep(1.0)
        r.js("() => { const c = document.getElementById('gtCase'); const o = [...c.options].find(o => o.text.startsWith('N34'));"
             " c.value = o.value; c.dispatchEvent(new Event('change')); }")
        time.sleep(0.8)
        r.click("#submitCase")
        page.wait_for_function("() => /REPLACE/.test(document.getElementById('subOut').innerText)", timeout=30_000)
        r.spot("#subOut")
        r.at(base + 41, "Resilience test matrix: 136 of 136")
        r.block("h2", "Resilience test matrix", offset=200)
        r.at(base + 46, "Close: back to the top")
        r.clear()
        r.scroll_y(0, ms=1500)
        r.at(base + 50, "End")
        time.sleep(1.0)

        video = page.video
        ctx.close()
        browser.close()
        raw = Path(video.path())

    final = out / "dashboard.webm"
    if final.exists():
        final.unlink()
    raw.replace(final)
    length = r.cues[-1]["t"] + 1.0
    r.checks["under three minutes"] = length <= 180.0
    valid = all(r.checks.values())
    (out / "cues.json").write_text(json.dumps({"valid": valid, "checks": r.checks, "length_s": length, "cues": r.cues},
                                              indent=2) + "\n", encoding="utf-8")
    lines = ["| Time | Scene |", "|---|---|"]
    for c in r.cues:
        m, s = divmod(c["t"], 60)
        lines.append(f"| {int(m)}:{s:04.1f} | {c['scene']} |")
    lines += ["", "Checks: " + ", ".join(f"{k} {'yes' if v else 'NO'}" for k, v in r.checks.items())]
    (out / "cues.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"-> {final}  ({final.stat().st_size / 1e6:.1f} MB), about {length:.0f} s")
    print("checks:", r.checks)
    if not valid:
        print("INVALID TAKE: the sandbox did not produce the story the voice-over tells; reset the store and record again.")
        raise SystemExit(2)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://localhost:8001")
    ap.add_argument("--out", default=str(ROOT / "demo_video"))
    ap.add_argument("--headed", action="store_true", help="show the browser window while recording")
    a = ap.parse_args()
    run(a.base, Path(a.out), a.headed)
