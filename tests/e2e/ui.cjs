// Test giao diện M4 bằng Playwright: chạy 3 thao tác (thả file, nghe A/B, tải về) và thanh trượt trên trình duyệt thật.
// Cách chạy: node tests/e2e/ui.cjs <base_url> <file_ok.wav> <file_short.wav> [chromium|webkit]
// tests/test_web.py tự dựng server và gọi script này. In ra 1 dòng JSON; mã thoát 0 = qua.
const { chromium, webkit } = require("playwright");

const [base, okFile, shortFile, engine = "chromium"] = process.argv.slice(2);

function check(cond, msg) {
  if (!cond) throw new Error(msg);
}

(async () => {
  const browser = await { chromium, webkit }[engine].launch({ args: engine === "chromium" ? ["--autoplay-policy=no-user-gesture-required"] : [] });
  const page = await browser.newPage({ acceptDownloads: true });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
  const out = { engine };
  try {
    await page.goto(base);

    // file quá ngắn: báo lỗi ngay ở trình duyệt, không upload
    await page.setInputFiles("#file-input", shortFile);
    await page.waitForSelector("#drop-error:not([hidden])");
    out.short_error = await page.textContent("#drop-error");
    check(out.short_error.includes("5 giây"), `lỗi file ngắn sai: ${out.short_error}`);

    // thao tác 1: thả file → phân tích → kết quả
    await page.setInputFiles("#file-input", okFile);
    await page.waitForSelector("#analysis-section:not([hidden])");
    await page.waitForFunction(() => document.getElementById("stat-noise").textContent !== "…", null, { timeout: 120000 });
    out.stats = await page.$$eval(".stat b", (els) => els.map((e) => e.textContent));
    await page.waitForSelector("#result-section:not([hidden])", { timeout: 180000 });
    out.rows = await page.$$eval("#metrics tr", (trs) => trs.map((tr) => [...tr.children].map((td) => td.textContent)));
    check(out.rows.length === 6, `bảng số đo có ${out.rows.length} dòng`);
    out.explanation = await page.textContent("#explanation");
    check(out.explanation.length > 20, "không có lời giải thích");
    out.sliders = await page.$$eval(".slider output", (els) => els.map((e) => e.textContent));
    check(out.sliders[1].startsWith("tự động"), "thanh Giảm ồn không hiện giá trị tự động");
    const waveInk = await page.$eval("#wave-proc", (c) => {
      const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;
      return n / (d.length / 4);
    });
    check(waveInk > 0.05, `waveform gần như trống (${waveInk})`);

    // thao tác 2: nghe A/B, chuyển bản giữ nguyên vị trí
    await page.click("#play");
    await page.waitForFunction(() => document.getElementById("audio-proc").currentTime > 0.6, null, { timeout: 10000 });
    await page.keyboard.press("a");
    const ab = await page.evaluate(() => ({
      orig: document.getElementById("audio-orig").currentTime,
      proc: document.getElementById("audio-proc").currentTime,
      on: document.querySelector(".ab button.on").dataset.v,
      playing: !document.getElementById("audio-orig").paused && !document.getElementById("audio-proc").paused,
    }));
    out.ab = ab;
    check(ab.on === "orig" && ab.playing, "chuyển sang A không được");
    check(Math.abs(ab.orig - ab.proc) < 0.1, `A và B lệch ${Math.abs(ab.orig - ab.proc)} s`);
    await page.keyboard.press("Space");
    check(await page.$eval("#audio-orig", (a) => a.paused), "Space không dừng");
    const posBefore = await page.$eval("#audio-orig", (a) => a.currentTime);

    // thanh trượt: render lại, vẫn giữ vị trí nghe
    const hrefBefore = await page.getAttribute("#dl-wav", "href");
    await page.$eval("#sl-loudness", (el) => { el.value = "-3"; el.dispatchEvent(new Event("input")); el.dispatchEvent(new Event("change")); });
    await page.waitForFunction((h) => document.getElementById("dl-wav").getAttribute("href") !== h, hrefBefore, { timeout: 120000 });
    await page.waitForSelector("#rerender", { state: "hidden" });
    out.lufs_target_after_slider = await page.$eval("#metrics tr:last-child td:nth-child(4)", (td) => td.textContent);
    check(out.lufs_target_after_slider.startsWith("−28"), `bảng số đo không cập nhật: ${out.lufs_target_after_slider}`);
    check((await page.textContent("#out-loudness")) === "−3 LU", "nhãn thanh Độ to sai");
    const posAfter = await page.$eval("#audio-orig", (a) => a.currentTime);
    check(Math.abs(posAfter - posBefore) < 0.1, `render lại làm mất vị trí nghe (${posBefore} → ${posAfter})`);

    // preset
    const hrefSlider = await page.getAttribute("#dl-wav", "href");
    await page.click('label:has(input[value="soft"])');
    await page.waitForFunction((h) => document.getElementById("dl-wav").getAttribute("href") !== h, hrefSlider, { timeout: 120000 });
    await page.waitForSelector("#rerender", { state: "hidden" });
    // preset Êm hơn: mục tiêu thêm −3 LU (đang có thanh Độ to −3) → −30
    out.lufs_target_soft = await page.$eval("#metrics tr:last-child td:nth-child(4)", (td) => td.textContent);
    check(out.lufs_target_soft.startsWith("−31"), `preset không áp dụng: ${out.lufs_target_soft}`);

    // thao tác 3: tải về
    const [dl] = await Promise.all([page.waitForEvent("download"), page.click("#dl-wav")]);
    out.download = dl.suggestedFilename();
    check(out.download.endsWith("_asmr.wav"), `tên file tải về sai: ${out.download}`);
    out.download_bytes = require("fs").statSync(await dl.path()).size;
    check(out.download_bytes > 1000, "file tải về rỗng");

    check(errors.length === 0, `lỗi JS: ${errors.join(" | ")}`);
    out.ok = true;
  } catch (e) {
    out.ok = false;
    out.error = String(e && e.message ? e.message : e);
    out.js_errors = errors;
    await page.screenshot({ path: `${process.env.E2E_SHOT_DIR || "."}/e2e-fail-${engine}.png`, fullPage: true }).catch(() => {});
  }
  await browser.close();
  console.log(JSON.stringify(out));
  process.exit(out.ok ? 0 : 1);
})();
