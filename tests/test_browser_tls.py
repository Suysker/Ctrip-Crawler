"""Verify project-only TLS behavior with an ephemeral local certificate."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import shutil
import ssl
import subprocess
import threading

import pytest

from section_loader import load_sections


pytestmark = pytest.mark.skipif(
    os.getenv('CTRIP_BROWSER_TESTS') != '1', reason='Requires the installed Camoufox runtime')


def test_project_context_accepts_untrusted_tls_without_relaxing_other_contexts(tmp_path):
    openssl = os.getenv('CTRIP_TEST_OPENSSL') or shutil.which('openssl')
    if not openssl:
        pytest.skip('Requires OpenSSL on PATH or CTRIP_TEST_OPENSSL')
    certificate, key = tmp_path / 'certificate.pem', tmp_path / 'key.pem'
    subprocess.run([
        openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
        '-keyout', str(key), '-out', str(certificate), '-subj', '/CN=localhost',
        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1',
    ], check=True, capture_output=True, timeout=20)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<title>Local TLS fixture</title><p>project context reached HTTPS</p>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *unused):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(certificate, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f'https://127.0.0.1:{server.server_port}/'

        async def verify():
            from playwright.async_api import Error
            app = load_sections('imports', 'config', 'processing', 'browser')
            config = dict(app.DEFAULT_CONFIG, headless=True)
            session = app.BrowserSession(config, app.generate_identity(config), {}, None)
            try:
                await session.start()
                page = await session.context.new_page()
                response = await page.goto(url, timeout=10000)
                assert response.status == 200
                assert await page.title() == 'Local TLS fixture'
                # The same browser's unrelated context must still reject this certificate.
                strict = await session.browser.new_context(ignore_https_errors=False)
                try:
                    strict_page = await strict.new_page()
                    with pytest.raises(Error, match='SSL_ERROR_UNKNOWN|SEC_ERROR_UNKNOWN_ISSUER'):
                        await strict_page.goto(url, timeout=10000)
                finally:
                    await strict.close()
            finally:
                await session.close()

        try:
            asyncio.run(verify())
        finally:
            server.shutdown()
            thread.join(timeout=5)
