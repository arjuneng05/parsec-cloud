from base64 import decodebytes, encodebytes
from datetime import datetime
import json
import sys

from cryptography.hazmat.backends.openssl import backend as openssl
from cryptography.hazmat.primitives import hashes
from logbook import Logger, StreamHandler
from marshmallow import fields
import websockets

from parsec.service import BaseService, cmd, service
from parsec.exceptions import ParsecError
from parsec.tools import BaseCmdSchema


LOG_FORMAT = '[{record.time:%Y-%m-%d %H:%M:%S.%f%z}] ({record.thread_name})' \
             ' {record.level_name}: {record.channel}: {record.message}'
log = Logger('Parsec-File-Service')
StreamHandler(sys.stdout, format_string=LOG_FORMAT).push_application()


class FileError(ParsecError):
    pass


class FileNotFound(FileError):
    status = 'not_found'


class cmd_READ_Schema(BaseCmdSchema):
    id = fields.String(required=True)


class cmd_WRITE_Schema(BaseCmdSchema):
    id = fields.String(required=True)
    version = fields.Integer(required=True)
    content = fields.String(required=True)


class cmd_STAT_Schema(BaseCmdSchema):
    id = fields.String(required=True)


class cmd_HISTORY_Schema(BaseCmdSchema):
    id = fields.String(required=True)


