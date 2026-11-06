const { test } = require("node:test");
const assert = require("node:assert/strict");
const {
  skillEvidence,
  parseDemoState,
  exampleData,
} = require("../.test-build/workspace-data.js");
const { api, ApiError } = require("../.test-build/api.js");

test("evidence matches complete skill names without claiming verification", () => {
  assert.deepEqual(
    skillEvidence(exampleData.projects, "FIGMA").map((project) => project.id),
    ["folio", "forma"],
  );
  assert.equal(skillEvidence(exampleData.projects, "design").length, 0);
});

test("invalid browser data recovers and valid edits survive reload", () => {
  for (const invalid of [
    null,
    "broken json",
    "null",
    '{"profile":{}}',
    JSON.stringify({ ...exampleData, projects: [{ id: "bad" }] }),
  ]) {
    assert.equal(parseDemoState(invalid).profile.full_name, "Alex Morgan");
  }
  const edited = structuredClone(exampleData);
  edited.profile.full_name = "Taylor Sample";
  const restored = parseDemoState(JSON.stringify(edited));
  assert.equal(restored.profile.full_name, "Taylor Sample");
  restored.projects[0].name = "Changed";
  assert.equal(exampleData.projects[0].name, "Folio — Finance, made human");
});

test("example resume details survive reload only when their shape is valid", () => {
  const edited = structuredClone(exampleData);
  edited.resume = {
    id: "demo", filename: "resume.txt", media_type: "text/plain", byte_size: 10,
    sha256: "demo", extraction_method: "heuristic",
    extracted_data: { full_name: "Taylor", email: "t@example.com", phone: "", target_roles: [], skills: ["Python"], education: [], experience: [], projects: [], certifications: [] },
    reviewed_at: null, created_at: "now", updated_at: "now",
  };
  assert.equal(parseDemoState(JSON.stringify(edited)).resume.extracted_data.skills[0], "Python");
  edited.resume.extracted_data.skills = "invalid";
  assert.equal(parseDemoState(JSON.stringify(edited)).resume, null);
});

test("API mutations send JSON, the decoded CSRF token, and same-origin credentials", async (t) => {
  global.document = { cookie: "unrelated=x; __Host-proofpath_csrf=abc%3D123" };
  t.after(() => delete global.document);
  const mock = t.mock.method(global, "fetch", async (url, options) => {
    assert.equal(url, "/api/v1/me/projects");
    assert.equal(options.method, "POST");
    assert.equal(options.credentials, "same-origin");
    assert.equal(options.headers["X-CSRF-Token"], "abc=123");
    assert.deepEqual(JSON.parse(options.body), { name: "Example" });
    return new Response(JSON.stringify({ id: "created" }), { status: 201 });
  });
  assert.deepEqual(await api("/me/projects", "POST", { name: "Example" }), {
    id: "created",
  });
  assert.equal(mock.mock.callCount(), 1);
});

test("API sends multipart bodies without overriding the browser boundary", async (t) => {
  global.document = { cookie: "__Host-proofpath_csrf=token" };
  t.after(() => delete global.document);
  const form = new FormData();
  form.set("file", new Blob(["resume"]), "resume.pdf");
  const mock = t.mock.method(global, "fetch", async (_url, options) => {
    assert.equal(options.body, form);
    assert.equal(options.headers["Content-Type"], undefined);
    assert.equal(options.headers["X-CSRF-Token"], "token");
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
  assert.deepEqual(await api("/me/resume", "POST", form), { ok: true });
  assert.equal(mock.mock.callCount(), 1);
});

test("API failures preserve validation messages and handle non-JSON errors", async (t) => {
  const mock = t.mock.method(
    global,
    "fetch",
    async () =>
      new Response(
        JSON.stringify({
          detail: "This project changed. Reload and try again.",
        }),
        { status: 409 },
      ),
  );
  await assert.rejects(
    api("/me/projects", "PATCH", {}),
    (error) =>
      error instanceof ApiError &&
      error.status === 409 &&
      error.message.includes("Reload"),
  );
  mock.mock.mockImplementation(
    async () => new Response("Bad gateway", { status: 502 }),
  );
  await assert.rejects(
    api("/projects"),
    (error) => error instanceof ApiError && error.status === 502,
  );
});

test("API accepts empty 204 responses and preserves cancellation", async (t) => {
  const mock = t.mock.method(
    global,
    "fetch",
    async () => new Response(null, { status: 204 }),
  );
  assert.equal(await api("/me/projects/example", "DELETE"), undefined);
  mock.mock.mockImplementation(async () => {
    throw new DOMException("Cancelled", "AbortError");
  });
  await assert.rejects(api("/projects"), (error) => error.name === "AbortError");
});
