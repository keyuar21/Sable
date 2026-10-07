/* Runtime configuration.
 *
 * Local development uses the values below. In a container this file is replaced
 * by the one mounted from the Helm ConfigMap, so the same image runs in every
 * environment without a rebuild. app.js falls back to these defaults if the
 * file is missing or a key is absent.
 */
window.__UPI_CONFIG__ = {
  psp:       'http://localhost:5001',
  switch:    'http://localhost:5002',
  payerBank: 'http://localhost:5003',
  payeeBank: 'http://localhost:5004',
  auth:      'http://localhost:5005',
};
