const PUBLIC_HOST = "romanlms.com";
const ORIGIN_HOST = "romanlms-origin.romanlms.com";

const ACCESS_CLIENT_ID = "ROMANLMS_ORIGIN_ACCESS_CLIENT_ID";
const ACCESS_CLIENT_SECRET = "ROMANLMS_ORIGIN_ACCESS_CLIENT_SECRET";

const HOP_BY_HOP_HEADERS = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

function errorResponse(status, message) {
  return new Response(message, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function httpsRedirect(publicUrl) {
  const secureUrl = new URL(publicUrl);
  secureUrl.protocol = "https:";
  secureUrl.port = "";
  return new Response(null, {
    status: 308,
    headers: {
      "Cache-Control": "no-store",
      Location: secureUrl.href,
    },
  });
}

function rewriteOriginRedirect(response, originUrl) {
  const location = response.headers.get("Location");
  if (!location) return response;

  let destination;
  try {
    destination = new URL(location, originUrl);
  } catch {
    return response;
  }

  if (destination.hostname.toLowerCase() !== ORIGIN_HOST) return response;

  destination.hostname = PUBLIC_HOST;
  destination.protocol = "https:";
  destination.port = "";

  const headers = new Headers(response.headers);
  headers.set("Location", destination.href);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export default {
  async fetch(request, env) {
    const publicUrl = new URL(request.url);

    if (publicUrl.hostname.toLowerCase() !== PUBLIC_HOST) {
      return errorResponse(404, "Not found.");
    }

    if (publicUrl.protocol === "http:") {
      return httpsRedirect(publicUrl);
    }

    if (publicUrl.protocol !== "https:") {
      return errorResponse(400, "Unsupported request scheme.");
    }

    const clientId = env[ACCESS_CLIENT_ID];
    const clientSecret = env[ACCESS_CLIENT_SECRET];
    if (!clientId || !clientSecret) {
      return errorResponse(503, "Service unavailable.");
    }

    const originUrl = new URL(publicUrl);
    originUrl.hostname = ORIGIN_HOST;
    originUrl.protocol = "https:";
    originUrl.port = "";
    const clientIp = request.headers.get("CF-Connecting-IP");

    try {
      const requestAtOrigin = new Request(originUrl, request);
      const headers = new Headers(requestAtOrigin.headers);

      for (const name of HOP_BY_HOP_HEADERS) headers.delete(name);

      for (const name of [...headers.keys()]) {
        const lowerName = name.toLowerCase();
        if (
          lowerName === "forwarded" ||
          lowerName.startsWith("x-forwarded-") ||
          lowerName.startsWith("cf-access-") ||
          lowerName === "cf-connecting-ip"
        ) {
          headers.delete(name);
        }
      }
      headers.delete("X-Real-IP");

      headers.set("X-Forwarded-Host", PUBLIC_HOST);
      headers.set("X-Forwarded-Proto", "https");
      headers.set("X-Forwarded-Port", "443");

      if (clientIp) {
        headers.set("X-Forwarded-For", clientIp);
        headers.set("X-Real-IP", clientIp);
      }

      headers.set("CF-Access-Client-Id", clientId);
      headers.set("CF-Access-Client-Secret", clientSecret);

      const authorizedRequest = new Request(requestAtOrigin, { headers });
      const response = await fetch(authorizedRequest);
      return rewriteOriginRedirect(response, originUrl);
    } catch {
      return errorResponse(502, "Upstream unavailable.");
    }
  },
};
