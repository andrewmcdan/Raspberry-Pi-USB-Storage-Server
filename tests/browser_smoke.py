"""Optional real-browser checks. Run against an isolated fixture, never a Pi.

Linux: python3 tests/browser_smoke.py (starts a temporary Flask fixture)
Windows: start tests/web_fixture.py in WSL, then pass --url http://127.0.0.1:8765
         --channel msedge to use the locally installed Edge browser.
Requires the development-only playwright package and a Chromium browser.
"""
import argparse
from pathlib import Path
import threading

from playwright.sync_api import sync_playwright, expect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--channel")
    args = parser.parse_args()
    fixture = server = None
    if not args.url:
        from web_fixture import WebFixture
        from werkzeug.serving import make_server
        fixture = WebFixture("true")
        server = make_server("127.0.0.1", 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        args.url = f"http://127.0.0.1:{server.server_port}"
    output = Path(__file__).resolve().parents[1] / "test-results"
    output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, **({"channel": args.channel} if args.channel else {}))
            context = browser.new_context(viewport={"width": 1280, "height": 1000})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # No external network access is needed for the UI or preview worker.
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(args.url + "/") else route.abort())
            page.goto(args.url)
            page.get_by_label("Username").fill("admin")
            page.get_by_label("Password").fill("test-only")
            page.get_by_role("button", name="Sign in").click()
            page.get_by_role("row").filter(has_text="jobs/part.GCODE").get_by_role("link", name="Preview", exact=True).click()
            expect(page.locator("#status")).to_have_text("Preview ready")
            expect(page.locator("#layer-label")).to_contain_text("2 / 2")
            expect(page.locator("#summary")).to_contain_text("10 move segments")
            page.get_by_role("button", name="Previous layer").click()
            expect(page.locator("#layer-label")).to_contain_text("1 / 2")
            page.get_by_label("Selected layer only").check()
            page.get_by_label("Show travel").check()
            page.get_by_role("button", name="Top view", exact=True).click()
            page.get_by_role("button", name="Zoom in", exact=True).click()
            page.get_by_role("button", name="Fit view", exact=True).click()
            page.get_by_role("button", name="3D view", exact=True).click()
            page.locator("canvas").focus()
            page.keyboard.press("ArrowRight")
            page.keyboard.press("+")
            page.keyboard.press("0")
            page.get_by_role("button", name="Next layer").click()
            page.get_by_label("Selected layer only").uncheck()
            # A rendered path must contribute colored pixels, not just a blank canvas.
            painted = page.locator("canvas").evaluate("canvas => { const data = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data; let n=0; for(let i=0;i<data.length;i+=4) if(data[i+3]) n++; return n; }")
            assert painted > 500, painted
            page.screenshot(path=str(output / "gcode-viewer-desktop.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.get_by_role("button", name="Next layer")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(output / "gcode-viewer-mobile.png"), full_page=True)
            # Use real uploads: request interception of worker fetches differs
            # between browser builds. This also checks the upload-to-preview flow.
            for name, content, message in [("empty.gcode", b"; empty\n", "No supported toolpath"),
                                           ("binary.gcode", b"GCDE\x00\x01", "Binary")]:
                page.get_by_role("link", name="Staged files").click()
                token = page.locator("#upload-form input[name=csrf_token]").input_value()
                response = context.request.post(args.url + "/api/uploads/file", multipart={
                    "csrf_token": token, "folder": "jobs", "file": {"name": name, "mimeType": "text/plain", "buffer": content}})
                assert response.status == 201
                page.reload()
                page.get_by_role("row").filter(has_text=f"jobs/{name}").get_by_role("link", name="Preview", exact=True).click()
                expect(page.locator("#status")).to_contain_text(message)
                expect(page.get_by_role("button", name="Top view", exact=True)).to_be_disabled()
            assert not errors, errors
            browser.close()
        print("Browser smoke checks passed: rendering, layers, controls, mobile layout, upload-to-preview, empty and binary files.")
    finally:
        if server:
            server.shutdown()
        if fixture:
            fixture.close()


if __name__ == "__main__":
    main()
