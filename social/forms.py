import re
from pathlib import PurePosixPath

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .blob_uploads import get_valid_ticket
from .models import BlobUploadTicket, Comment, Message, Post, Profile, Report, Sticker, Story

User = get_user_model()
MAX_UPLOAD_SIZE = 50 * 1024 * 1024
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
VIDEO_EXTENSIONS = {'.mp4', '.webm'}
CHAT_FILE_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | {
    '.pdf', '.txt', '.docx', '.xlsx', '.pptx'
}
MAX_CHAT_UPLOAD_SIZE = 20 * 1024 * 1024


def validate_upload(upload):
    extension = '.' + upload.name.rsplit('.', 1)[-1].lower() if '.' in upload.name else ''
    if upload.size > MAX_UPLOAD_SIZE:
        raise forms.ValidationError('Media must be 50 MB or smaller.')
    if extension not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        raise forms.ValidationError('Choose a JPG, PNG, WebP, GIF, MP4, or WebM file.')
    header = upload.read(12)
    upload.seek(0)
    image_signatures = {
        '.jpg': header.startswith(b'\xff\xd8\xff'),
        '.jpeg': header.startswith(b'\xff\xd8\xff'),
        '.png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        '.gif': header.startswith((b'GIF87a', b'GIF89a')),
        '.webp': header.startswith(b'RIFF') and header[8:12] == b'WEBP',
    }
    video_signatures = {
        '.mp4': len(header) >= 8 and header[4:8] == b'ftyp',
        '.webm': header.startswith(b'\x1a\x45\xdf\xa3'),
    }
    signature_is_valid = image_signatures.get(extension, video_signatures.get(extension, False))
    if not signature_is_valid:
        raise forms.ValidationError('The file contents do not match a supported media format.')
    return extension


def validate_text(text):
    for term in settings.SOCIAL_BLOCKED_TERMS:
        if re.search(r'(?<!\w)' + re.escape(term) + r'(?!\w)', text, re.IGNORECASE):
            raise forms.ValidationError('Please revise this text before posting.')
    return text


def validate_chat_upload(upload):
    extension = '.' + upload.name.rsplit('.', 1)[-1].lower() if '.' in upload.name else ''
    if upload.size > MAX_CHAT_UPLOAD_SIZE:
        raise forms.ValidationError('Chat files must be 20 MB or smaller.')
    if extension not in CHAT_FILE_EXTENSIONS:
        raise forms.ValidationError('Choose an image, video, PDF, text, or Office document.')
    header = upload.read(12)
    upload.seek(0)
    signatures = {
        '.jpg': header.startswith(b'\xff\xd8\xff'),
        '.jpeg': header.startswith(b'\xff\xd8\xff'),
        '.png': header.startswith(b'\x89PNG\r\n\x1a\n'),
        '.gif': header.startswith((b'GIF87a', b'GIF89a')),
        '.webp': header.startswith(b'RIFF') and header[8:12] == b'WEBP',
        '.mp4': len(header) >= 8 and header[4:8] == b'ftyp',
        '.webm': header.startswith(b'\x1a\x45\xdf\xa3'),
        '.pdf': header.startswith(b'%PDF-'),
        '.docx': header.startswith(b'PK\x03\x04'),
        '.xlsx': header.startswith(b'PK\x03\x04'),
        '.pptx': header.startswith(b'PK\x03\x04'),
        '.txt': True,
    }
    if not signatures[extension]:
        raise forms.ValidationError('The file contents do not match its file type.')
    if extension == '.txt':
        try:
            upload.read().decode('utf-8')
        except UnicodeDecodeError as error:
            upload.seek(0)
            raise forms.ValidationError('Text attachments must be UTF-8 encoded.') from error
        upload.seek(0)
    return extension


class SignUpForm(UserCreationForm):
    email = forms.EmailField()

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email


class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    def clean(self):
        identifier = self.cleaned_data.get('username', '')
        if '@' in identifier:
            username = User.objects.filter(email__iexact=identifier).values_list(
                'username', flat=True
            ).first()
            if username:
                self.cleaned_data['username'] = username
        return super().clean()


class AccountSettingsForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ('username', 'email')

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        if User.objects.filter(username__iexact=username).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('That username is already taken.')
        return username

    def clean_email(self):
        email = self.cleaned_data['email'].strip().lower()
        if not email:
            raise forms.ValidationError('An email address is required.')
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email


