export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // CORS: lets browser-based chat apps call this Worker.
    // Authorization is listed explicitly because "*" does not cover it.
    const cors = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers":
        request.headers.get("Access-Control-Request-Headers") ||
        "authorization, content-type",
      "Access-Control-Max-Age": "86400"
    };

    // Browser preflight requests carry no API key, so answer them before auth.
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    const json = (data, status = 200) =>
      Response.json(data, {
        status,
        headers: { "Cache-Control": "no-store", ...cors }
      });

    // Register the current Kaggle tunnel.
    if (url.pathname === "/_register") {
      if (request.method !== "POST") {
        return json({ error: "POST required" }, 405);
      }

      if (
        request.headers.get("Authorization") !==
        `Bearer ${env.REGISTER_TOKEN}`
      ) {
        return json({ error: "Unauthorized" }, 401);
      }

      let data;
      try {
        data = await request.json();
      } catch {
        return json({ error: "Invalid JSON" }, 400);
      }

      let tunnel;
      try {
        tunnel = new URL(data.url);
      } catch {
        return json({ error: "Invalid URL" }, 400);
      }

      if (
        tunnel.protocol !== "https:" ||
        !tunnel.hostname.endsWith(".trycloudflare.com")
      ) {
        return json({ error: "Invalid tunnel hostname" }, 400);
      }

      await env.TUNNEL_KV.put("upstream", tunnel.origin);
      return json({ ok: true });
    }

    // Authenticate API requests.
    if (
      request.headers.get("Authorization") !==
      `Bearer ${env.API_KEY}`
    ) {
      return json({
        error: {
          message: "Missing or invalid API key",
          type: "authentication_error"
        }
      }, 401);
    }

    const upstream = await env.TUNNEL_KV.get("upstream");

    if (!upstream) {
      return json({ error: "No Kaggle tunnel registered" }, 503);
    }

    // Only expose the API paths needed by clients.
    if (!url.pathname.startsWith("/v1/")) {
      return json({ error: "Not found" }, 404);
    }

    const destination = new URL(
      url.pathname + url.search,
      upstream
    );

    const headers = new Headers(request.headers);
    headers.set("Authorization", `Bearer ${env.API_KEY}`);
    headers.delete("host");
    headers.delete("content-length");
    // Browser-only headers; not needed upstream.
    headers.delete("origin");
    headers.delete("referer");

    const init = {
      method: request.method,
      headers,
      redirect: "manual"
    };

    if (!["GET", "HEAD"].includes(request.method)) {
      init.body = request.body;
    }

    try {
      const response = await fetch(destination, init);
      const responseHeaders = new Headers(response.headers);
      responseHeaders.set("Cache-Control", "no-store");
      responseHeaders.delete("content-length");
      responseHeaders.delete("content-encoding");
      for (const [k, v] of Object.entries(cors)) {
        responseHeaders.set(k, v);
      }

      // Pass the body straight through so streaming (SSE) keeps working.
      return new Response(response.body, {
        status: response.status,
        headers: responseHeaders
      });
    } catch {
      return json({
        error: "Kaggle tunnel is unavailable"
      }, 502);
    }
  }
};
