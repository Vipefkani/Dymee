import json
import mimetypes
import os
import re
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import Storage
from django.core.exceptions import SuspiciousFileOperation
from django.urls import reverse
from django.utils.encoding import filepath_to_uri


class BlobStorageError(OSError):
    pass


class VercelBlobStorage(Storage):
    def _headers(self, extra=None):
        headers = {
            'Authorization': f'Bearer {settings.BLOB_READ_WRITE_TOKEN}',
        }
        if extra:
            headers.update(extra)
        return headers

    def _blob_url(self, name):
        return (
            f'https://{settings.BLOB_STORE_ID}.private.blob.vercel-storage.com/'
            f'{quote(name.lstrip("/"), safe="/")}'
        )

    def open_response(self, name, range_header=None, method='GET'):
        headers = self._headers()
        if range_header:
            headers['Range'] = range_header
        request = Request(self._blob_url(name), headers=headers, method=method)
        return urlopen(request, timeout=30)

    def _open(self, name, _mode='rb'):
        if _mode != 'rb':
            raise ValueError('Vercel Blob media is opened in binary read mode only.')
        with self.open_response(name) as response:
            return ContentFile(response.read(), name=name)

    def _save(self, name, content):
        pathname = self.get_available_name(name)
        body = b''.join(content.chunks()) if hasattr(content, 'chunks') else content.read()
        content_type = getattr(content, 'content_type', None) or (
            mimetypes.guess_type(pathname)[0] or 'application/octet-stream'
        )
        headers = self._headers({
            'Content-Type': 'application/octet-stream',
            'x-api-version': '13',
            'x-api-blob-request-id': f'{settings.BLOB_STORE_ID}:{uuid.uuid4()}',
            'x-api-blob-request-attempt': '0',
            'x-vercel-blob-store-id': settings.BLOB_STORE_ID,
            'x-vercel-blob-access': 'private',
            'x-content-type': content_type,
            'x-add-random-suffix': '0',
            'x-content-length': str(len(body)),
        })
        request = Request(
            f'https://blob.vercel-storage.com/?{urlencode({"pathname": pathname})}',
            data=body,
            headers=headers,
            method='PUT',
        )
        try:
            with urlopen(request, timeout=60) as response:
                result = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
            raise BlobStorageError('Vercel Blob upload failed.') from error
        saved_path = result.get('pathname')
        if not isinstance(saved_path, str) or not saved_path:
            raise BlobStorageError('Vercel Blob returned an invalid upload response.')
        return saved_path

    def get_available_name(self, name, max_length=None):
        normalized = name.replace('\\', '/').lstrip('/')
        parts = normalized.split('/')
        upload_folders = {
            'avatars': 'avatar',
            'posts': 'post',
            'stories': 'story',
            'chat': 'chat',
            'stickers': 'sticker',
        }
        if parts[0] == 'uploads' and len(parts) >= 2:
            folder = '/'.join(parts[:2])
        elif parts[0] in upload_folders:
            folder = f'uploads/{upload_folders[parts[0]]}'
        else:
            raise BlobStorageError('Vercel Blob upload path has an unsupported media type.')
        _, filename = os.path.split(normalized)
        _, extension = os.path.splitext(filename)
        unique_name = f'{uuid.uuid4().hex}{extension.lower()}'
        available_name = f'{folder}/{unique_name}'
        if max_length and len(available_name) > max_length:
            raise SuspiciousFileOperation('The generated Blob pathname exceeds the field limit.')
        return available_name

    def exists(self, name):
        request = Request(self._blob_url(name), headers=self._headers(), method='HEAD')
        try:
            with urlopen(request, timeout=15):
                return True
        except HTTPError as error:
            if error.code == 404:
                return False
            raise BlobStorageError('Vercel Blob availability check failed.') from error
        except (URLError, TimeoutError) as error:
            raise BlobStorageError('Vercel Blob availability check failed.') from error

    def size(self, name):
        request = Request(self._blob_url(name), headers=self._headers(), method='HEAD')
        try:
            with urlopen(request, timeout=15) as response:
                return int(response.headers['Content-Length'])
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as error:
            raise BlobStorageError('Vercel Blob size lookup failed.') from error

    def peek(self, name):
        try:
            with self.open_response(name, 'bytes=0-11') as response:
                prefix = response.read(12)
                content_range = response.headers.get('Content-Range', '')
                match = re.search(r'/(\d+)$', content_range)
                size = int(match.group(1)) if match else int(response.headers['Content-Length'])
                return prefix, size
        except (HTTPError, URLError, TimeoutError, KeyError, ValueError) as error:
            raise BlobStorageError('Vercel Blob verification failed.') from error

    def url(self, name):
        path = reverse('private_media', kwargs={'path': name})
        return filepath_to_uri(path)
