"""Local-only HTTP server for the review workbench (stdlib, no extra dependencies).

Safety posture (a prototype, not production security):
- binds to loopback only; requests with a foreign Host header are refused
  (DNS-rebinding guard);
- every state change is a POST; a cross-origin Origin header is refused and the
  actor cookie is SameSite=Strict;
- documents are served only through (case, filename) rows recorded at ingestion,
  with a SHA-256 re-check, never from a client-supplied path;
- no script runs in any page; evidence text is escaped; text formats are served
  as text/plain with nosniff.
"""
from __future__ import annotations

import argparse
import re
import sys
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from . import views
from .policy import ACTORS, DEFAULT_ACTOR
from .service import NotFound, Workbench, WorkbenchError

LOOPBACK = {'127.0.0.1', 'localhost', '::1', '[::1]'}
MAX_BODY = 256 * 1024
ID_RE = re.compile(r'[A-Za-z0-9_-]{1,80}')
FILE_RE = re.compile(r'[A-Za-z0-9._-]{1,120}')
ACTIONS = {'edit', 'select-version', 'assign', 'request-correction', 'escalate', 'comment',
           'revalidate', 'approve', 'reject', 'reopen', 'pay', 'resolve-correction'}
NOTICE_KEY = {'select-version': 'select', 'request-correction': 'request'}
RAW_TYPES = {'pdf': 'application/pdf', 'json': 'text/plain; charset=utf-8', 'csv': 'text/plain; charset=utf-8',
             'xml': 'text/plain; charset=utf-8', 'txt': 'text/plain; charset=utf-8'}


