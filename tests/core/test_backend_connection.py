import pytest
import asyncio
from effect2.testing import perform_sequence, const
from unittest.mock import Mock, patch

from parsec.core.backend import BackendComponent, BackendConnection, BackendCmd


class AsyncMock(Mock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)


class TestBackendConnection:
    async def test_connection(self):
        identity = Mock()
        identity.private_key.sign.return_value = b'456'
        identity.id = 'John'
        with patch('parsec.core.backend.websockets.connect', new_callable=AsyncMock) as mocked_connect:
            mocked_websocket = mocked_connect.return_value
            pushed_event_future1 = asyncio.Future()
            pushed_event_future2 = asyncio.Future()
            to_recv = iter([
                # Handshake
                '{"handshake": "challenge", "challenge": "123"}',
                '{"handshake": "done", "status": "ok"}',
                # Send ping command
                '{"status": "ok", "pong": 1}',
                # Subscribe to event
                '{"status": "ok"}',
                # Subscribe to event
                '{"status": "ok"}',
                # Received event
                pushed_event_future1,
                # Received event
                pushed_event_future2
            ])
            expected_sent = iter([
                # Handshake
                '{"answer": "NDU2\\n", "handshake": "answer", "identity": "John"}',
                # Send ping command
                '{"cmd": "ping", "ping": 1}',
                # Subscribe to event
                '{"cmd": "subscribe", "event": "on_good", "sender": "good_sender"}',
                # Subscribe to event
                '{"cmd": "subscribe", "event": "on_bad", "sender": null}'
            ])

            async def recv():
                try:
                    data = next(to_recv)
                except StopIteration:
                    await asyncio.Future()  # Wait forever from now
                if asyncio.iscoroutine(data) or isinstance(data, asyncio.Future):
                    return await data
                else:
                    return data

            async def send(data):
                try:
                    expected = next(expected_sent)
                except StopIteration:
                    raise AssertionError('`websockend.send` was not expected to be called again')
                assert data == expected

            mocked_websocket.recv = recv
            mocked_websocket.send = send

            conn = BackendConnection('ws://foo')
            await conn.open_connection(identity)
            mocked_connect.assert_called_once_with('ws://foo')

            ret = await conn.send_cmd({'cmd': 'ping', 'ping': 1})
            assert ret == {'status': 'ok', 'pong': 1}

            on_good_cb_called = asyncio.Future()

            def on_good_cb(sender):
                on_good_cb_called.set_result(sender)

            def on_bad_cb(sender):
                raise AssertionError('Not expected to be called')

            await conn.connect_event('on_good', 'good_sender', on_good_cb)
            await conn.connect_event('on_bad', None, on_bad_cb)

            pushed_event_future1.set_result('{"event": "on_good", "sender": "bad_sender"}')
            pushed_event_future2.set_result('{"event": "on_good", "sender": "good_sender"}')
            sender = await on_good_cb_called
            assert sender == 'good_sender'

            await conn.close_connection()


@pytest.mark.parametrize('args', [
    ('ws://localhost:5000/foo', 's3://localhost:5000/foo', 's3://localhost:5000/foo'),
    ('ws://localhost:5000/foo', '/bar', 'http://localhost:5000/foo/bar'),
    ('wss://localhost:5000/foo', '/bar', 'https://localhost:5000/foo/bar')
])
async def test_perform_blockstore_get_url_local_to_backend(args):
    backend_url, raw_blockstore_url, cooked_blockstore_url = args
    backend = BackendComponent(url=backend_url)
    eff = backend.perform_blockstore_get_url(None)
    sequence = [
        (BackendCmd('blockstore_get_url'),
            const({'status': 'ok', 'url': raw_blockstore_url}))
    ]
    ret = perform_sequence(sequence, eff)
    assert ret == cooked_blockstore_url
