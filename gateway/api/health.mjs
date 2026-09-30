import proxyModule from './_proxy.cjs';
export default function handler(req, res) { return proxyModule.proxy(req, res, '/health'); }
