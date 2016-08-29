import argparse
import asyncio
import socket
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from importlib import machinery

import httptools
import sys

import uvloop
from httptools.parser.errors import HttpParserUpgrade
from werkzeug.urls import url_parse, url_unquote

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
loop = asyncio.get_event_loop()
executor = ThreadPoolExecutor(max_workers=20)


class HttpProtocol(asyncio.Protocol):

    def __init__(self, application, loop=None):
        self.loop = loop
        self.request = None
        self.parser = httptools.HttpRequestParser(self)
        self.application = application
        self.headers = {}
        self.path = None
        self.body_queue = []
        self.content_length = 0
        self.closed = False
        self.upgrade = False
        self.websocket_protocol = None

    def connection_made(self, transport):
        self.transport = transport
        sock = transport.get_extra_info('socket')
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, NameError):
            pass

    def data_received(self, data):
        if self.websocket_protocol:
            self.websocket_protocol.data_received(data)
            return

        try:
            self.parser.feed_data(data)
        except HttpParserUpgrade as e:
            '''
            let framework handle protocol upgrade
            '''
            self.upgrade = True

    def on_header(self, name, value):
        try:
            self.headers[name.decode('utf8')] = value.decode('utf8')
        except:
            traceback.print_exc()

    def on_headers_complete(self):
        if asyncio.iscoroutinefunction(self.application):
            asyncio.ensure_future(self.async_process_response(), loop=self.loop)
        else:
            loop.run_in_executor(executor, self.process_response)

    def on_url(self, url):
        self.path = url

    def on_body(self, data):
        self.body_queue.append(data)

    def read(self):
        return self.body_queue.pop(0)

    def eof_received(self):
        self.closed = True

    async def async_process_response(self):
        try:
            it = await self.application(self.make_environ(), self.start_response)
            self.write(b'\r\n')
            for data in it:
                self.write(data)

            # self.transport.write('Content-Length: {}\r\n'.format(len(b)).encode('utf8'))
            self.write_eof()
        except:
            traceback.print_exc()
            self.write_eof()

    def write(self, data):
        print('write', data)
        self.transport.write(data)

    def write_eof(self):
        if not self.closed and not self.upgrade:
            self.write_eof()

    def process_response(self):
        try:
            it = self.application(self.make_environ(), self.start_response)
            self.write(b'\r\n')
            for data in it:
                self.write(data)

            # self.transport.write('Content-Length: {}\r\n'.format(len(b)).encode('utf8'))
            if not self.closed:
                self.write_eof()
        except:
            traceback.print_exc()
            if not self.closed:
                self.write_eof()

    def start_response(self, status, response_headers):
        self.write('HTTP/1.1 {}\r\n'.format(status).encode('utf8'))
        for header in response_headers:
            if header[0].lower() == 'content-length':
                self.content_length = header[1]

            if header[0].lower() == 'connection' and header[1].lower() == 'upgrade':
                self.upgrade = True

            self.write('{0}: {1}\r\n'.format(header[0], header[1]).encode('utf8'))

    def make_environ(self):
        request_url = url_parse(self.path)

        # url_scheme = self.server.ssl_context is None and 'http' or 'https'
        path_info = url_unquote(request_url.path)

        environ = {
            'wsgi.version': (1, 0),
            'wsgi.url_scheme': 'http',
            'wsgi.input': self,
            'wsgi.errors': sys.stderr,
            'wsgi.multithread': False,
            'wsgi.multiprocess': False,
            'wsgi.run_once': True,
            'REQUEST_METHOD': self.parser.get_method().decode('utf8'),
            'SCRIPT_NAME': '',
            'PATH_INFO': path_info,
            'QUERY_STRING': request_url.query.decode('utf8'),
            'CONTENT_TYPE': self.headers.get('Content-Type', ''),
            'CONTENT_LENGTH': self.headers.get('Content-Length', ''),
            'REMOTE_ADDR': self.transport.get_extra_info('socket').getpeername()[0],
            'REMOTE_PORT': self.transport.get_extra_info('socket').getpeername()[1],
            'SERVER_NAME': self.transport.get_extra_info('socket').getsockname()[0],
            'SERVER_PORT': self.transport.get_extra_info('socket').getsockname()[1],
            'SERVER_PROTOCOL': ''
        }

        for key, value in self.headers.items():
            key = 'HTTP_' + key.upper().replace('-', '_')
            if key not in ('HTTP_CONTENT_TYPE', 'HTTP_CONTENT_LENGTH'):
                environ[key] = value

        if request_url.scheme and request_url.netloc:
            environ['HTTP_HOST'] = request_url.netloc

        return environ

    def set_websocket_protocol(self, websocket_protocol):
        self.websocket_protocol = websocket_protocol
        websocket_protocol.connection_made(self.transport)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='aWSGI server')
    parser.add_argument('wsgifile', metavar='wsgi file',
                        help='wsgi file that contains wsgi application')

    args = parser.parse_args()
    wsgi_module = machinery.SourceFileLoader('wsgi', args.wsgifile).load_module()

    server = loop.run_until_complete(loop.create_server(lambda: HttpProtocol(wsgi_module.application, loop), host='127.0.0.1', port=8000))
    print('serving on', server.sockets[0].getsockname())
    try:
        loop.run_forever()
    finally:
        server.close()
        loop.close()