class FileService(BaseService):

    crypto_service = service('CryptoService')
    identity_service = service('IdentityService')
    pub_keys_service = service('PubKeysService')
    user_manifest_service = service('UserManifestService')

    def __init__(self, backend_host, backend_port):
        super().__init__()
        self._backend_host = backend_host
        self._backend_port = backend_port

    async def send_cmd(self, **msg):
        req = json.dumps(msg).encode() + b'\n'
        log.debug('Send: %r' % req)
        websocket_path = 'ws://' + self._backend_host + ':' + str(self._backend_port)
        async with websockets.connect(websocket_path) as websocket:
            await websocket.send(req)
            raw_reps = await websocket.recv()
            log.debug('Received: %r' % raw_reps)
            return json.loads(raw_reps.decode())

    @cmd('create_file')
    async def _cmd_CREATE(self, msg):
        id = await self.create()
        return {'status': 'ok', 'file': id}

    @cmd('read_file')
    async def _cmd_READ(self, msg):
        msg = cmd_READ_Schema().load(msg)
        file = await self.read(msg['id'])
        file.update({'status': 'ok'})
        return file

    @cmd('write_file')
    async def _cmd_WRITE(self, msg):
        msg = cmd_WRITE_Schema().load(msg)
        await self.write(msg['id'], msg['version'], msg['content'])
        return {'status': 'ok'}

    @cmd('stat_file')
    async def _cmd_STAT(self, msg):
        msg = cmd_STAT_Schema().load(msg)
        stats = await self.stat(msg['id'])
        stats.update({'status': 'ok'})
        return stats

    @cmd('history')
    async def _cmd_HISTORY(self, msg):
        msg = cmd_STAT_Schema().load(msg)
        history = await self.history(msg['id'])
        return {'status': 'ok', 'history': history}

    async def create(self, id=None):
        response = await self.send_cmd(cmd='VlobService:create', id=id)
        if response['status'] != 'ok':
            raise FileError('Cannot create vlob.')
        ret = {}
        for key in ('id', 'read_trust_seed', 'write_trust_seed'):
            ret[key] = response[key]
        return response

    async def read(self, id):
        try:
            properties = await self.user_manifest_service.get_properties(id)
        except Exception:
            raise FileNotFound('Vlob not found.')
        key = decodebytes(properties['key'].encode()) if properties['key'] else None
        trust_seed = properties['read_trust_seed']
        if not key:
            return {'content': '', 'version': 0}
        challenge, hash = await self.identity_service.compute_seed_challenge(id, trust_seed)
        response = await self.send_cmd(cmd='VlobService:read',
                                       id=id,
                                       challenge=challenge,
                                       hash=hash)
        if response['status'] != 'ok':
            raise FileError('Cannot read vlob.')
        version = response['version']
        if response['blob']:
            encrypted_blob = decodebytes(response['blob'].encode())
            blob = await self.crypto_service.sym_decrypt(encrypted_blob, key)
            blob = json.loads(blob.decode())
            key = decodebytes(blob['key'].encode())
            old_digest = decodebytes(blob['digest'].encode())
            # Get content
            response = await self.send_cmd(cmd='BlockService:read', id=blob['block'])
            if response['status'] != 'ok':
                raise FileError('Cannot read block.')
            # Decrypt
            encrypted_content = response['content'].encode()
            content = await self.crypto_service.sym_decrypt(encrypted_content, key)
            # Check integrity
            digest = hashes.Hash(hashes.SHA512(), backend=openssl)
            digest.update(content)
            new_digest = digest.finalize()
            assert new_digest == old_digest
        return {'content': content.decode(), 'version': version}

    async def write(self, id, version, content):
        try:
            properties = await self.user_manifest_service.get_properties(id)
        except Exception:
            raise FileNotFound('Vlob not found.')
        key = decodebytes(properties['key'].encode()) if properties['key'] else None
        trust_seed = properties['write_trust_seed']
        content = content.encode()
        size = len(decodebytes(content))
        # Digest
        digest = hashes.Hash(hashes.SHA512(), backend=openssl)
        digest.update(content)
        content_digest = digest.finalize()  # TODO replace with hexdigest ?
        content_digest = encodebytes(content_digest).decode()
        # Encrypt block
        key, data = await self.crypto_service.sym_encrypt(content)
        key = encodebytes(key).decode()
        data = data.decode()
        # Store block
        response = await self.send_cmd(cmd='BlockService:create', content=data)
        if response['status'] != 'ok':
            raise FileError('Cannot create block.')
        # Update vlob
        challenge, hash = await self.identity_service.compute_seed_challenge(id, trust_seed)
        blob = {'block': response['id'],
                'size': size,
                'key': key,
                'digest': content_digest}
        # Encrypt blob
        blob = json.dumps(blob)
        blob = blob.encode()
        key, encrypted_blob = await self.crypto_service.sym_encrypt(blob)
        encrypted_blob = encodebytes(encrypted_blob).decode()
        response = await self.send_cmd(cmd='VlobService:update',
                                       id=id,
                                       version=version,
                                       blob=encrypted_blob,
                                       challenge=challenge,
                                       hash=hash)
        if response['status'] != 'ok':
            raise FileError('Cannot update vlob.')
        await self.user_manifest_service.update_key(id, key)  # TODO use event
        return key

    async def stat(self, id):
        try:
            properties = await self.user_manifest_service.get_properties(id)
        except Exception:
            raise FileNotFound('Vlob not found.')
        key = decodebytes(properties['key'].encode()) if properties['key'] else None
        trust_seed = properties['read_trust_seed']
        if not key:
            return {'id': id,
                    'ctime': datetime.utcnow().timestamp(),
                    'mtime': datetime.utcnow().timestamp(),
                    'atime': datetime.utcnow().timestamp(),
                    'size': 0}
        challenge, hash = await self.identity_service.compute_seed_challenge(id, trust_seed)
        response = await self.send_cmd(cmd='VlobService:read',
                                       id=id,
                                       challenge=challenge,
                                       hash=hash)
        if response['status'] != 'ok':
            raise FileError('Cannot read vlob.')
        encrypted_blob = response['blob']
        encrypted_blob = decodebytes(encrypted_blob.encode())
        blob = await self.crypto_service.sym_decrypt(encrypted_blob, key)
        blob = json.loads(blob.decode())
        response = await self.send_cmd(cmd='BlockService:stat', id=blob['block'])
        if response['status'] != 'ok':
            raise FileError('Cannot stat block.')
        return {'id': id,
                'ctime': response['creation_timestamp'],
                'mtime': response['creation_timestamp'],
                'atime': response['access_timestamp'],
                'size': blob['size']}

    async def history(self, id):
        # TODO raise ParsecNotImplementedError
        pass