class BlobUploadFormMixin:
    upload_field = ''
    upload_purpose = ''
    upload_required = False

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop('user', None)
        self.blob_upload_ticket = None
        super().__init__(*args, **kwargs)
        if self.upload_field:
            self.fields[self.upload_field].required = False

    def clean_blob_upload(self):
        uploaded_file = self.cleaned_data.get(self.upload_field) if self.upload_field else None
        token = self.cleaned_data.get('blob_upload_token', '')
        pathname = self.cleaned_data.get('blob_upload_path', '')
        if uploaded_file and (token or pathname):
            raise forms.ValidationError('Upload a file once, then submit it.')
        if bool(token) != bool(pathname):
            self.add_error('blob_upload_token', 'The uploaded file details are incomplete.')
        elif token and pathname:
            if not self.user or not self.upload_purpose:
                self.add_error('blob_upload_token', 'The upload cannot be associated with this account.')
            else:
                try:
                    self.blob_upload_ticket = get_valid_ticket(
                        self.user, self.upload_purpose, token, pathname
                    )
                    setattr(self.instance, self.upload_field, pathname)
                    self.cleaned_data[self.upload_field] = pathname
                except ValidationError as error:
                    self.add_error('blob_upload_token', error)
        elif self.upload_required and not uploaded_file:
            self.add_error(self.upload_field, 'Choose a file to upload.')

    def clean(self):
        cleaned_data = super().clean()
        self.clean_blob_upload()
        return cleaned_data

    @transaction.atomic
    def save_instance(self, instance):
        if self.blob_upload_ticket:
            ticket = BlobUploadTicket.objects.select_for_update().get(
                pk=self.blob_upload_ticket.pk,
                owner=self.user,
                completed_at__isnull=False,
                consumed_at__isnull=True,
                expires_at__gt=timezone.now(),
            )
            setattr(instance, self.upload_field, ticket.pathname)
        instance.save()
        if self.blob_upload_ticket:
            ticket.consumed_at = timezone.now()
            ticket.save(update_fields=('consumed_at',))
        return instance