def make_handler(wb):
    class Handler(BaseHTTPRequestHandler):
        server_version = 'InvoiceWorkbench/1'
        protocol_version = 'HTTP/1.1'

        # -------------------------------------------------- plumbing
        def log_message(self, fmt, *args):
            if not getattr(self.server, 'quiet', False):
                sys.stderr.write('%s - %s\n' % (self.address_string(), fmt % args))

        def actor(self):
            cookie = SimpleCookie(self.headers.get('Cookie', ''))
            morsel = cookie.get('demo_actor')
            return ACTORS.get(morsel.value if morsel else '', DEFAULT_ACTOR)

        def send(self, status, body, ctype='text/html; charset=utf-8', headers=None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('Cache-Control', 'no-store')
            if self.close_connection:
                self.send_header('Connection', 'close')
            if ctype.startswith('text/html'):
                self.send_header('Content-Security-Policy',
                                 "default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-src 'self'; "
                                 "form-action 'self'; base-uri 'none'; frame-ancestors 'self'")
                self.send_header('X-Frame-Options', 'SAMEORIGIN')
            elif ctype.startswith('text/plain'):
                self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def redirect(self, location, headers=None):
            self.send(HTTPStatus.SEE_OTHER, b'', 'text/plain; charset=utf-8', {'Location': location, **(headers or {})})

        def host_ok(self):
            host = (self.headers.get('Host') or '').strip().lower()
            name = host.rsplit(':', 1)[0] if not host.startswith('[') else host.split(']')[0] + ']'
            return name in LOOPBACK

        def fail(self, status, message, actor=None, back='/'):
            self.send(status, views.error_page(f'{status.value} {status.phrase}', message, actor or self.actor(), back, wb.clock()))

        # -------------------------------------------------- GET
        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self.host_ok():
                return self.send(HTTPStatus.MISDIRECTED_REQUEST, 'Host not allowed', 'text/plain; charset=utf-8')
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            parts = [unquote(p) for p in url.path.strip('/').split('/')]
            actor, now = self.actor(), wb.clock()
            try:
                if url.path == '/':
                    return self.send(HTTPStatus.OK, views.inbox(wb.list_cases(), query, actor, now))
                if url.path == '/healthz':
                    return self.send(HTTPStatus.OK, 'ok', 'text/plain; charset=utf-8')
                if url.path == '/favicon.ico':
                    return self.send(HTTPStatus.NO_CONTENT, b'', 'text/plain; charset=utf-8')
                if len(parts) == 2 and parts[0] == 'case' and ID_RE.fullmatch(parts[1]):
                    st = wb.case(parts[1])
                    notice = views.NOTICES.get(query.get('done', ''))
                    return self.send(HTTPStatus.OK, views.case_page(wb, st, wb.history(parts[1]), actor, query, now, notice))
                if len(parts) == 5 and parts[0] == 'case' and parts[2] == 'doc' and parts[4] == 'raw' \
                        and ID_RE.fullmatch(parts[1]) and FILE_RE.fullmatch(parts[3]):
                    path, fmt = wb.document_path(parts[1], parts[3])
                    return self.send(HTTPStatus.OK, path.read_bytes(), RAW_TYPES.get(fmt, 'application/octet-stream'),
                                     {'Content-Disposition': f'inline; filename="{path.name}"'})
                raise NotFound('No such page')
            except WorkbenchError as exc:
                self.fail(HTTPStatus(exc.status), str(exc), actor)

        # -------------------------------------------------- POST
        def do_POST(self):
            self.close_connection = True      # any early refusal leaves the body unread: never reuse the socket
            if not self.host_ok():
                return self.send(HTTPStatus.MISDIRECTED_REQUEST, 'Host not allowed', 'text/plain; charset=utf-8')
            origin = self.headers.get('Origin')
            if origin and urlsplit(origin).netloc.lower() != (self.headers.get('Host') or '').lower():
                return self.fail(HTTPStatus.FORBIDDEN, 'Cross-origin form posts are refused.')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                return self.fail(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'Request body too large.')
            if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/x-www-form-urlencoded':
                return self.fail(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, 'Forms only.')
            form = {k: v[-1] for k, v in parse_qs(self.rfile.read(length).decode('utf-8', 'replace'),
                                                  keep_blank_values=True).items()}
            self.close_connection = False
            parts = urlsplit(self.path).path.strip('/').split('/')
            actor = self.actor()
            if parts == ['actor']:
                chosen = form.get('actor', '')
                nxt = form.get('next', '/')
                nxt = nxt if re.fullmatch(r'/(?:case/[A-Za-z0-9_-]+)?', nxt) else '/'
                if chosen not in ACTORS:
                    return self.fail(HTTPStatus.UNPROCESSABLE_ENTITY, 'Unknown demo actor.')
                return self.redirect(nxt, {'Set-Cookie': f'demo_actor={chosen}; Path=/; SameSite=Strict; HttpOnly'})
            if len(parts) != 3 or parts[0] != 'case' or not ID_RE.fullmatch(parts[1]) or parts[2] not in ACTIONS:
                return self.fail(HTTPStatus.NOT_FOUND, 'No such action.')
            case_id, action = parts[1], parts[2]
            back = f'/case/{quote(case_id)}'
            rev, evidence = form.get('rev'), form.get('evidence')
            try:
                if action == 'edit':
                    values = {k[2:]: v for k, v in form.items() if k.startswith('f:')}
                    wb.edit_fields(case_id, actor, rev, form.get('doc', ''), values, form.get('reason'), evidence)
                elif action == 'select-version':
                    wb.select_version(case_id, actor, rev, form.get('filename', ''), form.get('reason'), evidence)
                elif action == 'assign':
                    wb.assign(case_id, actor, rev, form.get('assignee', ''), form.get('reason'), evidence)
                elif action == 'request-correction':
                    wb.request_correction(case_id, actor, rev, form.get('party', ''), form.get('message'), evidence)
                elif action == 'resolve-correction':
                    wb.resolve_correction(case_id, actor, rev, form.get('reason'), evidence)
                elif action == 'escalate':
                    wb.escalate(case_id, actor, rev, form.get('target', ''), form.get('reason'), evidence)
                elif action == 'comment':
                    wb.comment(case_id, actor, form.get('text'))
                elif action == 'revalidate':
                    wb.revalidate(case_id, actor, rev, evidence)
                elif action == 'approve':
                    wb.approve(case_id, actor, rev, form.get('gate', ''), evidence, form.get('reason'))
                elif action == 'reject':
                    wb.reject(case_id, actor, rev, form.get('reason'), evidence)
                elif action == 'reopen':
                    wb.reopen(case_id, actor, rev, form.get('reason'), evidence)
                elif action == 'pay':
                    wb.pay(case_id, actor, rev, evidence)
            except WorkbenchError as exc:
                return self.fail(HTTPStatus(exc.status), str(exc), actor, back)
            self.redirect(f'{back}?done={NOTICE_KEY.get(action, action)}#history')

    return Handler


def make_server(wb, host='127.0.0.1', port=8765, quiet=False):
    if host not in LOOPBACK:
        raise ValueError('The workbench only binds to loopback addresses')
    server = ThreadingHTTPServer((host, port), make_handler(wb))
    server.daemon_threads = True
    server.quiet = quiet
    return server


def main(argv=None):
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description='Local invoice review workbench (demo; not production RBAC).')
    parser.add_argument('--data-dir', type=Path, default=root / 'data/invoices')
    parser.add_argument('--db', type=Path, default=root / 'output/workbench.db',
                        help='SQLite file holding inventory, mock ledger and all review state')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if not args.data_dir.is_dir():
        parser.error(f'Data directory not found: {args.data_dir}')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbench(args.db, args.data_dir)
    report = wb.ingest()
    for name in report['changed_on_disk']:
        print(f'WARNING: {name} changed on disk since ingestion; its evidence is withheld.', file=sys.stderr)
    server = make_server(wb, args.host, args.port)
    print(f'Workbench on http://{args.host}:{args.port}/  db={args.db}  '
          f'(added {len(report["added"])}, already known {len(report["unchanged"])})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
