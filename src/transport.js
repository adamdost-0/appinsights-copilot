import https from 'node:https';
import { Readable } from 'node:stream';

// Unlike fetch, native HTTPS does not follow redirects or decompress responses.
export function sendHttps({ url, headers, body, signal }) {
  return new Promise((resolve, reject) => {
    const request = https.request(url, { method: 'POST', headers, signal }, (response) => {
      const responseHeaders = new Headers();
      for (const [name, value] of Object.entries(response.headers)) {
        if (value !== undefined) responseHeaders.set(name, Array.isArray(value) ? value.join(', ') : value);
      }
      resolve({
        status: response.statusCode,
        headers: responseHeaders,
        body: Readable.toWeb(response),
      });
    });
    request.on('error', reject);
    request.end(body);
  });
}
