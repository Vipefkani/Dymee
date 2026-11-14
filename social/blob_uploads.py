import base64
import hashlib
import hmac
import json
import mimetypes
import re
import uuid
from datetime import timedelta
from io import BytesIO
from pathlib import PurePosixPath

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.core.exceptions import SuspiciousFileOperation
from django.utils import timezone
from django.utils.text import get_valid_filename
from PIL import Image, UnidentifiedImageError

from .models import BlobUploadTicket
from .storage import BlobStorageError

UPLOAD_POLICIES = {
    BlobUploadTicket.Purpose.AVATAR: (5 * 1024 * 1024, {'.jpg', '.jpeg', '.png', '.webp', '.gif'}),
    BlobUploadTicket.Purpose.POST: (
        50 * 1024 * 1024,
        {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.webm'},
    ),
    BlobUploadTicket.Purpose.STORY: (
        50 * 1024 * 1024,
        {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.webm'},
    ),
    BlobUploadTicket.Purpose.CHAT: (
        20 * 1024 * 1024,
        {
            '.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.webm',
            '.pdf', '.txt', '.docx', '.xlsx', '.pptx',
        },
    ),
    BlobUploadTicket.Purpose.STICKER: (
        3 * 1024 * 1024,
        {'.jpg', '.jpeg', '.png', '.webp', '.gif'},
    ),
}

SIGNATURES = {
    '.jpg': lambda header: header.startswith(b'\xff\xd8\xff'),
    '.jpeg': lambda header: header.startswith(b'\xff\xd8\xff'),
    '.png': lambda header: header.startswith(b'\x89PNG\r\n\x1a\n'),
    '.gif': lambda header: header.startswith((b'GIF87a', b'GIF89a')),
    '.webp': lambda header: header.startswith(b'RIFF') and header[8:12] == b'WEBP',
    '.mp4': lambda header: len(header) >= 8 and header[4:8] == b'ftyp',
    '.webm': lambda header: header.startswith(b'\x1a\x45\xdf\xa3'),
    '.pdf': lambda header: header.startswith(b'%PDF-'),
    '.docx': lambda header: header.startswith(b'PK\x03\x04'),
    '.xlsx': lambda header: header.startswith(b'PK\x03\x04'),
    '.pptx': lambda header: header.startswith(b'PK\x03\x04'),
}


def _signing_secret():
    if not settings.BLOB_UPLOAD_SIGNING_SECRET:
        raise ValidationError('Blob uploads are not configured.')
    return settings.BLOB_UPLOAD_SIGNING_SECRET.encode()


def sign_ticket(ticket):
    payload = {
        'id': str(ticket.pk),
        'user': ticket.owner_id,
        'purpose': ticket.purpose,
        'path': ticket.pathname,
        'size': ticket.size,
        'content_type': ticket.content_type,
        'exp': int(ticket.expires_at.timestamp()),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).rstrip(b'=').decode()
    signature = hmac.new(_signing_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return f'{encoded}.{signature}'


def decode_ticket_token(token):
    try:
        encoded, signature = token.split('.', 1)
        expected = hmac.new(_signing_secret(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError('Signature mismatch.')
        payload = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
        if payload['exp'] <= int(timezone.now().timestamp()):
            raise ValueError('Upload token expired.')
        return payload
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValidationError('The upload authorization is invalid or expired.') from error


def create_upload_ticket(user, purpose, original_name, size):
    if purpose not in UPLOAD_POLICIES:
        raise ValidationError('Unsupported upload type.')
    max_size, extensions = UPLOAD_POLICIES[purpose]
    basename = original_name.replace('\\', '/').rsplit('/', 1)[-1]
    extension = PurePosixPath(basename).suffix.lower()
    if extension not in extensions:
        raise ValidationError('This file type is not supported.')
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > max_size:
        raise ValidationError(f'Files must be {max_size // (1024 * 1024)} MB or smaller.')

    try:
        valid_name = get_valid_filename(basename)
    except SuspiciousFileOperation as error:
        raise ValidationError('Choose a valid filename.') from error
    safe_name = re.sub(r'[^A-Za-z0-9._-]+', '_', valid_name).strip('._')[:180]
    if not safe_name or PurePosixPath(safe_name).suffix.lower() != extension:
        raise ValidationError('Choose a valid filename.')
    ticket_id = uuid.uuid4()
    expiry = timezone.now() + timedelta(hours=1)
    content_type = mimetypes.guess_type(safe_name)[0] or 'application/octet-stream'
    ticket = BlobUploadTicket.objects.create(
        owner=user,
        purpose=purpose,
        pathname=f'uploads/{purpose}/{ticket_id}/{safe_name}',
        original_name=safe_name,
        content_type=content_type,
        size=size,
        expires_at=expiry,
    )
    return ticket


def get_valid_ticket(user, purpose, token, pathname):
    payload = decode_ticket_token(token)
    if (
        payload.get('user') != user.pk
        or payload.get('purpose') != purpose
        or payload.get('path') != pathname
    ):
        raise ValidationError('This upload does not belong to your account or this form.')
    try:
        ticket = BlobUploadTicket.objects.get(
            pk=payload['id'],
            owner=user,
            purpose=purpose,
            pathname=pathname,
            size=payload['size'],
            content_type=payload['content_type'],
            expires_at__gt=timezone.now(),
            completed_at__isnull=False,
            consumed_at__isnull=True,
        )
    except (BlobUploadTicket.DoesNotExist, KeyError) as error:
        raise ValidationError('The upload is missing, expired, or already used.') from error
    return ticket


def verify_uploaded_blob(ticket):
    max_size, extensions = UPLOAD_POLICIES[ticket.purpose]
    extension = PurePosixPath(ticket.original_name).suffix.lower()
    if extension not in extensions or not 0 < ticket.size <= max_size:
        raise ValidationError('The uploaded file does not meet the size or type requirements.')
    try:
        header, actual_size = default_storage.peek(ticket.pathname)
    except BlobStorageError as error:
        raise ValidationError('The uploaded file could not be verified in Blob storage.') from error
    if actual_size != ticket.size or (
        extension != '.txt' and not SIGNATURES[extension](header)
    ):
        raise ValidationError('The file contents do not match a supported format.')
    if ticket.purpose in {BlobUploadTicket.Purpose.AVATAR, BlobUploadTicket.Purpose.STICKER}:
        try:
            with default_storage.open(ticket.pathname, 'rb') as upload:
                with Image.open(BytesIO(upload.read())) as image:
                    image.verify()
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as error:
            raise ValidationError('The uploaded image is invalid.') from error
