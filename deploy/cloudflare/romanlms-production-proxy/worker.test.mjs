import assert from "node:assert/strict";
import test from "node:test";

import worker from "./worker.mjs";

const ENV = {
  ROMANLMS_ORIGIN_ACCESS_CLIENT_ID: "test-client-id",
  ROMANLMS_ORIGIN_ACCESS_CLIENT_SECRET: "test-client-secret",
};

async function withFetch(stub, callback) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = stub;
  try {
    return await callback();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("forwards path, query, auth, cookies, and trusted public forwarding headers", async () => {
  await withFetch(async (request) => {
    assert.equal(request.url, "https://romanlms-origin.romanlms.com/lms/api/files?download=1");
    assert.equal(request.method, "POST");
    assert.equal(request.headers.get("Origin"), "https://romanlms.com");
    assert.equal(request.headers.get("Cookie"), "romanlms_rt=sample");
    assert.equal(request.headers.get("Authorization"), "Bearer sample");
    assert.equal(request.headers.get("X-Forwarded-Host"), "romanlms.com");
    assert.equal(request.headers.get("X-Forwarded-Proto"), "https");
    assert.equal(request.headers.get("X-Forwarded-Port"), "443");
    assert.equal(request.headers.get("X-Forwarded-For"), "198.51.100.24");
    assert.equal(request.headers.get("X-Real-IP"), "198.51.100.24");
    assert.equal(request.headers.get("Forwarded"), null);
    assert.equal(request.headers.get("X-Forwarded-Ssl"), null);
    assert.equal(request.headers.get("CF-Access-Authenticated-User-Email"), null);
    assert.equal(request.headers.get("CF-Connecting-IP"), null);
    assert.equal(request.headers.get("CF-Access-Client-Id"), ENV.ROMANLMS_ORIGIN_ACCESS_CLIENT_ID);
    assert.equal(request.headers.get("CF-Access-Client-Secret"), ENV.ROMANLMS_ORIGIN_ACCESS_CLIENT_SECRET);
    assert.equal(await request.text(), "{\"fileId\":\"example\"}");
    return new Response("ok", { status: 200 });
  }, async () => {
    const request = new Request("https://romanlms.com/lms/api/files?download=1", {
      method: "POST",
      headers: {
        Authorization: "Bearer sample",
        "CF-Connecting-IP": "198.51.100.24",
        Cookie: "romanlms_rt=sample",
        Forwarded: "for=spoofed",
        Origin: "https://romanlms.com",
        "X-Forwarded-For": "203.0.113.99",
        "X-Forwarded-Host": "spoofed.example",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Ssl": "on",
        "CF-Access-Authenticated-User-Email": "attacker@example.test",
      },
      body: "{\"fileId\":\"example\"}",
    });

    const response = await worker.fetch(request, ENV);
    assert.equal(response.status, 200);
    assert.equal(await response.text(), "ok");
  });
});

test("rewrites only redirects that point to the protected origin hostname", async () => {
  await withFetch(async () => new Response(null, {
    status: 302,
    headers: { Location: "https://romanlms-origin.romanlms.com/lms/auth/refresh?next=%2F" },
  }), async () => {
    const response = await worker.fetch(new Request("https://romanlms.com/"), ENV);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get("Location"), "https://romanlms.com/lms/auth/refresh?next=%2F");
  });

  await withFetch(async () => new Response(null, {
    status: 302,
    headers: { Location: "https://accounts.example.test/login" },
  }), async () => {
    const response = await worker.fetch(new Request("https://romanlms.com/"), ENV);
    assert.equal(response.headers.get("Location"), "https://accounts.example.test/login");
  });
});

test("does not proxy worker.dev or other hostnames", async () => {
  let called = false;
  await withFetch(async () => {
    called = true;
    return new Response("unexpected");
  }, async () => {
    const response = await worker.fetch(new Request("https://romanlms-edge.workers.dev/"), ENV);
    assert.equal(response.status, 404);
    assert.equal(called, false);
  });
});

test("redirects HTTP to HTTPS without calling the protected origin", async () => {
  let called = false;
  await withFetch(async () => {
    called = true;
    return new Response("unexpected");
  }, async () => {
    const response = await worker.fetch(new Request("http://romanlms.com/docs?format=pdf"), ENV);
    assert.equal(response.status, 308);
    assert.equal(response.headers.get("Location"), "https://romanlms.com/docs?format=pdf");
    assert.equal(called, false);
  });
});

test("fails closed when Access secrets are not configured", async () => {
  let called = false;
  await withFetch(async () => {
    called = true;
    return new Response("unexpected");
  }, async () => {
    const response = await worker.fetch(new Request("https://romanlms.com/"), {});
    assert.equal(response.status, 503);
    assert.equal(await response.text(), "Service unavailable.");
    assert.equal(called, false);
  });
});

test("returns a generic gateway error without leaking origin details", async () => {
  await withFetch(async () => {
    throw new Error("sensitive upstream detail");
  }, async () => {
    const response = await worker.fetch(new Request("https://romanlms.com/"), ENV);
    assert.equal(response.status, 502);
    assert.equal(await response.text(), "Upstream unavailable.");
    assert.equal(response.headers.get("Cache-Control"), "no-store");
  });
});
