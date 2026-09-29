/*
 * Run from the repository root with:
 *   node --test itis3135/tests/introduction-form.test.js
 *
 * The test checks the rendered Accumulus result and the standards-check
 * report, so it needs network access to the course validator services.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { test } = require("node:test");
const { chromium } = require("playwright");

const ROOT = path.resolve(__dirname, "../..");
const CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml"
};

function startStaticServer() {
    const server = http.createServer((request, response) => {
        const requestPath = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
        const filePath = path.resolve(ROOT, `.${requestPath}`);
        if (filePath !== ROOT && !filePath.startsWith(`${ROOT}${path.sep}`)) {
            response.writeHead(403);
            response.end();
            return;
        }

        fs.readFile(filePath, (error, contents) => {
            if (error) {
                response.writeHead(404);
                response.end();
                return;
            }
            response.writeHead(200, {
                "Content-Type": CONTENT_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream",
                "Cache-Control": "no-store"
            });
            response.end(request.method === "HEAD" ? undefined : contents);
        });
    });

    return new Promise((resolve, reject) => {
        server.once("error", reject);
        server.listen(0, "127.0.0.1", () => {
            const address = server.address();
            resolve({ server, origin: `http://127.0.0.1:${address.port}` });
        });
    });
}

function launchBrowser() {
    const candidates = [
        process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,
        process.env.CHROME_BIN,
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser"
    ].filter(Boolean);
    const executablePath = candidates.find((candidate) => fs.existsSync(candidate));
    const options = { headless: true, args: ["--no-sandbox"] };
    if (executablePath) {
        options.executablePath = executablePath;
    }
    return chromium.launch(options);
}

test("introduction generator works and Accumulus reports no errors", { timeout: 180000 }, async (context) => {
    const { server, origin } = await startStaticServer();
    let browser;
    context.after(async () => {
        if (browser) {
            await browser.close();
        }
        await new Promise((resolve) => server.close(resolve));
    });

    browser = await launchBrowser();
    const page = await browser.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(error.message));

    await page.goto(`${origin}/itis3135/introduction_form.html`, { waitUntil: "load" });
    await page.waitForSelector("#course-list [data-course-row]");
    await page.waitForFunction(() => document.querySelector('[name="img"]').value.startsWith("data:image/jpeg;base64,"));
    await page.waitForFunction(() => window.accumulusresults && window.accumulusresults.results);
    await page.waitForSelector("#standards-check-badge");

    const accumulusResults = await page.evaluate(() => {
        const results = window.accumulusresults.results;
        return Object.fromEntries(Object.entries(results).map(([category, result]) => [category, {
            errors: result.errors,
            warnings: result.warnings,
            issues: result.issues,
            targets: result.targets
                .filter((target) => target.errors || target.warnings || target.issues)
                .map((target) => ({
                    location: target.location,
                    errors: target.errors,
                    warnings: target.warnings,
                    issues: target.issues,
                    details: target.details
                }))
        }]));
    });
    const accumulusProblems = Object.entries(accumulusResults)
        .filter(([, result]) => result.errors || result.warnings || result.issues);
    assert.deepEqual(accumulusProblems, [], `Accumulus findings:\n${JSON.stringify(accumulusResults, null, 2)}`);

    const standardsBadge = page.locator("#standards-check-badge");
    assert.match(await standardsBadge.getAttribute("title"), /All standards checks pass/);
    await standardsBadge.click();
    const standardsReport = await page.locator("#standards-check-badge + div").innerText();
    assert.match(standardsReport, /0 fail/, standardsReport);
    assert.doesNotMatch(standardsReport, /❌|⚠️/, standardsReport);
    await standardsBadge.click();

    const requiredNames = [
        "firstName", "lastName", "adjectives", "animal", "img", "caption", "personalStatement",
        "personalBackground", "professionalBackground", "academicBackground", "primaryWorkComputer",
        "primaryWorkLocation", "alternateComputerLocation"
    ];
    for (const name of requiredNames) {
        assert.equal(await page.locator(`[name="${name}"]`).getAttribute("required"), "", `${name} should be required`);
    }
    for (const name of ["middleName", "nickname", "divider", "funnyItem", "somethingToShare"]) {
        assert.equal(await page.locator(`[name="${name}"]`).getAttribute("required"), null, `${name} should be optional`);
    }

    assert.equal(await page.locator("#course-list [data-course-row]").count(), 5);
    for (const name of ["department", "courseNumber", "courseTitle", "reasonfortaking"]) {
        assert.equal(await page.locator(`#course-list [name="${name}"]`).first().getAttribute("required"), "", `${name} should be required in each course row`);
    }
    await page.getByRole("button", { name: "Clear", exact: true }).click();
    const clearedValues = await page.locator("#introduction-form input:not([type='file']), #introduction-form textarea")
        .evaluateAll((fields) => fields.every((field) => field.value === ""));
    assert.equal(clearedValues, true, "Clear should empty every form field");
    await page.getByRole("button", { name: "Reset", exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll("#course-list [data-course-row]").length === 5
        && document.querySelector('[name="firstName"]').value === "Thomas");

    await page.locator('[name="firstName"]').fill("Taylor");
    await page.locator('[name="divider"]').fill("~");
    await page.getByRole("button", { name: "Add Course" }).click();
    const newCourse = page.locator("#course-list [data-course-row]").last();
    await newCourse.locator('[name="department"]').fill("WEB");
    await newCourse.locator('[name="courseNumber"]').fill("9999");
    await newCourse.locator('[name="courseTitle"]').fill("Test Course");
    await newCourse.locator('[name="reasonfortaking"]').fill("Testing any-number course support.");
    await page.locator("#course-list [data-course-row]").first().getByRole("button", { name: "Delete Course" }).click();

    await page.getByRole("button", { name: "Generate JSON" }).click();
    const data = JSON.parse(await page.locator("#json-output").inputValue());
    assert.equal(data.firstName, "Taylor");
    assert.match(data.img, /^data:image\/jpeg;base64,/);
    assert.equal(data.caption, "Me, at a friend's house");
    assert.ok(data.personalBackground);
    assert.ok(data.professionalBackground);
    assert.ok(data.academicBackground);
    assert.ok(data.primaryWorkComputer);
    assert.ok(data.primaryWorkLocation);
    assert.ok(data.alternateComputerLocation);
    assert.equal(data.courses.length, 5);
    assert.deepEqual(data.courses[data.courses.length - 1], {
        department: "WEB",
        courseNumber: "9999",
        courseTitle: "Test Course",
        reasonfortaking: "Testing any-number course support."
    });

    const wrapToggle = page.getByRole("checkbox", { name: "Wrap output text" });
    assert.equal(await wrapToggle.isChecked(), false);
    const noWrapState = await page.locator("#json-output").evaluate((textarea) => ({
        wrap: textarea.wrap,
        whiteSpace: getComputedStyle(textarea).whiteSpace,
        classApplied: textarea.classList.contains("no-wrap")
    }));
    assert.deepEqual(noWrapState, { wrap: "off", whiteSpace: "pre", classApplied: true });

    await wrapToggle.check();
    assert.equal(await page.locator("#json-output").evaluate((textarea) => textarea.wrap), "soft");
    await page.getByRole("button", { name: "Generate XML" }).click();
    assert.equal(await page.locator("#xml-output").evaluate((textarea) => textarea.wrap), "soft");
    const xmlIsValid = await page.evaluate((xml) => {
        const document = new DOMParser().parseFromString(xml, "application/xml");
        return document.querySelector("parsererror") === null;
    }, await page.locator("#xml-output").inputValue());
    assert.equal(xmlIsValid, true, "Generated XML should be well-formed");
    await wrapToggle.uncheck();
    assert.equal(await page.locator("#json-output").evaluate((textarea) => textarea.classList.contains("no-wrap")), true);
    assert.equal(await page.locator("#xml-output").evaluate((textarea) => textarea.classList.contains("no-wrap")), true);
    assert.equal(await page.locator("#json-output").evaluate((textarea) => textarea.wrap), "off");

    await page.locator("#picture-upload").setInputFiles(path.join(ROOT, "itis3135/images/tommyforintroduction.jpeg"));
    await page.waitForFunction(() => document.querySelector("#image-preview").src.startsWith("data:image/"));
    await page.getByRole("button", { name: "Generate HTML" }).click();
    const html = await page.locator("#html-output").inputValue();
    assert.equal(await page.locator("#html-output").evaluate((textarea) => textarea.wrap), "off");
    assert.equal(await page.locator("#html-output").evaluate((textarea) => textarea.classList.contains("no-wrap")), true);
    assert.match(html, /^<!DOCTYPE html>/i);
    assert.match(html, /styles\/default\.css/);
    assert.match(html, /standards-check\.js/);
    assert.match(html, /lint\.page\/kit\/4d0fe3\.js/);
    assert.match(html, /components\/header\.html/);
    assert.match(html, /scripts\/HTMLInclude\.min\.js/);
    assert.match(html, /data:image\//, "HTML output should use the uploaded picture");
    assert.match(html, /Test Course/);
    const generatedHTML = await page.evaluate((markup) => {
        const parsed = new DOMParser().parseFromString(markup, "text/html");
        return {
            title: parsed.title,
            heading: parsed.querySelector("main > h2").textContent,
            image: parsed.querySelector("main img").getAttribute("src"),
            hasFooter: Boolean(parsed.querySelector("main footer"))
        };
    }, html);
    assert.equal(generatedHTML.heading, "Introduction");
    assert.equal(generatedHTML.title, "Thomas McElroy's Terrific Muskrat | ITIS3135 | Introduction");
    assert.equal(generatedHTML.hasFooter, true);
    assert.match(generatedHTML.image, /^data:image\//);

    await page.getByRole("button", { name: "Submit", exact: true }).click();
    await page.waitForSelector("#generated-introduction:not([hidden])");
    assert.match(await page.locator("#display-name").innerText(), /Taylor A\. "Tommy" McElroy ~ Terrific Muskrat/);
    assert.match(await page.locator("#generated-introduction img").getAttribute("src"), /^data:image\//);
    assert.equal(await page.locator("#generated-introduction ol li").count(), 5);
    assert.equal(await page.locator("#start-over a").isVisible(), true);
    assert.deepEqual(pageErrors, [], `Browser JavaScript errors:\n${pageErrors.join("\n")}`);
});