class ProfileForm(BlobUploadFormMixin, forms.ModelForm):
    blob_upload_token = forms.CharField(required=False, widget=forms.HiddenInput)
    blob_upload_path = forms.CharField(required=False, widget=forms.HiddenInput)
    username = forms.CharField(max_length=150)

    upload_field = 'avatar'
    upload_purpose = BlobUploadTicket.Purpose.AVATAR

    class Meta:
        model = Profile
        fields = ('avatar', 'bio', 'website')
        widgets = {
            'avatar': forms.ClearableFileInput(attrs={'accept': 'image/*'}),
            'bio': forms.Textarea(attrs={'rows': 3, 'maxlength': 160}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.user:
            self.fields['username'].initial = self.user.username

    def clean_username(self):
        username = self.cleaned_data['username'].strip()
        exists = User.objects.filter(username__iexact=username).exclude(pk=self.user.pk).exists()
        if exists:
            raise forms.ValidationError('That username is already taken.')
        return username

    def clean_avatar(self):
        avatar = self.cleaned_data.get('avatar')
        if avatar and not avatar.name.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
            raise forms.ValidationError('Choose a JPG, PNG, WebP, or GIF image.')
        if avatar and avatar.size > 5 * 1024 * 1024:
            raise forms.ValidationError('Profile photos must be 5 MB or smaller.')
        return avatar

    def clean_bio(self):
        return validate_text(self.cleaned_data.get('bio', ''))

    def save(self, commit=True):
        profile = super().save(commit=False)
        if commit:
            with transaction.atomic():
                self.save_instance(profile)
                self.user.username = self.cleaned_data['username']
                self.user.save(update_fields=['username'])
        return profile


class PostForm(BlobUploadFormMixin, forms.ModelForm):
    blob_upload_token = forms.CharField(required=False, widget=forms.HiddenInput)
    blob_upload_path = forms.CharField(required=False, widget=forms.HiddenInput)

    upload_field = 'media'
    upload_purpose = BlobUploadTicket.Purpose.POST
    upload_required = True

    class Meta:
        model = Post
        fields = ('media', 'caption', 'location', 'media_filter')
        widgets = {
            'media': forms.ClearableFileInput(attrs={'accept': 'image/*,video/mp4,video/webm'}),
            'caption': forms.Textarea(attrs={'rows': 3, 'maxlength': 2200}),
        }

    def clean_media(self):
        media = self.cleaned_data.get('media')
        if media:
            extension = validate_upload(media)
            self.media_type = (
                Post.MediaType.VIDEO if extension in VIDEO_EXTENSIONS else Post.MediaType.IMAGE
            )
        return media

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if self.blob_upload_ticket:
            extension = PurePosixPath(self.blob_upload_ticket.original_name).suffix.lower()
            self.media_type = (
                Post.MediaType.VIDEO if extension in VIDEO_EXTENSIONS else Post.MediaType.IMAGE
            )
        return cleaned_data

    def clean_caption(self):
        return validate_text(self.cleaned_data.get('caption', ''))


class StoryForm(BlobUploadFormMixin, forms.ModelForm):
    blob_upload_token = forms.CharField(required=False, widget=forms.HiddenInput)
    blob_upload_path = forms.CharField(required=False, widget=forms.HiddenInput)

    upload_field = 'media'
    upload_purpose = BlobUploadTicket.Purpose.STORY
    upload_required = True

    class Meta:
        model = Story
        fields = ('media', 'caption')
        widgets = {
            'media': forms.ClearableFileInput(attrs={'accept': 'image/*,video/mp4,video/webm'}),
        }

    def clean_media(self):
        media = self.cleaned_data.get('media')
        if media:
            extension = validate_upload(media)
            self.media_type = (
                Post.MediaType.VIDEO if extension in VIDEO_EXTENSIONS else Post.MediaType.IMAGE
            )
        return media

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        if self.blob_upload_ticket:
            extension = PurePosixPath(self.blob_upload_ticket.original_name).suffix.lower()
            self.media_type = (
                Post.MediaType.VIDEO if extension in VIDEO_EXTENSIONS else Post.MediaType.IMAGE
            )
        return cleaned_data

    def clean_caption(self):
        return validate_text(self.cleaned_data.get('caption', ''))


class CommentForm(forms.ModelForm):
    class Meta:
        model = Comment
        fields = ('body',)
        widgets = {'body': forms.TextInput(attrs={'placeholder': 'Add a comment…'})}

    def clean_body(self):
        return validate_text(self.cleaned_data['body'].strip())


class MessageForm(BlobUploadFormMixin, forms.ModelForm):
    blob_upload_token = forms.CharField(required=False, widget=forms.HiddenInput)
    blob_upload_path = forms.CharField(required=False, widget=forms.HiddenInput)
    sticker = forms.ModelChoiceField(
        queryset=Sticker.objects.none(), required=False, widget=forms.HiddenInput
    )

    upload_field = 'attachment'
    upload_purpose = BlobUploadTicket.Purpose.CHAT

    class Meta:
        model = Message
        fields = ('body', 'attachment', 'sticker')
        widgets = {'body': forms.TextInput(attrs={'placeholder': 'Write a message…'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['sticker'].queryset = Sticker.objects.filter(owner=self.user)
        self.fields['body'].required = False
        self.fields['attachment'].required = False
        self.fields['attachment'].widget.attrs.update({
            'accept': 'image/*,video/mp4,video/webm,.pdf,.txt,.docx,.xlsx,.pptx',
        })

    def clean_body(self):
        return validate_text(self.cleaned_data['body'].strip())

    def clean_attachment(self):
        attachment = self.cleaned_data.get('attachment')
        if attachment:
            validate_chat_upload(attachment)
        return attachment

    def clean(self):
        cleaned_data = super().clean()
        if not any((
            cleaned_data.get('body'),
            cleaned_data.get('attachment'),
            cleaned_data.get('sticker'),
            self.blob_upload_ticket,
        )):
            raise forms.ValidationError('Write a message, attach a file, or choose a sticker.')
        return cleaned_data


class StickerForm(BlobUploadFormMixin, forms.ModelForm):
    blob_upload_token = forms.CharField(required=False, widget=forms.HiddenInput)
    blob_upload_path = forms.CharField(required=False, widget=forms.HiddenInput)

    upload_field = 'image'
    upload_purpose = BlobUploadTicket.Purpose.STICKER
    upload_required = True

    class Meta:
        model = Sticker
        fields = ('image', 'label')
        widgets = {
            'image': forms.ClearableFileInput(attrs={'accept': 'image/png,image/jpeg,image/webp,image/gif'}),
        }

    def clean_image(self):
        image = self.cleaned_data.get('image')
        if not image:
            return image
        if image.size > 3 * 1024 * 1024:
            raise forms.ValidationError('Stickers must be 3 MB or smaller.')
        extension = '.' + image.name.rsplit('.', 1)[-1].lower() if '.' in image.name else ''
        if extension not in IMAGE_EXTENSIONS:
            raise forms.ValidationError('Choose a JPG, PNG, WebP, or GIF image.')
        header = image.read(12)
        image.seek(0)
        signatures = {
            '.jpg': header.startswith(b'\xff\xd8\xff'),
            '.jpeg': header.startswith(b'\xff\xd8\xff'),
            '.png': header.startswith(b'\x89PNG\r\n\x1a\n'),
            '.gif': header.startswith((b'GIF87a', b'GIF89a')),
            '.webp': header.startswith(b'RIFF') and header[8:12] == b'WEBP',
        }
        if not signatures[extension]:
            raise forms.ValidationError('The file contents do not match an image.')
        return image

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned_data = super().clean()
        return cleaned_data

    def clean_label(self):
        return validate_text(self.cleaned_data.get('label', '').strip())


class ReportForm(forms.ModelForm):
    class Meta:
        model = Report
        fields = ('reason', 'details')
        widgets = {'details': forms.Textarea(attrs={'rows': 3, 'maxlength': 500})}
