/* Runtime configuration.
 *
 * In a container this file is replaced by the one mounted from the Helm
 * ConfigMap (see values/frontend.yaml), so the same image runs in every
 * environment without a rebuild. app.js falls back to these values if the file
 * is missing or a key is absent.
 *
 * These are the Ingress URLs, not the in-cluster Service DNS. This file is read
 * by the browser, which can reach api.upi-sim.local but not upi-auth:5005.
 */
window.__UPI_CONFIG__ = {
  psp:       'https://api.upi-sim.local/psp',
  switch:    'https://api.upi-sim.local/switch',
  payerBank: 'https://api.upi-sim.local/payer',
  payeeBank: 'https://api.upi-sim.local/payee',
  auth:      'https://api.upi-sim.local/auth',
};
