/* Runtime configuration.
 *
 * In a container this file is replaced by the one mounted from the Helm
 * ConfigMap (see values/frontend.yaml), so the same image runs in every
 * environment without a rebuild. app.js falls back to these values if the file
 * is missing or a key is absent.
 *
 * The paths are relative on purpose. The browser calls the same origin it loaded
 * the app from (the Ingress host), which:
 *   - uses the certificate the browser already trusts (a second host means a
 *     second exception, and fetch() fails silently without it), and
 *   - is same-origin, so no CORS.
 * Each Ingress rewrites /api/<svc> away before the request reaches the pod.
 */
window.__UPI_CONFIG__ = {
  psp:       '/api/psp',
  switch:    '/api/switch',
  payerBank: '/api/payer',
  payeeBank: '/api/payee',
  auth:      '/api/auth',
  recharge:  '/api/recharge',
  // Sable agent — same-origin, Ingress forwards /agent/voice + /agent/ops
  // un-rewritten to the Unified Agent Service.
  agent:     '/agent',
};
