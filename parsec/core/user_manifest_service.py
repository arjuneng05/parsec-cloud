from base64 import encodebytes
import json
import sys

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


class UserManifestError(ParsecError):
    pass


class UserManifestNotFound(UserManifestError):
    status = 'not_found'


class cmd_CREATE_FILE_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class cmd_RENAME_FILE_Schema(BaseCmdSchema):
    old_path = fields.String(required=True)
    new_path = fields.String(required=True)


class cmd_DELETE_FILE_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class cmd_LIST_DIR_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class cmd_MAKE_DIR_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class cmd_REMOVE_DIR_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class cmd_HISTORY_Schema(BaseCmdSchema):
    path = fields.String(required=True)


class UserManifestService(BaseService):

    file_service = service('FileService')
    identity_service = service('IdentityService')

    def __init__(self, backend_host, backend_port):
        super().__init__()
        self.manifest = {}
        self._backend_host = backend_host
        self._backend_port = backend_port
        self.version = 0

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
    async def _cmd_CREATE_FILE(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        file = await self.create_file(msg['path'])
        file.update({'status': 'ok'})
        return file

    @cmd('rename_file')
    async def _cmd_RENAME_FILE(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        await self.rename_file(msg['old_path'], msg['new_path'])
        return {'status': 'ok'}

    @cmd('delete_file')
    async def _cmd_DELETE_FILE(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        await self.delete_file(msg['path'])
        return {'status': 'ok'}

    @cmd('list_dir')
    async def _cmd_LIST_DIR(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        current, childrens = await self.list_dir(msg['path'])
        return {'status': 'ok', 'current': current, 'childrens': childrens}

    @cmd('make_dir')
    async def _cmd_MAKE_DIR(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        await self.make_dir(msg['path'])
        return {'status': 'ok'}

    @cmd('remove_dir')
    async def _cmd_REMOVE_DIR(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        await self.remove_dir(msg['path'])
        return {'status': 'ok'}

    @cmd('history')
    async def _cmd_HISTORY(self, msg):
        msg = cmd_CREATE_FILE_Schema().load(msg)
        history = await self.history(msg['path'])
        return {'status': 'ok', 'history': history}

    @cmd('load_user_manifest')
    # TODO event when new identity loaded in indentity service
    async def _cmd_LOAD_USER_MANIFEST(self, msg):
        await self.load_user_manifest()
        return {'status': 'ok'}

    async def create_file(self, path):
        if path in self.manifest:
            raise UserManifestError('already_exist', 'Target already exists.')
        else:
            ret = await self.file_service.create()
            file = {}
            for key in ['id', 'read_trust_seed', 'write_trust_seed']:
                file[key] = ret[key]
            file['key'] = None  # TODO set value
            self.manifest[path] = file
            await self.save_user_manifest()
        return self.manifest[path]

    async def rename_file(self, old_path, new_path):
        self.manifest[new_path] = self.manifest[old_path]
        del self.manifest[old_path]
        await self.save_user_manifest()

    async def delete_file(self, path):
        try:
            del self.manifest[path]
            await self.save_user_manifest()
        except KeyError:
            raise UserManifestNotFound('File not found.')

    async def list_dir(self, path):
        if path != '/' and path not in self.manifest:
            raise UserManifestNotFound('Directory not found.')
        results = {}
        for entry in self.manifest:
            if entry != path and entry.startswith(path) and entry.count('/', len(path) + 1) == 0:
                results[entry.split('/')[-1]] = self.manifest[entry]
        return self.manifest[path], results

    async def make_dir(self, path):
        if path in self.manifest:
            raise UserManifestError('already_exist', 'Target already exists.')
        else:
            self.manifest[path] = {'id': None,
                                   'read_trust_seed': None,
                                   'write_trust_seed': None,
                                   'key': None}  # TODO set correct values
            await self.save_user_manifest()
        return self.manifest[path]

    async def remove_dir(self, path):
        if path == '/':
            raise UserManifestError('cannot_remove_root', 'Cannot remove root directory.')
        for entry in self.manifest:
            if entry != path and entry.startswith(path):
                raise UserManifestError('directory_not_empty', 'Directory not empty.')
        try:
            del self.manifest[path]
            await self.save_user_manifest()
        except KeyError:
            raise UserManifestNotFound('Directory not found.')

    async def load_user_manifest(self):
        user_identity, challenge = await self.identity_service.compute_sign_challenge()
        try:
            response = await self.send_cmd(cmd='VlobService:read',
                                           id=user_identity,
                                           challenge=challenge)
        except Exception:
            response = await self.send_cmd(cmd='VlobService:create',
                                           id=user_identity,
                                           read_trust_seed=user_identity,
                                           write_trust_seed=user_identity)
            if response['status'] != 'ok':
                raise UserManifestError('Cannot create vlob.')
            await self.make_dir('/')
        user_identity, challenge = await self.identity_service.compute_sign_challenge()
        try:
            response = await self.send_cmd(cmd='VlobService:read',
                                           id=user_identity,
                                           challenge=challenge.decode())
        except Exception:
            raise UserManifestError('Unable to load newly created user manifest.')
        self.version = response['version']
        blob = response['blob']
        content = await self.identity_service.decrypt(blob)
        content = content.decode()
        manifest = json.loads(content)
        consistency = await self.check_consistency(manifest)
        if consistency:
            self.manifest = manifest
        return consistency

    async def save_user_manifest(self):
        user_identity, challenge = await self.identity_service.compute_sign_challenge()
        blob = json.dumps(self.manifest)
        blob = blob.encode()
        encrypted_blob = await self.identity_service.encrypt(blob)
        self.version += 1
        response = await self.send_cmd(cmd='VlobService:update',
                                       id=user_identity,
                                       version=self.version,
                                       blob=encrypted_blob.decode(),
                                       challenge=challenge.decode())
        if response['status'] != 'ok':
            raise UserManifestError('Cannot update vlob.')

    async def check_consistency(self, manifest):
        for _, entry in manifest.items():
            if entry['id']:
                try:
                    await self.file_service.stat(entry['id'], entry['read_trust_seed'])
                except Exception:
                    return False
        return True

    async def get_properties(self, id):
        for entry in self.manifest.values():  # TODO bad complexity
            if entry['id'] == id:
                key = entry['key']
                entry['key'] = key if key else None
                return entry
        raise(UserManifestNotFound('File not found.'))

    async def update_key(self, id, new_key):  # TODO don't call when update manifest
        for key, values in self.manifest.items():
            if values['id'] == id:
                values['key'] = encodebytes(new_key).decode()
                self.manifest[key] = values
                break
        await self.save_user_manifest()

    async def history(self):
        # TODO raise ParsecNotImplementedError
        pass